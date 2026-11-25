"""自动接单轮次配额，重连和 worker 重启沿用同一轮计数。"""
import uuid

from .contracts import ManagedError, identifier

SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_runs (
 device_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, max_packages INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS managed_run_results (
 device_id TEXT NOT NULL, run_id TEXT NOT NULL, lease_id TEXT NOT NULL UNIQUE
);
"""


def configure(c, device_id, value):
    if value is None:
        return
    if not isinstance(value, dict):
        raise ManagedError('接单轮次格式不正确')
    run_id = identifier(value.get('run_id'), 'run_id')
    limit = value.get('max_packages')
    if type(limit) is not int or not 0 <= limit <= 1000000:
        raise ManagedError('分片上限必须为0～1000000的整数')
    c.execute('INSERT INTO managed_runs(device_id,run_id,max_packages) VALUES(?,?,?)'
              ' ON CONFLICT(device_id) DO UPDATE SET max_packages=excluded.max_packages',
              (device_id, run_id, limit))


def view(c, device_id):
    row=c.execute('SELECT * FROM managed_runs WHERE device_id=?',(device_id,)).fetchone()
    return dict(row) if row else {}


def reached(c, device_id):
    run=view(c, device_id)
    return bool(run and run['max_packages'] and run['completed'] >= run['max_packages'])


def restart(c, device_id):
    if reached(c, device_id):
        c.execute('UPDATE managed_runs SET run_id=?,completed=0 WHERE device_id=?',
                  (uuid.uuid4().hex, device_id))


def can_prefetch(c, device_id):
    run=view(c,device_id)
    return not run or not run['max_packages'] or run['completed'] + 1 < run['max_packages']


def completed(c, lease):
    run=view(c, lease['device_id'])
    if not run or lease['kind'] == 'offline':
        return
    added=c.execute('INSERT OR IGNORE INTO managed_run_results VALUES(?,?,?)',
                    (lease['device_id'],run['run_id'],lease['lease_id'])).rowcount
    if added:
        c.execute('UPDATE managed_runs SET completed=completed+1 WHERE device_id=?',(lease['device_id'],))
    if reached(c, lease['device_id']):
        from .automation import intent
        intent(c,lease['device_id'],'stop',reason='limit')
        c.execute("UPDATE leases SET status='released',closed_reason='run_limit'"
                  " WHERE device_id=? AND status='open' AND kind='prelock'",(lease['device_id'],))


def can_claim(c, device_id):
    if reached(c, device_id):
        return False
    row=c.execute('SELECT desired FROM managed_policy WHERE device_id=?',(device_id,)).fetchone()
    return not row or row['desired']=='running'
