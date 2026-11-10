"""fox-label v3 调度层：派活、租约、心跳。

和 v2 的区别
------------
v2：客户端自己 claim，抢不到就 409。
v3：**服务端派**。客户端只说"我是谁、我在干什么、干到哪了"，服务端决定给谁什么。
    一个包同一时刻只可能有一个 open 租约（active 或 prelock），不会重复派发。

三条硬规则
----------
1. 每设备最多 1 个 active + 1 个 prelock（同时只处理一片）。
2. 一切占用都有 TTL，靠心跳续约；心跳断 → 自动回收，不依赖客户端优雅退出。
3. 提交/完成必须带 lease_id，超时被回收后的旧设备提交一律拒绝（栅栏）。

设计约定
--------
* 服务端只判"派出去/收回来"，**不判产物是否完整**——一片 6253 张打没打满是
  打标器（tagger verify / 交付环节）的事，这里只存客户端自报的 progress。
* 时间统一用 epoch 秒参与比较（ISO 字符串只用于审计展示）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import time

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    secret     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    last_seen  REAL
);

CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    first_seen  REAL NOT NULL,
    last_seen   REAL,
    client_ver  TEXT,
    speed       REAL NOT NULL DEFAULT 0,      -- 张/秒（EWMA）
    tier        TEXT NOT NULL DEFAULT 'unknown',  -- fast|mid|slow|unknown
    status      TEXT NOT NULL DEFAULT 'online',
    local_pkgs  TEXT NOT NULL DEFAULT '[]'    -- 该设备本地已有产物的包（"repo/id"）
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id         TEXT PRIMARY KEY,
    repo             TEXT NOT NULL,
    pkg              TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    device_id        TEXT NOT NULL,
    kind             TEXT NOT NULL,           -- active | prelock
    attempt          INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'open',  -- open|done|failed|expired|released
    issued_at        REAL NOT NULL,
    expires_at       REAL NOT NULL,
    last_beat_at     REAL,
    last_progress_at REAL,
    progress_json    TEXT,
    closed_reason    TEXT
);

CREATE INDEX IF NOT EXISTS idx_lease_open  ON leases(status, repo, pkg);
-- 并发兜底：同一包在数据库层面就不可能有第二个 open 租约
CREATE UNIQUE INDEX IF NOT EXISTS idx_lease_one_open ON leases(repo, pkg) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_lease_dev   ON leases(device_id, status);
"""


MAX_ID = 64          # user_id / device_id / lease_id 长度上限
MAX_SECRET = 256


class _Tx:
    """显式 BEGIN IMMEDIATE 事务。

    作用有二：
      * 崩溃/异常时整段回滚，不会留下"包已派出去但租约没落库"的半截状态；
      * 多进程部署时由 SQLite 自己串行化写入，不再依赖进程内的锁。
    """

    def __init__(self, c: sqlite3.Connection):
        self.c = c

    def __enter__(self) -> sqlite3.Connection:
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.c.execute("COMMIT")
        else:
            try:
                self.c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        return False


class AuthError(Exception):
    pass


