"""显式恢复请求的去重和审计；不改变分派范围或租约。"""
from .contracts import ManagedError, identifier

SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_recovery (
 session_id TEXT NOT NULL, request_id TEXT NOT NULL, intent TEXT NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(session_id,request_id)
);
CREATE INDEX IF NOT EXISTS idx_managed_recovery_request ON managed_recovery(request_id);
"""


def request(body):
    intent = body.get('intent')
    if intent not in (None, 'start', 'force_start'):
        raise ManagedError('托管接单意图不正确')
    request_id = body.get('intent_id')
    if request_id is not None:
        identifier(request_id, 'intent_id')
    if intent == 'force_start' and request_id is None:
        raise ManagedError('强制继续缺少请求编号')
    return intent, request_id


def apply(c, store, session, device_id, intent, request_id, seq, now, actor):
    if request_id:
        # ACK 丢失后客户端可能换会话重送；去重绑定已认证设备身份，不能覆盖后来的暂停。
        previous = c.execute('SELECT r.intent FROM managed_recovery r'
                             ' JOIN managed_sessions old ON old.session_id=r.session_id'
                             ' JOIN managed_sessions current ON current.session_id=?'
                             ' WHERE r.request_id=? AND old.device_id=current.device_id'
                             ' AND old.user_id=current.user_id AND old.secret_hash=current.secret_hash'
                             ' ORDER BY r.created_at LIMIT 1', (session, request_id)).fetchone()
        if previous:
            if previous['intent'] != intent:
                raise ManagedError('恢复请求编号已被使用', 409)
            return False
    from .automation import intent as set_intent
    from .limits import restart
    restart(c, device_id)
    if intent == 'force_start':
        # 取消未接收的旧启停指令但保留序号，客户端仍依序确认。
        c.execute("UPDATE managed_commands SET status='canceled',error='被强制继续取代',updated_at=?"
                  " WHERE session_id=? AND seq>? AND status='queued' AND action IN ('start','stop')",
                  (now, session, seq))
    set_intent(c, device_id, 'start', reason=f'{actor}:{intent}')
    c.execute("UPDATE managed_sessions SET automation_error='' WHERE session_id=?", (session,))
    if request_id:
        c.execute('INSERT INTO managed_recovery VALUES(?,?,?,?)', (session, request_id, intent, now))
    store.event(c, 'managed_resume', None, None, actor,
                f'device={device_id}; session={session}; intent={intent}; request={request_id or "legacy"}; seq={seq}')
    return True
