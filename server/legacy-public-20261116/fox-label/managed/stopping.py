"""停工意图独立于在线状态，换会话后按原意图继续执行。"""
import secrets


def cancel_starts(c, session, now):
    c.execute("UPDATE managed_commands SET status='canceled',error='管理员已停止设备',updated_at=?"
              " WHERE session_id=? AND action='start' AND status='queued'", (now, session))


def reconcile(c, session, device_id, state, now):
    policy = c.execute('SELECT desired,pause_reason FROM managed_policy WHERE device_id=?', (device_id,)).fetchone()
    if not policy or policy['desired'] != 'paused' or policy['pause_reason'] in ('failure', 'limit'):
        return
    from .automation import BUSY
    if not (state.get('running') or state.get('preparing') or state.get('history_running') or state.get('status') in BUSY):
        return
    cancel_starts(c, session, now)
    if c.execute("SELECT 1 FROM managed_commands WHERE session_id=? AND action='stop'"
                 " AND status IN ('queued','accepted')", (session,)).fetchone():
        return
    seq = c.execute('SELECT next_seq FROM managed_sessions WHERE session_id=?', (session,)).fetchone()[0]
    c.execute("INSERT INTO managed_commands(id,session_id,seq,action,config,actor,created_at,updated_at)"
              " VALUES(?,?,?,'stop','{}','policy',?,?)", ('stop-'+secrets.token_hex(12), session, seq, now, now))
    c.execute('UPDATE managed_sessions SET next_seq=next_seq+1 WHERE session_id=?', (session,))
