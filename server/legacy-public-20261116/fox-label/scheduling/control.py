"""管理员控制在线派单；暂停保留现有租约与离线工单。"""
import json
import time

KEY = 'auto_dispatch_v1'


def ensure_schema(c):
    c.execute('INSERT OR IGNORE INTO dispatch_settings(key,value) VALUES(?,?)',
              (KEY, json.dumps(dict(running=False, changed_at=time.time(), changed_by='migration'))))


def state(c):
    row = c.execute('SELECT value FROM dispatch_settings WHERE key=?', (KEY,)).fetchone()
    return json.loads(row['value']) if row else dict(running=False, changed_at=0, changed_by='')


def running(c):
    return state(c)['running']


def cancel_starts(c, now=None):
    """取消尚未接收的开始指令，保留序号供客户端正常确认。"""
    return c.execute("UPDATE managed_commands SET status='canceled',error='管理员已暂停自动派单',updated_at=?"
                     " WHERE action='start' AND status='queued' AND (actor='auto' OR NOT EXISTS ("
                     " SELECT 1 FROM managed_sessions s JOIN leases l ON l.device_id=s.device_id"
                     " WHERE s.session_id=managed_commands.session_id AND l.kind='active' AND l.status='open'))",
                     (time.time() if now is None else now,)).rowcount


def update(c, body, actor):
    if type(body.get('running')) is not bool:
        raise ValueError('请选择启动或暂停自动派单')
    value = dict(running=body['running'], changed_at=time.time(), changed_by=actor)
    c.execute('INSERT INTO dispatch_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
              (KEY, json.dumps(value)))
    canceled = cancel_starts(c, value['changed_at']) if not value['running'] else 0
    return dict(dispatch=value, canceled_starts=canceled)


def offered(c, leases):
    """当前分片继续处理；暂停期间不把预锁交给 worker 下载或升级。"""
    return leases if running(c) else [lease for lease in leases if lease['kind'] != 'prelock']


def overview(c):
    counts = {r['kind']: r['count'] for r in c.execute(
        "SELECT kind,COUNT(*) count FROM leases WHERE status='open' GROUP BY kind")}
    pending = c.execute("SELECT COUNT(*) FROM managed_commands c JOIN managed_sessions s USING(session_id)"
                        " WHERE c.action='start' AND c.status IN ('queued','accepted') AND s.released=0").fetchone()[0]
    return dict(ok=True, dispatch=state(c), active_leases=counts.get('active', 0),
                prelocks=counts.get('prelock', 0), offline_leases=counts.get('offline', 0), pending_starts=pending)
