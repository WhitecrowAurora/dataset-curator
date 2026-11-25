"""手动分派快照：导出不领取，应用时核对当前分项归属。"""
import hashlib
import json
import secrets
import time

from . import annotation

FORMAT = 'lulynx-dispatch-plan'


def ensure_schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS dispatch_plans (id TEXT PRIMARY KEY,created_at REAL NOT NULL,manifest TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS device_dispatch_plans (device_id TEXT PRIMARY KEY,plan_id TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS dispatch_plan_applications (plan_id TEXT NOT NULL,device_id TEXT NOT NULL,mode TEXT NOT NULL,job_id TEXT,PRIMARY KEY(plan_id,device_id))')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load(c, plan_id):
    row = c.execute('SELECT manifest FROM dispatch_plans WHERE id=?', (plan_id,)).fetchone()
    if not row:
        raise ValueError('分派清单不存在，请管理员重新导出')
    return json.loads(row[0])


def rows(c, dispatch, ws):
    repos = ws['repos']
    if not repos:
        return []
    marks = ','.join('?' for _ in repos)
    devices = {r['device_id']: dict(r) for r in c.execute('SELECT * FROM devices')}
    device_workspaces = {did: (dispatch.workspace_of(c,d) or {}).get('ws_id') for did,d in devices.items()}
    workspace_users = {d['user_id'] for did,d in devices.items() if device_workspaces[did]==ws['ws_id']}
    assignments = {(r['repo'], r['pkg'], r['part']): dict(r) for r in c.execute('SELECT * FROM part_assignments')}
    queues = {(r['repo'], r['pkg']): dict(r) for r in c.execute('SELECT * FROM package_queue')}
    targets = {(r['repo'], r['pkg']): r['user_id'] for r in c.execute('SELECT * FROM package_targets')}
    occupied = {(r['repo'], r['pkg'], r['part']): dict(r) for r in c.execute('SELECT l.*,lp.part FROM leases l JOIN lease_parts lp USING(lease_id) WHERE lp.open=1')}
    result = []
    for package in c.execute(f'SELECT p.*,r.repo_type FROM packages p JOIN repos r ON r.repo=p.repo WHERE p.repo IN ({marks}) ORDER BY p.repo,p.id', repos):
        package = dict(package)
        key = package['repo'], package['id']
        for part in dispatch.missing_parts(package):
            target = assignments.get((*key, part))
            queued = queues.get(key)
            active = occupied.get((*key, part))
            if target:
                did = target['target_id'] if target['target_type'] == 'device' else ''
                user = devices.get(did, {}).get('user_id', '') if did else target['target_id']
            elif queued:
                did, user = queued['device_id'], queued['user_id']
            elif key in targets:
                did, user = '', targets[key]
            elif active:
                did, user = active['device_id'], active['user_id']
            else:
                continue
            if not user:
                continue
            if did:
                if device_workspaces.get(did) != ws['ws_id']:
                    continue
            else:
                user_device = dict(user_id=user, device_id='', workspace_id=None)
                owner_ws = dispatch.workspace_of(c, user_device)
                in_workspace = owner_ws and owner_ws['ws_id'] == ws['ws_id']
                if not in_workspace:
                    in_workspace = user in workspace_users
                if not in_workspace:
                    continue
            result.append(dict(repo=key[0], package=key[1], part=part, user_id=user, device_id=did,
                               size=package['size'], sha256=package['sha256'], repo_type=package['repo_type'],
                               annotation=annotation.for_package(c, package, [part]),
                               state='执行中' if active else '待领取'))
    return result


def create(store, dispatch, body, actor):
    from managed.contracts import config
    from annotation.store import config as artist_config
    from source_catalog.store import config as source_config
    from urllib.parse import urlsplit
    server = body.get('server', '')
    if not isinstance(server, str):
        raise ValueError('调度中心地址不正确')
    url = urlsplit(server)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('请填写调度中心 HTTP 地址')
    order = body.get('sort', 'user')
    if order not in ('user', 'package'):
        raise ValueError('排序方式不正确')
    fallback = config(body.get('config', {}))
    with store._write_lock:
        c = store.conn()
        try:
            with dispatch._Tx(c):
                ws = dispatch.ws_get(c, body.get('ws_id'))
                tasks = rows(c, dispatch, ws)
                if not tasks:
                    raise ValueError('当前工作区没有已分配的待处理分项')
                tasks.sort(key=(lambda r: (r['user_id'], r['device_id'], r['repo'], r['package'], r['part'])) if order == 'user' else
                           (lambda r: (r['package'], r['repo'], r['user_id'], r['device_id'], r['part'])))
                from managed.store import current
                configs = {}
                for task in tasks:
                    did = task['device_id']
                    if did not in configs:
                        session = current(c, did) if did else None
                        configs[did] = config(json.loads(session['config'])) if session else fallback
                    task['config'] = configs[did]
                repositories = [dict(repo=repo, artist=artist_config(c, repo), source=source_config(c, repo)) for repo in ws['repos']]
                manifest = dict(format=FORMAT, version=1, id='P'+secrets.token_hex(12), created_at=time.time(),
                                server=server.rstrip('/'), workspace=dispatch.workspace_brief(ws),
                                repositories=repositories, sort=order, tasks=tasks)
                manifest['fingerprint'] = fingerprint(manifest)
                c.execute('INSERT INTO dispatch_plans VALUES(?,?,?)', (manifest['id'], manifest['created_at'], json.dumps(manifest, ensure_ascii=False)))
                store.event(c, 'dispatch_plan_export', None, None, actor, manifest['id'])
                return dict(ok=True, manifest=manifest)
        finally:
            c.close()


def selected(c, dispatch, device, plan):
    ws = dispatch.workspace_of(c, device)
    if not ws or ws['ws_id'] != plan['workspace']['ws_id']:
        raise ValueError('分派清单不属于本机工作区')
    return [r for r in plan['tasks'] if r['user_id'] == device['user_id'] and r['device_id'] in ('', device['device_id'])]


def outstanding_offline(c, device_id, plan_id=None):
    query = ("SELECT 1 FROM dispatch_plan_applications a JOIN offline_leases o ON o.job_id=a.job_id"
             " JOIN leases l ON l.lease_id=o.lease_id WHERE a.device_id=? AND a.mode='offline'"
             " AND l.status IN ('open','submitted')")
    return c.execute(query + (' AND a.plan_id=?' if plan_id else ''),
                     (device_id,plan_id) if plan_id else (device_id,)).fetchone() is not None


def client(store, dispatch, body):
    from .store import identity
    with store._write_lock:
        c = store.conn()
        try:
            with dispatch._Tx(c):
                device = identity(c, dispatch, body, update_parts=False)
                action = body.get('action', 'inbox')
                if action == 'inbox':
                    items = []
                    latest = set()
                    for row in c.execute('SELECT manifest FROM dispatch_plans ORDER BY created_at DESC LIMIT 100'):
                        plan = json.loads(row[0])
                        ws_id=plan['workspace']['ws_id']
                        if ws_id in latest:continue
                        latest.add(ws_id)
                        try:
                            mine = selected(c, dispatch, device, plan)
                        except ValueError:
                            continue
                        if mine:
                            items.append(dict(id=plan['id'], created_at=plan['created_at'], workspace=plan['workspace'], tasks=len(mine), fingerprint=plan['fingerprint']))
                    return dict(ok=True, items=items)
                plan = load(c, body.get('id'))
                if action == 'clear':
                    if outstanding_offline(c,device['device_id'],plan['id']):
                        raise ValueError('离线任务仍保留占用；请在中心取消离线分配，或完成后补传')
                    c.execute('DELETE FROM device_dispatch_plans WHERE device_id=? AND plan_id=?', (device['device_id'],plan['id']))
                    c.execute('DELETE FROM dispatch_plan_applications WHERE plan_id=? AND device_id=?', (plan['id'],device['device_id']))
                    return dict(ok=True)
                mine = selected(c, dispatch, device, plan)
                if not mine:
                    raise ValueError('此分派没有分给本机的任务；设备指定不能在其他机器应用')
                if action == 'get':
                    return dict(ok=True, manifest={**plan, 'recipient':device['user_id'], 'tasks':[r for r in plan['tasks'] if r['user_id']==device['user_id']]})
                if action != 'apply' or body.get('mode') not in ('online', 'offline'):
                    raise ValueError('请选择联网执行或离线执行')
                if body.get('fingerprint') != plan['fingerprint']:
                    raise ValueError('分派清单内容已变化，请重新导入')
                prior = c.execute('SELECT * FROM dispatch_plan_applications WHERE plan_id=? AND device_id=?', (plan['id'], device['device_id'])).fetchone()
                if prior:
                    if prior['mode'] != body['mode']:
                        raise ValueError('已经应用另一执行方式，请先归还或取消原任务')
                    if prior['mode'] == 'offline':
                        from .offline import manifest
                        return dict(ok=True, mode='offline', manifest=manifest(store, prior['job_id']), replay=True)
                if outstanding_offline(c,device['device_id']):
                    raise ValueError('本机仍持有离线分派，请先补传或在中心取消原分配')
                current = {(r['repo'], r['package'], r['part']): r for r in rows(c, dispatch, ws=dispatch.ws_get(c, plan['workspace']['ws_id']))}
                usable = []
                from scheduling.policies import repo_order
                allowed = repo_order(c, device, ws=dispatch.workspace_of(c, device))
                for task in mine:
                    repo_state = c.execute('SELECT enabled FROM repos WHERE repo=?',(task['repo'],)).fetchone()
                    if not repo_state or not repo_state[0]:raise ValueError('仓库已经停用：'+task['repo'])
                    now = current.get((task['repo'], task['package'], task['part']))
                    if not now:
                        package = c.execute('SELECT * FROM packages WHERE repo=? AND id=?', (task['repo'], task['package'])).fetchone()
                        if package and task['part'] not in dispatch.missing_parts(dict(package)):
                            continue
                        raise ValueError('派工已取消或改派，请重新导入：'+task['package'])
                    if task['repo'] not in allowed or any(now[k] != task[k] for k in ('user_id', 'device_id', 'sha256', 'size', 'repo_type', 'annotation')):
                        raise ValueError('派工归属、源分片或画师配置已变化：'+task['package'])
                    usable.append(task)
                if not usable:
                    raise ValueError('此分派的任务已经完成')
                binding = c.execute('SELECT plan_id FROM device_dispatch_plans WHERE device_id=?',(device['device_id'],)).fetchone()
                replay_online = body['mode']=='online' and binding and binding[0]==plan['id']
                if c.execute("SELECT 1 FROM leases WHERE device_id=? AND status IN ('open','submitted')", (device['device_id'],)).fetchone() and not replay_online:
                    raise ValueError('本机仍持有任务，请先完成或主动释放，再应用新分派')
                if body['mode'] == 'online':
                    modes = {r['part'] for r in usable}
                    c.execute('UPDATE devices SET parts=? WHERE device_id=?', ('both' if len(modes)==2 else next(iter(modes)),device['device_id']))
                    c.execute('INSERT OR REPLACE INTO device_dispatch_plans VALUES(?,?)', (device['device_id'], plan['id']))
                    c.execute('INSERT OR IGNORE INTO dispatch_plan_applications VALUES(?,?,?,NULL)', (plan['id'], device['device_id'], 'online'))
                    return dict(ok=True, mode='online', tasks=usable)
        finally:
            c.close()
    from .plan_offline import apply
    return apply(store, dispatch, body, plan, device, usable)


def scope(c, device):
    row = c.execute('SELECT plan_id FROM device_dispatch_plans WHERE device_id=?', (device['device_id'],)).fetchone()
    if not row:
        return None
    plan = load(c, row[0])
    return {(r['repo'], r['package'], r['part']) for r in plan['tasks']
            if r['user_id'] == device['user_id'] and r['device_id'] in ('', device['device_id'])}


def clear_device(c, device_id):
    c.execute('DELETE FROM device_dispatch_plans WHERE device_id=?', (device_id,))
    c.execute("DELETE FROM dispatch_plan_applications WHERE device_id=? AND mode='online'", (device_id,))
