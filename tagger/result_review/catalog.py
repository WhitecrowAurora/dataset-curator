"""获取原图包的固定版本和大小，供下载前选择。"""
import json
from . import store


def resolve(api,repo,pkg,meta):
    revision=meta.get('revision') or 'main'
    if len(revision)!=40:
        revision=api.repo_info(repo,repo_type='dataset',revision=revision).sha
    entries=api.get_paths_info(repo,[pkg],repo_type='dataset',revision=revision)
    if not entries:raise ValueError('源仓库的指定版本没有此分包')
    entry=entries[0];lfs=entry.lfs or {}
    sha=lfs.get('sha256','') if isinstance(lfs,dict) else getattr(lfs,'sha256','')
    if meta.get('sha256') and sha and meta['sha256']!=sha:
        raise ValueError('此仓库版本的分包 SHA256 与回收结果来源不同，请核对源版本')
    if meta.get('size') and meta['size']!=entry.size:
        raise ValueError('此仓库版本的分包大小与回收结果来源不同，请核对源版本')
    result={**meta,'size':entry.size,'revision':revision}
    if sha:result['sha256']=sha
    return result


def prepare(sid,packages=None):
    from huggingface_hub import HfApi
    from ..web.center import CENTER
    api=HfApi(token=CENTER.credentials.effective_token() or None)
    with store.connect(sid) as c:
        rows=list(c.execute('SELECT repo,pkg,meta FROM packages'))
    selected={(p['repo'],p['pkg']) for p in packages} if packages else None
    for row in rows:
        repo,pkg=row['repo'],row['pkg']
        if selected is not None and (repo,pkg) not in selected:continue
        try:
            meta=resolve(api,repo,pkg,json.loads(row['meta']))
            with store.connect(sid) as c:
                c.execute('UPDATE packages SET meta=?,message=? WHERE repo=? AND pkg=?',
                    (json.dumps(meta),'原图包版本和大小已核对',repo,pkg))
        except Exception as exc:
            with store.connect(sid) as c:
                c.execute('UPDATE packages SET message=? WHERE repo=? AND pkg=?',
                    (CENTER.credentials.redact(str(exc)),repo,pkg))
