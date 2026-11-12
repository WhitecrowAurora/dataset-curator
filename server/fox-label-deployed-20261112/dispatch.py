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
    local_pkgs  TEXT NOT NULL DEFAULT '[]',   -- 该设备本地已有产物的包（"repo/id"）
    -- 设备联动（v3.1，只增不改）：硬件上报与 idle 心跳带来的新列
    gpu_name      TEXT,     -- 主卡型号，≤64 字符；无 GPU 为 NULL
    gpu_vram_mb   INTEGER,  -- 显存 MB，0~1_000_000；无 GPU 为 NULL
    mode          TEXT,     -- 客户端当前 tier 配置原样上报，≤32 字符
    hw_report_at  REAL      -- 最近一次携带 hw 的 handshake 时间（epoch 秒）
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

# 设备联动（SPEC-link §2.1）：hw 字段长度/范围上限
HW_GPU_NAME_MAX = 64
HW_VRAM_MAX = 1_000_000
HW_MODE_MAX = 32

# 派生状态阈值（SPEC-link §2.3，展示层口径，不改静态 status 列）
ONLINE_WINDOW_S = 120      # last_seen > now-120s → online
IDLE_AFTER_S = 600         # now-86400s < last_seen ≤ now-600s → idle
OFFLINE_AFTER_S = 86400    # last_seen ≤ now-86400s 或 NULL → offline

# POST /api/v1/ping 限流：每设备每分钟 ≤10 次（SPEC-link §2.2）
PING_RATE_LIMIT = 10
PING_RATE_WINDOW_S = 60.0

# /done 的 stats 落到 packages.note。200 字是早期"只放一句汇总"的口径，
# 装不下成员上报的"本片哪些 image_id 被跳过/剔除、各自什么原因"。
# 放宽到 16 KiB（POST body 上限 64 KiB），旧客户端的短 note 行为完全不变。
# ★这是"服务端可选升级"：不重启 VPS 时客户端的长 note 只是被截掉明细段，
#   统计部分仍在前面，不影响任何现有判定；重启后明细自动全量入库。
STATS_NOTE_LIMIT = 16 * 1024


def derived_status(last_seen: float | None, now: float | None = None) -> str:
    """按 last_seen 实时派生三态：online / idle / offline。

    这是展示层口径，**不落库、不改静态 status 列**（静态列只在
    unassign/prune/handshake 重新上线时变化，详见 SPEC-link §2.3）。
    旧客户端不 ping 但 handshake 会更新 last_seen，因此不会因缺 hw
    被误判 offline——判定只看 last_seen。
    """
    if now is None:
        now = time.time()
    if last_seen is None:
        return "offline"
    age = now - last_seen
    if age < ONLINE_WINDOW_S:
        return "online"
    if IDLE_AFTER_S <= age < OFFLINE_AFTER_S:
        return "idle"
    return "offline"   # 含 120~600s 观测空洞（SPEC 口径：offline=其余）与 ≥24h


def validate_hw(hw) -> dict | None:
    """校验 handshake 请求里的可选 hw 对象（SPEC-link §1.1）。

    返回归一化后的 {gpu_name, gpu_vram_mb, mode}；任何字段非法一律按 null
    处理（宽松降级，不整包失败）。整个 hw 为 null / 缺失 → 返回 None
    （旧客户端兼容：不带 hw 也能正常 handshake）。
    """
    if hw is None:
        return None
    if not isinstance(hw, dict):
        raise HwError("hw 必须是 JSON 对象或 null")
    out: dict = {"gpu_name": None, "gpu_vram_mb": None, "mode": None}
    gpu_name = hw.get("gpu_name")
    if gpu_name is not None:
        if isinstance(gpu_name, bool) or not isinstance(gpu_name, str):
            raise HwError("hw.gpu_name 必须是字符串或 null")
        if len(gpu_name) > HW_GPU_NAME_MAX:
            raise HwError(f"hw.gpu_name 过长（上限 {HW_GPU_NAME_MAX} 字符）")
        if any(ord(ch) < 32 and ch != "\t" for ch in gpu_name):
            raise HwError("hw.gpu_name 含控制字符")
        out["gpu_name"] = gpu_name
    vram = hw.get("gpu_vram_mb")
    if vram is not None:
        if isinstance(vram, bool) or not isinstance(vram, int):
            raise HwError("hw.gpu_vram_mb 必须是整数或 null")
        if not (0 <= vram <= HW_VRAM_MAX):
            raise HwError(f"hw.gpu_vram_mb 超出范围（0~{HW_VRAM_MAX}）")
        out["gpu_vram_mb"] = vram
    mode = hw.get("mode")
    if mode is not None:
        if isinstance(mode, bool) or not isinstance(mode, str):
            raise HwError("hw.mode 必须是字符串或 null")
        if len(mode) > HW_MODE_MAX:
            raise HwError(f"hw.mode 过长（上限 {HW_MODE_MAX} 字符）")
        if any(ord(ch) < 32 and ch != "\t" for ch in mode):
            raise HwError("hw.mode 含控制字符")
        out["mode"] = mode
    return out


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


class HwError(AuthError):
    """handshake 里 hw 字段非法：这是请求体校验错误，服务端应回 400，
    而不是与身份错误混用 401。继承 AuthError 以兼容既有 except 分支。"""


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
    """全部可调参数。

    申报制（SPEC-declare）：prelock_ttl / active_ttl / stall_warn / stall_kill
    已随自动回收一起拆除；fail_cooldown 是派工策略（失败冷却），保留。
    """
    def envf(name: str, default: str) -> float:
        try:
            return float(os.environ.get(name, default))
        except ValueError:
            return float(default)

    return {
        "beat": envf("FOXLABEL_V3_BEAT", "120"),
        "quota_min": envf("FOXLABEL_V3_QUOTA_MIN", "5"),
        "quota_pct": envf("FOXLABEL_V3_QUOTA_PCT", "0.1"),
        "fast_eta_h": envf("FOXLABEL_V3_FAST_ETA_H", "12"),
        "fail_cooldown": envf("FOXLABEL_V3_FAIL_COOLDOWN", "3600"),
        # 保险丝：租约 30 天寿命上限，正常申报永不触及（SPEC-declare §4）。
        # 触达只可能来自时钟回拨/手工改库/真正遗忘 30 天；不自动回收，只提示。
        "lease_max_h": envf("FOXLABEL_V3_LEASE_MAX_H", "720"),
        # 48h 逾期提示阈值（SPEC-declare §3）：领取后超该时长未申报完成即逾期。
        # 测试可设小值（如 0.001 ≈ 3.6s）模拟，无需 mock 时钟。
        "declare_hours": envf("FOXLABEL_DECLARE_HOURS", "48"),
        # 允许下发 prelock 的设备分级白名单（逗号分隔）。历史上只给
        # fast/mid/unknown，因为慢机器多押一片会让那片空锁一整天。
        # 现在客户端会趁当前片跑活时把预锁这片**预先下好**，"预锁"不再等于
        # "占着不动"，慢机器也该拿到——它一片要跑十几小时，下一片提前下好
        # 正好把交付后那段下载空窗抹掉。要收回成旧口径只需设环境变量。
        "prelock_tiers": [t.strip() for t in (os.environ.get(
            "FOXLABEL_V3_PRELOCK_TIERS") or "fast,mid,unknown,slow").split(",")
            if t.strip()],
        # prelock 的最低客户端版本。3.4.0 起客户端才会"趁跑活时把预锁那片下好"；
        # 更老的客户端拿到 prelock 只是空锁着那片一整天——比不发更糟。所以
        # 能力必须按客户端版本判断，不能靠"我们以为大家都升级了"。
        "prelock_min_ver": (os.environ.get("FOXLABEL_V3_PRELOCK_MIN_VER")
                            or "3.4.0").strip(),
    }


def ver_tuple(raw) -> tuple[int, ...]:
    """"3.4.1" / "v3.4" / "3.4.0-rc2" → (3,4,1) / (3,4) / (3,4,0)。解析不了给 ()。"""
    if not raw:
        return ()
    out: list[int] = []
    text = str(raw).strip().lstrip("vV")
    for piece in text.replace("-", ".").replace("+", ".").split("."):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