def _sid(value, name: str) -> str:
    """严格校验标识类字段：必须是字符串、去空白后非空、不在长度上限外、无控制字符。

    这些字段会进日志与审计，放任它们等于允许注入换行伪造审计行。
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise AuthError(f"{name} 必须是字符串")
    s = value.strip()
    if not s:
        raise AuthError(f"缺少 {name}")
    if len(s) > MAX_ID:
        raise AuthError(f"{name} 过长（上限 {MAX_ID} 字符）")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in s):
        raise AuthError(f"{name} 含控制字符")
    return s


def _opt_str(value, name: str, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str):
        raise AuthError(f"{name} 必须是字符串")
    if len(value) > limit:
        raise AuthError(f"{name} 过长（上限 {limit} 字符）")
    if any(ord(ch) < 32 and ch not in "\t" for ch in value):
        raise AuthError(f"{name} 含控制字符")
    return value


class LeaseError(Exception):
    pass


def params() -> dict:
    """全部可调参数；默认值按 120s 心跳口径。"""
    def envf(name: str, default: str) -> float:
        try:
            return float(os.environ.get(name, default))
        except ValueError:
            return float(default)

    return {
        "beat": envf("FOXLABEL_V3_BEAT", "120"),
        "prelock_ttl": envf("FOXLABEL_V3_PRELOCK_TTL", "240"),
        "active_ttl": envf("FOXLABEL_V3_ACTIVE_TTL", "360"),
        "quota_min": envf("FOXLABEL_V3_QUOTA_MIN", "5"),
        "quota_pct": envf("FOXLABEL_V3_QUOTA_PCT", "0.1"),
        "fast_eta_h": envf("FOXLABEL_V3_FAST_ETA_H", "12"),
        "stall_warn": envf("FOXLABEL_V3_STALL_WARN", "21600"),
        "stall_kill": envf("FOXLABEL_V3_STALL_KILL", "86400"),
        "fail_cooldown": envf("FOXLABEL_V3_FAIL_COOLDOWN", "3600"),
        # 单租约绝对寿命上限：慢设备一片可能要 30+ 小时，所以给到 7 天；
        # 目的是挡住"时钟回拨/手工改库"造成的永久占用，而不是催活。
        "lease_max_h": envf("FOXLABEL_V3_LEASE_MAX_H", "168"),
    }


def hash_secret(raw: str) -> str:
    """secret 只存 sha256，和 flk 密钥一样的处理方式：库被读走也拿不到可用凭据。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _looks_hashed(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def ensure_schema(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA_V3)
    # 迁移：早期把 secret 明文存库，这里就地换成哈希（幂等）
    for row in c.execute("SELECT user_id, secret FROM users").fetchall():
        if row["secret"] and not _looks_hashed(row["secret"]):
            c.execute("UPDATE users SET secret=? WHERE user_id=?",
                      (hash_secret(row["secret"]), row["user_id"]))


# ------------------------------------------------------------------ 工具

def _row(c: sqlite3.Connection, sql: str, args=()) -> dict | None:
    r = c.execute(sql, args).fetchone()
    return dict(r) if r else None


def _rows(c: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def _lease_view(c: sqlite3.Connection, lease: dict) -> dict:
    """给客户端看的租约视图：带上下载所需的元数据。"""
    pkg = _row(c, "SELECT p.*, r.repo_type FROM packages p JOIN repos r ON r.repo=p.repo"
                  " WHERE p.repo=? AND p.id=?", (lease["repo"], lease["pkg"])) or {}
    return {
        "lease_id": lease["lease_id"],
        "kind": lease["kind"],
        "repo": lease["repo"],
        "package": lease["pkg"],
        "size": pkg.get("size"),
        "sha256": pkg.get("sha256"),
        "repo_type": pkg.get("repo_type", "dataset"),
        "attempt": lease["attempt"],
        "expires_in": max(0, int(lease["expires_at"] - time.time())),
        "progress": json.loads(lease["progress_json"] or "{}"),
    }


def clean_progress(raw: dict) -> dict:
    """只保留"有限数 / 短字符串"，挡住 NaN、Infinity、负数、嵌套对象。

    NaN/Infinity 不是合法 JSON，原样入库后再回吐给客户端会让严格解析器崩掉；
    负数或超大值会污染速率统计与分档。
    """
    out: dict = {}
    for key, val in list(raw.items())[:20]:
        if not isinstance(key, str) or len(key) > 32:
            continue
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)):
            if isinstance(val, float) and not math.isfinite(val):
                continue
            if val < 0 or val > 1e12:
                continue
            out[key] = val
        elif isinstance(val, str) and len(val) <= 64:
            out[key] = val
    return out


def _tier_for(speed: float) -> str:
    """按 6000 张/片的 ETA 分档：<12h 快、<30h 中、其余慢。"""
    if speed <= 0:
        return "unknown"
    eta_h = 6000 / speed / 3600
    if eta_h < 12:
        return "fast"
    if eta_h < 30:
        return "mid"
    return "slow"


def _require_user(c: sqlite3.Connection, user_id: str, secret: str | None) -> dict:
    """查询类接口用：用户必须已存在，且 secret 必须对得上（不允许顺手创建）。"""
    row = _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,))
    if row is None:
        raise AuthError(f"用户未登记：{user_id}（先 handshake）")
    presented = hash_secret(secret) if secret else ""
    if not row["secret"] or not secrets.compare_digest(presented, row["secret"]):
        raise AuthError("secret 不匹配")
    return row


def _upsert_user(c: sqlite3.Connection, user_id: str, secret: str | None) -> tuple[dict, str | None]:
    """首次登记签发 secret；已有用户必须校验。返回 (user, 新签发的 secret 或 None)。"""
    row = _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,))
    if row is None:
        new_secret = secrets.token_urlsafe(24)
        c.execute("INSERT INTO users(user_id, secret, created_at) VALUES(?,?,?)",
                  (user_id, hash_secret(new_secret), time.time()))
        return _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,)), new_secret
    presented = hash_secret(secret) if secret else ""
    stored = row["secret"] or ""
    if not stored or not secrets.compare_digest(presented, stored):
        raise AuthError("secret 不匹配")
    return row, None


