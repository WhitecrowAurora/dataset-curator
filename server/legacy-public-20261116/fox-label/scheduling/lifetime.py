"""在线租约：成功回报续期 48 小时；离线派单不依赖在线心跳。"""
import time

SECONDS = 48 * 3600
ONLINE_KINDS = ('active', 'prelock')


def ensure_schema(c):
    marker = 'online_lease_lifetime_v1'
    if c.execute('SELECT 1 FROM dispatch_settings WHERE key=?', (marker,)).fetchone():
        return
    # 首次升级给已有在线租约完整宽限，不因旧哨兵值回收正在执行的任务。
    now = time.time()
    c.execute("UPDATE leases SET expires_at=? WHERE status='open' AND kind IN ('active','prelock')",
              (now + SECONDS,))
    c.execute('INSERT INTO dispatch_settings VALUES(?,?)', (marker, str(now)))


def expired(lease, now=None):
    return (lease['status'] == 'open' and lease['kind'] in ONLINE_KINDS
            and lease['expires_at'] <= (time.time() if now is None else now))


def renew(c, device_id, now=None, lease_ids=None):
    now = time.time() if now is None else now
    query = ("UPDATE leases SET last_beat_at=?,expires_at=? WHERE device_id=?"
             " AND status='open' AND kind IN ('active','prelock') AND expires_at>?")
    args = [now, now + SECONDS, device_id, now]
    if lease_ids is not None:
        if not lease_ids:
            return
        query += ' AND lease_id IN (' + ','.join('?' for _ in lease_ids) + ')'
        args.extend(lease_ids)
    c.execute(query, args)


def expire(c, now=None):
    from .leases import refresh, release_targets
    now = time.time() if now is None else now
    rows = c.execute("SELECT * FROM leases WHERE status='open'"
                     " AND kind IN ('active','prelock') AND expires_at<=?", (now,)).fetchall()
    if not rows:
        return []
    c.execute("UPDATE leases SET status='expired',closed_reason='heartbeat_timeout'"
              " WHERE status='open' AND kind IN ('active','prelock') AND expires_at<=?", (now,))
    for lease in rows:
        release_targets(c, dict(lease))
    for repo, pkg in {(r['repo'], r['pkg']) for r in rows}:
        refresh(c, repo, pkg)
    return [r['lease_id'] for r in rows]