def ver_at_least(raw, floor: str) -> bool:
    """客户端版本够不够。**没报版本按不够处理**——旧客户端确实不预取。

    3.4 要按 3.4.0 比：补齐到同样长度再逐位比较，否则 (3,4) < (3,4,0) 会把
    一个正常版本判成太旧。
    """
    need = ver_tuple(floor)
    if not need:
        return True
    got = ver_tuple(raw)
    if not got:
        return False
    n = max(len(got), len(need))
    return (got + (0,) * (n - len(got))) >= (need + (0,) * (n - len(need)))


def notify_tz() -> str:
    """逾期按天去重使用的时区（SPEC-declare §2.5/§3.4），默认 Asia/Shanghai。"""
    return os.environ.get("FOXLABEL_NOTIFY_TZ", "Asia/Shanghai") or "Asia/Shanghai"


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
    # 一把 secret 只能挂一个名字（unique 口径的硬约束）。用部分索引：空 secret
    # 表示"绑定已被管理员解除"，多个空值必须合法，不能落进唯一约束。
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_secret_unique"
                  " ON users(secret) WHERE secret<>''")
    except sqlite3.IntegrityError:
        # 历史库里已经存在重号：不阻断启动；握手时的代码级查重仍然拦新的重号
        pass
    # 迁移：设备联动新列（SPEC-link §2.1）。幂等：先查 PRAGMA，列已存在则跳过。
    existing = {r["name"] for r in c.execute("PRAGMA table_info(devices)")}
    for ddl in ("ALTER TABLE devices ADD COLUMN gpu_name TEXT",
                "ALTER TABLE devices ADD COLUMN gpu_vram_mb INTEGER",
                "ALTER TABLE devices ADD COLUMN mode TEXT",
                "ALTER TABLE devices ADD COLUMN hw_report_at REAL"):
        col = ddl.split("ADD COLUMN ")[1].split()[0]
        if existing and col not in existing:
            c.execute(ddl)
    # 运行指标表（旁证，见下方 RUN_METRICS_SCHEMA）：随 schema 一起幂等建，
    # 避免"第一条申报到达时表还不存在"。
    ensure_metrics_schema(c)


# ------------------------------------------------------------------ 工具

def _row(c: sqlite3.Connection, sql: str, args=()) -> dict | None:
    r = c.execute(sql, args).fetchone()
    return dict(r) if r else None


def _rows(c: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def _lease_view(c: sqlite3.Connection, lease: dict, p: dict | None = None) -> dict:
    """给客户端看的租约视图：带上下载所需的元数据。

    申报制（SPEC-declare §8）：`expires_in` 保留字段但恒为 None（无 TTL）；
    新增 `held_seconds`（已持秒数）、`overdue` / `overdue_seconds`（48h 逾期）。
    """
    pkg = _row(c, "SELECT p.*, r.repo_type FROM packages p JOIN repos r ON r.repo=p.repo"
                  " WHERE p.repo=? AND p.id=?", (lease["repo"], lease["pkg"])) or {}
    now = time.time()
    held = max(0.0, now - lease["issued_at"])
    declare_h = (p or params())["declare_hours"]
    overdue_s = held - declare_h * 3600
    return {
        "lease_id": lease["lease_id"],
        "kind": lease["kind"],
        "repo": lease["repo"],
        "package": lease["pkg"],
        "size": pkg.get("size"),
        "sha256": pkg.get("sha256"),
        "repo_type": pkg.get("repo_type", "dataset"),
        "attempt": lease["attempt"],
        # 兼容期保留字段：申报制无 TTL，恒为 None（SPEC-declare §1/§8）
        "expires_in": None,
        "held_seconds": round(held, 1),
        "overdue": overdue_s > 0,
        "overdue_seconds": round(max(0.0, overdue_s), 1),
        # 心跳时刻也一并给出：客户端/面板要做"心跳还活着吗"的指示灯时需要它
        "last_beat_at": lease.get("last_beat_at"),
        "last_beat_ago": (round(time.time() - lease["last_beat_at"], 1)
                          if lease.get("last_beat_at") else None),
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
    """老口径：用户必须已存在，且 secret 逐字对得上（strict 模式仍在用）。"""
    row = _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,))
    if row is None:
        raise AuthError(f"用户未登记：{user_id}（先 handshake）")
    presented = hash_secret(secret) if secret else ""
    if not row["secret"] or not secrets.compare_digest(presented, row["secret"]):
        raise AuthError("secret 不匹配")
    return row


def _require_device(c: sqlite3.Connection, user_id: str, device_id: str,
                    secret: str | None) -> dict:
    """查询/展示类接口的鉴权：**认设备，不认 secret**。

    为什么要改：3.2 起 users.secret 只是"一把 key 挂一个名字"的登记位，同一个人
    在两台机器上各持一把时，后握手的那台会把这一行改掉——按 secret 校验的话，
    先来的那台下一次 idle ping 就 401，工作被无辜打断。device_id 才是"这台机器"
    的凭据（服务端生成、只有本机和面板可见），lease 归属再按 device_id 栅栏一次，
    所以按设备认人既准确又不会互相挤。

    归属不一致（同一台机器改了名字、还没重新握手）时：secret 认得就把设备就地
    改绑过去，免得"刚改完名字就 401"。strict 口径保持老行为，不受影响。
    """
    device = _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))
    if device is None:
        raise AuthError(f"设备未登记：{device_id}（先 handshake）")
    if secret_policy() == "strict":
        _require_user(c, user_id, secret)
        if device["user_id"] != user_id:
            raise AuthError("设备与用户不匹配")
        return device
    if device["user_id"] == user_id:
        return device
    row = _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,))
    if row is not None and secret and row["secret"] and \
            secrets.compare_digest(hash_secret(secret), row["secret"]):
        busy = c.execute("SELECT COUNT(*) n FROM leases WHERE device_id=? AND status='open'",
                         (device_id,)).fetchone()["n"]
        if not busy:
            c.execute("UPDATE devices SET user_id=? WHERE device_id=?", (user_id, device_id))
            return _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))
        return device
    raise AuthError(
        f"设备 {device_id} 现在登记在用户 {device['user_id']} 名下，与请求的 {user_id} 不一致；"
        f"同一台机器换名字请重新 login（或让管理员 forget --device {device_id}）")


def secret_policy() -> str:
    """secret 口径：`unique`（默认）/ `strict`（老口径：服务端签发、必须逐字相等）。

    unique 口径（现行）：
      * 服务端**不签发、不作废** secret，它只做两件事——登记、查重；
      * 客户端拿来什么就用什么，"我这台机器"这件事由机器自己说了算；
      * 唯一硬约束：**同一个 secret 不能挂在两个用户名下**。这样把配置目录
        整份拷给别人不会变成"两个人共用一个身份"，而是直接报冲突。
    代价（明确记一笔）：知道某个 user_id 的人，用一把新生成的 secret 也能接管
    这个名字——鉴别力等于"知道名字"。池子是熟人局可以接受；要严格就设
    FOXLABEL_SECRET_POLICY=strict 回到服务端签发那一套。
    """
    return (os.environ.get("FOXLABEL_SECRET_POLICY") or "unique").strip().lower()


def _secret_owner(c: sqlite3.Connection, presented: str, user_id: str) -> str | None:
    """这把 secret 是否已经属于**别的**用户名（空 secret 不参与查重）。"""
    if not presented:
        return None
    row = _row(c, "SELECT user_id FROM users WHERE secret=? AND user_id<>?",
               (presented, user_id))
    return row["user_id"] if row else None