def _upsert_device(c: sqlite3.Connection, user_id: str, device_id: str, client_ver: str,
                   local_pkgs: list[str] | None) -> dict:
    row = _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))
    now = time.time()
    if row is None:
        c.execute("INSERT INTO devices(device_id,user_id,first_seen,last_seen,client_ver,local_pkgs)"
                  " VALUES(?,?,?,?,?,?)",
                  (device_id, user_id, now, now, client_ver, json.dumps(local_pkgs or [])))
    else:
        if row["user_id"] != user_id:
            # 设备被换了用户：拒绝，避免一个 device_id 变成公用马甲
            raise AuthError(f"设备 {device_id} 已绑定到用户 {row['user_id']}")
        sets = ["last_seen=?", "client_ver=?"]
        args: list = [now, client_ver]
        if local_pkgs is not None:
            sets.append("local_pkgs=?")
            args.append(json.dumps(sorted(set(local_pkgs))))
        sets.append("status='online'")          # 归档设备重新上线
        args.append(device_id)
        c.execute(f"UPDATE devices SET {', '.join(sets)} WHERE device_id=?", args)
    return _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))


# ------------------------------------------------------------------ 回收

def sweep_expired(c: sqlite3.Connection, p: dict, on_close) -> list[dict]:
    """把该收回的租约收回：TTL 到期、超出绝对寿命、包被删（孤儿）、设备被删。"""
    now = time.time()
    max_age = p["lease_max_h"] * 3600
    dead = _rows(c, "SELECT * FROM leases WHERE status='open' AND (expires_at<? OR issued_at<?)",
                 (now, now - max_age))
    # 孤儿租约：包被手工删掉 / 仓库被移除
    dead += _rows(c, "SELECT l.* FROM leases l WHERE l.status='open' AND NOT EXISTS"
                     " (SELECT 1 FROM packages p WHERE p.repo=l.repo AND p.id=l.pkg)")
    # 设备被删：租约没人续，按孤儿处理
    dead += _rows(c, "SELECT l.* FROM leases l WHERE l.status='open' AND NOT EXISTS"
                     " (SELECT 1 FROM devices d WHERE d.device_id=l.device_id)")
    closed = []
    for lease in dead:
        c.execute("UPDATE leases SET status='expired', closed_reason='ttl',"
                  " last_beat_at=COALESCE(last_beat_at, issued_at) WHERE lease_id=?",
                  (lease["lease_id"],))
        if lease["kind"] == "active":
            c.execute("UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                      " downloaded_at=NULL, updated_at=? WHERE repo=? AND id=? AND status='claimed'",
                      (time.strftime("%Y-%m-%dT%H:%M:%S%z"), lease["repo"], lease["pkg"]))
        closed.append(lease)
        on_close(c, lease, "ttl")
    # 进度长时间不动的 active：告警（不收回，交给 stall_kill 那条线）
    stalled = _rows(c, "SELECT * FROM leases WHERE status='open' AND kind='active'"
                       " AND last_progress_at IS NOT NULL AND last_progress_at<?",
                    (now - p["stall_warn"],))
    return closed, stalled


def sweep_stalled(c: sqlite3.Connection, p: dict, on_close) -> list[dict]:
    """进度超过 stall_kill 没推进：强制收回（防"心跳在、活没干"）。"""
    now = time.time()
    dead = _rows(c, "SELECT * FROM leases WHERE status='open' AND kind='active'"
                    " AND last_progress_at IS NOT NULL AND last_progress_at<?",
                 (now - p["stall_kill"],))
    for lease in dead:
        c.execute("UPDATE leases SET status='expired', closed_reason='stalled' WHERE lease_id=?",
                  (lease["lease_id"],))
        c.execute("UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                  " downloaded_at=NULL, updated_at=? WHERE repo=? AND id=? AND status='claimed'",
                  (time.strftime("%Y-%m-%dT%H:%M:%S%z"), lease["repo"], lease["pkg"]))
        on_close(c, lease, "stalled")
    return dead


# ------------------------------------------------------------------ 分配

def _busy_keys(c: sqlite3.Connection) -> set[tuple[str, str]]:
    return {(r["repo"], r["pkg"]) for r in
            c.execute("SELECT repo,pkg FROM leases WHERE status='open'")}


