"""中心缓存小型分片画师表；下载发生在调度事务之外。"""
import hashlib
import json
import threading
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from source_catalog.network import open_url
from . import store as settings

LOCK = threading.Lock()


@lru_cache(maxsize=16)
def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def token(store):
    from server import read_hf_token_for
    return read_hf_token_for(store)


def pin(store, repo, cfg):
    if cfg['enabled'] and not cfg['revision']:
        with open_url('https://huggingface.co/api/datasets/' + quote(repo, safe='/') + '/revision/main', token(store)) as r:
            cfg['revision'] = json.load(r)['sha']
    return settings.validate(cfg)


def stem(package):
    name = package.replace('\\', '/').rsplit('/', 1)[-1]
    for suffix in ('.tar.gz', '.tar', '.tgz', '.zip'):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    raise ValueError('画师表需要对应 tar 或 zip 分片')


@lru_cache(maxsize=16)
def read(path):
    import pyarrow.parquet as pq
    table = pq.read_table(path, columns=['id', 'artists'], use_threads=False)
    meta = table.schema.metadata or {}
    if meta.get(b'rows') and int(meta[b'rows']) != table.num_rows:
        raise ValueError('画师表记录行数与文件不一致')
    mapping, names_pool = {}, {}
    for iid, names in zip(table['id'].to_pylist(), table['artists'].to_pylist()):
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            raise ValueError('画师表 artists 列格式不正确')
        normalized = tuple(dict.fromkeys(n.strip() for n in names if n.strip()))
        names = names_pool.setdefault(normalized, list(normalized))
        key = str(int(iid))
        if key in mapping:
            raise ValueError('画师表图片 ID 重复')
        mapping[key] = names
    return mapping, meta.get(b'shard', b'').decode(), table.num_rows


def prepare(store, repo, package, cfg=None, *, cached=False):
    if cfg is None:
        c = store.conn()
        try: cfg = settings.config(c, repo)
        finally: c.close()
    if not cfg['enabled']:
        return None
    name = stem(package)
    relative = cfg['directory'] + '/' + name + '.artist.parquet'
    key = hashlib.sha256((repo + '\n' + cfg['revision'] + '\n' + relative).encode()).hexdigest()
    folder = Path(store.dir) / 'artist-tables';folder.mkdir(parents=True, exist_ok=True)
    path = folder / (key + '.parquet')
    if cached and not path.is_file():
        return None
    with LOCK:
        if not path.is_file():
            url = 'https://huggingface.co/datasets/' + quote(repo, safe='/') + '/resolve/' + cfg['revision'] + '/' + quote(relative, safe='/')
            with open_url(url, token(store)) as r: data = r.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise ValueError('单分片画师表过大，请检查目录')
            temporary = path.with_suffix('.new');temporary.write_bytes(data)
            try: read(str(temporary))
            except Exception:
                temporary.replace(folder / (key + '.invalid'))
                raise
            temporary.replace(path)
    mapping, mark, rows = read(str(path))
    if mark and stem(mark) != name:
        raise ValueError('画师表分片标记与任务不一致')
    return dict(mapping=mapping, shard=package, rows=rows, source=settings.spec(repo, cfg),
                sha256=digest(str(path)))


def for_task(store, task, *, cached=False):
    from urllib.parse import urlsplit, parse_qs
    options = json.loads(task.get('annotation_json') or '{}')
    uri = options.get('artist_table')
    if not uri:
        raise ValueError('任务没有画师表配置')
    parsed = urlsplit(uri)
    path = parsed.path.strip('/').split('/')
    if parsed.scheme != 'hf' or parsed.netloc != 'datasets' or len(path) < 3 or '/'.join(path[:2]) != task['repo']:
        raise ValueError('画师表来源与任务仓库不一致')
    cfg = settings.validate(dict(enabled=True, directory='/'.join(path[2:]), revision=parse_qs(parsed.query)['revision'][0]))
    return prepare(store, task['repo'], task['pkg'], cfg, cached=cached)