def _upsert_user(c: sqlite3.Connection, user_id: str, secret: str | None,
                 device_id: str | None = None) -> tuple[dict, str | None, str]:
    """登记用户。返回 (user, 需要回给客户端的 secret 或 None, 动作)。

    动作：`bound` 首次登记 / `adopted` 采纳了客户端这台机器的新 secret /
    `issued` 老客户端没带 secret，退化为服务端签发 / `""` 无变化。
    """
    row = _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,))
    if row is None:
        if secret:
            owner = _secret_owner(c, hash_secret(secret), user_id)
            if owner:
                # 同一台机器改名字：secret 跟着机器走，把用户名迁过来而不是报冲突。
                # 前提是这台机器手上没有在跑的租约（有就先交付完，别把包搅混）。
                busy = c.execute("SELECT COUNT(*) n FROM leases WHERE device_id=?"
                                 " AND status='open'", (device_id or "",)).fetchone()["n"]
                mine = device_id and c.execute(
                    "SELECT 1 FROM devices WHERE device_id=? AND user_id=?",
                    (device_id, owner)).fetchone()
                if not (mine and not busy):
                    raise AuthError(
                        f"这个 secret 已经被用户 {owner} 用了"
                        + ("（那台机器上还有在跑的包，交完再改名）" if mine else "")
                        + "；换一台机器或换一个 secret")
                c.execute("UPDATE users SET user_id=? WHERE user_id=?", (user_id, owner))
                return _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,)), None, "renamed"
            c.execute("INSERT INTO users(user_id, secret, created_at) VALUES(?,?,?)",
                      (user_id, hash_secret(secret), time.time()))
            return _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,)), None, "bound"
        # 兼容极老的客户端（不会自带 secret）：仍然签发一个
        new_secret = secrets.token_urlsafe(24)
        c.execute("INSERT INTO users(user_id, secret, created_at) VALUES(?,?,?)",
                  (user_id, hash_secret(new_secret), time.time()))
        return _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,)), new_secret, "issued"

    presented = hash_secret(secret) if secret else ""
    stored = row["secret"] or ""
    if stored and presented and secrets.compare_digest(presented, stored):
        return row, None, ""
    if not secret:
        # 老客户端（3.2 以前）本地配置里没有 secret 时什么都不会带过来。此时
        # 没有"本机的 secret"可采纳，只能按老规矩签发一个，否则这台机器直接废掉。
        new_secret = secrets.token_urlsafe(24)
        c.execute("UPDATE users SET secret=? WHERE user_id=?", (hash_secret(new_secret), user_id))
        return _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,)), new_secret, "issued"
    if secret_policy() == "strict":
        raise AuthError(f"secret 不匹配：用户 {user_id} 已绑在别的机器上"
                        f"（strict 口径由服务端签发，需管理员 reset-secret）")
    owner = _secret_owner(c, presented, user_id)
    if owner:
        raise AuthError(f"这个 secret 已经被用户 {owner} 用了，换一个 secret 再试")
    # unique 口径：以客户端这台机器的 secret 为准，直接改绑并留痕
    c.execute("UPDATE users SET secret=? WHERE user_id=?", (presented, user_id))
    return _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,)), None, "adopted"


def _upsert_device(c: sqlite3.Connection, user_id: str, device_id: str, client_ver: str,
                   local_pkgs: list[str] | None, hw: dict | None = None) -> tuple[dict, str | None]:
    """登记/更新设备。返回 (设备行, 改绑前的旧用户；没有改绑则为 None)。

    改绑（device_id 换用户）**不再一律拒绝**：一台机器换账号的常见原因是
    user_id 打错、多人共用机器、旧 config 没清干净，而不是"公用马甲"。真正
    需要拦的只有一种情况——设备手上还有 open 租约，那说明它正在替旧用户干活，
    这时改绑会把这批包的归属搅乱。没有在跑的包就允许改，历史租约与审计不动。
    """
    row = _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))
    now = time.time()
    rebound_from = None
    # hw 只随 handshake 落库（SPEC-link §1.1）：合法子字段写值，非法/缺失写 NULL
    hw_sets, hw_args = [], []
    if hw is not None:
        hw_sets = ["gpu_name=?", "gpu_vram_mb=?", "mode=?", "hw_report_at=?"]
        hw_args = [hw.get("gpu_name"), hw.get("gpu_vram_mb"), hw.get("mode"), now]
    if row is None:
        c.execute("INSERT INTO devices(device_id,user_id,first_seen,last_seen,client_ver,"
                  "local_pkgs,gpu_name,gpu_vram_mb,mode,hw_report_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (device_id, user_id, now, now, client_ver,
                   json.dumps(local_pkgs or []),
                   hw.get("gpu_name") if hw else None,
                   hw.get("gpu_vram_mb") if hw else None,
                   hw.get("mode") if hw else None,
                   now if hw is not None else None))
    else:
        if row["user_id"] != user_id:
            busy = c.execute("SELECT COUNT(*) n FROM leases WHERE device_id=? AND status='open'",
                             (device_id,)).fetchone()["n"]
            if busy:
                raise AuthError(
                    f"设备 {device_id} 还挂着用户 {row['user_id']} 的 {busy} 个在跑的包，"
                    f"不能直接改绑到 {user_id}；请先在面板「强制解绑」这台设备")
            rebound_from = row["user_id"]
        sets = ["last_seen=?", "client_ver=?"] + hw_sets
        args: list = [now, client_ver] + hw_args
        if rebound_from is not None:
            sets.append("user_id=?")            # 改绑：旧用户的租约/审计一条不动
            args.append(user_id)
        if local_pkgs is not None:
            sets.append("local_pkgs=?")
            args.append(json.dumps(sorted(set(local_pkgs))))
        sets.append("status='online'")          # 归档设备重新上线
        args.append(device_id)
        c.execute(f"UPDATE devices SET {', '.join(sets)} WHERE device_id=?", args)
    return _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,)), rebound_from


# ---------------------------------------------------------------- 逾期计算（申报制）
# SPEC-declare：sweep_expired / sweep_stalled 已拆除——open 租约永久有效，
# 唯一关闭路径是 finish(done/fail) 与管理员 release/unassign（confirm 全流程）。
# 代码中不允许存在任何绕过管理员的租约关闭路径（SPEC-declare 附：原则 2）。


