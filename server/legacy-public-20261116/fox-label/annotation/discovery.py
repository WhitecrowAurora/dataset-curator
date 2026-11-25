"""从固定 HF 文件树识别与已登记分片对应的画师分表。"""
from pathlib import PurePosixPath
from urllib.error import HTTPError
import json

from source_catalog import network, retry
from . import store as settings
from . import tables


def detect(store, repo, revision='', *, requested_package=''):
    revision = settings.validate({'revision': revision})['revision']
    c = store.conn()
    try:
        row = c.execute('SELECT repo_type FROM repos WHERE repo=?', (repo,)).fetchone()
        if not row:
            raise ValueError('仓库未登记')
        if row[0] != 'dataset':
            raise ValueError('画师分表识别只支持 HF 数据集仓库')
        packages = [r[0] for r in c.execute('SELECT id FROM packages WHERE repo=? ORDER BY id', (repo,))]
    finally:
        c.close()
    version, tree = network.metadata(repo, 'dataset', revision or 'main', tables.token(store))
    # 同名分片处在不同子目录时，不能把一张表猜给其中某个分片。
    names = {}
    unsupported = []
    for package in packages:
        try:
            names.setdefault(tables.stem(package), []).append(package)
        except ValueError:
            unsupported.append(package)
    groups, root_files = {}, 0
    for path in tree:
        item = PurePosixPath(path)
        if not item.name.endswith('.artist.parquet'):
            continue
        directory = str(item.parent)
        if directory == '.':
            root_files += 1
            continue
        try:
            settings.validate({'directory': directory})
        except ValueError:
            continue
        name = item.name[:-len('.artist.parquet')]
        groups.setdefault(directory, {})[name] = path
    candidates = []
    for directory, files in groups.items():
        if requested_package and (tables.stem(requested_package) not in files or
                                  len(names.get(tables.stem(requested_package), [])) != 1):
            continue
        matched = [ps[0] for name, ps in names.items() if len(ps) == 1 and name in files]
        missing = [p for name, ps in names.items() if name not in files for p in ps] + unsupported
        ambiguous = [p for name, ps in names.items() if name in files and len(ps) > 1 for p in ps]
        sample = requested_package or (matched[0] if matched else next(iter(sorted(files))) + '.tar')
        candidate = dict(directory=directory, files=len(files), matched=len(matched),
                         missing=len(missing), missing_samples=missing[:20], ambiguous=len(ambiguous),
                         sample_package=sample, sample_path=files[tables.stem(sample)],
                         validated=False, error='')
        cfg = settings.validate(dict(enabled=True, directory=directory, revision=version))
        try:
            table = retry.call(lambda: tables.prepare(store, repo, sample, cfg))
            candidate.update(validated=True, sample_rows=table['rows'],
                             sample_with_artist=sum(bool(n) for n in table['mapping'].values()))
        except Exception as exc:
            candidate['error'] = retry.describe(exc)
        candidates.append(candidate)
    candidates.sort(key=lambda v: (-v['matched'], not v['validated'], v['directory']))
    return dict(ok=True, repo=repo, revision=version, total_packages=len(packages),
                candidates=candidates, root_files=root_files,
                message='未发现兼容的画师分表目录' if not candidates else
                        '按分片文件名匹配；每个目录校验一份样本，其余分表在领取时校验')


def for_history(store, repo, package, annotation):
    """补注入请求自动找表，仅固定到本次检查，不改仓库开关或旧租约。"""
    c = store.conn()
    try:
        cfg = settings.config(c, repo)
        saved = c.execute('SELECT 1 FROM repo_artist WHERE repo=?', (repo,)).fetchone()
    finally:
        c.close()
    if saved and not cfg['enabled']:
        raise ValueError('管理员已禁用此仓库画师表，请先启用后补注入')
    selected = dict(annotation)
    if not selected:
        selected = {k: cfg[k] for k in ('artist_inject', 'artist_inject_tags')}
    if not any(selected.get(k) for k in ('artist_inject', 'artist_inject_tags')):
        raise ValueError('此分片的画师注入已关闭，请先启用需要的分项')
    if selected.get('artist_table'):
        try:
            tables.for_task(store, dict(repo=repo, pkg=package, annotation_json=json.dumps(selected)))
            return selected
        except HTTPError as exc:
            if exc.code != 404:
                raise
    detected = detect(store, repo, cfg['revision'], requested_package=package)
    candidates = [v for v in detected['candidates'] if v['validated'] and v['matched'] > 0
                  and not v['ambiguous']]
    # 目录唯一才自动应用；最终 prepare 核对本分片，不以样本通过代替本片校验。
    if len(candidates) != 1:
        raise ValueError('仓库未发现唯一可用的画师分表目录，请管理员使用自动识别选择目录')
    resolved = settings.validate({**cfg, 'enabled': True, 'directory': candidates[0]['directory'],
                                  'revision': detected['revision']})
    table = tables.prepare(store, repo, package, resolved)
    selected['artist_table'] = table['source']
    return selected
