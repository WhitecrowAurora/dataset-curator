"""配置转换后的许可、指定及预约同步；执行租约继续保留。"""
from functools import wraps
import json


def atomic(fn):
    @wraps(fn)
    def wrapped(c, *args, **kwargs):
        if c.in_transaction:
            return fn(c, *args, **kwargs)
        from dispatch import _Tx
        with _Tx(c):
            return fn(c, *args, **kwargs)
    return wrapped


def reconcile(c, dispatch):
    from .policies import repo_order
    from .selection import valid_prelock

    accounts = {r['user_id']: r['status'] for r in c.execute('SELECT user_id,status FROM users')}
    devices = {r['device_id']: dict(r) for r in c.execute('SELECT * FROM devices')}
    usable = {did: d for did, d in devices.items() if d['status'] not in ('blocked', 'archived')
              and not did.startswith('offline-') and accounts.get(d['user_id']) == 'active'}
    workspaces = {did: dispatch.workspace_of(c, d) for did, d in usable.items()}
    removed = 0
    for row in c.execute('SELECT * FROM repo_devices').fetchall():
        ws = workspaces.get(row['device_id'])
        if not ws or ws['ws_id'] != row['ws_id'] or row['repo'] not in ws['repos']:
            c.execute('DELETE FROM repo_devices WHERE ws_id=? AND repo=? AND device_id=?',
                      (row['ws_id'], row['repo'], row['device_id']))
            removed += 1
    from assignments.preassignment import user_allowed, user_workspace
    for row in c.execute('SELECT * FROM repo_users').fetchall():
        try:
            ws = user_workspace(c, dispatch, row['user_id'])
        except ValueError:
            ws = None
        if not ws or ws['ws_id'] != row['ws_id'] or row['repo'] not in ws['repos']:
            c.execute('DELETE FROM repo_users WHERE ws_id=? AND repo=? AND user_id=?',
                      (row['ws_id'], row['repo'], row['user_id']))
            removed += 1
    # 用户仓库许可可能刚被清理，重新计算设备可用范围。
    permitted = {did: set(repo_order(c, d, workspaces[did])) for did, d in usable.items()}
    targets = {}
    for did, d in usable.items():
        targets.setdefault(d['user_id'], []).append(did)

    def candidates(repo, user=None, did=None, part=None):
        ids = [did] if did else targets.get(user, [])
        return [key for key in ids if repo in permitted.get(key, ())
                and (part is None or part in (dispatch._wanted_parts(usable[key]) or dispatch.PARTS))]

    cleared = 0
    for row in c.execute('SELECT * FROM part_assignments').fetchall():
        preserve_user = row['target_type'] == 'user' and user_allowed(c, dispatch, row['target_id'], row['repo'])
        if not preserve_user and not candidates(row['repo'],
                          user=row['target_id'] if row['target_type'] == 'user' else None,
                          did=row['target_id'] if row['target_type'] == 'device' else None, part=row['part']):
            c.execute('DELETE FROM part_assignments WHERE repo=? AND pkg=? AND part=?',
                      (row['repo'], row['pkg'], row['part']))
            cleared += 1
    for row in c.execute('SELECT * FROM package_queue').fetchall():
        if not candidates(row['repo'], did=row['device_id']):
            c.execute('DELETE FROM package_queue WHERE repo=? AND pkg=?', (row['repo'], row['pkg']))
            cleared += 1
    manual = set()
    for row in c.execute("SELECT repos FROM workspaces WHERE allocation_mode='manual'"):
        manual.update(json.loads(row['repos']))
    for row in c.execute('SELECT * FROM package_targets').fetchall():
        # 自动仓库的旧接口允许提前指定尚未登记的用户，继续保留此行为。
        known = row['user_id'] in accounts and bool(c.execute(
            'SELECT 1 FROM devices WHERE user_id=?', (row['user_id'],)).fetchone())
        if not user_allowed(c, dispatch, row['user_id'], row['repo']) and (known or row['repo'] in manual) and not candidates(row['repo'], user=row['user_id']):
            c.execute('DELETE FROM package_targets WHERE repo=? AND pkg=?', (row['repo'], row['pkg']))
            cleared += 1
    released = 0
    for row in c.execute("SELECT * FROM leases WHERE kind='prelock' AND status='open'").fetchall():
        device = usable.get(row['device_id'])
        if not device or not valid_prelock(c, dispatch, device, dict(row)):
            c.execute("UPDATE leases SET status='released',closed_reason='policy_changed' WHERE lease_id=?", (row['lease_id'],))
            released += 1
    return dict(released_prelocks=released, cleared_assignments=cleared, removed_repo_devices=removed)