def overdue_leases(c: sqlite3.Connection, p: dict | None = None, now: float | None = None) -> list[dict]:
    """逾期未申报清单（服务端实时计算，SPEC-declare §3.1）。

    口径：status='open' 且 kind='active' 且 held = now - issued_at
    > declare_hours（FOXLABEL_DECLARE_HOURS，默认 48h）。按包维度取该包
    当前 open 租约的 issued_at；无 open 租约即不逾期。
    """
    p = p or params()
    now = time.time() if now is None else now
    cutoff = now - p["declare_hours"] * 3600
    rows = _rows(c, "SELECT * FROM leases WHERE status='open' AND kind='active' AND issued_at<?"
                    " ORDER BY issued_at", (cutoff,))
    out = []
    for l in rows:
        held = max(0.0, now - l["issued_at"])
        max_age = p["lease_max_h"] * 3600
        out.append({
            "lease_id": l["lease_id"],
            "repo": l["repo"],
            "pkg": l["pkg"],
            "device_id": l["device_id"],
            "user_id": l["user_id"],
            "claim_at": l["issued_at"],
            "held_seconds": round(held, 1),
            "overdue_seconds": round(held - p["declare_hours"] * 3600, 1),
            # 保险丝越界（≥30 天）：红色特殊提示，不自动回收（SPEC-declare §4）
            "fuse_exceeded": held >= max_age,
        })
    return out


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
                attempt: int) -> dict:
    """开租约。申报制：无 TTL。

    expires_at 列保留（schema 不动，避免迁移），写入保险丝地平线
    issued_at + lease_max_h 作为惰性哨兵值——无任何代码路径读取它做判定
    （SPEC-declare §0.2 要求写 NULL，但 schema 声明 NOT NULL，取不迁移的
    兼容写法；语义一致：租约不因该值关闭）。
    """
    now = time.time()
    lease_id = f"L{secrets.token_hex(6)}"
    fuse_horizon = now + params()["lease_max_h"] * 3600
    c.execute("INSERT INTO leases(lease_id,repo,pkg,user_id,device_id,kind,attempt,status,"
              "issued_at,expires_at,last_beat_at,last_progress_at)"
              " VALUES(?,?,?,?,?,?,?,'open',?,?,?,?)",
              (lease_id, pkg["repo"], pkg["id"], _device_user(c, device_id), device_id,
               kind, attempt, now, fuse_horizon, now, now))
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
    tried: set[tuple[str, str]] = set()
    for _ in range(5):
        pkg = _pick_package(c, device, p, tried)
        if pkg is None:
            return None
        prev_attempts = c.execute("SELECT COUNT(*) n FROM leases WHERE repo=? AND pkg=?",
                                  (pkg["repo"], pkg["id"])).fetchone()["n"]
        try:
            lease = _open_lease(c, device["device_id"], pkg, kind, prev_attempts + 1)
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
            # 申报制：prelock→active 只改 kind，不重设 TTL（SPEC-declare §1.1）。
            # expires_at 哨兵同步延到保险丝地平线，保持惰性语义一致。
            c.execute("UPDATE leases SET kind='active', expires_at=? WHERE lease_id=?",
                      (time.time() + p["lease_max_h"] * 3600, pre["lease_id"]))
            c.execute("UPDATE packages SET status='claimed', owner=?, claimed_at=?, updated_at=?"
                      " WHERE repo=? AND id=? AND status IN ('available','claimed')",
                      (device["user_id"], time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       time.strftime("%Y-%m-%dT%H:%M:%S%z"), pre["repo"], pre["pkg"]))
            active = _row(c, "SELECT * FROM leases WHERE lease_id=?", (pre["lease_id"],))
        else:
            active = _issue(c, device, p, "active", on_open)

    # prelock：库存充足 + 该分级在白名单里才发
    # （白名单见 params()["prelock_tiers"]，可用 FOXLABEL_V3_PRELOCK_TIERS 覆盖）
    free = _free_count(c)
    threshold = max(p["quota_min"], int(c.execute(
        "SELECT COUNT(*) n FROM packages").fetchone()["n"] * p["quota_pct"]))
    # 两个条件缺一不可：这台机器的分级在白名单里，**且**它的客户端会预取。
    # 只看 tier 的话，升级前的老客户端会白拿一片锁一整天（见 params 注释）。
    fast_enough = (device.get("tier") in p["prelock_tiers"]
                   and ver_at_least(device.get("client_ver"), p["prelock_min_ver"]))
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
    raw_dev = body.get("device_id")
    device_id = _sid(raw_dev, "device_id") if raw_dev else f"D{secrets.token_hex(6)}"
    # 先算 device_id 再登记用户：secret 冲突时"这台机器是不是本来就是他的"
    # 要靠 device_id 判断（见 _upsert_user 的 client 口径）
    user, new_secret, secret_action = _upsert_user(c, user_id, secret, device_id)
    c.execute("UPDATE users SET last_seen=? WHERE user_id=?", (time.time(), user_id))
    client_ver = _opt_str(body.get("client_ver"), "client_ver", 32)
    local_pkgs = body.get("local_pkgs")
    if local_pkgs is not None:
        if not isinstance(local_pkgs, list) or len(local_pkgs) > 500:
            raise AuthError("local_pkgs 必须是数组（上限 500 项）")
        local_pkgs = [x for x in local_pkgs if isinstance(x, str)][:500]
    # 可选 hw（SPEC-link §1.1，只增）：校验失败 400；hw=null / 缺失一律按旧语义处理
    hw = validate_hw(body.get("hw"))
    device, rebound_from = _upsert_device(c, user_id, device_id, client_ver, local_pkgs, hw)
    if on_event:
        if secret_action == "adopted":
            # 采纳了客户端带来的新 secret：audit 里要能看出"谁把绑定换掉了"
            on_event(c, "v3_secret_adopt", user_id, device_id,
                     f"user={user_id}; device={device_id}; policy={secret_policy()}")
        elif secret_action == "renamed":
            on_event(c, "v3_user_rename", user_id, device_id,
                     f"machine={device_id}; to={user_id}")
        if rebound_from:
            # 改绑留痕：旧用户是谁、新用户是谁，事后查审计能直接看懂
            on_event(c, "v3_device_rebind", user_id, device_id,
                     f"device={device_id}; from={rebound_from}; to={user_id}")
        on_event(c, "v3_handshake", user_id, device_id)
    # 申报制：无 sweep_expired——租约永久有效，回收只走 finish/管理员 confirm
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
        "leases": [_lease_view(c, l, p) for l in open_leases],
        "active_count": len(open_leases),
        "actions": [] if open_leases else ["idle"],   # 池子空了要明确告诉客户端
        "limit": 2,
    }


def _heartbeat_impl(c: sqlite3.Connection, p: dict, body: dict, on_open=None,
              on_event=None) -> dict:
    """heartbeat 空操作（SPEC-declare §8.2，兼容期）。

    鉴权走设备（见 _require_device：device_id 认机器，secret 在 unique 口径下不参与）；
    **无任何副作用**：
    不续约、不回收、不更新 last_beat_at / last_seen / 派工。响应结构保持
    既有字段：renew（原样回显请求中的 open lease_id）、rejected 恒为空、
    tier / leases / actions / lease_id 按当前库态只读给出。旧客户端心跳
    不报错，open 租约本来就不会消失。
    """
    if not isinstance(body, dict):
        raise AuthError("请求体必须是 JSON 对象")
    user_id = _sid(body.get("user_id"), "user_id")
    device_id = _sid(body.get("device_id"), "device_id")
    secret = body.get("secret")
    if secret is not None and (not isinstance(secret, str) or len(secret) > MAX_SECRET):
        raise AuthError("secret 类型或长度非法")
    device = _require_device(c, user_id, device_id, secret)

    items = body.get("leases")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise AuthError("leases 必须是数组")
    renew = []
    for item in items:
        if not isinstance(item, dict):
            continue
        lease_id = item.get("lease_id")
        if not isinstance(lease_id, str):
            continue
        lease = _row(c, "SELECT status,device_id FROM leases WHERE lease_id=?", (lease_id,))
        # 无回收逻辑，请求里的 open 租约原样确认；已关闭的也不判 rejected
        # （rejected 恒为空数组——新服务端没有 lease 会被"判失效"）
        if lease is not None and lease["status"] == "open" and lease["device_id"] == device_id:
            renew.append(lease_id)
    open_leases = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'",
                        (device_id,))
    return {
        "renew": renew,
        "rejected": [],                 # 申报制：无回收，恒为空
        "tier": device["tier"],
        "leases": [_lease_view(c, l, p) for l in open_leases],
        "actions": [] if open_leases else ["idle"],
        "lease_id": next((l["lease_id"] for l in open_leases if l["kind"] == "active"), None),
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
        raise LeaseError(f"租约已 {lease['status']}（租约已关闭：可能已被管理员释放或已申报），拒绝处理")

    now = time.time()
    # 申报制（SPEC-declare §8.1，契约只增）：finish 请求可带最终 progress——
    # Beater 拆除后面板租约进度停在最后已知值，worker 在申报时一次性补齐。
    # 仅记录，不参与任何判定。
    progress = body.get("progress")
    progress = clean_progress(progress) if isinstance(progress, dict) else {}
    # 旁证：下载/跑活耗时、连接数、出口协议族、失败类型。旧客户端不带，全空。
    metrics = clean_metrics(body.get("metrics"))
    fail_kind = "" if ok else clean_fail_kind(body.get("fail_kind"))
    if ok and lease["kind"] == "prelock":
        # 预锁只是"下一片的预约"，从没开工过；直接 done 会把一个没处理过的包
        # 标成已完成（数据永久丢失且没人发现）。必须拒绝。
        raise LeaseError("预锁尚未开工，不能直接完成；请先完成当前 active 租约")

    if ok:
        c.execute("UPDATE leases SET status='done', closed_reason='done' WHERE lease_id=?",
                  (lease_id,))
        c.execute("UPDATE packages SET status='completed', completed_at=?, note=?, updated_at=?"
                  " WHERE repo=? AND id=?",
                  (time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   (body.get("stats") or "")[:STATS_NOTE_LIMIT],
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
    try:
        write_run_metric(c, lease, device, ok, fail_kind, metrics, now)
    except sqlite3.Error:
        # 指标是旁证：表坏了/磁盘满了也不该让一次正常申报失败。
        pass
    # 最终进度随申报落账（若客户端带了 progress 字段；旧客户端不带则保留原值）
    c.execute("UPDATE leases SET progress_json=?, last_progress_at=? WHERE lease_id=?",
              (json.dumps(progress, ensure_ascii=False), now, lease_id))
    active = assign(c, device, p, on_open=on_open)
    open_leases = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'",
                        (device_id,))
    return {"ok": True,
            "leases": [_lease_view(c, l, p) for l in open_leases],
            "lease_id": active["lease_id"] if active else None}


def reset_secret(c: sqlite3.Connection, user_id: str) -> str:
    """管理员用：**解除 secret 绑定**（不再签发新的）。

    语义变了：secret 以客户端为准，服务端只登记。所以"重签"变成"清空"——
    下次握手无论哪台机器来，都按它本机带来的 secret 重新绑定。真正的"换机器
    并把旧机器踢出去"用 forget_user，那条会连设备一起清。
    """
    row = _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,))
    if row is None:
        raise AuthError(f"用户不存在：{user_id}")
    c.execute("UPDATE users SET secret='' WHERE user_id=?", (user_id,))
    return ""


