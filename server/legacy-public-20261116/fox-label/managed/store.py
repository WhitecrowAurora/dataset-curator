"""托管会话和顺序指令与原调度数据库共用事务。"""
import json
import time
from contextlib import contextmanager

from .contracts import ManagedError, TERMINAL, config, identifier, public, report
from . import automation

ONLINE_SECONDS = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_sessions (
 session_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, user_id TEXT NOT NULL,
 secret_hash TEXT NOT NULL, created_at REAL NOT NULL, last_seen REAL NOT NULL,
 released INTEGER NOT NULL DEFAULT 0, last_seq INTEGER NOT NULL DEFAULT 0,
 next_seq INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT '{}',
 config TEXT NOT NULL DEFAULT '{}', capabilities TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS managed_devices (
 device_id TEXT PRIMARY KEY, session_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS managed_commands (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
 action TEXT NOT NULL, config TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 error TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL, created_at REAL NOT NULL,
 updated_at REAL NOT NULL, UNIQUE(session_id,seq)
);
CREATE INDEX IF NOT EXISTS idx_managed_commands ON managed_commands(session_id,seq);
"""


def ensure_schema(c):
    c.executescript(SCHEMA)
    from .recovery import SCHEMA as recovery_schema
    c.executescript(recovery_schema)
    from .limits import SCHEMA as limits_schema
    from scheduling.pending_results import SCHEMA as pending_schema
    c.executescript(limits_schema + pending_schema)
    c.execute("CREATE TABLE IF NOT EXISTS managed_policy (device_id TEXT PRIMARY KEY,desired TEXT NOT NULL)")
    if 'automation_error' not in {r['name'] for r in c.execute('PRAGMA table_info(managed_sessions)')}:
        c.execute("ALTER TABLE managed_sessions ADD COLUMN automation_error TEXT NOT NULL DEFAULT ''")
    for field in ('pause_reason',):
        if field not in {r['name'] for r in c.execute('PRAGMA table_info(managed_policy)')}:
            c.execute(f"ALTER TABLE managed_policy ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
            # 旧失败暂停可在新会话恢复；显式管理员暂停仍保留。
            c.execute("UPDATE managed_policy SET pause_reason=CASE WHEN EXISTS ("
                      " SELECT 1 FROM managed_commands c JOIN managed_sessions s USING(session_id)"
                      " WHERE s.device_id=managed_policy.device_id AND c.action='start'"
                      " AND c.status IN ('failed','canceled') AND c.created_at=("
                      " SELECT MAX(c2.created_at) FROM managed_commands c2 JOIN managed_sessions s2 USING(session_id)"
                      " WHERE s2.device_id=managed_policy.device_id AND c2.action IN ('start','stop')))"
                      " THEN 'failure' ELSE 'manual' END WHERE desired='paused'")
            c.execute("UPDATE managed_policy SET desired='running',pause_reason='' WHERE pause_reason='failure' AND EXISTS ("
                      " SELECT 1 FROM managed_commands c JOIN managed_sessions s USING(session_id)"
                      " WHERE s.device_id=managed_policy.device_id AND c.action='start' AND c.status='failed'"
                      " AND c.error='设备已在执行，请先停止' AND c.created_at=("
                      " SELECT MAX(c2.created_at) FROM managed_commands c2 JOIN managed_sessions s2 USING(session_id)"
                      " WHERE s2.device_id=managed_policy.device_id AND c2.action IN ('start','stop')))")


    fields = {r['name'] for r in c.execute('PRAGMA table_info(managed_policy)')}
    for field, definition in [('changed_at', 'REAL NOT NULL DEFAULT 0'), ('changed_by', "TEXT NOT NULL DEFAULT ''")]:
        if field not in fields:
            c.execute(f'ALTER TABLE managed_policy ADD COLUMN {field} {definition}')


@contextmanager
def transaction(store):
    with store._write_lock:
        c = store.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def current(c, device_id):
    return c.execute("SELECT s.* FROM managed_devices d JOIN managed_sessions s"
                     " ON s.session_id=d.session_id WHERE d.device_id=?", (device_id,)).fetchone()


def retire(c, session, now):
    c.execute("UPDATE managed_sessions SET released=1 WHERE session_id=?", (session,))
    c.execute("UPDATE managed_commands SET status='canceled',error='托管会话已退出',updated_at=?"
              " WHERE session_id=? AND status IN ('queued','accepted')", (now, session))
    c.execute("UPDATE history_checks SET status='canceled',updated_at=? WHERE session_id=? AND status='pending'", (now, session))


def poll(store, dispatch, body):
    session, seq, state, caps, results = report(body)
    cfg = config(body.get("config", {}))
    from .recovery import request, apply
    requested, request_id = request(body)
    if requested and (body.get('released', False) or state.get('history_only')):
        raise ManagedError('退出或历史核对不能请求开始接单')
    user = identifier(body.get("user_id"), "user_id")
    device_id = identifier(body.get("device_id"), "device_id")
    secret = body.get("secret")
    if not isinstance(secret, str) or not secret or len(secret) > dispatch.MAX_SECRET:
        raise dispatch.AuthError("设备凭据格式不正确")
    secret_hash = dispatch.hash_secret(secret)
    now = time.time()
    response = {"protocol_version": 1, "session_id": session, "commands": []}
    with transaction(store) as c:
        dev = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if not dev or dev["user_id"] != user:
            raise dispatch.AuthError("设备与用户不匹配，请先连接调度中心")
        dispatch._require_device(c, user, device_id, secret)
        account = c.execute("SELECT status,secret FROM users WHERE user_id=?", (user,)).fetchone()
        if not account or account["status"] != "active" or dev["status"] in ("archived", "blocked"):
            raise dispatch.AuthError("设备或用户已停用")
        old = c.execute("SELECT * FROM managed_sessions WHERE session_id=?", (session,)).fetchone()
        active = current(c, device_id)
        bound = active and active["user_id"] == user and active["secret_hash"] == secret_hash
        if not bound and account["secret"] != secret_hash:
            raise dispatch.AuthError("托管凭据不匹配，请先在客户端重新连接中心")
        if old:
            if old["device_id"] != device_id or old["user_id"] != user or old["secret_hash"] != secret_hash:
                raise dispatch.AuthError("托管会话身份不匹配")
            if old["released"] or not active or active["session_id"] != session:
                response["released"] = True
                return response
            if seq < old["last_seq"] or seq >= old["next_seq"]:
                raise ManagedError("托管确认序号不正确", 409)
        else:
            if seq != 0:
                raise ManagedError("新托管会话必须从序号 0 开始", 409)
            c.execute("INSERT INTO managed_sessions(session_id,device_id,user_id,secret_hash,created_at,last_seen)"
                      " VALUES(?,?,?,?,?,?)", (session, device_id, user, secret_hash, now, now))
            if not body.get("released", False):
                if active:
                    retire(c, active["session_id"], now)
                c.execute("INSERT INTO managed_devices VALUES(?,?) ON CONFLICT(device_id)"
                          " DO UPDATE SET session_id=excluded.session_id", (device_id, session))
                if state.get('history_only') is not True:
                    c.execute("INSERT INTO managed_policy(device_id,desired,pause_reason) VALUES(?,'running','')"
                              " ON CONFLICT(device_id) DO UPDATE SET desired='running',pause_reason=''"
                              " WHERE managed_policy.pause_reason='failure'", (device_id,))
                    c.execute('UPDATE devices SET queue_only=0 WHERE device_id=?', (device_id,))
        for result in results:
            if (not isinstance(result, dict) or not isinstance(result.get("id"), str)
                    or type(result.get("seq")) is not int or result["seq"] > seq
                    or result.get("status") not in ("accepted", *TERMINAL)
                    or not isinstance(result.get("error", ""), str)):
                raise ManagedError("托管指令结果格式不正确")
            cmd = c.execute("SELECT * FROM managed_commands WHERE id=? AND session_id=? AND seq=?",
                            (result["id"], session, result["seq"])).fetchone()
            if not cmd:
                raise ManagedError("托管结果对应的指令不存在", 409)
            if cmd["status"] not in TERMINAL:
                c.execute("UPDATE managed_commands SET status=?,error=?,updated_at=? WHERE id=?",
                          (result["status"], public(result.get("error", ""))[:4096], now, result["id"]))
                if cmd["action"] == "check" and result["status"] in ("failed", "canceled"):
                    c.execute("UPDATE history_checks SET status=?,updated_at=? WHERE check_id=? AND status='pending'",
                              (result["status"], now, cmd["id"]))
                if cmd["action"] == "start" and result["status"] in ("failed", "canceled"):
                    if result.get('error') != '设备已在执行，请先停止':
                        later = c.execute("SELECT 1 FROM managed_commands WHERE session_id=? AND seq>? AND action IN ('start','stop')", (session, cmd['seq'])).fetchone()
                        if not later:
                            automation.intent(c, device_id, "stop", reason="failure")
        c.execute("UPDATE managed_sessions SET last_seen=?,last_seq=?,state=?,config=?,capabilities=?"
                  " WHERE session_id=?", (now, seq, dump(state), dump(cfg), dump(caps), session))
        if old:
            from .tuning import confirmed
            confirmed(c, session, seq, json.loads(old['config']), cfg)
        if "inventory" in body:
            from history.store import inventory
            inventory(c, device_id, body["inventory"], now)
        c.execute("UPDATE managed_commands SET status='accepted',updated_at=?"
                  " WHERE session_id=? AND seq<=? AND status='queued'", (now, session, seq))
        if body.get("released", False):
            retire(c, session, now)
            response["released"] = True
        elif state.get('history_only') is not True:
            preference = body.get("parts_pref", "both")
            if preference not in dispatch.PARTS_CHOICES:
                raise ManagedError("打标倾向格式不正确")
            mode = cfg["mode"]
            from scheduling.leases import release_offline
            release_offline(c)
            effective = mode if mode in ('tag','nl') else preference if preference in ('tag','nl') else 'both'
            c.execute("UPDATE devices SET parts=?,parts_pref=? WHERE device_id=?", (effective, preference, device_id))
            if dev['parts'] != effective:
                from scheduling.transitions import reconcile
                reconcile(c, dispatch)
            c.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (now, device_id))
            from scheduling.lifetime import renew
            renew(c, device_id, now)
            c.execute("UPDATE users SET last_seen=? WHERE user_id=?", (now, user))
            device = dict(c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone())
            from .limits import configure as configure_limit, view as limit_view
            configure_limit(c,device_id,state.get('execution_limit'))
            if requested:
                applied = apply(c, store, session, device_id, requested, request_id, seq, now, user)
                if applied and state.get('status') == 'failed':
                    state = {**state, 'status': 'managed_idle'}
                response['intent_applied'] = requested
                response['intent_id'] = request_id
            from scheduling.onboarding import join
            from scheduling.control import state as dispatch_state, running as dispatch_running, cancel_starts
            if not dispatch_running(c):
                cancel_starts(c, now)
            from .stopping import reconcile as reconcile_stop
            reconcile_stop(c, session, device_id, state, now)
            policy = c.execute('SELECT * FROM managed_policy WHERE device_id=?', (device_id,)).fetchone()
            workspace = (join(c, dispatch, device) if policy and policy['desired'] == 'running'
                         else dispatch.workspace_of(c, device))
            response['waiting_reason'] = automation.arrange(c, dispatch, session, device, state, cfg, caps, now)
            policy = c.execute('SELECT * FROM managed_policy WHERE device_id=?', (device_id,)).fetchone()
            from .parallel import sources, current_package
            from scheduling.pending_results import view as pending_view
            current_session=current(c,device_id)
            response['automation'] = {**(dict(policy) if policy else {}),
                'dispatch': dispatch_state(c),
                'limit': limit_view(c, device_id),
                'pending_results': pending_view(c,device_id),
                'parallel': sources(c,current_session,device,current_package(c,device_id)),
                'workspace': {'ws_id': workspace['ws_id'], 'name': workspace['name']} if workspace else None}
            response["commands"] = [{"id": r["id"], "seq": r["seq"], "session_id": session,
                                     "action": r["action"], "config": json.loads(r["config"]),
                                     "canceled": r['status'] == 'canceled'}
                for r in c.execute("SELECT * FROM managed_commands WHERE session_id=? AND seq>?"
                                   " ORDER BY seq LIMIT 16", (session, seq))]
        if not body.get('released', False):
            response['history_reports'] = [dict(check_id=r['check_id'],candidate_id=r['candidate_id'],
                                                status=r['status'],report=json.loads(r['report']))
                                           for r in c.execute('SELECT * FROM history_checks WHERE device_id=? AND user_id=?'
                                                              ' ORDER BY updated_at DESC LIMIT 64', (device_id, user))]
    return response


def command_view(row):
    return {k: row[k] for k in ("id", "seq", "action", "status", "error", "actor", "created_at", "updated_at")}


def view(store, device_id=None):
    c = store.conn()
    try:
        sql = ("SELECT s.*,d.status device_status,d.user_id device_user,u.status user_status FROM managed_devices m"
               " JOIN managed_sessions s ON s.session_id=m.session_id"
               " JOIN devices d ON d.device_id=m.device_id JOIN users u ON u.user_id=s.user_id")
        rows = c.execute(sql + (" WHERE m.device_id=?" if device_id else " ORDER BY s.last_seen DESC"),
                         (device_id,) if device_id else ()).fetchall()
        devices = []
        now = time.time()
        for row in rows:
            enabled = (row["device_user"] == row["user_id"]
                       and row["device_status"] not in ("archived", "blocked") and row["user_status"] == "active")
            online = not row["released"] and now - row["last_seen"] <= ONLINE_SECONDS and enabled
            item = {k: row[k] for k in ("device_id", "user_id", "session_id", "last_seen", "last_seq")}
            item.update(online=online, released=bool(row["released"]), enabled=enabled,
                        last_seen_ago=round(now - row["last_seen"], 1))
            policy = c.execute("SELECT * FROM managed_policy WHERE device_id=?", (row['device_id'],)).fetchone()
            item.update(**(dict(policy) if policy else {'desired': 'paused'}), automation_error=row['automation_error'])
            item.update({k: json.loads(row[k]) for k in ("state", "config", "capabilities")})
            item['leases'] = [{"lease_id": lease['lease_id'], "repo": lease['repo'], "package": lease['pkg'],
                               "kind": lease['kind'], "beat_ago_s": round(now - (lease['last_beat_at'] or lease['issued_at']), 1),
                               "progress": json.loads(lease['progress_json'] or '{}')}
                              for lease in c.execute("SELECT * FROM leases WHERE device_id=? AND status='open'", (row['device_id'],))]
            from .progress import current as current_progress
            item['state']['progress'] = current_progress(item['state'], item['leases'])
            item['completed_packages'] = c.execute("SELECT COUNT(*) FROM leases WHERE device_id=? AND status IN ('done','submitted')", (row['device_id'],)).fetchone()[0]
            from .limits import view as limit_view
            from scheduling.pending_results import view as pending_view
            item['execution_limit'] = limit_view(c,row['device_id'])
            item['pending_results'] = pending_view(c,row['device_id'])
            from .parallel import sources, current_package
            import dispatch
            device=dict(c.execute('SELECT * FROM devices WHERE device_id=?',(row['device_id'],)).fetchone())
            item['parallel'] = sources(c,row,device,current_package(c,row['device_id']))
            item['waiting_reason'] = automation.reason(c, row['session_id'], dict(c.execute('SELECT * FROM devices WHERE device_id=?', (row['device_id'],)).fetchone()), item['state'])
            item["commands"] = [command_view(r) for r in c.execute(
                "SELECT * FROM managed_commands WHERE session_id=? ORDER BY seq DESC LIMIT 64", (row["session_id"],))]
            devices.append(item)
        from scheduling.control import state as dispatch_state
        return {"ok": True, "protocol_version": 1, "online_seconds": ONLINE_SECONDS,
                "devices": devices, "dispatch": dispatch_state(c)}
    finally:
        c.close()


def enqueue(store, body, actor):
    if not isinstance(body, dict):
        raise ManagedError("指令必须是对象")
    device_id = identifier(body.get("device_id"), "device_id")
    session = identifier(body.get("session_id"), "session_id")
    command_id = identifier(body.get("id"), "id")
    action, changes = body.get("action"), body.get("config", {})
    force = body.get('force_continue', False)
    if type(force) is not bool or force and action != 'start':
        raise ManagedError('强制继续只能用于开始指令')
    if action not in ("start", "stop", "check", "configure", "probe") or not isinstance(changes, dict):
        raise ManagedError("本机配置已锁定，仅支持开始、停止和历史检查")
    if action in ("start", "stop") and changes:
        raise ManagedError("本机配置已锁定，启停指令不能修改执行环境")
    if action == "check":
        required = {"check_id", "candidate_id", "repo", "package", "size", "sha256"}
        if not required <= set(changes) or set(changes)-required-{'annotation','reinject'}:
            raise ManagedError("历史检查指令格式不正确")
        if changes.get('reinject','') not in ('','missing','force') or ('reinject' in changes and not changes.get('annotation')):
            raise ManagedError('画师补注入指令缺少有效画师表配置')
    if action == "stop" and changes:
        raise ManagedError("停止指令不能包含配置")
    now = time.time()
    with transaction(store) as c:
        active = current(c, device_id)
        if not active or active["session_id"] != session or active["released"] and action != 'stop':
            raise ManagedError("设备已经退出或更换托管会话，请刷新", 409)
        replay = c.execute("SELECT * FROM managed_commands WHERE id=?", (command_id,)).fetchone()
        if replay:
            if replay["session_id"] != session or replay["action"] != action or json.loads(replay["config"]) != changes:
                raise ManagedError("指令 id 已被使用", 409)
            return {"ok": True, "replay": True, "command": command_view(replay)}
        dev = c.execute("SELECT d.status,u.status user_status FROM devices d JOIN users u ON u.user_id=d.user_id"
                        " WHERE d.device_id=? AND d.user_id=?", (device_id, active["user_id"])).fetchone()
        if not dev or dev["status"] in ("archived", "blocked") or dev["user_status"] != "active":
            raise ManagedError("设备或用户已停用", 403)
        offline = bool(active['released'] or now - active['last_seen'] > ONLINE_SECONDS)
        if offline and action != 'stop':
            raise ManagedError("设备已离线，等待重新上报后再操作", 409)
        if action == 'start':
            from scheduling.control import running
            if not running(c) and not c.execute("SELECT 1 FROM leases WHERE device_id=?"
                                                " AND kind='active' AND status='open'", (device_id,)).fetchone():
                raise ManagedError('管理员已暂停自动派单，请先启动自动派单', 409)
        if action == 'stop':
            from .stopping import cancel_starts
            cancel_starts(c, session, now)
        if force and 'force_continue' not in json.loads(active['capabilities']).get('actions', []):
            raise ManagedError('此客户端尚不支持强制继续，请升级客户端；也可先使用开始打标', 409)
        pending = c.execute("SELECT * FROM managed_commands WHERE session_id=? AND status IN ('queued','accepted')"
                            " ORDER BY seq", (session,)).fetchall()
        if len(pending) >= 64 and action != 'stop':
            raise ManagedError("待处理指令过多，请等待设备执行", 409)
        if action in ('configure', 'probe'):
            from .tuning import validate
            validate(action, changes, json.loads(active['config']), json.loads(active['capabilities']))
            if pending:
                raise ManagedError("已有待执行指令，请等待确认后再调整", 409)
        if action == "start":
            config({}, json.loads(active["config"]), json.loads(active["capabilities"]))
            if force:
                from .recovery import apply
                apply(c, store, session, device_id, 'force_start', command_id, active['last_seq'], now, actor)
        seq = active["next_seq"]
        c.execute("INSERT INTO managed_commands(id,session_id,seq,action,config,actor,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?)", (command_id, session, seq, action, dump(changes), actor, now, now))
        c.execute("UPDATE managed_sessions SET next_seq=next_seq+1 WHERE session_id=?", (session,))
        if action in ('start','stop'):
            if action == 'start':
                from .limits import restart
                restart(c, device_id)
            automation.intent(c, device_id, action, reason=f'admin:{actor}')
            c.execute("UPDATE managed_sessions SET automation_error='' WHERE session_id=?", (session,))
        store.event(c, "managed_command", None, None, actor,
                    f"device={device_id}; session={session}; seq={seq}; action={action}; id={command_id}")
        row = c.execute("SELECT * FROM managed_commands WHERE id=?", (command_id,)).fetchone()
        return {"ok": True, "command": command_view(row), "pending_reconnect": offline}
