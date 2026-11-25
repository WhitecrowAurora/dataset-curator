"""未归属成员沿用最近可自动派工的工作区，已指定的归属保持不变。"""


def join(c, dispatch, device):
    current = dispatch.workspace_of(c, device)
    if current is not None:
        return current
    from .control import running
    if not running(c):
        return None
    if device.get('queue_only') or c.execute(
            "SELECT 1 FROM leases WHERE user_id=? AND status IN ('open','submitted') LIMIT 1",
            (device['user_id'],)).fetchone():
        return None
    if c.execute('SELECT 1 FROM device_dispatch_plans WHERE device_id=?',
                 (device['device_id'],)).fetchone():
        return None
    if c.execute("SELECT 1 FROM package_targets WHERE user_id=? UNION ALL"
                 " SELECT 1 FROM package_queue WHERE device_id=? UNION ALL"
                 " SELECT 1 FROM part_assignments WHERE (target_type='user' AND target_id=?)"
                 " OR (target_type='device' AND target_id=?) LIMIT 1",
                 (device['user_id'], device['device_id'], device['user_id'], device['device_id'])).fetchone():
        return None
    for row in c.execute("SELECT ws_id FROM workspaces WHERE allocation_mode='auto'"
                         " ORDER BY COALESCE(NULLIF(auto_assign_at,''),created_at) DESC,rowid DESC").fetchall():
        candidate = {**device, 'workspace_id': row['ws_id']}
        if dispatch._pick_package(c, candidate, dispatch.params()) is not None:
            # 账号默认让后来登记的同用户设备沿用；管理员仍可覆盖账号或单台设备。
            dispatch.ws_assign(c, row['ws_id'], user_id=device['user_id'])
            return dispatch.workspace_of(c, device)
    return None