def forget_user(c: sqlite3.Connection, user_id: str, reason: str = "admin_forget",
                on_event=None) -> dict:
    """清掉一个用户的全部调度身份：作废租约 → 删设备 → 删用户行。

    和 forget_device 的分工：那台是"这台机器换人"，这条是"这个名字不要了"。
    删完 users 行之后，任何一台机器拿这个 user_id 握手都会当成新用户重新登记，
    secret 以它本机为准——这正是"以本地为准"的落地方式。
    """
    with _Tx(c):
        user = _row(c, "SELECT * FROM users WHERE user_id=?", (user_id,))
        if user is None:
            raise LeaseError(f"用户不存在：{user_id}")
        devices = _rows(c, "SELECT device_id FROM devices WHERE user_id=?", (user_id,))
        released = 0
        for dev in devices:
            released += _release_device_impl(c, dev["device_id"], reason, on_event)["released"]
        c.execute("DELETE FROM devices WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        if on_event:
            on_event(c, "v3_user_forget", user_id, "",
                     f"user={user_id}; devices={len(devices)}; released={released};"
                     f" reason={reason}")
        return {"user_id": user_id, "devices": len(devices), "released": released,
                "forgotten": True}


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


def _ping_impl(c: sqlite3.Connection, body: dict) -> dict:
    """idle ping（SPEC-link §2.2）：仅更新 last_seen，不写 events、不改 status。

    语义收窄（SPEC-declare §7.2）：不再是"续约保活"，仅是"我在（可能带进度）"
    的面板三态展示信号 + 进度 EWMA 宿主（新增可选 progress 字段，契约只增）。
    只认 device 身份；body 里除 progress 外的写入性字段都被忽略。
    鉴权与 heartbeat 一致：认设备（_require_device），secret 在 unique 口径下只做查重。
    """
    if not isinstance(body, dict):
        raise AuthError("请求体必须是 JSON 对象")
    user_id = _sid(body.get("user_id"), "user_id")
    device_id = _sid(body.get("device_id"), "device_id")
    secret = body.get("secret")
    if secret is not None and (not isinstance(secret, str) or len(secret) > MAX_SECRET):
        raise AuthError("secret 类型或长度非法")
    device = _require_device(c, user_id, device_id, secret)
    c.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (time.time(), device_id))
    # 进度 EWMA 迁移（SPEC-declare §7.3）：heartbeat 空操作化后，idle ping 新增
    # 可选 progress 字段承接速率/分档更新（契约只增）；旧客户端不带该字段 →
    # 不更新 EWMA → 仅影响派工分档精度，不影响正确性。
    progress = body.get("progress")
    if isinstance(progress, dict) and progress:
        clean = clean_progress(progress)
        done = clean.get("done")
        prev = json.loads(device_json_progress(c, device_id) or "{}")
        if isinstance(done, (int, float)) and (
                not isinstance(prev.get("done"), (int, float)) or done > prev.get("done")):
            last = device.get("last_seen") or (time.time() - PING_RATE_WINDOW_S)
            dt = max(1.0, time.time() - last)
            delta = done - (prev.get("done") or 0)
            if delta > 0:
                speed = delta / dt
                old = device["speed"] or 0
                new_speed = speed if old <= 0 else 0.7 * old + 0.3 * speed
                tier = _tier_for(new_speed)
                c.execute("UPDATE devices SET speed=?, tier=? WHERE device_id=?",
                          (new_speed, tier, device_id))
        # 顺带把最新进度快照写进该设备 open active 租约（展示用，无回收语义）
        c.execute("UPDATE leases SET progress_json=?, last_progress_at=?"
                  " WHERE device_id=? AND kind='active' AND status='open'",
                  (json.dumps(clean, ensure_ascii=False), time.time(), device_id))
    # 派生状态即时反馈：ping 本身就是"我还活着"的信号
    last = _row(c, "SELECT last_seen FROM devices WHERE device_id=?", (device_id,))
    return {"ok": True,
            "status": derived_status(last["last_seen"] if last else None),
            "tier": device["tier"]}


def device_json_progress(c: sqlite3.Connection, device_id: str) -> str | None:
    """该设备 open active 租约的当前进度快照（EWMA 增量判定的前值来源）。"""
    row = c.execute("SELECT progress_json FROM leases WHERE device_id=? AND kind='active'"
                    " AND status='open' ORDER BY issued_at DESC LIMIT 1", (device_id,)).fetchone()
    return row["progress_json"] if row else None


def ping(c: sqlite3.Connection, body: dict) -> dict:
    with _Tx(c):
        return _ping_impl(c, body)


# ------------------------------------------------------------------ /skips
# 剔除/跳过明细分批上报（新增路由，旧客户端不会调）。
#
# ★为什么不能塞进 /done 的 stats：/done 是租约唯一的关闭路径，一片只调一次，
#   明细只能挤在那一条 note 里，装不下就丢，没有第二次机会。
# ★为什么不塞进 /ping：ping 的语义已收窄成"我在，空闲"（不写 events），
#   而且它只在**手上没租约**时才发；打标期间一次都不发，夹不进去。塞进去还要
#   在 clean_progress 之前特判，等于把"ping 不写 events"这条契约拆掉。
# ★所以单开一个可多次调用的接口：租约开着时随便发多少批，每批自带 batch_no，
#   服务端按 (lease_id, batch_no) 幂等去重，客户端收到 200 才推进游标。

SKIP_BATCH_MAX = 200          # 单批条数上限（客户端按 50 发，留足余量）
SKIP_ITEMS_TOTAL_MAX = 5000   # 单次请求的条目硬上限，挡住异常大的 body


def _skip_item(raw: object) -> dict | None:
    """校验并清洗一条明细。坏条目**跳过而不是整批报错**：

    明细是补充信息，一条脏数据不该让整批 50 条白跑——客户端会重发同一批，
    永远发不成功，反而把"上报失败"变成常态。
    """
    if not isinstance(raw, dict):
        return None
    iid = str(raw.get("image_id") or "").strip()
    if not iid or len(iid) > 128:
        return None
    kind = str(raw.get("kind") or "skipped").strip().lower()
    if kind not in ("skipped", "excused", "excluded"):
        kind = "skipped"
    reason = str(raw.get("reason") or "").strip()[:200]
    return {"image_id": iid, "kind": kind, "reason": reason}


def require_device_only(c: sqlite3.Connection, body: dict) -> dict:
    """身份校验入口，给"先鉴权、再限流"的调用方用。

    为什么单独暴露：限流必须在鉴权**之后**——device_id 来自请求体、未经验证，
    拿它当限流的桶 key，任何人填别人的设备号就能把那人的配额打满。
    这里就是把 _require_device 的入参校验一并带上，让调用方一行拿到结论。

    注意 _require_device 在 unique 口径下可能改写 devices.user_id（设备改名
    未重新 handshake 时），但那种情况要求"该设备没有 open 租约"，而 /skips
    的调用方一定持有 open 租约，所以不会被它改绑。
    """
    if not isinstance(body, dict):
        raise AuthError("请求体必须是 JSON 对象")
    user_id = _sid(body.get("user_id"), "user_id")
    device_id = _sid(body.get("device_id"), "device_id")
    secret = body.get("secret")
    if secret is not None and (not isinstance(secret, str) or len(secret) > MAX_SECRET):
        raise AuthError("secret 类型或长度非法")
    return _require_device(c, user_id, device_id, secret)