def _free_count(c: sqlite3.Connection) -> int:
    total = c.execute(
        "SELECT COUNT(*) n FROM packages p WHERE p.status='available' OR"
        " (p.status='claimed' AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.repo=p.repo"
        "  AND l.pkg=p.id AND l.status='open'))").fetchone()["n"]
    busy = len(_busy_keys(c))
    return max(0, total - busy)


def _pick_package(c: sqlite3.Connection, device: dict, p: dict | None = None,
                  exclude: set | None = None) -> dict | None:
    """挑一个包。快设备优先大包（LPT），慢设备优先小包；本地已有产物的优先续跑。

    同一台设备刚失败过的包会被"冷却"一段时间，否则一旦失败是确定性的
    （配置错、磁盘满），就会陷入"派给它→失败→再派给它"的死循环。
    """
    busy = _busy_keys(c)
    rows = [dict(r) for r in c.execute(
        "SELECT p.repo, p.id, p.size FROM packages p JOIN repos r ON r.repo=p.repo"
        " WHERE r.enabled=1 AND (p.status='available' OR (p.status='claimed' AND NOT EXISTS"
        "  (SELECT 1 FROM leases l WHERE l.repo=p.repo AND l.pkg=p.id AND l.status='open')))"
    ).fetchall()]
    cand = [r for r in rows if (r["repo"], r["id"]) not in busy]
    if exclude:
        cand = [r for r in cand if (r["repo"], r["id"]) not in exclude]
    if not cand:
        return None
    if p is not None:
        cutoff = time.time() - p.get("fail_cooldown", 3600)
        cooled = {(r["repo"], r["pkg"]) for r in c.execute(
            "SELECT repo,pkg FROM leases WHERE device_id=? AND status='failed'"
            " AND issued_at>?", (device["device_id"], cutoff))}
        fresh = [r for r in cand if (r["repo"], r["id"]) not in cooled]
        if fresh:
            cand = fresh
    local = set(json.loads(device.get("local_pkgs") or "[]"))
    resume = [r for r in cand if f"{r['repo']}/{r['id']}" in local]
    if resume:
        return max(resume, key=lambda r: r["size"] or 0)
    if device.get("tier") == "fast":
        return max(cand, key=lambda r: r["size"] or 0)
    if device.get("tier") in ("slow", "mix"):
        return min(cand, key=lambda r: r["size"] or 0)
    return max(cand, key=lambda r: r["size"] or 0)


def _open_lease(c: sqlite3.Connection, device_id: str, pkg: dict, kind: str,
                attempt: int, ttl: float) -> dict:
    now = time.time()
    lease_id = f"L{secrets.token_hex(6)}"
    c.execute("INSERT INTO leases(lease_id,repo,pkg,user_id,device_id,kind,attempt,status,"
              "issued_at,expires_at,last_beat_at,last_progress_at)"
              " VALUES(?,?,?,?,?,?,?,'open',?,?,?,?)",
              (lease_id, pkg["repo"], pkg["id"], _device_user(c, device_id), device_id,
               kind, attempt, now, now + ttl, now, now))
    if kind == "active":
        c.execute("UPDATE packages SET status='claimed', owner=?, claimed_at=?,"
                  " updated_at=? WHERE repo=? AND id=? AND status IN ('available','claimed')",
                  (_device_user(c, device_id), time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   time.strftime("%Y-%m-%dT%H:%M:%S%z"), pkg["repo"], pkg["id"]))
    return _row(c, "SELECT * FROM leases WHERE lease_id=?", (lease_id,))


def _device_user(c: sqlite3.Connection, device_id: str) -> str:
    row = c.execute("SELECT user_id FROM devices WHERE device_id=?", (device_id,)).fetchone()
    return row["user_id"] if row else ""


def _issue(c: sqlite3.Connection, device: dict, p: dict, kind: str, on_open=None) -> dict | None:
    """给某设备发一个租约（active 或 prelock）。已存在同类则直接返回。"""
    existing = _row(c, "SELECT * FROM leases WHERE device_id=? AND kind=? AND status='open'",
                    (device["device_id"], kind))
    if existing:
        return existing
    ttl = p["active_ttl"] if kind == "active" else p["prelock_ttl"]
    tried: set[tuple[str, str]] = set()
    for _ in range(5):
        pkg = _pick_package(c, device, p, tried)
        if pkg is None:
            return None
        prev_attempts = c.execute("SELECT COUNT(*) n FROM leases WHERE repo=? AND pkg=?",
                                  (pkg["repo"], pkg["id"])).fetchone()["n"]
        try:
            lease = _open_lease(c, device["device_id"], pkg, kind, prev_attempts + 1, ttl)
        except sqlite3.IntegrityError:
            # 唯一索引挡下"同一包被并发派给两台设备"：换一个包再试
            tried.add((pkg["repo"], pkg["id"]))
            continue
        if on_open:
            on_open(c, lease)
        return lease
    return None


