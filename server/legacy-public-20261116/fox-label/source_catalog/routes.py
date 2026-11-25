"""复用管理员鉴权和写通道的清单配置接口。"""
import json
import hashlib
from . import store as catalogs


def read(store, repo=None, package=None, offset=0, conflict_state='all', export=False):
    c = store.conn()
    try:
        if repo and package:
            if export:
                from delivery.reconciliation import snapshot
                item=c.execute('SELECT sha256 FROM packages WHERE repo=? AND id=?',(repo,package)).fetchone()
                if not item: raise ValueError('分片不存在')
                result=snapshot(c,repo,package,item[0])
                if (result.get('report') or {}).get('conflict_count') or (result.get('report') or {}).get('conflicts'):
                    raise ValueError('请先处理此分片的内容冲突，再下载当前核验结果')
                return result
            from delivery.details import view
            return view(c, repo, package, offset, conflict_state)
        return catalogs.view(c)
    finally:
        c.close()


def write(store, dispatch, body, actor):
    if not isinstance(body, dict):
        raise ValueError('请求体必须是 JSON 对象')
    repo = body.get("repo")
    action = body.get("action")
    if not isinstance(repo, str) or not repo:
        raise ValueError('请选择仓库')
    packages = body.get('packages', [])
    if not isinstance(packages, list) or any(not isinstance(p, str) or not p for p in packages):
        raise ValueError('分片列表格式不正确')
    if len(set(packages)) != len(packages):
        raise ValueError('不能重复选择分片')
    if action in ('configure_artist', 'reinject_artist', 'detect_artist'):
        from annotation.routes import write as artist_write
        return artist_write(store, dispatch, body, actor)
    if action in ('export_results', 'revoke_export'):
        from result_exports.service import create, revoke
        return create(store,body,actor) if action=='export_results' else revoke(store,body.get('id'))
    if action == 'configure' and not isinstance(body.get('config'), dict):
        raise ValueError('清单配置必须是 JSON 对象')
    with store._write_lock:
        c = store.conn()
        try:
            with dispatch._Tx(c):
                if not c.execute('SELECT 1 FROM repos WHERE repo=?', (repo,)).fetchone():
                    raise ValueError('仓库未登记')
                for package in packages:
                    if not c.execute('SELECT 1 FROM packages WHERE repo=? AND id=?', (repo, package)).fetchone():
                        raise ValueError('分片不存在')
                if action == 'resolve':
                    from delivery.details import resolve
                    result = resolve(c, dispatch, body, actor)
                    store.event(c, 'artifact_resolution', repo, body.get('package'), actor,
                                str(body.get('image_id')) + ':' + str(body.get('part')) + ':' + str(body.get('resolution')) + ':' + str(body.get('variant')))
                    return result
                if action == 'resolve_batch':
                    from delivery.resolution_batch import resolve
                    request_id = body.get('request_id')
                    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
                        raise ValueError('批量操作缺少有效请求 ID')
                    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    previous = c.execute("SELECT detail FROM events WHERE kind='artifact_resolution_batch' AND repo=? AND pkg=? AND actor=?"
                                         " AND json_extract(detail,'$.request_id')=? ORDER BY seq DESC LIMIT 1",
                                         (repo, body.get('package'), actor, request_id)).fetchone()
                    if previous:
                        batch = json.loads(previous['detail'])
                        if batch.get('fingerprint') != fingerprint:
                            raise ValueError('同一请求 ID 不能用于不同批量操作')
                        from delivery.details import view
                        result = view(c, repo, body.get('package'))
                        result['batch'] = batch
                        return result
                    result = resolve(c, dispatch, body, actor)
                    result['batch'].update(request_id=request_id, fingerprint=fingerprint)
                    store.event(c, 'artifact_resolution_batch', repo, body.get('package'), actor,
                                json.dumps(result['batch'], ensure_ascii=False))
                    return result
                if action == "cancel":
                    for package in body.get('packages') or []:
                        c.execute("UPDATE leases SET status='released',closed_reason='source_canceled' WHERE lease_id IN"
                                  " (SELECT lease_id FROM source_submissions WHERE repo=? AND pkg=? AND state='pending')", (repo, package))
                        c.execute("UPDATE source_submissions SET state='canceled' WHERE repo=? AND pkg=? AND state='pending'", (repo, package))
                        c.execute("UPDATE history_checks SET status='incomplete' WHERE repo=? AND package=? AND status='verifying'", (repo, package))
                        c.execute("DELETE FROM source_requests WHERE repo=? AND pkg=?", (repo, package))
                        from scheduling.leases import refresh
                        refresh(c, repo, package)
                elif action == "configure":
                    pending = c.execute("SELECT 1 FROM source_submissions WHERE repo=? AND state='pending' UNION ALL"
                                        " SELECT 1 FROM history_checks WHERE repo=? AND status='verifying' LIMIT 1", (repo, repo)).fetchone()
                    if pending and not (body.get('config') or {}).get('enabled'):
                        raise ValueError('此仓库仍有待核验提交，请先取消核验')
                    catalogs.configure(c, repo, body.get("config") or {})
                    if (body.get('config') or {}).get('enabled'):
                        c.execute("INSERT OR IGNORE INTO source_requests SELECT a.repo,a.pkg,a.source_sha256 FROM package_artifacts a"
                                  " JOIN packages p ON p.repo=a.repo AND p.id=a.pkg AND p.sha256=a.source_sha256 WHERE a.repo=?", (repo,))
                        from delivery.reconciliation import merge
                        rows = c.execute("SELECT a.* FROM package_artifacts a JOIN packages p ON p.repo=a.repo AND p.id=a.pkg"
                                         " AND p.sha256=a.source_sha256 WHERE a.repo=?", (repo,)).fetchall()
                        for row in rows:
                            report = merge(c, repo, row['pkg'], row['source_sha256'], json.loads(row['expected_json']), [])
                            if report['source_verification']['state'] != 'ready':
                                c.execute("UPDATE packages SET tag_status='',nl_status='',status='available',completed_at=NULL"
                                          " WHERE repo=? AND id=?", (repo, row['pkg']))
                    catalogs.enqueue(c, repo, retry=True)
                elif action in ("sync", "retry"):
                    if not catalogs.config(c, repo)["enabled"]:
                        raise ValueError("请先启用清单核验")
                    catalogs.enqueue(c, repo, retry=True)
                    for item in body.get("packages") or []:
                        row = c.execute("SELECT sha256 FROM packages WHERE repo=? AND id=?", (repo, item)).fetchone()
                        if not row:
                            raise ValueError("分片不存在")
                        c.execute("INSERT OR IGNORE INTO source_requests VALUES(?,?,?)", (repo, item, row["sha256"]))
                else:
                    raise ValueError("未知清单操作")
                store.event(c, 'source_catalog', repo, None, actor, 'action=' + str(action))
            result = catalogs.view(c)
        finally:
            c.close()
    if hasattr(store, 'source_catalog'):
        store.source_catalog.wake.set()
    return result
