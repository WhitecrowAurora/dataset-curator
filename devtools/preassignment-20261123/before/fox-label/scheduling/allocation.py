"""仓库设备分配；主动领取和被动派工使用同一许可。"""
import json


def ensure_schema(c, dispatch):
    dispatch._add_columns(c, 'workspaces', (("allocation_mode", "TEXT NOT NULL DEFAULT 'auto'"),))
    c.execute('CREATE TABLE IF NOT EXISTS repo_devices (ws_id TEXT NOT NULL,repo TEXT NOT NULL,'
              'device_id TEXT NOT NULL,PRIMARY KEY(ws_id,repo,device_id))')


def members(c, ws_id):
    result = {}
    for row in c.execute('SELECT repo,device_id FROM repo_devices WHERE ws_id=? ORDER BY repo,device_id', (ws_id,)):
        result.setdefault(row['repo'], []).append(row['device_id'])
    return result


def forget(c, *, device_id=None, user_id=None):
    if device_id is not None:
        c.execute('DELETE FROM repo_devices WHERE device_id=?', (device_id,))
        from .identities import retire_device
        retire_device(c, device_id)
    if user_id is not None:
        from .identities import retire_device
        for row in c.execute('SELECT device_id FROM devices WHERE user_id=?', (user_id,)).fetchall():
            retire_device(c, row['device_id'])
        c.execute('DELETE FROM repo_devices WHERE device_id IN (SELECT device_id FROM devices WHERE user_id=?)', (user_id,))
        c.execute("DELETE FROM part_assignments WHERE target_type='user' AND target_id=?", (user_id,))
        c.execute('DELETE FROM package_targets WHERE user_id=?', (user_id,))


def permissions(c, device, ws):
    """手动仓库只允许明确选中的设备；未归属设备也不能绕过分配。"""
    manual = set()
    for row in c.execute("SELECT repos FROM workspaces WHERE allocation_mode='manual'"):
        manual.update(json.loads(row['repos']))
    assigned = {r['repo'] for r in c.execute('SELECT repo FROM repo_devices WHERE ws_id=? AND device_id=?',
                                            (ws['ws_id'] if ws else '', device['device_id']))}
    return manual, assigned


def allowed(c, dispatch, device, repo):
    ws = dispatch.workspace_of(c, device)
    if ws is not None and repo not in ws['repos']:
        return False
    manual, assigned = permissions(c, device, ws)
    return repo not in manual or bool(ws and ws.get('allocation_mode') == 'manual' and repo in assigned)


def update(c, dispatch, body):
    ws = dispatch.ws_get(c, body.get('ws_id'))
    mode, mapping = body.get('mode'), body.get('repo_devices', {})
    if mode not in ('auto', 'manual') or not isinstance(mapping, dict):
        raise ValueError('仓库分配格式不正确')
    if any(repo not in ws['repos'] for repo in mapping):
        raise ValueError('仓库超出工作区')
    for repo, ids in mapping.items():
        if not isinstance(ids, list) or any(not isinstance(did, str) for did in ids) or len(set(ids)) != len(ids):
            raise ValueError('设备列表格式不正确')
        for did in ids:
            device = dispatch._row(c, 'SELECT * FROM devices WHERE device_id=?', (did,))
            if not device or device['status'] in ('blocked', 'archived') or did.startswith('offline-'):
                raise ValueError('设备不存在或已停用')
            account = c.execute('SELECT status FROM users WHERE user_id=?', (device['user_id'],)).fetchone()
            effective = dispatch.workspace_of(c, device)
            if not account or account['status'] != 'active' or not effective or effective['ws_id'] != ws['ws_id']:
                raise ValueError('设备不属于当前工作区或用户已停用')
    c.execute('UPDATE workspaces SET allocation_mode=? WHERE ws_id=?', (mode, ws['ws_id']))
    c.execute('DELETE FROM repo_devices WHERE ws_id=?', (ws['ws_id'],))
    c.executemany('INSERT INTO repo_devices VALUES(?,?,?)',
                  [(ws['ws_id'], repo, did) for repo, ids in mapping.items() for did in ids])
    from .transitions import reconcile
    return reconcile(c, dispatch)