def assign(c: sqlite3.Connection, device: dict, p: dict, on_open=None) -> dict:
    """保证这台设备有 active；在配额允许时再给一个 prelock。"""
    active = _row(c, "SELECT * FROM leases WHERE device_id=? AND kind='active' AND status='open'",
                  (device["device_id"],))
    if active is None:
        # 手上没有 active：优先把已有的 prelock 升上来，否则新派一个
        pre = _row(c, "SELECT * FROM leases WHERE device_id=? AND kind='prelock' AND status='open'",
                   (device["device_id"],))
        if pre is not None:
            c.execute("UPDATE leases SET kind='active', expires_at=? WHERE lease_id=?",
                      (time.time() + p["active_ttl"], pre["lease_id"]))
            c.execute("UPDATE packages SET status='claimed', owner=?, claimed_at=?, updated_at=?"
                      " WHERE repo=? AND id=? AND status IN ('available','claimed')",
                      (device["user_id"], time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       time.strftime("%Y-%m-%dT%H:%M:%S%z"), pre["repo"], pre["pkg"]))
            active = _row(c, "SELECT * FROM leases WHERE lease_id=?", (pre["lease_id"],))
        else:
            active = _issue(c, device, p, "active", on_open)

    # prelock：库存充足 + 设备够快才发
    free = _free_count(c)
    threshold = max(p["quota_min"], int(c.execute(
        "SELECT COUNT(*) n FROM packages").fetchone()["n"] * p["quota_pct"]))
    fast_enough = device.get("tier") in ("fast", "mid", "unknown")
    pre = _row(c, "SELECT * FROM leases WHERE device_id=? AND kind='prelock' AND status='open'",
               (device["device_id"],))
    if pre is None and free > threshold and fast_enough and active is not None:
        _issue(c, device, p, "prelock", on_open)
    return active


# ------------------------------------------------------------------ 入口

def _handshake_impl(c: sqlite3.Connection, p: dict, body: dict, on_open=None,
              on_event=None) -> dict:
    if not isinstance(body, dict):
        raise AuthError("请求体必须是 JSON 对象")
    user_id = _sid(body.get("user_id"), "user_id")
    secret = body.get("secret")
    if secret is not None and (not isinstance(secret, str) or len(secret) > MAX_SECRET):
        raise AuthError("secret 类型或长度非法")
    user, new_secret = _upsert_user(c, user_id, secret)
    c.execute("UPDATE users SET last_seen=? WHERE user_id=?", (time.time(), user_id))
    raw_dev = body.get("device_id")
    device_id = _sid(raw_dev, "device_id") if raw_dev else f"D{secrets.token_hex(6)}"
    client_ver = _opt_str(body.get("client_ver"), "client_ver", 32)
    local_pkgs = body.get("local_pkgs")
    if local_pkgs is not None:
        if not isinstance(local_pkgs, list) or len(local_pkgs) > 500:
            raise AuthError("local_pkgs 必须是数组（上限 500 项）")
        local_pkgs = [x for x in local_pkgs if isinstance(x, str)][:500]
    device = _upsert_device(c, user_id, device_id, client_ver, local_pkgs)
    if on_event:
        on_event(c, "v3_handshake", user_id, device_id)
    sweep_expired(c, p, lambda cc, lease, why: on_event(cc, "v3_expire", lease["user_id"],
                                                        lease["device_id"]) if on_event else None)
    active = assign(c, device, p, on_open=on_open)
    open_leases = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'",
                        (device_id,))
    return {
        "user_id": user_id,
        "device_id": device_id,
        "secret": new_secret,          # 仅首次登记时返回，客户端必须保存
        "server_id": body.get("server_id"),   # 由 handler 覆盖为真实值
        "beat_interval": p["beat"],
        "tier": device["tier"],
        "leases": [_lease_view(c, l) for l in open_leases],
        "active_count": len(open_leases),
        "actions": [] if open_leases else ["idle"],   # 池子空了要明确告诉客户端
        "limit": 2,
    }