def _skips_impl(c: sqlite3.Connection, body: dict, on_event=None) -> dict:
    """记录某分片的一批剔除/跳过明细。幂等：同一 (lease_id, batch_no) 只入一次。

    鉴权与 ping 一致（认设备），再按 lease_id 栅栏一次归属，避免 A 设备往
    B 的租约里写明细。

    ★每次调用写**一行** events，detail 是一批的 JSON 数组，而不是一条明细一行：
      几十张图一批时，逐条写会让 events 表在千万级规模下被明细灌满，面板
      和 SSE 都会被拖慢。按批存既保留了完整信息，行数又和"发了几次"同阶。
    """
    if not isinstance(body, dict):
        raise AuthError("请求体必须是 JSON 对象")
    user_id = _sid(body.get("user_id"), "user_id")
    device_id = _sid(body.get("device_id"), "device_id")
    secret = body.get("secret")
    if secret is not None and (not isinstance(secret, str) or len(secret) > MAX_SECRET):
        raise AuthError("secret 类型或长度非法")
    _require_device(c, user_id, device_id, secret)

    # lease_id 走 _sid：它会被拼进去重前缀（见下面的 marker），不校验长度和
    # 控制字符的话，`%` / `_` 会被 SQL 的 LIKE 当成通配符，构造一个含 % 的
    # lease_id 就能匹配到别的租约的行、让自己的明细被判"重复"而静默丢弃。
    lease_id = _sid(body.get("lease_id"), "lease_id")
    lease = _row(c, "SELECT * FROM leases WHERE lease_id=?", (lease_id,))
    if lease is None:
        raise LeaseError("租约不存在")
    if lease["device_id"] != device_id:
        raise LeaseError("租约不属于这台设备")
    if lease["status"] != "open":
        # 已关闭的租约不再收明细：否则设备改绑到别的用户后，还能往旧用户的
        # 已关闭租约里灌数据，审计记的是旧 user，归属就乱了。
        raise LeaseError(f"租约已 {lease['status']}，不再接收明细")

    items = body.get("items")
    if not isinstance(items, list):
        raise AuthError("items 必须是数组")
    if len(items) > SKIP_ITEMS_TOTAL_MAX:
        raise AuthError(f"items 单次最多 {SKIP_ITEMS_TOTAL_MAX} 条")
    cleaned = [r for r in (_skip_item(x) for x in items) if r is not None]
    if not cleaned:
        return {"ok": True, "accepted": 0, "batch_no": body.get("batch_no"),
                "dedup": False}

    try:
        batch_no = int(body.get("batch_no") or 0)
    except (TypeError, ValueError):
        batch_no = 0

    # 第一批顺带把"这一片有剔除"这个事实记在包上，面板不必翻 events 才知道。
    if on_event is not None and batch_no <= 1:
        kinds: dict[str, int] = {}
        for r in cleaned:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        on_event(c, "v3_skips", lease["user_id"], device_id,
                 f"pkg={lease['pkg']}; batch={batch_no}; "
                 + "; ".join(f"{k}={v}" for k, v in sorted(kinds.items())))

    # 幂等键塞进 detail 头部：events 表没有额外索引，用它做去重前缀扫描。
    marker = f"lease={lease_id}; batch={batch_no};"
    # LIKE 的 % 和 _ 必须转义：lease_id 里出现它们就会把去重查询变成模糊匹配，
    # 命中别人的行 → 本批被判重复 → 返回 200 accepted=0 → 客户端推进游标，
    # 明细永久丢失。
    # ★一次性映射替换而不是链式 replace：先换 \ 再换 % 时，id 里的反斜杠会被
    #   二次转义成 `\\%`，转义反而失效。str.translate 对每个字符只处理一次。
    _esc = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})
    pattern = marker.translate(_esc) + "%"
    dup = c.execute("SELECT seq FROM events WHERE kind='v3_skip_items'"
                    " AND detail LIKE ? ESCAPE '\\' LIMIT 1", (pattern,)).fetchone()
    if dup is not None:
        return {"ok": True, "accepted": 0, "batch_no": batch_no, "dedup": True}

    if on_event is not None:
        on_event(c, "v3_skip_items", lease["user_id"], device_id,
                 marker + " items=" + json.dumps(cleaned, ensure_ascii=False))
    return {"ok": True, "accepted": len(cleaned), "batch_no": batch_no,
            "dedup": False}


def skips(c: sqlite3.Connection, body: dict, on_event=None) -> dict:
    with _Tx(c):
        return _skips_impl(c, body, on_event)


def status(c: sqlite3.Connection, body: dict, p: dict | None = None) -> dict:
    """只读：看自己当前的租约与档位。**不派工、不续约**，供 worker.py show 用。"""
    user_id = _sid(body.get("user_id"), "user_id")
    device_id = _sid(body.get("device_id"), "device_id")
    secret = body.get("secret")
    if secret is not None and (not isinstance(secret, str) or len(secret) > MAX_SECRET):
        raise AuthError("secret 类型或长度非法")
    device = _require_device(c, user_id, device_id, secret)
    leases = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'", (device_id,))
    now = time.time()
    return {
        "user_id": user_id,
        "device_id": device_id,
        "tier": device["tier"],
        "speed": device["speed"],
        "status": device["status"],
        "last_seen": device["last_seen"],
        "last_seen_ago": round(now - (device["last_seen"] or now), 1),
        "leases": [_lease_view(c, l, p) for l in leases],
    }


def work(c: sqlite3.Connection, device_id: str, p: dict | None = None) -> dict:
    device = _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))
    if device is None:
        raise AuthError("设备未登记")
    leases = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'", (device_id,))
    return {"device": device_view(device, time.time()),
            "leases": [_lease_view(c, l, p) for l in leases]}


def device_view(d: dict, now: float | None = None) -> dict:
    """单台设备的 /api/v1/work 视图。

    既有字段（device_id/user_id/tier/speed/status/last_seen）逐字不动
    （SPEC-link §3 契约白名单）；status 从静态列改为派生三态（online/idle/
    offline），静态 status 列保留在 DB 但不再作为展示依据——注意派生值与
    静态列的语义差异：静态列只在 unassign/prune/重新 handshake 时变化。
    既有 6 字段在此处显式按原顺序重建，新增字段只增不改。
    """
    pkgs = json.loads(d.get("local_pkgs") or "[]")
    return {
        # ---- 既有字段，逐字不动 ----
        "device_id": d["device_id"],
        "user_id": d["user_id"],
        "tier": d["tier"],
        "speed": d["speed"],
        "status": derived_status(d.get("last_seen"), now),   # 派生，替代静态列展示
        "last_seen": d["last_seen"],
        "client_ver": d.get("client_ver"),
        "local_pkgs": pkgs,
        # ---- 新增字段（只增，始终出现，可为 null）----
        "gpu_name": d.get("gpu_name"),
        "gpu_vram_mb": d.get("gpu_vram_mb"),
        "mode": d.get("mode"),
        "hw_report_at": d.get("hw_report_at"),
        "first_seen": d["first_seen"],
        "local_pkgs_count": len(pkgs),
    }


