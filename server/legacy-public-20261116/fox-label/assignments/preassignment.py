"""按用户预派分项；设备上线后仍通过原领取事务取得唯一租约。"""
import json
import time

from . import annotation
from .store import selection

TAG_FIELDS = ('tag_backend', 'tag_precision', 'tag_batch')
NL_FIELDS = ('inference_source', 'nl_engine', 'nl_model', 'nl_precision', 'concurrency')


def user_workspace(c, dispatch, user):
    account = c.execute('SELECT status FROM users WHERE user_id=?', (user,)).fetchone()
    if not account or account['status'] != 'active':
        raise ValueError('用户不存在或已停用：' + str(user))
    ws = dispatch.workspace_of(c, dict(user_id=user, device_id='', workspace_id=None))
    if not ws or ws.get('missing'):
        raise ValueError('请先为用户分配工作区：' + str(user))
    return ws


def user_allowed(c, dispatch, user, repo):
    try:
        user_workspace(c, dispatch, user)
    except ValueError:
        return False
    from scheduling.allocation import allowed
    return allowed(c, dispatch, dict(user_id=user, device_id='', workspace_id=None), repo)


def requirements(value, mode):
    if not isinstance(value, dict):
        raise ValueError('模型配置必须为对象')
    if not value:
        return {}
    from managed.contracts import config
    merged = config({**value, 'mode': mode})
    fields = (TAG_FIELDS if mode != 'nl' else ()) + (NL_FIELDS if mode != 'tag' else ())
    # 留空模型和自动并发不限制设备；本机路径、凭据、显卡编号不随预派单设置。
    return {key: merged[key] for key in fields if key in value and merged[key] not in ('', 0)}


def mismatch(target, current, part, allow_tuning=None):
    expected = json.loads(target.get('config') or '{}')
    fields = TAG_FIELDS if part == 'tag' else NL_FIELDS
    different = [key for key in fields if key not in ('concurrency', 'tag_batch')
                 and key in expected and current.get(key) != expected[key]
                 and not (allow_tuning or {}).get(key,False)]
    labels = dict(tag_backend='TAG 模型', tag_precision='TAG 精度', tag_batch='TAG 批大小',
                  inference_source='NL 推理方式', nl_engine='NL 引擎', nl_model='NL 模型',
                  nl_precision='NL 精度', concurrency='NL 并发')
    return '、'.join(labels[key] for key in different)


def assign(c, dispatch, body, actor):
    users = body.get('users')
    if (not isinstance(users, list) or not 1 <= len(users) <= 100
            or any(not isinstance(user, str) or not user for user in users) or len(set(users)) != len(users)):
        raise ValueError('请选择 1～100 个用户')
    items = selection(body)
    now = time.time()
    with dispatch._Tx(c):
        ws = dispatch.ws_get(c, body.get('ws_id'))
        from .templates import defaults, remember
        body = defaults(c, ws['ws_id'], body)
        mode, start = body.get('mode', 'both'), body.get('start', True)
        if mode not in ('tag', 'nl', 'both') or type(start) is not bool:
            raise ValueError('打标内容或自动开始选项不正确')
        config = requirements(body.get('config', {}), mode)
        source = body['config_source']
        if source not in ('inherit', 'specified') or source == 'inherit' and config:
            raise ValueError('执行配置来源与模型配置不匹配')
        options = annotation.options(body['annotation']) if 'annotation' in body else None
        assign_workspace = body.get('assign_workspace', False)
        if type(assign_workspace) is not bool:
            raise ValueError('工作区归属选项不正确')
        for user in users:
            if assign_workspace:
                account = c.execute('SELECT status FROM users WHERE user_id=?', (user,)).fetchone()
                owner = dispatch.workspace_of(c, dict(user_id=user, device_id='', workspace_id=None))
                if account and account['status'] == 'active' and owner is None:
                    dispatch.ws_assign(c, ws['ws_id'], user_id=user)
            if user_workspace(c, dispatch, user)['ws_id'] != ws['ws_id']:
                raise ValueError('用户不属于当前工作区：' + user)
        grant = body.get('grant_repos', False)
        if type(grant) is not bool:
            raise ValueError('仓库许可选项不正确')
        if grant:
            if any(repo not in ws['repos'] for repo, _ in items):
                raise ValueError('分片不属于当前工作区')
            c.executemany('INSERT OR IGNORE INTO repo_users VALUES(?,?,?)',
                          [(ws['ws_id'], repo, user) for repo in {repo for repo, _ in items} for user in users])
        loads = {user: c.execute("SELECT COUNT(*) FROM part_assignments WHERE target_type='user' AND target_id=?",
                                 (user,)).fetchone()[0] for user in users}
        from scheduling.policies import repo_order
        permitted = {user: set(repo_order(c, dict(user_id=user, device_id='', workspace_id=None), ws)) for user in users}
        from scheduling.leases import held
        from delivery.conflicts import blocked
        busy = held(c)
        for key, parts in blocked(c).items():
            busy.setdefault(key, set()).update(parts)
        assigned, reserved = [], 0
        for repo, pkg in items:
            package = dispatch._row(c, 'SELECT p.*,r.enabled FROM packages p JOIN repos r ON r.repo=p.repo WHERE p.repo=? AND p.id=?', (repo, pkg))
            if not package or not package['enabled'] or repo not in ws['repos'] or package['status'] not in ('available', 'claimed'):
                raise dispatch.LeaseError('分片不能预派：' + pkg)
            parts = [part for part in dispatch.missing_parts(package) if mode == 'both' or mode == part]
            if not parts or any(part in busy.get((repo, pkg), ()) for part in parts):
                raise dispatch.LeaseError('所选分项已完成、占用或待冲突处理：' + pkg)
            old = {row['part']: dict(row) for row in c.execute('SELECT * FROM part_assignments WHERE repo=? AND pkg=?', (repo, pkg))}
            whole = c.execute('SELECT user_id FROM package_targets WHERE repo=? AND pkg=?', (repo, pkg)).fetchone()
            queue = c.execute('SELECT device_id FROM package_queue WHERE repo=? AND pkg=?', (repo, pkg)).fetchone()
            if queue or any(part in old and (old[part]['target_type'] != 'user' or old[part]['target_id'] not in users) for part in parts):
                raise dispatch.LeaseError('分项已有其他指定，请先取消原分配：' + pkg)
            candidates = [user for user in users if repo in permitted[user] and (not whole or whole['user_id'] == user)]
            if not candidates:
                raise dispatch.LeaseError('所选用户没有这个仓库的许可：' + repo)
            owner = min(candidates, key=lambda user: (loads[user], user))
            for part in parts:
                user = old[part]['target_id'] if part in old else owner
                c.execute('INSERT INTO part_assignments(repo,pkg,part,target_type,target_id,actor,updated_at,config)'
                          " VALUES(?,?,?,'user',?,?,?,?) ON CONFLICT(repo,pkg,part) DO UPDATE SET"
                          ' actor=excluded.actor,updated_at=excluded.updated_at,config=excluded.config',
                          (repo, pkg, part, user, actor, now, json.dumps(config)))
                if options is not None:
                    annotation.save(c, package, part, options)
                if part not in old:
                    loads[user] += 1
                    reserved += 1
                assigned.append(dict(repo=repo, id=pkg, part=part, user_id=user))
        last = remember(c, ws['ws_id'], mode, source, config, options)
        return dict(ok=True, assigned=assigned, reserved=reserved, already_reserved=len(assigned)-reserved,
                    preassignment_last=last)