def _heartbeat_impl(c: sqlite3.Connection, p: dict, body: dict, on_open=None,
              on_event=None) -> dict:
    if not isinstance(body, dict):
        raise AuthError("请求体必须是 JSON 对象")
    user_id = _sid(body.get("user_id"), "user_id")
    device_id = _sid(body.get("device_id"), "device_id")
    secret = body.get("secret")
    if secret is not None and (not isinstance(secret, str) or len(secret) > MAX_SECRET):
        raise AuthError("secret 类型或长度非法")
    _require_user(c, user_id, secret)
    device = _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))
    if device is None:
        raise AuthError(f"设备未登记：{device_id}（先 handshake）")
    if device["user_id"] != user_id:
        raise AuthError("设备与用户不匹配")

    now = time.time()
    # 所有回收都必须在续约之前完成：
    #  * 过期/停滞的租约不能被这一拍"复活"；
    #  * 该收回的要当拍就进 rejected，让客户端立刻停手，而不是等下一拍。
    for sweep in (sweep_expired, sweep_stalled):
        sweep(c, p, lambda cc, lease, why: on_event(cc, "v3_expire", lease["user_id"],
                                                    lease["device_id"]) if on_event else None)
    items = body.get("leases")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise AuthError("leases 必须是数组")
    renew, rejected = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        lease_id = item.get("lease_id")
        if not isinstance(lease_id, str):
            continue
        lease = _row(c, "SELECT * FROM leases WHERE lease_id=?", (lease_id,))
        if lease is None or lease["status"] != "open" or lease["device_id"] != device_id:
            rejected.append(lease_id)
            continue
        progress = item.get("progress")
        progress = clean_progress(progress) if isinstance(progress, dict) else {}
        prev = json.loads(lease["progress_json"] or "{}")
        done = progress.get("done")
        advanced = isinstance(done, (int, float)) and (
            not isinstance(prev.get("done"), (int, float)) or done > prev.get("done"))
        ttl = p["active_ttl"] if lease["kind"] == "active" else p["prelock_ttl"]
        # 续约不能越过绝对寿命上限：expires_at 一律夹到 issued_at + lease_max_h
        cap = lease["issued_at"] + p["lease_max_h"] * 3600
        ttl = max(0.0, min(now + ttl, cap) - now)
        # 顺带用进度增量更新速率（EWMA），供分档与 ETA 用
        if advanced:
            last = lease["last_beat_at"] or lease["issued_at"]
            dt = max(1.0, now - last)
            delta = done - (prev.get("done") or 0)
            if delta > 0:
                speed = delta / dt
                old = device["speed"] or 0
                new_speed = speed if old <= 0 else 0.7 * old + 0.3 * speed
                tier = _tier_for(new_speed)
                c.execute("UPDATE devices SET speed=?, tier=? WHERE device_id=?",
                          (new_speed, tier, device_id))
                device["speed"], device["tier"] = new_speed, tier
        c.execute("UPDATE leases SET expires_at=?, last_beat_at=?, progress_json=?,"
                  " last_progress_at=CASE WHEN ? THEN ? ELSE last_progress_at END"
                  " WHERE lease_id=?",
                  (now + ttl, now, json.dumps(progress, ensure_ascii=False),
                   1 if advanced else 0, now, lease_id))
        renew.append(lease_id)
    c.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (now, device_id))

    active = assign(c, device, p, on_open=on_open)

    open_leases = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'",
                        (device_id,))
    actions = []
    if not open_leases:
        actions.append("idle")          # 池子空了
    return {
        "renew": renew,
        "rejected": rejected,           # 这些 lease 已失效，客户端必须停手
        "tier": device["tier"],
        "leases": [_lease_view(c, l) for l in open_leases],
        "actions": actions,
        "lease_id": active["lease_id"] if active else None,
    }


