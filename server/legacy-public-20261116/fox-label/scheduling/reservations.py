"""批量队列按分项指定；实际领取仍使用设备的两个租约槽。"""
import time

from . import leases


def enqueue(c, dispatch, body, actor=None, on_event=None):
    from assignments.store import identity, selection
    from assignments import annotation
    items = selection(body)
    annotation_options = annotation.options(body["annotation"]) if "annotation" in body else None
    if actor is None and annotation_options is not None:
        raise ValueError("标注选项由派单面板设置")
    with dispatch._Tx(c):
        if actor is None:
            devices = [identity(c, dispatch, body)]
        else:
            ids = body.get('devices')
            if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
                raise ValueError('请选择设备')
            devices = [dispatch._row(c, 'SELECT * FROM devices WHERE device_id=?', (did,)) for did in ids]
        if any(not d or d['status'] in ('blocked','archived') or d['device_id'].startswith('offline-') for d in devices):
            raise dispatch.LeaseError('设备不存在或已停用')
        for d in devices:
            user = dispatch._row(c, 'SELECT status FROM users WHERE user_id=?', (d['user_id'],))
            if not user or user['status'] != 'active':
                raise dispatch.LeaseError('用户已停用')
            ws = dispatch.workspace_of(c, d)
            if actor is None and ws is not None and not ws.get('allow_manual_claim'):
                raise dispatch.LeaseError('管理员未允许客户端手动领取')
            if actor is not None and (ws is None or ws['ws_id'] != body.get('ws_id')):
                raise dispatch.LeaseError('设备不属于当前工作区')
        busy = leases.held(c)
        from delivery.conflicts import blocked
        for key, parts in blocked(c).items():
            busy.setdefault(key, set()).update(parts)
        loads = {d['device_id']: c.execute("SELECT COUNT(*) FROM part_assignments WHERE target_type='device' AND target_id=?",
                                         (d['device_id'],)).fetchone()[0] for d in devices}
        results, reserved, already = [], 0, 0
        for repo, pkg in items:
            package = dispatch._row(c, 'SELECT p.*,r.enabled FROM packages p JOIN repos r ON r.repo=p.repo WHERE p.repo=? AND p.id=?', (repo,pkg))
            if not package or not package['enabled'] or package['status'] not in ('available','claimed'):
                raise dispatch.LeaseError('分片不能分配')
            assigned = 0
            for part in dispatch.missing_parts(package):
                if part in busy.get((repo,pkg), ()):
                    continue
                old = dispatch._row(c, 'SELECT * FROM part_assignments WHERE repo=? AND pkg=? AND part=?', (repo,pkg,part))
                whole = dispatch._row(c,'SELECT user_id FROM package_targets WHERE repo=? AND pkg=?',(repo,pkg))
                queue = dispatch._row(c,'SELECT device_id FROM package_queue WHERE repo=? AND pkg=?',(repo,pkg))
                candidates = []
                for d in devices:
                    from .allocation import allowed
                    if not allowed(c, dispatch, d, repo):
                        continue
                    ws = dispatch.workspace_of(c, d)
                    if ws is not None and repo not in ws['repos']:
                        continue
                    if part not in (dispatch._wanted_parts(d) or dispatch.PARTS):
                        continue
                    if whole and whole['user_id'] != d['user_id'] or queue and queue['device_id'] != d['device_id']:
                        continue
                    if old and old['target_id'] != (d['device_id'] if old['target_type'] == 'device' else d['user_id']):
                        continue
                    candidates.append(d)
                if not candidates:
                    continue
                d = min(candidates, key=lambda x: loads[x['device_id']])
                assigned += 1
                if old:
                    already += 1
                else:
                    c.execute('INSERT INTO part_assignments(repo,pkg,part,target_type,target_id,actor,updated_at) VALUES(?,?,?,\'device\',?,?,?)',
                              (repo,pkg,part,d['device_id'],actor or 'client',time.time()))
                    reserved += 1
                    if on_event:
                        on_event(c,repo,pkg,d['user_id'],d['device_id'])
                loads[d['device_id']] += 1
                if annotation_options is not None:
                    annotation.save(c, package, part, annotation_options)
                if actor is None:
                    c.execute('UPDATE devices SET queue_only=1 WHERE device_id=?', (d['device_id'],))
                results.append(dict(repo=repo,id=pkg,part=part,device_id=d['device_id'],
                                    annotation=annotation.for_package(c, package, [part])))
            if not assigned:
                raise dispatch.LeaseError('没有可接收分项的选中设备：' + pkg)
    return dict(ok=True, assigned=results, reserved=reserved, already_reserved=already)