def overview(c: sqlite3.Connection) -> dict:
    """看板：设备、租约、池子余量。"""
    devices = _rows(c, "SELECT * FROM devices ORDER BY last_seen DESC")
    leases = _rows(c, "SELECT * FROM leases WHERE status='open'")
    total = c.execute("SELECT COUNT(*) n FROM packages").fetchone()["n"]
    avail = c.execute("SELECT COUNT(*) n FROM packages WHERE status='available'").fetchone()["n"]
    busy = len({(l["repo"], l["pkg"]) for l in leases})
    return {"packages_total": total, "available": avail, "reserved_or_active": busy,
            "free_for_assign": max(0, avail - busy),
            "devices": [device_view(d, time.time()) for d in devices],
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


def forget_device(c: sqlite3.Connection, device_id: str, reason: str = "admin_forget",
                  on_event=None) -> dict:
    """强行解绑：作废租约 + **删掉设备行**。

    与 release_device 的区别是后者只让设备"下线"，device_id → user_id 的绑定
    还在，客户端换账号再握手依旧被挡（历史上就是这么卡死的）。这里把绑定本身
    擦掉：设备表里没有这台机器之后，下一个 handshake 会按新用户重新登记，等于
    一次干净的重装。租约/事件/审计一律保留——我们只断"当前绑定"，不改历史。
    """
    with _Tx(c):
        dev = _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))
        if dev is None:
            raise LeaseError(f"设备不存在：{device_id}")
        res = _release_device_impl(c, device_id, reason, on_event)
        c.execute("DELETE FROM devices WHERE device_id=?", (device_id,))
        if on_event:
            on_event(c, "v3_device_forget", dev["user_id"], device_id,
                     f"device={device_id}; user={dev['user_id']}; reason={reason};"
                     f" released={res['released']}")
        return {"device_id": device_id, "user_id": dev["user_id"],
                "released": res["released"], "forgotten": True}


def release_lease(c: sqlite3.Connection, body: dict, on_event=None) -> dict:
    with _Tx(c):
        return _release_lease_impl(c, body, on_event)


# ---------------------------------------------------------------- 逾期通知落地（申报制）

def _local_date(epoch: float, tz_name: str | None = None) -> str:
    """按 FOXLABEL_NOTIFY_TZ（默认 Asia/Shanghai）取本地日期，供按天去重。"""
    import datetime
    import zoneinfo
    tz = zoneinfo.ZoneInfo(tz_name or notify_tz())
    dt = datetime.datetime.fromtimestamp(epoch, tz)
    return dt.strftime("%Y-%m-%d")


def record_overdue_notices(c: sqlite3.Connection, p: dict | None = None,
                           now: float | None = None) -> dict:
    """每日一次提示落地（SPEC-declare §3.4）：惰性物化，无后台循环。

    1. 实时计算逾期包（overdue_leases）；
    2. 对每个逾期包查 notifications 当日是否已有记录（key 命中即跳过）；
    3. 无 → 落一条新提醒（同日重复查询幂等，不重复写）；已有未解决记录
       则跨日续记（remind_count+1 / last_reminded_at 更新）。
    4. 已解决（该包无 open 租约）的历史条目标记 resolved（resolved_at/
       resolved_by='auto'），不再产生新提示。

    notifications 表由 ensure_schema 幂等建表（新增，不影响既有 schema 数据）。
    返回 {"overdue": 清单（含 today_notified 标记）, "new_notices": n,
          "resolved": n}。
    """
    p = p or params()
    now = time.time() if now is None else now
    today = _local_date(now)
    open_keys = {(r["repo"], r["pkg"]) for r in
                 c.execute("SELECT repo,pkg FROM leases WHERE status='open'")}
    # resolve 回填：有提醒记录但已无 open 租约 → 标记已解决（不物理删除）
    resolved = 0
    for row in _rows(c, "SELECT id, repo, pkg FROM notifications"
                        " WHERE kind='overdue' AND resolved_at IS NULL"):
        if (row["repo"], row["pkg"]) not in open_keys:
            c.execute("UPDATE notifications SET resolved_at=?, resolved_by='auto'"
                      " WHERE id=? AND resolved_at IS NULL", (now, row["id"]))
            resolved += 1
    overdue = overdue_leases(c, p, now)
    new_notices = 0
    for item in overdue:
        key = f"overdue:{item['repo']}/{item['pkg']}:{today}"
        dup = c.execute("SELECT 1 FROM notifications WHERE key=?", (key,)).fetchone()
        if dup:
            item["today_notified"] = True
            continue
        # 同包未解决的历史提醒：跨日续记（remind_count+1），逐包只占一席
        c.execute("UPDATE notifications SET remind_count=remind_count+1,"
                  " last_reminded_at=? WHERE kind='overdue' AND repo=? AND pkg=?"
                  " AND resolved_at IS NULL", (now, item["repo"], item["pkg"]))
        c.execute("INSERT INTO notifications(key,kind,repo,pkg,device_id,user_id,"
                  "claim_at,held_seconds,overdue_seconds,fuse_exceeded,"
                  "first_reminded_at,last_reminded_at,remind_count)"
                  " VALUES(?,'overdue',?,?,?,?,?,?,?,?,?,?,1)",
                  (key, item["repo"], item["pkg"], item["device_id"], item["user_id"],
                   item["claim_at"], item["held_seconds"], item["overdue_seconds"],
                   1 if item["fuse_exceeded"] else 0, now, now))
        item["today_notified"] = False
        new_notices += 1
    return {"overdue": overdue, "new_notices": new_notices, "resolved": resolved}


def overdue_view(c: sqlite3.Connection, p: dict | None = None,
                 now: float | None = None) -> dict:
    """只读聚合：当前逾期清单 + 未解决提醒（含历史 remind_count），供面板三通道。"""
    p = p or params()
    now = time.time() if now is None else now
    open_keys = {(r["repo"], r["pkg"]) for r in
                 c.execute("SELECT repo,pkg FROM leases WHERE status='open'")}
    items = []
    for row in _rows(c, "SELECT * FROM notifications WHERE kind='overdue'"
                        " AND resolved_at IS NULL ORDER BY first_reminded_at"):
        live = (row["repo"], row["pkg"]) in open_keys
        items.append({
            "repo": row["repo"], "pkg": row["pkg"],
            "device_id": row["device_id"], "user_id": row["user_id"],
            "claim_at": row["claim_at"],
            "held_seconds": row["held_seconds"],
            "overdue_seconds": row["overdue_seconds"],
            "fuse_exceeded": bool(row["fuse_exceeded"]),
            "first_reminded_at": row["first_reminded_at"],
            "last_reminded_at": row["last_reminded_at"],
            "remind_count": row["remind_count"],
            "still_open": live,
        })
    return {"ok": True, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "declare_hours": p["declare_hours"], "items": items}


NOTIFICATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    key               TEXT NOT NULL UNIQUE,   -- overdue:<repo>/<pkg>:<local_date>
    kind              TEXT NOT NULL,          -- overdue
    repo              TEXT NOT NULL,
    pkg               TEXT NOT NULL,
    device_id         TEXT,
    user_id           TEXT,
    claim_at          REAL,
    held_seconds      REAL,
    overdue_seconds   REAL,
    fuse_exceeded     INTEGER NOT NULL DEFAULT 0,
    first_reminded_at REAL NOT NULL,
    last_reminded_at  REAL NOT NULL,
    remind_count      INTEGER NOT NULL DEFAULT 1,
    resolved_at       REAL,
    resolved_by       TEXT
);
CREATE INDEX IF NOT EXISTS idx_notif_open ON notifications(kind, resolved_at);
"""


def ensure_notifications_schema(c: sqlite3.Connection) -> None:
    """幂等建通知表（申报制新增，不影响既有表）。"""
    c.executescript(NOTIFICATIONS_SCHEMA)


# ---------------------------------------------------------------- 运行指标（旁证）
#
# 为什么单独一张表而不是塞进 events：events 是自由文本的审计流水，要按
# "失败类型/下载耗时"聚合时得解析字符串，而且 retention 策略也不一样。
# 表里只有耗时、张数、重试次数这类纯数字，不含任何网络位置信息（见下方
# _DROP_METRIC_COLUMNS 的说明）。
# 这些数字**只用于调优与趋势**，不参与任何派工/回收判定——写坏了最多少一个点。

FAIL_KINDS = ("net", "data", "disk", "cmd", "oom", "other")

RUN_METRICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_metrics (
    lease_id     TEXT PRIMARY KEY,
    ts           REAL NOT NULL,
    repo         TEXT NOT NULL,
    pkg          TEXT NOT NULL,
    device_id    TEXT,
    user_id      TEXT,
    tier         TEXT,
    client_ver   TEXT,
    ok           INTEGER NOT NULL,
    download_s   REAL,
    task_s       REAL,
    prefetch_hit INTEGER,
    images       INTEGER,
    retries      INTEGER,
    fail_kind    TEXT,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON run_metrics(ts);
CREATE INDEX IF NOT EXISTS idx_metrics_pkg ON run_metrics(repo, pkg);
CREATE INDEX IF NOT EXISTS idx_metrics_dev ON run_metrics(device_id, ts);
"""