def _finish_impl(c: sqlite3.Connection, p: dict, body: dict, ok: bool, on_open=None,
                 on_event=None) -> dict:
    """设备上报 done / fail。带 lease_id 做栅栏：失效的租约一律拒绝。"""
    if not isinstance(body, dict):
        raise AuthError("请求体必须是 JSON 对象")
    device_id = _sid(body.get("device_id"), "device_id")
    lease_id = _sid(body.get("lease_id"), "lease_id")
    device = _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))
    if device is None:
        raise AuthError("设备未登记")
    lease = _row(c, "SELECT * FROM leases WHERE lease_id=?", (lease_id,))
    if lease is None:
        raise LeaseError(f"租约不存在：{lease_id}")
    if lease["device_id"] != device_id:
        raise LeaseError("该租约不属于这台设备")
    if lease["status"] != "open":
        raise LeaseError(f"租约已 {lease['status']}（可能已超时被回收），拒绝处理")

    now = time.time()
    if ok and lease["kind"] == "prelock":
        # 预锁只是"下一片的预约"，从没开工过；直接 done 会把一个没处理过的包
        # 标成已完成（数据永久丢失且没人发现）。必须拒绝。
        raise LeaseError("预锁尚未开工，不能直接完成；请先完成当前 active 租约")

    if ok:
        c.execute("UPDATE leases SET status='done', closed_reason='done' WHERE lease_id=?",
                  (lease_id,))
        c.execute("UPDATE packages SET status='completed', completed_at=?, note=?, updated_at=?"
                  " WHERE repo=? AND id=?",
                  (time.strftime("%Y-%m-%dT%H:%M:%S%z"), (body.get("stats") or "")[:200],
                   time.strftime("%Y-%m-%dT%H:%M:%S%z"), lease["repo"], lease["pkg"]))
    else:
        c.execute("UPDATE leases SET status='failed', closed_reason=? WHERE lease_id=?",
                  (str(body.get("reason") or "failed")[:100], lease_id))
        if lease["kind"] == "active":
            c.execute("UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                      " downloaded_at=NULL, updated_at=? WHERE repo=? AND id=?",
                      (time.strftime("%Y-%m-%dT%H:%M:%S%z"), lease["repo"], lease["pkg"]))
    if on_event:
        on_event(c, "v3_done" if ok else "v3_fail", lease["user_id"], lease["device_id"])
    active = assign(c, device, p, on_open=on_open)
    open_leases = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'",
                        (device_id,))
    return {"ok": True,
            "leases": [_lease_view(c, l) for l in open_leases],
            "lease_id": active["lease_id"] if active else None}


def reset_secret(c: sqlite3.Connection, user_id: str) -> str:
    """管理员用：重签某用户的 secret（换机、重装、怀疑泄漏）。

    客户端只填 user_id 的设计里，secret 只在首次握手签发一次；本地配置丢了
    就会被永久挡在门外，所以必须有一条管理员侧的恢复路径。
    """
    row = _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,))
    if row is None:
        raise AuthError(f"用户不存在：{user_id}")
    new = secrets.token_urlsafe(24)
    # 与 _upsert_user 保持一致：库里只存 sha256，明文只回给管理员一次
    c.execute("UPDATE users SET secret=? WHERE user_id=?", (hash_secret(new), user_id))
    return new


def _release_device_impl(c: sqlite3.Connection, device_id: str, reason: str = "released",
                         on_event=None) -> dict:
    """运维用：把这台设备手上的 open 租约全部作废，包退回公共池。"""
    leases = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'", (device_id,))
    for lease in leases:
        c.execute("UPDATE leases SET status='released', closed_reason=? WHERE lease_id=?",
                  (reason[:60], lease["lease_id"]))
        if lease["kind"] == "active":
            c.execute("UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                      " downloaded_at=NULL, updated_at=? WHERE repo=? AND id=?",
                      (time.strftime("%Y-%m-%dT%H:%M:%S%z"), lease["repo"], lease["pkg"]))
        if on_event:
            on_event(c, "v3_release_device", lease["user_id"], device_id)
    c.execute("UPDATE devices SET status='offline' WHERE device_id=?", (device_id,))
    return {"released": len(leases), "device_id": device_id}


def prune_devices(c: sqlite3.Connection, days: float = 30, on_event=None) -> dict:
    """归档长期没心跳、且手上没有 open 租约的设备（只改状态，不删数据）。"""
    cutoff = time.time() - days * 86400
    rows = _rows(c, "SELECT d.device_id FROM devices d WHERE d.last_seen<?"
                    " AND d.status!='archived' AND NOT EXISTS (SELECT 1 FROM leases l"
                    " WHERE l.device_id=d.device_id AND l.status='open')", (cutoff,))
    for row in rows:
        c.execute("UPDATE devices SET status='archived' WHERE device_id=?", (row["device_id"],))
        if on_event:
            user = c.execute("SELECT user_id FROM devices WHERE device_id=?",
                             (row["device_id"],)).fetchone()
            on_event(c, "v3_archive", user["user_id"] if user else "", row["device_id"])
    return {"archived": len(rows), "days": days}


