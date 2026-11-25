"""画师配置和旧产物补注入，复用清单管理接口。"""
import hashlib
import json
from . import store as settings
from .tables import pin, prepare


def write(store, dispatch, body, actor):
    repo, action = body['repo'], body['action']
    if action == 'detect_artist':
        from .discovery import detect
        return detect(store, repo, body.get('revision', ''))
    if action == 'configure_artist':
        cfg = pin(store, repo, settings.validate(body.get('config')))
        # 保存前验证一份真实分片表，凭据／路径问题在开工前反馈。
        if cfg['enabled']:
            c = store.conn()
            try:
                first = c.execute('SELECT id FROM packages WHERE repo=? ORDER BY id LIMIT 1',(repo,)).fetchone()
            finally: c.close()
            if first: prepare(store,repo,first[0],cfg)
        with store._write_lock:
            c = store.conn()
            try:
                with dispatch._Tx(c):
                    if not c.execute('SELECT 1 FROM repos WHERE repo=?',(repo,)).fetchone():
                        raise ValueError('仓库未登记')
                    c.execute('INSERT OR REPLACE INTO repo_artist VALUES(?,?)',(repo,json.dumps(cfg)))
                    store.event(c,'artist_config',repo,None,actor,json.dumps(cfg))
            finally: c.close()
        from source_catalog.routes import read
        return read(store)
    packages = body.get('packages',[])
    if not packages:
        raise ValueError('请选择需要补注入的分片')
    request_id = body.get('request_id')
    if not isinstance(request_id,str) or not request_id or len(request_id)>128:
        raise ValueError('补注入操作缺少有效请求 ID')
    force = body.get('force',False)
    if type(force) is not bool:
        raise ValueError('强制补注入选项必须为布尔值')
    fingerprint = hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    c = store.conn()
    try:
        cfg = settings.config(c,repo)
        identities = {p:c.execute('SELECT sha256 FROM packages WHERE repo=? AND id=?',(repo,p)).fetchone() for p in packages}
    finally: c.close()
    if not cfg['enabled']:
        raise ValueError('请先保存并启用此仓库的画师表')
    if any(not row for row in identities.values()):
        raise ValueError('分片不存在')
    tables = {p:prepare(store,repo,p,cfg) for p in packages}
    from .maintenance import normalize
    from assignments.annotation import for_package
    from delivery.reconciliation import merge
    from scheduling.leases import held
    result = {'ok':True,'items':[],'request_id':request_id,'fingerprint':fingerprint}
    with store._write_lock:
        c = store.conn()
        try:
            with dispatch._Tx(c):
                prior = c.execute("SELECT detail FROM events WHERE kind='artist_reinject' AND repo=? AND actor=? AND json_extract(detail,'$.request_id')=?",(repo,actor,request_id)).fetchone()
                if prior:
                    old = json.loads(prior[0])
                    if old['fingerprint'] != fingerprint: raise ValueError('同一请求 ID 不能用于不同操作')
                    return old
                if cfg != settings.config(c,repo):
                    raise ValueError('画师配置已变化，请刷新重试')
                occupied = held(c)
                for pkg in packages:
                    current = c.execute('SELECT sha256 FROM packages WHERE repo=? AND id=?',(repo,pkg)).fetchone()
                    if not current or current[0]!=identities[pkg][0]:
                        raise ValueError('源分片版本已变化，请刷新重试')
                    item = c.execute('SELECT expected_json FROM package_artifacts WHERE repo=? AND pkg=? AND source_sha256=?',(repo,pkg,current[0])).fetchone()
                    if occupied.get((repo,pkg)) or not item:
                        result['items'].append(dict(package=pkg,skipped='分片正在执行／核验' if occupied.get((repo,pkg)) else '尚无中心产物'))
                        continue
                    # 原正文选择与完成状态由既有核验维护，补注入不清除失败或冲突。
                    package=dict(c.execute('SELECT * FROM packages WHERE repo=? AND id=?',(repo,pkg)).fetchone())
                    stats = normalize(c,repo,pkg,current[0],tables[pkg],for_package(c,package,['tag','nl']),force=force)
                    if stats.get('changed'):
                        merge(c,repo,pkg,current[0],json.loads(item[0]),[],artist_normalized=True)
                    result['items'].append(dict(package=pkg,**stats))
                store.event(c,'artist_reinject',repo,None,actor,json.dumps(result,ensure_ascii=False))
        finally: c.close()
    return result
