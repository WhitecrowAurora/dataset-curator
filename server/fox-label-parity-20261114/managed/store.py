"""托管会话和顺序指令与原调度数据库共用事务。"""
import json
import time
from contextlib import contextmanager

from .contracts import ManagedError, TERMINAL, config, identifier, public, report

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


def poll(store, dispatch, body):
    session, seq, state, caps, results = report(body)
    cfg = config(body.get("config", {}))
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
        c.execute("UPDATE managed_sessions SET last_seen=?,last_seq=?,state=?,config=?,capabilities=?"
                  " WHERE session_id=?", (now, seq, dump(state), dump(cfg), dump(caps), session))
        c.execute("UPDATE managed_commands SET status='accepted',updated_at=?"
                  " WHERE session_id=? AND seq<=? AND status='queued'", (now, session, seq))
        if body.get("released", False):
            retire(c, session, now)
            response["released"] = True
        else:
            c.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (now, device_id))
            c.execute("UPDATE users SET last_seen=? WHERE user_id=?", (now, user))
            response["commands"] = [{"id": r["id"], "seq": r["seq"], "session_id": session,
                                     "action": r["action"], "config": json.loads(r["config"])}
                for r in c.execute("SELECT * FROM managed_commands WHERE session_id=? AND seq>?"
                                   " ORDER BY seq LIMIT 16", (session, seq))]
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
            item.update({k: json.loads(row[k]) for k in ("state", "config", "capabilities")})
            item["commands"] = [command_view(r) for r in c.execute(
                "SELECT * FROM managed_commands WHERE session_id=? ORDER BY seq DESC LIMIT 64", (row["session_id"],))]
            devices.append(item)
        return {"ok": True, "protocol_version": 1, "online_seconds": ONLINE_SECONDS, "devices": devices}
    finally:
        c.close()


def enqueue(store, body, actor):
    if not isinstance(body, dict):
        raise ManagedError("指令必须是对象")
    device_id = identifier(body.get("device_id"), "device_id")
    session = identifier(body.get("session_id"), "session_id")
    command_id = identifier(body.get("id"), "id")
    action, changes = body.get("action"), body.get("config", {})
    if action not in ("configure", "start", "stop") or not isinstance(changes, dict):
        raise ManagedError("仅支持配置、开始、停止")
    if action == "stop" and changes:
        raise ManagedError("停止指令不能包含配置")
    now = time.time()
    with transaction(store) as c:
        active = current(c, device_id)
        if not active or active["session_id"] != session or active["released"]:
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
        if now - active["last_seen"] > ONLINE_SECONDS:
            raise ManagedError("设备已离线，等待重新上报后再操作", 409)
        pending = c.execute("SELECT * FROM managed_commands WHERE session_id=? AND status IN ('queued','accepted')"
                            " ORDER BY seq", (session,)).fetchall()
        if len(pending) >= 64:
            raise ManagedError("待处理指令过多，请等待设备执行", 409)
        projected = json.loads(active["config"])
        for cmd in pending:
            projected.update(json.loads(cmd["config"]))
        if action != "stop":
            config(changes, projected, json.loads(active["capabilities"]))
        seq = active["next_seq"]
        c.execute("INSERT INTO managed_commands(id,session_id,seq,action,config,actor,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?)", (command_id, session, seq, action, dump(changes), actor, now, now))
        c.execute("UPDATE managed_sessions SET next_seq=next_seq+1 WHERE session_id=?", (session,))
        store.event(c, "managed_command", None, None, actor,
                    f"device={device_id}; session={session}; seq={seq}; action={action}; id={command_id}")
        row = c.execute("SELECT * FROM managed_commands WHERE id=?", (command_id,)).fetchone()
        return {"ok": True, "command": command_view(row)}
