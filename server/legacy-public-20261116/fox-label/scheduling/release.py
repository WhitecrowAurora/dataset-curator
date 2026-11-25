"""释放包与租约共用关闭流程，暂停原设备并保留已回传结果。"""
import secrets
import time

from .leases import release_targets, refresh


def close(c, leases, reason, actor):
    leases = [dict(lease) for lease in leases]
    online_devices = {lease['device_id'] for lease in leases if lease['kind'] in ('active', 'prelock')}
    for lease in leases:
        release_targets(c, lease)
        c.execute("UPDATE leases SET status='released',closed_reason=? WHERE lease_id=? AND status='open'",
                  (reason[:60], lease['lease_id']))
    from assignments.offline import refresh as refresh_offline
    from managed.automation import intent
    from managed.store import current
    now = time.time()
    for device_id in {lease['device_id'] for lease in leases}:
        refresh_offline(c, device_id)
        if device_id not in online_devices:
            continue
        intent(c, device_id, 'stop', reason=reason)
        session = current(c, device_id)
        if not session or session['released']:
            continue
        c.execute("UPDATE managed_commands SET status='canceled',error='租约已释放',updated_at=?"
                  " WHERE session_id=? AND action='start' AND status='queued'", (now, session['session_id']))
        if c.execute("SELECT 1 FROM managed_commands WHERE session_id=? AND action='stop'"
                     " AND status IN ('queued','accepted')", (session['session_id'],)).fetchone():
            continue
        c.execute("INSERT INTO managed_commands(id,session_id,seq,action,config,actor,created_at,updated_at)"
                  " VALUES(?,?,?,'stop','{}',?,?,?)",
                  ('release-' + secrets.token_hex(12), session['session_id'], session['next_seq'], actor, now, now))
        c.execute('UPDATE managed_sessions SET next_seq=next_seq+1 WHERE session_id=?', (session['session_id'],))
    for repo, pkg in {(lease['repo'], lease['pkg']) for lease in leases}:
        refresh(c, repo, pkg)
    return leases
