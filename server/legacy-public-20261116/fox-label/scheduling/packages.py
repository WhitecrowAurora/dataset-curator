"""分片列表的分项占用与逐图计数，按页读取。"""
import json
from .leases import held
from .selection import eligible


def page(c, dispatch, device=None, ws_id=None, offset=0):
    offset = int(offset)
    if offset < 0:
        raise ValueError("分页位置不能为负数")
    ws = dispatch.workspace_of(c, device) if device else dispatch.ws_get(c, ws_id)
    repos = ws["repos"] if ws is not None else [r['repo'] for r in c.execute('SELECT repo FROM repos WHERE enabled=1')]
    if device:
        from .policies import repo_order
        repos = repo_order(c, device, ws)
    if not repos:
        return dict(ok=True, items=[], total=0, next_offset=None, workspace=dispatch.workspace_brief(ws),
                    allow_manual_claim=bool(ws and ws.get('allow_manual_claim')), queue_only=bool(device and device.get('queue_only')),
                    empty_reason='等待管理员分配' if ws and ws.get('allocation_mode') == 'manual' else '等待管理员配置')
    marks = ','.join('?' for _ in repos)
    query = f" FROM packages p JOIN repos r ON r.repo=p.repo WHERE p.repo IN ({marks}) AND r.enabled=1"
    # 管理员也需要看已完成分片的上传与验收状态。
    if device:
        query += " AND p.status<>'completed'"
    total = c.execute('SELECT COUNT(*)' + query, repos).fetchone()[0]
    rows = c.execute('SELECT p.*' + query + ' ORDER BY p.repo,p.id LIMIT 100 OFFSET ?', [*repos, offset]).fetchall()
    items = []
    busy = held(c)
    from assignments.preassignment import states
    page_keys = {(r['repo'], r['id']) for r in rows}
    page_assignments = [dict(a) for a in c.execute('SELECT * FROM part_assignments') if (a['repo'], a['pkg']) in page_keys]
    pending_states = states(c, dispatch, page_assignments) if not device else {}
    from delivery.conflicts import blocked
    for key, parts in blocked(c).items():
        busy.setdefault(key, set()).update(parts)
    for r in rows:
        item = dict(r)
        occupied = [dict(l) for l in c.execute("SELECT l.lease_id,l.device_id,l.kind,lp.part FROM lease_parts lp"
                   " JOIN leases l ON l.lease_id=lp.lease_id WHERE lp.repo=? AND lp.pkg=? AND lp.open=1", (r['repo'], r['id']))]
        item['leases'] = occupied
        item['pending_results']=[dict(x) for x in c.execute('SELECT device_id,parts_json,created_at FROM pending_local_results'
                                                          ' WHERE repo=? AND pkg=? AND source_sha256=?',(r['repo'],r['id'],r['sha256']))]
        item['missing_parts'] = dispatch.missing_parts(item)
        free = [p for p in item['missing_parts'] if p not in busy.get((r['repo'], r['id']), ())]
        assignments = [dict(a) for a in c.execute('SELECT * FROM part_assignments WHERE repo=? AND pkg=?', (r['repo'], r['id']))]
        q = c.execute('SELECT device_id FROM package_queue WHERE repo=? AND pkg=?', (r['repo'], r['id'])).fetchone()
        target = c.execute('SELECT user_id FROM package_targets WHERE repo=? AND pkg=?', (r['repo'], r['id'])).fetchone()
        item['unassigned_parts'] = ([part for part in free if not any(a['part'] == part for a in assignments)]
                                    if not q and not target else [])
        item['reserved_user'] = target['user_id'] if target else None
        if device:
            free, _ = eligible(dispatch, device, item, busy.get((r['repo'], r['id']), ()),
                               {a['part']: a for a in assignments}, target['user_id'] if target else None,
                               q['device_id'] if q else None)
        item['assignments'] = assignments
        for target in assignments:
            target['state'] = '执行中' if any(l['part'] == target['part'] for l in occupied) else pending_states.get((r['repo'], r['id'], target['part']), '待领取')
        from assignments.annotation import for_package as annotation_for_package
        item['annotation'] = annotation_for_package(c, item, ('tag', 'nl'))
        item['claimable_parts'] = free
        item['claimable'] = bool(free) and (not device or ws is None or bool(ws.get('allow_manual_claim')))
        item['working_device'] = occupied[0]['device_id'] if occupied else None
        item['assigned_device'] = q['device_id'] if q else None
        proof = c.execute('SELECT report_json FROM package_artifacts WHERE repo=? AND pkg=? AND source_sha256=?',
                          (r['repo'], r['id'], r['sha256'])).fetchone()
        item['verification'] = json.loads(proof['report_json']) if proof else None
        from source_catalog.store import status as source_status
        item['source_verification'] = source_status(c, r['repo'], r['id'], r['sha256'], r['size'])
        item['source_submitted'] = bool(c.execute("SELECT 1 FROM source_submissions WHERE repo=? AND pkg=? AND source_sha256=? AND state='pending'",
                                                  (r['repo'],r['id'],r['sha256'])).fetchone() or
                                        c.execute("SELECT 1 FROM history_checks WHERE repo=? AND package=? AND status='verifying'", (r['repo'],r['id'])).fetchone())
        items.append(item)
    return dict(ok=True, items=items, total=total, next_offset=offset + len(items) if offset + len(items) < total else None,
                workspace=dispatch.workspace_brief(ws), allow_manual_claim=ws is None or bool(ws.get('allow_manual_claim')),
                queue_only=bool(device and device.get('queue_only')), empty_reason='暂无分片' if not total else '')