def _release_lease_impl(c: sqlite3.Connection, body: dict, on_event=None) -> dict:
    """客户端主动归还租约（--once 收工、Ctrl-C、要关机）。

    与 fail 的区别：**不算失败**，不触发失败冷却，包立刻回池，别的设备马上能领。
    没有这个接口，客户端每次短跑都会把 1~2 个包闲置到 TTL 到期。
    """
    if not isinstance(body, dict):
        raise AuthError("请求体必须是 JSON 对象")
    device_id = _sid(body.get("device_id"), "device_id")
    lease_id = _sid(body.get("lease_id"), "lease_id")
    device = _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))
    if device is None:
        raise AuthError("设备未登记")
    lease = _row(c, "SELECT * FROM leases WHERE lease_id=?", (lease_id,))
    if lease is None or lease["device_id"] != device_id:
        raise LeaseError("租约不存在或不属于这台设备")
    if lease["status"] != "open":
        raise LeaseError(f"租约已 {lease['status']}，无需归还")
    c.execute("UPDATE leases SET status='released', closed_reason=? WHERE lease_id=?",
              (str(body.get("reason") or "client_release")[:60], lease_id))
    if lease["kind"] == "active":
        c.execute("UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                  " downloaded_at=NULL, updated_at=? WHERE repo=? AND id=? AND status='claimed'",
                  (time.strftime("%Y-%m-%dT%H:%M:%S%z"), lease["repo"], lease["pkg"]))
    if on_event:
        on_event(c, "v3_release", lease["user_id"], device_id)
    left = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'", (device_id,))
    return {"ok": True, "released": lease_id,
            "leases": [_lease_view(c, l) for l in left]}


def status(c: sqlite3.Connection, body: dict) -> dict:
    """只读：看自己当前的租约与档位。**不派工、不续约**，供 worker.py show 用。"""
    user_id = _sid(body.get("user_id"), "user_id")
    device_id = _sid(body.get("device_id"), "device_id")
    secret = body.get("secret")
    if secret is not None and (not isinstance(secret, str) or len(secret) > MAX_SECRET):
        raise AuthError("secret 类型或长度非法")
    _require_user(c, user_id, secret)
    device = _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))
    if device is None:
        raise AuthError(f"设备未登记：{device_id}（先 handshake）")
    if device["user_id"] != user_id:
        raise AuthError("设备与用户不匹配")
    leases = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'", (device_id,))
    return {
        "user_id": user_id,
        "device_id": device_id,
        "tier": device["tier"],
        "speed": device["speed"],
        "status": device["status"],
        "leases": [_lease_view(c, l) for l in leases],
    }


def work(c: sqlite3.Connection, device_id: str) -> dict:
    device = _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))
    if device is None:
        raise AuthError("设备未登记")
    leases = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'", (device_id,))
    return {"device": {k: device[k] for k in
                       ("device_id", "user_id", "tier", "speed", "status", "last_seen")},
            "leases": [_lease_view(c, l) for l in leases]}


def overview(c: sqlite3.Connection) -> dict:
    """看板：设备、租约、池子余量。"""
    devices = _rows(c, "SELECT * FROM devices ORDER BY last_seen DESC")
    leases = _rows(c, "SELECT * FROM leases WHERE status='open'")
    total = c.execute("SELECT COUNT(*) n FROM packages").fetchone()["n"]
    avail = c.execute("SELECT COUNT(*) n FROM packages WHERE status='available'").fetchone()["n"]
    busy = len({(l["repo"], l["pkg"]) for l in leases})
    return {"packages_total": total, "available": avail, "reserved_or_active": busy,
            "free_for_assign": max(0, avail - busy),
            "devices": devices,
            "leases": [{**_lease_view(c, l), "user_id": l["user_id"],
                        "device_id": l["device_id"], "issued_at": l["issued_at"]}
                       for l in leases]}


# ------------------------------------------------------------------ 事务包装
# 对外只暴露这四个入口，内部一律在同一个 BEGIN IMMEDIATE 事务里完成。

def handshake(c: sqlite3.Connection, p: dict, body: dict, on_open=None,
              on_event=None) -> dict:
    with _Tx(c):
        return _handshake_impl(c, p, body, on_open, on_event)


def heartbeat(c: sqlite3.Connection, p: dict, body: dict, on_open=None,
              on_event=None) -> dict:
    with _Tx(c):
        return _heartbeat_impl(c, p, body, on_open, on_event)


def finish(c: sqlite3.Connection, p: dict, body: dict, ok: bool, on_open=None,
           on_event=None) -> dict:
    with _Tx(c):
        return _finish_impl(c, p, body, ok, on_open, on_event)


def release_device(c: sqlite3.Connection, device_id: str, reason: str = "released",
                   on_event=None) -> dict:
    with _Tx(c):
        return _release_device_impl(c, device_id, reason, on_event)


def release_lease(c: sqlite3.Connection, body: dict, on_event=None) -> dict:
    with _Tx(c):
        return _release_lease_impl(c, body, on_event)
