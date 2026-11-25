"""当前设备身份转换；历史租约和回传记录不改名。"""
import time
from .transitions import atomic


def retire_device(c, device_id):
    from managed.store import retire
    for row in c.execute('SELECT session_id FROM managed_sessions WHERE device_id=? AND released=0', (device_id,)).fetchall():
        retire(c, row['session_id'], time.time())
    c.execute('DELETE FROM history_candidates WHERE device_id=?', (device_id,))


@atomic
def rebind(c, dispatch, device_id, user_id=None):
    if user_id is not None and c.execute("SELECT 1 FROM leases WHERE device_id=? AND status='open'", (device_id,)).fetchone():
        raise dispatch.AuthError('设备仍有执行或预约任务，不能直接改绑')
    from .allocation import forget
    dispatch.package_queue.clear_device(c, device_id)
    forget(c, device_id=device_id)
    retire_device(c, device_id)
    c.execute('UPDATE devices SET workspace_id=NULL,repo_order=\'[]\' WHERE device_id=?', (device_id,))
    if user_id is not None:
        c.execute('UPDATE devices SET user_id=? WHERE device_id=?', (user_id, device_id))
        from .transitions import reconcile
        reconcile(c, dispatch)


def rename(c, dispatch, old, new):
    if c.execute("SELECT 1 FROM leases WHERE user_id=? AND status='open'", (old,)).fetchone():
        raise dispatch.AuthError('该用户还有执行或预约任务，请先完成或回收后改名')
    if c.execute('SELECT 1 FROM managed_sessions WHERE user_id=? AND released=0', (old,)).fetchone():
        raise dispatch.AuthError('该用户仍在托管，请先退出托管后改名')
    from assignments.plans import clear_device
    for device in c.execute('SELECT device_id FROM devices WHERE user_id=?', (old,)).fetchall():
        clear_device(c, device['device_id'])
    c.execute('UPDATE users SET user_id=? WHERE user_id=?', (new, old))
    c.execute('UPDATE devices SET user_id=? WHERE user_id=?', (new, old))
    c.execute("UPDATE part_assignments SET target_id=? WHERE target_type='user' AND target_id=?", (new, old))
    c.execute('UPDATE package_targets SET user_id=? WHERE user_id=?', (new, old))
    c.execute('UPDATE package_queue SET user_id=? WHERE user_id=?', (new, old))
    c.execute('UPDATE repo_users SET user_id=? WHERE user_id=?', (new, old))