# 早期版本把连接数/出口 IP/协议族也存了进来。出口 IP 能定位网络位置，
# 属于不该离开本机的元数据；这几个列一律删掉，别留着等以后有人再用。
_DROP_METRIC_COLUMNS = ("conns", "cdn_ip", "cdn_family")


def ensure_metrics_schema(c: sqlite3.Connection) -> None:
    c.executescript(RUN_METRICS_SCHEMA)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(run_metrics)")}
    for col in _DROP_METRIC_COLUMNS:
        if col in cols:                  # 老库就地清理，SQLite 3.35+ 支持 DROP COLUMN
            try:
                c.execute(f"ALTER TABLE run_metrics DROP COLUMN {col}")
            except sqlite3.Error:
                pass                      # 老 SQLite 不支持就留着，代码不再读写它


def clean_fail_kind(raw) -> str:
    """失败类型收敛成枚举。

    自由文本（"CUDA out of memory"）事后没法统计，得靠正则猜；客户端本来就
    知道自己是下载挂了还是命令挂了，让它直接报类型，原文继续走 reason。
    """
    kind = str(raw or "").strip().lower()
    return kind if kind in FAIL_KINDS else "other"


def _num(raw, lo: float = 0.0, hi: float = 1e9) -> float | None:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val:                       # NaN
        return None
    return max(lo, min(hi, val))


def clean_metrics(raw) -> dict:
    """把客户端上报的指标洗成定长字段：脏的那一项丢掉，不整条丢。

    只认纯数字与短枚举。旧客户端若还带 conns/cdn_ip/cdn_family，直接不认——
    白名单式取值，多出来的键一律丢，免得哪天又被顺手存进库里。
    """
    if not isinstance(raw, dict):
        return {}
    return {
        "download_s": _num(raw.get("download_s")),
        "task_s": _num(raw.get("task_s")),
        "prefetch_hit": 1 if raw.get("prefetch_hit") else 0,
        "images": int(_num(raw.get("images"), 0, 1e9) or 0) or None,
        "retries": int(_num(raw.get("retries"), 0, 999) or 0),
        "note": str(raw.get("note") or "").strip()[:200] or None,
    }


def write_run_metric(c: sqlite3.Connection, lease: dict, device: dict | None,
                     ok: bool, fail_kind: str = "", metrics: dict | None = None,
                     now: float | None = None) -> None:
    m = metrics or {}
    dev = device or {}
    c.execute(
        "INSERT OR REPLACE INTO run_metrics(lease_id,ts,repo,pkg,device_id,user_id,"
        "tier,client_ver,ok,download_s,task_s,prefetch_hit,"
        "images,retries,fail_kind,note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lease["lease_id"], time.time() if now is None else now, lease["repo"],
         lease["pkg"], lease["device_id"], lease.get("user_id"),
         dev.get("tier"), dev.get("client_ver"), 1 if ok else 0,
         m.get("download_s"), m.get("task_s"), m.get("prefetch_hit"),
         m.get("images"), m.get("retries"), (fail_kind or None) if not ok else None,
         m.get("note")))


def metrics_view(c: sqlite3.Connection, *, hours: float = 72.0, limit: int = 500,
                 now: float | None = None) -> dict:
    """最近一段时间的指标 + 按失败类型聚合（面板趋势用）。

    刻意不按协议族/出口切分：面板只该看"慢了还是挂了"，不该看"走的是哪条出口"。
    """
    now = time.time() if now is None else now
    since = now - max(1.0, float(hours)) * 3600
    rows = _rows(c, "SELECT * FROM run_metrics WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                 (since, max(1, min(5000, int(limit)))))
    by_fail: dict[str, int] = {}
    downloads: list[float] = []
    for r in rows:
        if not r["ok"]:
            key = r.get("fail_kind") or "other"
            by_fail[key] = by_fail.get(key, 0) + 1
        if r.get("download_s"):
            downloads.append(r["download_s"])
    return {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hours": hours,
        "count": len(rows),
        "avg_download_s": round(sum(downloads) / len(downloads), 1) if downloads else None,
        "by_fail_kind": by_fail,
        "items": rows,
    }


# ---------------------------------------------------------------- 主动推送
#
# 逾期信息原本是"惰性物化"：只有有人打开面板查 /admin/v1/overdue 时才落一条
# 提醒。没人看 = 没人知道，48h 逾期等于从来没有提醒过。这里补上外发通道：
# 后台按间隔扫一次，把**当天新产生的**那条 POST 出去，按天去重已经在库里
# （notifications.key 带日期），重复扫不会刷屏。
#
# 只做"发现 + 外发"，**不做任何回收**——申报制的原则不变：只有管理员能关租约。


def webhook_url() -> str:
    """外发地址（FOXLABEL_WEBHOOK_URL）。空 = 不推，逾期只在面板里看。"""
    return (os.environ.get("FOXLABEL_WEBHOOK_URL") or "").strip()


def push_webhook(items: list[dict], *, kind: str = "overdue") -> bool:
    """把一批提醒 POST 出去。失败只打一行警告，绝不抛给调用方。

    刻意不引第三方库：一个 urlopen 够用，装 requests 是给零依赖服务端加部署门槛。
    """
    import sys
    import urllib.request

    url = webhook_url()
    if not url or not items:
        return False
    body = json.dumps({"kind": kind, "count": len(items),
                       "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "items": items}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "fox-label/overdue-watcher"})
    try:
        with urllib.request.urlopen(
                req, timeout=float(os.environ.get("FOXLABEL_PUSH_TIMEOUT", "8") or 8)) as r:
            ok = 200 <= getattr(r, "status", 200) < 300
    except Exception as exc:            # noqa: BLE001 - 外发失败不该影响任何主流程
        print(f"[warn] 逾期推送失败：{exc}", file=sys.stderr, flush=True)
        return False
    if not ok:
        print(f"[warn] 逾期推送返回 {getattr(r, 'status', '?')}", file=sys.stderr, flush=True)
    return ok


def push_overdue(c: sqlite3.Connection, p: dict | None = None,
                 now: float | None = None) -> dict:
    """扫描 → 物化通知 → 只把今天新产生的那批推出去。

    `record_overdue_notices` 已经把"今天是否已提醒"算在 `today_notified` 里，
    所以这里推的是**新增项**，跨日会重推一次（提醒本该每天一次，不是只响一次）。
    """
    res = record_overdue_notices(c, p, now)
    fresh = [it for it in res["overdue"] if not it.get("today_notified")]
    sent = push_webhook(fresh) if fresh else False
    return {**res, "pushed": len(fresh) if sent else 0,
            "webhook": bool(webhook_url())}


def start_overdue_watcher(conn_factory, interval: float | None = None):
    """起后台线程定期扫逾期并推送；没配 webhook 就返回 None（不起线程）。

    `conn_factory()` 必须每次返回**调用线程自己的** sqlite 连接——连接对象
    不能跨线程用，也不能长期复用一个（长连接会把 WAL 拖着）。
    """
    import sys
    import threading

    if not webhook_url():
        return None
    try:
        period = float(interval if interval is not None
                       else os.environ.get("FOXLABEL_PUSH_INTERVAL", "900") or 900)
    except ValueError:
        period = 900.0
    period = max(60.0, period)

    def loop() -> None:
        while True:
            try:
                c = conn_factory()
                try:
                    push_overdue(c)
                    c.commit()
                finally:
                    c.close()
            except Exception as exc:    # noqa: BLE001 - 后台线程不能因为一次失败就断
                print(f"[warn] 逾期扫描失败：{exc}", file=sys.stderr, flush=True)
            time.sleep(period)

    thread = threading.Thread(target=loop, name="overdue-watcher", daemon=True)
    thread.start()
    return thread
