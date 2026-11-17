"""客户端自主核对沿用历史检查、设备身份与会话占用。"""
import secrets
import time

from managed import store as managed


def device_identity(c, dispatch, body):
    did, user, secret = (body.get(k) for k in ('device_id', 'user_id', 'secret'))
    device = c.execute('SELECT * FROM devices WHERE device_id=?', (did,)).fetchone()
    account = c.execute('SELECT status FROM users WHERE user_id=?', (user,)).fetchone()
    if (not device or device['user_id'] != user or device['status'] in ('blocked', 'archived')
            or not account or account['status'] != 'active' or not isinstance(secret, str)):
        raise dispatch.AuthError('历史核对设备身份不匹配，请重新连接中心')
    return dispatch._require_device(c, user, did, secret)


def authenticate(c, dispatch, body):
    device_identity(c, dispatch, body)
    did, user, secret = (body.get(k) for k in ('device_id', 'user_id', 'secret'))
    current = managed.current(c, did)
    if (not current or current['released'] or current['user_id'] != user
            or current['session_id'] != body.get('session_id')
            or not secrets.compare_digest(dispatch.hash_secret(secret), current['secret_hash'])):
        raise dispatch.AuthError('历史核对身份或会话不匹配，请重新连接中心')
    return current


def begin(store, dispatch, body):
    from .store import request
    return request(store, dispatch, body, body.get('user_id'), local=True)


def cancel(store, dispatch, body):
    with managed.transaction(store) as c:
        current = authenticate(c, dispatch, body)
        c.execute("UPDATE history_checks SET status='canceled',updated_at=? WHERE check_id=?"
                  " AND session_id=? AND device_id=? AND status='pending'",
                  (time.time(), body.get('check_id'), current['session_id'], body.get('device_id')))
    return {'ok': True}


def catalog(store, dispatch, body):
    from contextlib import closing
    with closing(store.conn()) as c:
        device = device_identity(c, dispatch, body)
        workspace = dispatch.workspace_of(c, device)
        offset = max(0, int(body.get('offset', 0)))
        sql = 'SELECT p.repo,p.id,p.size,p.sha256 FROM packages p JOIN repos r ON r.repo=p.repo WHERE r.enabled=1'
        args = []
        if 'sources' in body:
            sources = body['sources']
            if not isinstance(sources, list) or len(sources) > 100:
                raise ValueError('每次最多查询 100 个本地分片')
            if not sources:
                return {'ok': True, 'packages': [], 'next_offset': None}
            names = set()
            for source in sources:
                stem, size = source.get('stem'), source.get('size')
                if not isinstance(stem, str) or not stem or not isinstance(size, int) or size < 0:
                    raise ValueError('本地分片名称或大小无效')
                names.update((stem + suffix, size) for suffix in ('.tar', '.tar.gz', '.tgz', '.zip'))
            wanted = sorted(names)
            args = [value for name, size in wanted for value in
                    (name, size, '%/' + name.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_'))]
            sql = ('WITH wanted(name,size,suffix) AS (VALUES ' + ','.join('(?,?,?)' for _ in wanted) + ') '
                   'SELECT DISTINCT p.repo,p.id,p.size,p.sha256 FROM wanted w JOIN packages p '
                   "ON p.size=w.size AND (p.id=w.name OR p.id LIKE w.suffix ESCAPE '\\') "
                   'JOIN repos r ON r.repo=p.repo WHERE r.enabled=1')
        if workspace is not None:
            repos = workspace['repos']
            if not repos:
                return {'ok': True, 'packages': [], 'next_offset': None}
            sql += ' AND p.repo IN (' + ','.join('?' for _ in repos) + ')'
            args.extend(repos)
        rows = [dict(r) for r in c.execute(sql + ' ORDER BY p.repo,p.id LIMIT 501 OFFSET ?', [*args, offset])]
    return {'ok': True, 'packages': rows[:500], 'next_offset': offset + 500 if len(rows) > 500 else None}


def reports(store, dispatch, body):
    import json
    from contextlib import closing
    with closing(store.conn()) as c:
        did, user = body.get('device_id'), body.get('user_id')
        device_identity(c, dispatch, body)
        sql = 'SELECT * FROM history_checks WHERE device_id=? AND user_id=?'
        args = [did, user]
        if 'check_ids' in body:
            ids = body['check_ids']
            if not isinstance(ids, list) or len(ids) > 500 or any(not isinstance(i, str) for i in ids):
                raise ValueError('每次最多查询 500 个核对记录')
            if not ids:
                return {'ok': True, 'history_reports': []}
            sql += ' AND check_id IN (' + ','.join('?' for _ in ids) + ')'
            args.extend(ids)
        items = [{**{k:r[k] for k in ('check_id','candidate_id','status')}, 'report': json.loads(r['report'])}
                 for r in c.execute(sql + ' ORDER BY updated_at DESC LIMIT 500', args)]
    return {'ok': True, 'history_reports': items}