def cancel(c, dispatch, body):
    items = selection(body)
    mode = body.get('mode', 'both')
    if mode not in ('tag', 'nl', 'both'):
        raise ValueError('请选择要取消的分项')
    ws = dispatch.ws_get(c, body.get('ws_id'))
    from scheduling.leases import held
    with dispatch._Tx(c):
        busy, count = held(c), 0
        for repo, pkg in items:
            if repo not in ws['repos']:
                raise ValueError('分片不属于当前工作区')
            parts = dispatch.PARTS if mode == 'both' else [mode]
            if any(part in busy.get((repo, pkg), ()) for part in parts):
                raise dispatch.LeaseError('分项正在执行或预锁，不能取消预派单：' + pkg)
            for part in parts:
                count += c.execute("DELETE FROM part_assignments WHERE repo=? AND pkg=? AND part=? AND target_type='user'", (repo, pkg, part)).rowcount
        return dict(ok=True, canceled=count)


def states(c, dispatch, assignments):
    """管理员查看未领取分项；批量读取设备与会话，不逐项查会话。"""
    from managed.store import ONLINE_SECONDS
    now = time.time()
    devices = [dict(row) for row in c.execute('SELECT d.*,s.last_seen managed_seen,s.released,s.config managed_config,'
        'p.desired,p.pause_reason FROM devices d LEFT JOIN managed_devices m USING(device_id)'
        ' LEFT JOIN managed_sessions s USING(session_id) LEFT JOIN managed_policy p USING(device_id)')]
    from scheduling.policies import repo_order
    online_users = {}
    for device in devices:
        if (device['status'] in ('blocked', 'archived') or device['device_id'].startswith('offline-')
                or not device['managed_seen'] or device['released'] or now-device['managed_seen'] > ONLINE_SECONDS):
            continue
        device['configuration'] = json.loads(device['managed_config'] or '{}')
        device['permitted'] = set(repo_order(c, device, dispatch.workspace_of(c, device)))
        online_users.setdefault(device['user_id'], []).append(device)
    result = {}
    for target in assignments:
        if target['target_type'] != 'user':
            continue
        key = target['repo'], target['pkg'], target['part']
        online = [d for d in online_users.get(target['target_id'], ())
                  if target['part'] in (dispatch._wanted_parts(d) or dispatch.PARTS)]
        if not online:
            message = '待上线领取'
        elif all(d['desired'] != 'running' for d in online):
            message = '成员已暂停，等待恢复'
        else:
            ready = [d for d in online if d['desired'] == 'running'
                     and not mismatch(target, d['configuration'], target['part'])
                     and target['repo'] in d['permitted']]
            message = '待设备领取' if ready else '待符合模型配置及仓库许可的设备'
        result[key] = message
    return result
