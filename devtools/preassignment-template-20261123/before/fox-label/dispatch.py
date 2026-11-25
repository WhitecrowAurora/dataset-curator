"""fox-label v3 调度层：派活、租约、心跳。

和 v2 的区别
------------
v2：客户端自己 claim，抢不到就 409。
v3：**服务端派**。客户端只说"我是谁、我在干什么、干到哪了"，服务端决定给谁什么。
    每个分项同一时刻只有一个 open 占用；同片 TAG/NL 可由不同设备并行。

三条硬规则
----------
1. 每设备最多 1 个 active + 1 个 prelock（同时只处理一片）。
2. 在线租约成功回报续期 48 小时；离线派单独立处理。
3. 提交/完成必须带 lease_id，超时被回收后的旧设备提交一律拒绝（栅栏）。

设计约定
--------
* 回传产物按图片和分项归并验收；失败、漏项和冲突不能登记完成。
* 时间统一用 epoch 秒参与比较（ISO 字符串只用于审计展示）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from diagnostics import store as execution_results
from assignments import store as package_queue
from assignments import offline as offline_tasks
from scheduling import leases as part_leases, policies as scheduling_policies, selection as scheduling_selection
from scheduling import lifetime as lease_lifetime
from scheduling.transitions import atomic as _atomic_policy, reconcile as _reconcile_policy
import secrets
import sqlite3
import time

STATS_NOTE_LIMIT = 16 * 1024

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    secret     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    last_seen  REAL
);

-- 工作区（2026-11-13）：把"哪些仓库的活归谁做"变成可配置的归属关系。
-- 改造前派工 SQL 只过滤 r.enabled=1，**所有启用仓库的包混在一起派**，谁拿到
-- 哪个仓库全看池子分布。工作区就是给仓库加一层归属：
--   repos      = 这个工作区能做的仓库（JSON 数组，多仓库）
--   users.workspace_id / devices.workspace_id = 谁属于它
-- 未分配工作区的设备**不受限制**（保持改造前的行为），所以这个功能是纯增量的。
CREATE TABLE IF NOT EXISTS workspaces (
    ws_id      TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    -- '[]' 是**有效值**：明确表示"这个工作区没有任何仓库"。
    -- 不能当成"不限制"——管理员建了工作区却忘了加仓库时，那台机器会静默
    -- 抢到所有仓库的包，而账面上看不出任何异常。
    repos      TEXT NOT NULL DEFAULT '[]',
    note       TEXT,
    created_at TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_lease_dev   ON leases(device_id, status);

-- 批量定向：尚未注册的用户也可先保留分片。租约仍只有 active/prelock 两个槽位，
-- 用户下次领工时从自己保留的队列逐片领取。
CREATE TABLE IF NOT EXISTS package_targets (
    repo        TEXT NOT NULL,
    pkg         TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'admin',
    PRIMARY KEY (repo, pkg)
);
CREATE INDEX IF NOT EXISTS idx_package_targets_user
    ON package_targets(user_id, repo, pkg);

-- 分项产物（SPEC-parts §1）：一个包有两个可独立完成的产物——tag 分类头结果
-- 与 NL 描述。以前的 status='completed' 是"整包一个布尔"，只做 NL 的机器
-- 交上来的包在账面与真包没有区别，于是永远缺 tag 也没人知道。
-- 单列：
--   ''   = 该分项还没完成（初始值，也是"整包完成"的取值）
--   done = 已申报完成
--   skip = 该设备能力上明确不做（如 tag 模型跑不动），不算完成、留给人补
-- 老库由 ensure_schema 的 ALTER 迁移补列，默认 ''，语义与改造前一致。
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

    在线租约统一成功上报后 48 小时过期；fail_cooldown 保留失败冷却。
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
        # 离线派单保留原哨兵值，不依赖在线续期。
        "lease_max_h": envf("FOXLABEL_V3_LEASE_MAX_H", "720"),
        # 离线任务领取时长展示阈值；在线任务以 expires_at 为准。
        "declare_hours": envf("FOXLABEL_DECLARE_HOURS", "48"),
    }


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
    # 迁移：设备联动新列（SPEC-link §2.1）+ 分项意愿（SPEC-parts §2）。
    # 幂等：先查 PRAGMA，列已存在则跳过。
    #   ⚠ 取列名只能用 (表名, 列名) 元组。以前拿 ddl 字符串自己 split，切出来的是
    #   列名**后面那个词**（"TEXT"/"REAL"），于是 `col not in existing` 恒真、
    #   ALTER 每次都跑——新库第一次启动直接撞 "duplicate column name"。模板
    #   早就写在注释里了，别回去用字符串切。
    _add_columns(c, "devices", (
        ("gpu_name", "TEXT"),
        ("gpu_vram_mb", "INTEGER"),
        ("mode", "TEXT"),
        ("hw_report_at", "REAL"),
        # 这台设备愿意做哪几项，'[]' = 都做。
        # 服务端**不按硬件硬派**，只把它当派工排序的偏好（2080Ti 跑 tag 慢、
        # 跑 Q6 的 Qwen3-VL 还行，快不快只有那台机器自己知道）。
        ("parts", "TEXT NOT NULL DEFAULT '[]'"),
    ))
    # 分项产物状态（SPEC-parts §1）。老库补列后一律 ''，语义按 status 区分：
    #   completed 的包 → 两侧都齐（改造前只存在"一起做"这一种跑法）；
    #   其余 → 两侧都还没做。这是本改动唯一的隐式约定，改口径前先看 parts_of()。
    _add_columns(c, "packages", (
        ("tag_status", "TEXT NOT NULL DEFAULT ''"),
        ("nl_status", "TEXT NOT NULL DEFAULT ''"),
        # 这一片是谁交的、哪一侧交的：查"缺 tag 的包都被谁拿走过"要靠它，
        # 只记在 events 里的话面板翻不动 40 万行的历史
        ("tag_by", "TEXT"),
        ("nl_by", "TEXT"),
    ))
    # 工作区归属（2026-11-13）。两级：
    #   users.workspace_id   = 这个账号的默认工作区
    #   devices.workspace_id = 单台设备的覆盖（NULL = 跟随账号）
    # 改成 NULL 就是"解除归属"，设备回到不限制的状态。老库补列后全是 NULL，
    # 语义与改造前逐字一致。
    _add_columns(c, "users", (("workspace_id", "TEXT"),))
    _add_columns(c, "devices", (("workspace_id", "TEXT"),))
    _add_columns(c, "devices", (("queue_only", "INTEGER NOT NULL DEFAULT 0"),))
    _add_columns(c, "workspaces", (("allow_manual_claim", "INTEGER NOT NULL DEFAULT 1"),))
    package_queue.ensure_schema(c)
    from assignments.annotation import ensure_schema as annotation_schema
    annotation_schema(c, __import__(__name__))
    offline_tasks.ensure_schema(c)
    from assignments.plans import ensure_schema as ensure_plans
    ensure_plans(c)
    execution_results.ensure_schema(c)
    scheduling_policies.ensure_schema(c, __import__(__name__))
    part_leases.ensure_schema(c, __import__(__name__))


def _add_columns(c: sqlite3.Connection, table: str, cols: tuple[tuple[str, str], ...]) -> None:
    """幂等地补列：表不存在（老库还没建主表）或列已存在都直接跳过。"""
    existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    if not existing:
        return
    for name, decl in cols:
        if name not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# ------------------------------------------------------------------ 工具

def _row(c: sqlite3.Connection, sql: str, args=()) -> dict | None:
    r = c.execute(sql, args).fetchone()
    return dict(r) if r else None


def _rows(c: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def _lease_view(c: sqlite3.Connection, lease: dict, p: dict | None = None) -> dict:
    """给客户端看的租约视图：带上下载所需的元数据。

    在线任务返回真实截止时间；离线派单继续由设备申报或管理员回收。
    """
    pkg = _row(c, "SELECT p.*, r.repo_type FROM packages p JOIN repos r ON r.repo=p.repo"
                  " WHERE p.repo=? AND p.id=?", (lease["repo"], lease["pkg"])) or {}
    now = time.time()
    held = max(0.0, now - lease["issued_at"])
    declare_h = (p or params())["declare_hours"]
    overdue_s = now - lease['expires_at'] if lease['kind'] in lease_lifetime.ONLINE_KINDS else held - declare_h * 3600
    return {
        "lease_id": lease["lease_id"],
        "kind": lease["kind"],
        "repo": lease["repo"],
        "package": lease["pkg"],
        "size": pkg.get("size"),
        "sha256": pkg.get("sha256"),
        "repo_type": pkg.get("repo_type", "dataset"),
        # 分项（SPEC-parts §5，只增）：这一片还欠什么，客户端据此只跑该跑的那一侧。
        # 老包 status='completed' 且分项为空 → 两侧都算齐，missing 为空。
        "parts": parts_of(pkg) if pkg else {"tag": "", "nl": ""},
        "missing_parts": [k for k in (missing_parts(pkg) if pkg else PARTS) if k in part_leases.parts(lease)],
        "assigned_parts": part_leases.parts(lease),
        "annotation": json.loads(lease.get("annotation_json") or "{}"),
        "resume_supported": True,
        "source_verification_supported": True,
        "attempt": lease["attempt"],
        "expires_in": round(max(0, lease['expires_at'] - now), 1) if lease['kind'] in lease_lifetime.ONLINE_KINDS else None,
        "expires_at": lease['expires_at'] if lease['kind'] in lease_lifetime.ONLINE_KINDS else None,
        "renewal_seconds": lease_lifetime.SECONDS if lease['kind'] in lease_lifetime.ONLINE_KINDS else None,
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


# ------------------------------------------------------------------ 分项产物
# 一个包有两个可以各自完成的产物：tag（分类头）与 NL（描述）。
# 机器能力不一样：2080Ti 这类没有稀疏计算的卡跑大张量 tag 模型很慢，
# 但跑 Q6 的 Qwen3-VL 反而还行——所以允许"只做 NL"。那一片的 tag 依然是
# 缺的，需要账面上分得清，否则整包标 completed 之后就永远没人去补。

PARTS = ("tag", "nl")
# 设备的分项意愿四值（2026-11-12 起，取代旧的 ["tag"] 数组写法）。
#   both       = 两侧都做（= 不表态，老客户端也是这个意思）
#   tag / nl   = **仅**这一侧，不含它的片不接（池子里没有就空转等）
#   pref-tag / pref-nl = 优先这一侧，另一侧也能做（兜底接）
# 与 worker.py 的 PARTS_CHOICES 必须逐字一致，否则握手会被 400 拒掉。
PARTS_CHOICES = ("both", "tag", "nl", "pref-tag", "pref-nl")
PART_DONE, PART_SKIP = "done", "skip"
# 只有 done 算"这一侧齐了"。skip 是"这台机器不干这一项"，包必须留在池子里
# 等别的机器来补——曾经把 skip 也当终止态，结果只做 nl 的机器一交，
# 整包就被判 completed，tag 永远空着且没人知道（本地验证第 4 条抓到的）。
_TERMINAL = (PART_DONE,)


def parts_of(row: dict) -> dict:
    """把一行 packages 归一成 {tag: '', nl: ''}。

    **空串有两层含义**，这是本改动唯一的"隐式约定"，务必按上下文区分：
      * 老库迁移上来的已完成包 = 两侧都齐（改造前只存在"一起做"这一种跑法）；
      * 新建的包 = 两侧都还没做。
    两者靠 status 区分：status='completed' 时''算完成，否则算未做。
    所以任何按分项筛选的查询都必须带上 status 条件，不能只看 tag_status。
    """
    st = row.get("status") or ""
    done = st == "completed"
    out = {}
    for k in PARTS:
        val = row.get(f"{k}_status") or ""
        if val == PART_SKIP and not done:
            # skip 是**某台机器的陈述**，它照样"回池"等人补（这一点不动）。
            # 但对外要说成"还没做"：对下一台设备来说，缺 tag 和
            # 「上一台跳过了 tag」没有区别，都是"这活还在"。写成 skip 会让
            # 本地已有的派工日志长出一期新协议版本——那是纯噪音。
            val = ""
        elif done and not val:
            val = PART_DONE
        out[k] = val
    return out


def missing_parts(row: dict) -> list[str]:
    """还欠哪几项。空列表 = 整包齐了。"""
    got = parts_of(row)
    return [k for k in PARTS if got[k] not in _TERMINAL]


def part_count_expr(alias: str = "p") -> str:
    """SQL 里的"欠 tag / 欠 nl"计数表达式。

    与 missing_parts 同一个口径，只是压到 SQL 里：40 万包不能拉进 Python 数。
    只有 'done' 算齐；**skip 也算欠**（这台机器不做 ≠ 做好了），
    所以表达式写 `<>'done'` 而不是 `<>''`——后者会把 skip 漏成已完成。
    """
    cond = (f"({alias}.tag_status='done' OR"
            f" ({alias}.status='completed' AND COALESCE({alias}.tag_status,'')=''))")
    cond2 = (f"({alias}.nl_status='done' OR"
             f" ({alias}.status='completed' AND COALESCE({alias}.nl_status,'')=''))")
    return {
        "missing_tag": f"CASE WHEN NOT {cond} THEN 1 ELSE 0 END",
        "missing_nl": f"CASE WHEN NOT {cond2} THEN 1 ELSE 0 END",
    }


def _clean_parts(raw) -> dict:
    """校验 finish 申报里的分项对象。

    只认 {"tag": "done"|"skip", "nl": "done"|"skip"}；未出现的键表示
    "本次没有对这一侧做出声明"（旧客户端整个字段都没有，走整包口径）。
    值非法直接 400——静默当成没交，会让一台配置写错的机器悄悄交半成品。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AuthError("parts 必须是 JSON 对象")
    out: dict = {}
    for key, val in raw.items():
        k = str(key).strip().lower()
        if k not in PARTS:
            raise AuthError(f"parts 里有未知分项：{key}（只认 {'/'.join(PARTS)}）")
        v = str(val or "").strip().lower()
        if v not in (PART_DONE, PART_SKIP):
            raise AuthError(f"parts.{k} 只能是 {'/'.join((PART_DONE, PART_SKIP))}，现在是 {val!r}")
        out[k] = v
    return out


def workspace_of(c: sqlite3.Connection, device: dict) -> dict | None:
    """这台设备**实际生效**的工作区；None = 不属于任何工作区（不限制仓库）。

    优先级：设备级覆盖 > 账号默认。两级都要是因为常见场景是"这个人的机器
    一般都做 A 组仓库，但其中一台专门留给 B 组"——只按账号绑就做不到，
    只按设备绑则每接一台新机器都要手动配。

    返回值里的 repos 是 list[str]。**空列表与 None 不是一回事**：
      None → 没有归属，能拿所有启用仓库的包（改造前的行为）
      []   → 属于一个空工作区，一个包都拿不到
    这个区分很重要：管理员建了工作区却忘了加仓库时，机器应该空转并看得见
    "你的工作区没有仓库"，而不是悄悄去抢别的仓库的活。
    """
    ws_id = (device.get("workspace_id") or "").strip()
    if not ws_id:
        row = _row(c, "SELECT workspace_id FROM users WHERE user_id=?",
                   (device.get("user_id"),))
        ws_id = ((row or {}).get("workspace_id") or "").strip()
    if not ws_id:
        group = scheduling_policies.group(c, device.get("user_id"))
        ws_id = (group["workspace_id"] or "") if group else ""
    if not ws_id:
        return None
    row = _row(c, "SELECT * FROM workspaces WHERE ws_id=?", (ws_id,))
    if row is None:
        # 工作区被删了但归属还挂着：同样不能退回"不限制"，否则这台机器会
        # 突然开始抢所有仓库的包，而管理员以为自己刚把它的活停了。
        return {"ws_id": ws_id, "name": "", "repos": [], "missing": True}
    try:
        repos = json.loads(row.get("repos") or "[]")
    except ValueError:
        repos = []
    if not isinstance(repos, list):
        repos = []
    row["repos"] = [str(r) for r in repos]
    row.pop("missing", None)
    return row


def repo_done(c: sqlite3.Connection, repo: str) -> bool:
    """这个仓库的包是不是全做完了（没有任何非 completed 的包）。

    0 包的仓库**不算做完**：它可能只是还没 sync。若把"没包"当成"做完"，
    工作区会一路跳到最后一个空仓库然后彻底没活干，看起来像是派工坏了。
    """
    row = c.execute("SELECT COUNT(*) n FROM packages WHERE repo=?", (repo,)).fetchone()
    if not row or not row["n"]:
        return False
    left = c.execute("SELECT COUNT(*) n FROM packages WHERE repo=? AND status<>'completed'",
                     (repo,)).fetchone()
    return not (left and left["n"])


def repo_ready(c: sqlite3.Connection, repo: str) -> bool:
    """有包且仓库是启用状态，才算"可以被派工"。"""
    row = c.execute("SELECT COUNT(*) n FROM packages WHERE repo=?", (repo,)).fetchone()
    if not row or not row["n"]:
        return False
    en = c.execute("SELECT enabled FROM repos WHERE repo=?", (repo,)).fetchone()
    return bool(en and en["enabled"])


def active_repo_of(c: sqlite3.Connection, ws: dict | None) -> str | None:
    """工作区当前该做哪个仓库：按工作区里登记的**顺序**，取第一个还没做完的。

    2026-11-13 口径（方案 A · 顺序推进）：
      - 同一时刻只派"当前仓库"的包，后面的仓库先不放开；
      - 当前仓库做完，自动顺延到列表里下一个**还没做完**的——
        遇到已经做完的（比如 3 号仓库被提前做完）直接跳过，所以是
        "1 完了去 2，2 也完了去 4"，不是简单的 +1；
      - 还没 sync 出包的仓库跳过（不然会卡在一个空仓库上）；
      - 全都没得做时返回 None，调用方据此派不到包。

    为什么不是"谁有包派谁"：那样多仓库会同时铺开，每台机器在几个仓库之间
    随机跳，进度不可预期，也没法回答"还剩多少包"。
    """
    if ws is None:
        return None
    for repo in ws.get("repos") or []:
        if not repo_ready(c, repo):
            continue
        if repo_done(c, repo):
            continue
        return repo
    return None


def workspace_brief(ws: dict | None) -> dict | None:
    """给客户端/面板看的工作区摘要（只出能公开的字段）。"""
    if ws is None:
        return None
    return {"ws_id": ws.get("ws_id"), "name": ws.get("name") or "",
            "repos": list(ws.get("repos") or []),
            "allow_manual_claim": bool(ws.get("allow_manual_claim", 1)),
            "allocation_mode": ws.get('allocation_mode', 'auto'),
            "missing": bool(ws.get("missing"))}


def workspace_view(c: sqlite3.Connection, device: dict) -> dict | None:
    """当前设备的实际执行仓库与生效优先级。"""
    ws = workspace_of(c, device)
    brief = workspace_brief(ws)
    if brief is not None:
        current = _row(c, "SELECT repo FROM leases WHERE device_id=? AND kind='active' AND status='open'", (device['device_id'],))
        order = scheduling_policies.repo_order(c, device, ws)
        brief['repos'] = order
        brief["active_repo"] = current['repo'] if current else None
    return brief


def _ws_id(raw) -> str:
    """工作区 id：只允许字母数字与 - _ .（它会出现在命令行和 URL 里）。"""
    ws_id = str(raw or "").strip()
    if not ws_id:
        raise AuthError("工作区 id 不能为空")
    if len(ws_id) > MAX_ID:
        raise AuthError(f"工作区 id 最长 {MAX_ID} 字符，现在是 {len(ws_id)}")
    if not all(ch.isalnum() or ch in "-_." for ch in ws_id):
        raise AuthError(f"工作区 id 只能用字母、数字与 - _ .，收到 {ws_id!r}")
    return ws_id


def _ws_repos(c: sqlite3.Connection, repos) -> list[str]:
    """校验工作区的仓库列表：每个都必须**已登记**（repo add 过）。

    为什么要求已存在：打错一个仓库名时，如果只是静默存下来，那台机器会
    空转到天荒地老，而管理员以为自己配好了。这一步报错能立刻发现。
    允许空列表（= 明确的工作区没配仓库），但不允许未知仓库。
    """
    if repos is None:
        return []
    if isinstance(repos, str):
        repos = [repos]
    out: list[str] = []
    for raw in repos:
        name = str(raw or "").strip()
        if not name:
            continue
        if _row(c, "SELECT 1 FROM repos WHERE repo=?", (name,)) is None:
            raise AuthError(f"仓库 {name!r} 还没登记（先跑 server.py repo add）")
        if name not in out:
            out.append(name)
    return out


@_atomic_policy
def ws_create(c: sqlite3.Connection, ws_id, name: str = "", repos=None,
              note: str = "", allow_manual_claim=None) -> dict:
    """新建工作区。同名再建一次是**更新仓库列表**（方便脚本重跑）。"""
    ws_id = _ws_id(ws_id)
    repo_list = _ws_repos(c, repos)
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    row = _row(c, "SELECT ws_id FROM workspaces WHERE ws_id=?", (ws_id,))
    if row is None:
        c.execute("INSERT INTO workspaces(ws_id,name,repos,note,created_at)"
                  " VALUES(?,?,?,?,?)",
                  (ws_id, str(name or ws_id), json.dumps(repo_list), note or None, now))
    else:
        c.execute("UPDATE workspaces SET name=?, repos=?, note=? WHERE ws_id=?",
                  (str(name or ws_id), json.dumps(repo_list), note or None, ws_id))
    if repo_list:
        c.execute('DELETE FROM repo_devices WHERE ws_id=? AND repo NOT IN (' + ','.join('?' for _ in repo_list) + ')', [ws_id, *repo_list])
        c.execute('DELETE FROM repo_users WHERE ws_id=? AND repo NOT IN (' + ','.join('?' for _ in repo_list) + ')', [ws_id, *repo_list])
    else:
        c.execute('DELETE FROM repo_devices WHERE ws_id=?', (ws_id,))
        c.execute('DELETE FROM repo_users WHERE ws_id=?', (ws_id,))
    if allow_manual_claim is not None:
        if type(allow_manual_claim) is not bool:
            raise AuthError("是否允许手动领取必须是布尔值")
        c.execute("UPDATE workspaces SET allow_manual_claim=? WHERE ws_id=?", (int(allow_manual_claim),ws_id))
    _reconcile_policy(c, __import__(__name__))
    return ws_get(c, ws_id)


def ws_get(c: sqlite3.Connection, ws_id) -> dict:
    """工作区详情，附带绑定的用户与设备（解绑/排查时要知道谁挂在上面）。"""
    ws_id = _ws_id(ws_id)
    row = _row(c, "SELECT * FROM workspaces WHERE ws_id=?", (ws_id,))
    if row is None:
        raise AuthError(f"没有这个工作区：{ws_id}")
    try:
        row["repos"] = json.loads(row.get("repos") or "[]")
    except ValueError:
        row["repos"] = []
    row["users"] = [r["user_id"] for r in
                    c.execute("SELECT user_id FROM users WHERE workspace_id=?"
                              " ORDER BY user_id", (ws_id,))]
    row["devices"] = [r["device_id"] for r in
                      c.execute("SELECT device_id FROM devices WHERE workspace_id=?"
                                " ORDER BY device_id", (ws_id,))]
    from scheduling.allocation import members
    row['repo_devices'] = members(c, ws_id)
    from scheduling.allocation import user_members
    row['repo_users'] = user_members(c, ws_id)
    return row


def ws_list(c: sqlite3.Connection) -> list[dict]:
    """所有工作区 + 各自挂了几台设备/几个账号。"""
    out = []
    for row in c.execute("SELECT ws_id FROM workspaces ORDER BY ws_id"):
        out.append(ws_get(c, row["ws_id"]))
    return out


@_atomic_policy
def ws_assign(c: sqlite3.Connection, ws_id, user_id: str | None = None,
              device_id: str | None = None) -> dict:
    """把一个账号或一台设备指到某个工作区。

    **不打断正在跑的包**：这里只改归属，派工下一次读到的就是新值，
    已经在跑的租约照旧做完——这就是"下一个包才切换"。
    不符合新归属的预锁释放，仍符合许可的指定与预约保留。
    """
    ws_id = _ws_id(ws_id)
    if _row(c, "SELECT 1 FROM workspaces WHERE ws_id=?", (ws_id,)) is None:
        raise AuthError(f"没有这个工作区：{ws_id}（先 workspace add）")
    if not user_id and not device_id:
        raise AuthError("要指定 --user 或 --device")
    if device_id:
        if _row(c, "SELECT 1 FROM devices WHERE device_id=?", (device_id,)) is None:
            raise AuthError(f"没有这台设备：{device_id}")
        c.execute("UPDATE devices SET workspace_id=? WHERE device_id=?", (ws_id, device_id))
    if user_id:
        if _row(c, "SELECT 1 FROM users WHERE user_id=?", (user_id,)) is None:
            raise AuthError(f"没有这个账号：{user_id}")
        c.execute("UPDATE users SET workspace_id=? WHERE user_id=?", (ws_id, user_id))
    _reconcile_policy(c, __import__(__name__))
    return ws_get(c, ws_id)


@_atomic_policy
def ws_unassign(c: sqlite3.Connection, user_id: str | None = None,
                device_id: str | None = None) -> str:
    """解除归属，回到"不限制仓库"。传 --device 只解设备级覆盖。"""
    if not user_id and not device_id:
        raise AuthError("要指定 --user 或 --device")
    n = 0
    if device_id:
        cur = c.execute("UPDATE devices SET workspace_id=NULL WHERE device_id=?",
                        (device_id,))
        n += cur.rowcount or 0
    if user_id:
        cur = c.execute("UPDATE users SET workspace_id=NULL WHERE user_id=?", (user_id,))
        n += cur.rowcount or 0
    _reconcile_policy(c, __import__(__name__))
    return f"已解除 {n} 处归属（回归不限制仓库）"


@_atomic_policy
def ws_remove(c: sqlite3.Connection, ws_id) -> str:
    """删除工作区，并把挂在它上面的账号/设备一起解绑。

    不删的话会留下一堆指向不存在工作区的归属——那种状态派不到任何包
    （见 workspace_of 的 missing 分支），机器会静默空转。
    """
    ws_id = _ws_id(ws_id)
    if _row(c, "SELECT 1 FROM workspaces WHERE ws_id=?", (ws_id,)) is None:
        raise AuthError(f"没有这个工作区：{ws_id}")
    u = c.execute("UPDATE users SET workspace_id=NULL WHERE workspace_id=?",
                  (ws_id,)).rowcount or 0
    d = c.execute("UPDATE devices SET workspace_id=NULL WHERE workspace_id=?",
                  (ws_id,)).rowcount or 0
    c.execute('UPDATE user_groups SET workspace_id=NULL WHERE workspace_id=?', (ws_id,))
    c.execute("DELETE FROM workspaces WHERE ws_id=?", (ws_id,))
    c.execute('DELETE FROM repo_devices WHERE ws_id=?', (ws_id,))
    c.execute('DELETE FROM repo_users WHERE ws_id=?', (ws_id,))
    _reconcile_policy(c, __import__(__name__))
    return f"已删除 {ws_id}，同时解绑 {u} 个账号、{d} 台设备"


def _wanted_parts(device: dict) -> list[str]:
    """这台设备**能产出**哪几侧。空列表 = 没声明，按全做算（老客户端）。

    兼容两种历史写法，都归一成 ["tag"] / ["nl"] / []：
      * 旧数组：`[]` / `["nl"]` / `["tag"]` / `["tag","nl"]`；
      * 新四值：`both` / `tag` / `nl` / `pref-tag` / `pref-nl`。
    pref-* 的"能做"是两侧都行，它只是**优先**某一侧，见 _device_pref。
    """
    raw = device.get("parts")
    if isinstance(raw, str):
        text = raw.strip().lower()
        if not text:
            return []
        if not text.startswith("["):
            # 四值枚举（2026-11-12 起的新写法）
            if text in ("both", "pref-tag", "pref-nl"):
                return []
            if text in PARTS:
                return [text]
            return []
        try:
            raw = json.loads(text)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    got = [str(x).strip().lower() for x in raw]
    if not got or "both" in got:
        return []
    return [x for x in got if x in PARTS]


def _device_pref(device: dict) -> str:
    """这台设备**优先**要哪一侧：返回 "tag" / "nl" / ""（不挑）。

    与 _wanted_parts 的区别只在"另一侧怎么办"：
      * 仅 nl   → 能做的只有 nl，缺 tag 的片它根本不接；
      * 优先 nl → 能做两侧，但缺 nl 的片排在前面。
    """
    raw = device.get("parts_pref")
    if raw in ("pref-tag", "pref-nl", "tag", "nl"):
        return raw.removeprefix("pref-")
    raw = device.get("parts")
    if not isinstance(raw, str):
        return ""
    text = raw.strip().lower()
    if text == "pref-tag":
        return "tag"
    if text == "pref-nl":
        return "nl"
    if text in ("tag", "nl"):
        return text
    return ""


def _part_rank(row: dict, want: list[str], pref: str) -> int:
    """这一个包对这台设备的"对口程度"，越小越优先。

    0 = 只缺我优先（或唯一能做）的那一侧 —— 最该给它；
    1 = 两侧都缺，里面有我要做的 —— 至少能把我这侧做掉；
    2 = 只缺我不做的那一侧 —— 对它毫无价值；
    3 = 什么都不缺（已完成，正常不会进候选）。
    """
    miss = set(missing_parts(row))
    if not miss:
        return 3
    mine = set(want) if want else set(PARTS)
    if pref:
        if miss == {pref}:
            return 0
        if pref in miss:
            return 1 if len(miss) > 1 else 0
        return 2
    if miss == mine:
        return 0
    if miss & mine:
        return 1
    return 2


def normalize_parts(raw) -> str:
    """把设备申报的分项意愿归一成四值字符串，落库用。

    数组与字符串两种写法都收（见 MODULE 里的 SPEC-parts 说明）：
      ["nl"] / ["tag"] / ["tag","nl"] / []  →  "nl" / "tag" / "both" / "both"
      "pref-nl" 等四值                        →  原样
    认不出来的一律 "both"（= 都做）：这比默认"什么都不做"安全得多，
    后者会让一台配置写错的机器永远领不到活，而且看不出原因。
    """
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in PARTS_CHOICES:
            return text
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except ValueError:
                return "both"
        else:
            return "both"
    if isinstance(raw, list):
        got = {str(x).strip().lower() for x in raw}
        if "both" in got or not got or got >= set(PARTS):
            return "both"
        if got == {"nl"}:
            return "nl"
        if got == {"tag"}:
            return "tag"
    return "both"


def _tier_for(speed: float) -> str:
    """按 6000 张/片的 ETA 分档：<12h 快、<30h 中、其余慢。

    ★**只用于展示，不参与任何派工决策**（2026-11-12 起）。
      派工与预锁都已改成只看"这片缺什么"和"手上有没有活"。这个字段留着
      是因为面板与 `server.py` 的设备列表要显示"这台快不快"，便于人判断
      该把活派给谁——但代码不再据此排序或限制。别把它接回决策路径。
    """
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
            from scheduling.identities import rebind
            rebind(c, __import__(__name__), device_id, user_id)
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
                from scheduling.identities import rename
                rename(c, __import__(__name__), owner, user_id)
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
                   local_pkgs: list[str] | None, hw: dict | None = None,
                   parts: object = None) -> tuple[dict, str | None]:
    """登记/更新设备。返回 (设备行, 改绑前的旧用户；没有改绑则为 None)。

    改绑（device_id 换用户）**不再一律拒绝**：一台机器换账号的常见原因是
    user_id 打错、多人共用机器、旧 config 没清干净，而不是"公用马甲"。真正
    需要拦的只有一种情况——设备手上还有 open 租约，那说明它正在替旧用户干活，
    这时改绑会把这批包的归属搅乱。没有在跑的包就允许改，历史租约与审计不动。

    parts 是设备自述的"愿意做哪几项"（SPEC-parts §2）。传 None = 本次没表态，
    保留原值；传空数组 / "both" = 明确"都做"。

    ★存储一律归一成四值字符串。旧的数组写法要落成 "tag"/"nl"/"both"——
      **不能直接 json.dumps 数组**：老代码 `sorted(set(["nl"]))` 对字符串会拆成
      字符（'n','l'），一台机器申报 pref-nl 会变成 ['-','e','f','l','n','p','r']。
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
                  "local_pkgs,gpu_name,gpu_vram_mb,mode,hw_report_at,parts)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (device_id, user_id, now, now, client_ver,
                   json.dumps(local_pkgs or []),
                   hw.get("gpu_name") if hw else None,
                   hw.get("gpu_vram_mb") if hw else None,
                   hw.get("mode") if hw else None,
                   now if hw is not None else None,
                   normalize_parts(parts)))
    else:
        if row["user_id"] != user_id:
            busy = c.execute("SELECT COUNT(*) n FROM leases WHERE device_id=? AND status='open'",
                             (device_id,)).fetchone()["n"]
            if busy:
                raise AuthError(
                    f"设备 {device_id} 还挂着用户 {row['user_id']} 的 {busy} 个在跑的包，"
                    f"不能直接改绑到 {user_id}；请先在面板「强制解绑」这台设备")
            rebound_from = row["user_id"]
            from scheduling.identities import rebind
            rebind(c, __import__(__name__), device_id)
        sets = ["last_seen=?", "client_ver=?"] + hw_sets
        args: list = [now, client_ver] + hw_args
        if rebound_from is not None:
            sets.append("user_id=?")            # 改绑：旧用户的租约/审计一条不动
            args.append(user_id)
        if local_pkgs is not None:
            sets.append("local_pkgs=?")
            args.append(json.dumps(sorted(set(local_pkgs))))
        if parts is not None:
            sets.append("parts=?")
            args.append(normalize_parts(parts))
        sets.append("status='online'")          # 归档设备重新上线
        args.append(device_id)
        c.execute(f"UPDATE devices SET {', '.join(sets)} WHERE device_id=?", args)
    return _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,)), rebound_from


# ---------------------------------------------------------------- 在线租约逾期计算


def overdue_leases(c: sqlite3.Connection, p: dict | None = None, now: float | None = None) -> list[dict]:
    """尚未被后台回收的到期在线租约；持续正常上报不会标成逾期。"""
    p = p or params()
    now = time.time() if now is None else now
    rows = _rows(c, "SELECT * FROM leases WHERE status='open' AND kind IN ('active','prelock') AND expires_at<=?"
                    " ORDER BY expires_at", (now,))
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
            "overdue_seconds": round(now - l['expires_at'], 1),
            "fuse_exceeded": held >= max_age,
        })
    return out


# ------------------------------------------------------------------ 分配

def _busy_keys(c: sqlite3.Connection) -> set[tuple[str, str]]:
    busy = {(r["repo"], r["pkg"]) for r in c.execute("SELECT repo,pkg FROM leases WHERE status='open'")}
    busy.update((r["repo"], r["package"]) for r in c.execute(
        "SELECT h.repo,h.package FROM history_checks h JOIN managed_sessions s ON s.session_id=h.session_id"
        " WHERE h.status='pending' AND s.released=0 AND s.last_seen>=?", (time.time() - 30,)))
    busy.update((r['repo'], r['pkg']) for r in c.execute("SELECT s.repo,s.pkg FROM source_submissions s JOIN packages p"
                                                       " ON p.repo=s.repo AND p.id=s.pkg AND p.sha256=s.source_sha256 WHERE s.state='pending'"))
    busy.update((r['repo'], r['package']) for r in c.execute("SELECT repo,package FROM history_checks WHERE status='verifying'"))
    return busy


def _free_count(c: sqlite3.Connection) -> int:
    total = c.execute(
        "SELECT COUNT(*) n FROM packages p WHERE p.status='available' OR"
        " (p.status='claimed' AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.repo=p.repo"
        "  AND l.pkg=p.id AND l.status='open'))").fetchone()["n"]
    busy = len(_busy_keys(c))
    return max(0, total - busy)


def _pick_package(c: sqlite3.Connection, device: dict, p: dict | None = None,
                  exclude: set | None = None) -> dict | None:
    return scheduling_selection.pick(c, __import__(__name__), device, p, exclude)


def _open_lease(c: sqlite3.Connection, device_id: str, pkg: dict, kind: str,
                attempt: int) -> dict:
    """开租约；在线任务默认 48 小时，离线任务保留独立申报语义。"""
    now = time.time()
    parts = pkg.get("dispatch_parts")
    if kind != 'offline':
        from scheduling.allocation import allowed
        target_device = _row(c, 'SELECT * FROM devices WHERE device_id=?', (device_id,))
        if not target_device or not allowed(c, __import__(__name__), target_device, pkg['repo']):
            raise LeaseError('设备未获仓库分配')
    if parts is None:
        device = _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,)) or {}
        parts = [k for k in missing_parts(pkg) if k in (_wanted_parts(device) or PARTS)]
    from delivery.conflicts import blocked
    unavailable = part_leases.held(c).get((pkg['repo'], pkg['id']), set()) | blocked(c).get((pkg['repo'], pkg['id']), set())
    if not parts or set(parts) & unavailable:
        raise LeaseError("该分项正在执行或检查中")
    lease_id = f"L{secrets.token_hex(6)}"
    from assignments.annotation import for_package as annotation_for_package
    annotation = annotation_for_package(c, pkg, parts)
    fuse_horizon = now + (lease_lifetime.SECONDS if kind in lease_lifetime.ONLINE_KINDS
                          else params()["lease_max_h"] * 3600)
    try:
        c.execute("INSERT INTO leases(lease_id,repo,pkg,user_id,device_id,kind,attempt,status,"
                  "issued_at,expires_at,last_beat_at,last_progress_at,parts_json,annotation_json)"
                  " VALUES(?,?,?,?,?,?,?,'open',?,?,?,?,?,?)",
                  (lease_id, pkg["repo"], pkg["id"], _device_user(c, device_id), device_id,
                   kind, attempt, now, fuse_horizon, now, now, json.dumps(parts), json.dumps(annotation)))
    except sqlite3.IntegrityError:
        # AFTER INSERT 的分项唯一冲突不能遗留没有占用的父租约。
        c.execute('DELETE FROM leases WHERE lease_id=?', (lease_id,))
        raise
    if kind == "active":
        c.execute("UPDATE packages SET status='claimed', owner=?, claimed_at=?,"
                  " updated_at=? WHERE repo=? AND id=? AND status IN ('available','claimed')",
                  (_device_user(c, device_id), time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   time.strftime("%Y-%m-%dT%H:%M:%S%z"), pkg["repo"], pkg["id"]))
    from source_catalog.store import require as prepare_source
    prepare_source(c, pkg['repo'], pkg['id'], pkg['sha256'], pkg['size'])
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
    part_leases.release_offline(c)
    active = _row(c, "SELECT * FROM leases WHERE device_id=? AND kind='active' AND status='open'",
                  (device["device_id"],))
    if active is None:
        # 手上没有 active：优先把已有的 prelock 升上来，否则新派一个
        pre = _row(c, "SELECT * FROM leases WHERE device_id=? AND kind='prelock' AND status='open'",
                   (device["device_id"],))
        if pre and not scheduling_selection.valid_prelock(c, __import__(__name__), device, pre):
            c.execute("UPDATE leases SET status='released',closed_reason='policy_changed' WHERE lease_id=?", (pre['lease_id'],))
            pre = None
        if pre is not None:
            c.execute("UPDATE leases SET kind='active' WHERE lease_id=?", (pre['lease_id'],))
            c.execute("UPDATE packages SET status='claimed', owner=?, claimed_at=?, updated_at=?"
                      " WHERE repo=? AND id=? AND status IN ('available','claimed')",
                      (device["user_id"], time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       time.strftime("%Y-%m-%dT%H:%M:%S%z"), pre["repo"], pre["pkg"]))
            active = _row(c, "SELECT * FROM leases WHERE lease_id=?", (pre["lease_id"],))
        else:
            active = _issue(c, device, p, "active", on_open)

    # prelock：库存充足 + 手上确有在跑的包才发。
    # ★不再看快慢（tier）。原条件是 fast/mid/unknown 才预锁，等于让慢机
    #   "一次只能有一片"，而 slow 机器的判定本身还取决于 EWMA 速度样本够不够
    #   ——样本少时算 unknown，于是同一台机器时而预锁、时而不预锁，行为不可预测。
    #   用户口径：只要它手上还有 active（=正在干），就提前把下一片下载好，
    #   这样这一片跑完能立刻接上，不用等下载。
    free = _free_count(c)
    threshold = max(p["quota_min"], int(c.execute(
        "SELECT COUNT(*) n FROM packages").fetchone()["n"] * p["quota_pct"]))
    pre = _row(c, "SELECT * FROM leases WHERE device_id=? AND kind='prelock' AND status='open'",
               (device["device_id"],))
    if pre is None and free > threshold and active is not None:
        _issue(c, device, p, "prelock", on_open)
    return active


# ------------------------------------------------------------------ 入口

def _handshake_impl(c: sqlite3.Connection, p: dict, body: dict, on_open=None,
              on_event=None) -> dict:
    part_leases.release_offline(c)
    if not isinstance(body, dict):
        raise AuthError("请求体必须是 JSON 对象")
    user_id = _sid(body.get("user_id"), "user_id")
    secret = body.get("secret")
    if secret is not None and (not isinstance(secret, str) or len(secret) > MAX_SECRET):
        raise AuthError("secret 类型或长度非法")
    raw_dev = body.get("device_id")
    device_id = _sid(raw_dev, "device_id") if raw_dev else f"D{secrets.token_hex(6)}"
    if c.execute("SELECT 1 FROM offline_jobs WHERE device_id=?",(device_id,)).fetchone():
        raise AuthError("离线分配身份只用于回传，请使用本机身份连接中心")
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
    # 可选 parts（SPEC-parts §2，只增）：这台机器愿意做哪几项。
    # **服务端不按硬件指定**——谁跑得快只有那台机器自己知道（2080Ti 跑 tag 慢、
    # 跑 Q6 的 Qwen3-VL 反而行），这里只收它的自述，当派工排序的依据。
    #
    # 两种写法都收（2026-11-12 起新写法是四值字符串，旧数组继续兼容）：
    #   数组  ["nl"] / ["tag"] / []           ← v3.3 及以前
    #   字符串 both|tag|nl|pref-tag|pref-nl   ← 现在
    # 非法值仍然 400：静默当成"都做"会让一台配错的机器默默拿错片。
    parts_want = body.get("parts")
    if parts_want is not None:
        if isinstance(parts_want, str):
            key = parts_want.strip().lower()
            if key not in PARTS_CHOICES:
                raise AuthError(f"parts 只能是 {'/'.join(PARTS_CHOICES)}，"
                                f"收到 {parts_want!r}")
        elif isinstance(parts_want, list):
            bad = [x for x in parts_want if str(x) not in PARTS]
            if bad:
                raise AuthError(f"parts 里有未知分项：{bad}（只认 {'/'.join(PARTS)}）")
        else:
            raise AuthError("parts 必须是数组（'tag' / 'nl'）"
                            f"或其中之一 {'/'.join(PARTS_CHOICES)}")
    previous_device = _row(c, 'SELECT parts FROM devices WHERE device_id=?', (device_id,))
    device, rebound_from = _upsert_device(c, user_id, device_id, client_ver, local_pkgs, hw,
                                          parts_want)
    if isinstance(parts_want, str):
        c.execute('UPDATE devices SET parts_pref=? WHERE device_id=?', (parts_want, device_id))
        device['parts_pref'] = parts_want
    if rebound_from is not None or (previous_device and previous_device['parts'] != device['parts']):
        _reconcile_policy(c, __import__(__name__))
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
    # 握手成功续期有效在线租约；已到期租约由分配前清理回收。
    if "register_only" in body and type(body["register_only"]) is not bool:
        raise AuthError("register_only 必须为布尔值")
    active = None if body.get("register_only") else assign(c, device, p, on_open=on_open)
    lease_lifetime.renew(c, device_id)
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
        # 池子余量（SPEC-parts §7，只增）：客户端一握手就有一份数，不用等下一个 ping
        "parts": parts_summary(c),
        # 工作区（2026-11-13，只增）：null = 无归属（所有仓库都能拿）。
        # 客户端拿它显示"我只做哪几个仓库"，null 与空数组要分开显示——
        # 前者是"没限制"，后者是"配了工作区但里面没有仓库"，后者必然没活干。
        "workspace": workspace_view(c, device),
    }


def _heartbeat_impl(c: sqlite3.Connection, p: dict, body: dict, on_open=None,
              on_event=None) -> dict:
    """确认开放在线租约，并从本次成功上报重新计时 48 小时。"""
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
    now = time.time()
    lease_lifetime.expire(c, now)
    c.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (now, device_id))
    renew, rejected = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        lease_id = item.get("lease_id")
        if not isinstance(lease_id, str):
            continue
        lease = _row(c, "SELECT status,device_id FROM leases WHERE lease_id=?", (lease_id,))
        if lease is not None and lease["status"] == "open" and lease["device_id"] == device_id:
            renew.append(lease_id)
            if isinstance(item.get('progress'), dict):
                from managed.progress import update
                update(c, __import__(__name__), device, lease_id, item['progress'])
        else:
            rejected.append(lease_id)
    lease_lifetime.renew(c, device_id, now, renew)
    open_leases = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'",
                        (device_id,))
    return {
        "renew": renew,
        "rejected": rejected,
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
    _require_device(c, _sid(body.get('user_id'), 'user_id'), device_id, body.get('secret'))
    if lease["kind"] == "offline":
        offline_tasks.authenticate(c,__import__(__name__),lease,body.get("secret"))
        if body.get("user_id") != lease["user_id"]:
            raise AuthError("离线任务用户不匹配")
    if ok and lease['status'] in ('done', 'submitted'):
        current = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'", (device_id,))
        return {'ok': True, 'replay': True, 'verification_pending': lease['status'] == 'submitted',
                'leases': [_lease_view(c, l, p) for l in current],
                'lease_id': next((l['lease_id'] for l in current if l['kind'] == 'active'), None)}
    if lease["status"] != "open":
        raise LeaseError(f"租约已 {lease['status']}（租约已关闭：可能已被管理员释放或已申报），拒绝处理")
    if lease_lifetime.expired(lease):
        raise LeaseError('租约已连续 48 小时未上报而过期，请重新领取后续做')

    now = time.time()
    # 申报制（SPEC-declare §8.1，契约只增）：finish 请求可带最终 progress——
    # Beater 拆除后面板租约进度停在最后已知值，worker 在申报时一次性补齐。
    # 仅记录，不参与任何判定。
    progress = body.get("progress")
    progress = clean_progress(progress) if isinstance(progress, dict) else {}
    # 分项申报（SPEC-parts §4，契约只增）：{"tag":"done"|"skip","nl":...}。
    # 旧客户端不带结果时，按已登记的单项能力落账；全做设备保留整包口径。
    parts = _clean_parts(body.get("parts"))
    if ok and not parts:
        parts = {part: "done" for part in part_leases.parts(lease)}
    if set(parts) - set(part_leases.parts(lease)):
        raise LeaseError("不能提交未分配的分项")
    proof = c.execute('SELECT report_json FROM package_artifacts a JOIN packages p'
                      ' ON a.repo=p.repo AND a.pkg=p.id AND a.source_sha256=p.sha256'
                      ' WHERE a.repo=? AND a.pkg=?', (lease['repo'], lease['pkg'])).fetchone()
    report = json.loads(proof['report_json']) if proof else None
    verified = set(report['complete_parts']) if report else None
    from source_catalog.store import config as source_config
    if ok and source_config(c, lease['repo'])['enabled'] and not proof:
        raise LeaseError('此仓库需要上传逐图产物后才能登记完成')
    pending = bool(report and report.get('source_verification', {}).get('state') in ('pending', 'failed'))
    if ok and not pending and verified is not None and any(v == 'done' and k not in verified for k, v in parts.items()):
        raise LeaseError('逐图验收仍有失败、漏项或冲突，不能登记分项完成')
    if ok and lease["kind"] == "prelock":
        # 预锁只是"下一片的预约"，从没开工过；直接 done 会把一个没处理过的包
        # 标成已完成（数据永久丢失且没人发现）。必须拒绝。
        raise LeaseError("预锁尚未开工，不能直接完成；请先完成当前 active 租约")

    execution_results.record(c, lease_id, body, ok)
    if ok and pending and lease['kind'] in ('active', 'offline'):
        from source_catalog.revalidation import defer
        package = _row(c, 'SELECT * FROM packages WHERE repo=? AND id=?', (lease['repo'], lease['pkg']))
        defer(c, lease, package, body)
        part_leases.refresh(c, lease['repo'], lease['pkg'])
        active = assign(c, device, p, on_open=on_open) if lease['kind'] != 'offline' else None
        current = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'", (device_id,))
        return {'ok': True, 'verification_pending': True, 'leases': [_lease_view(c, l, p) for l in current],
                'lease_id': active['lease_id'] if active else None}
    if ok:
        c.execute("DELETE FROM part_assignments WHERE repo=? AND pkg=? AND part IN"
                  " (SELECT value FROM json_each(?))", (lease['repo'],lease['pkg'],lease['parts_json']))
    c.execute("DELETE FROM package_queue WHERE repo=? AND pkg=? AND device_id=?",
              (lease["repo"],lease["pkg"],device_id))

    if ok:
        c.execute("UPDATE leases SET status='done', closed_reason='done' WHERE lease_id=?",
                  (lease_id,))
        if parts:
            # 带分项：只把本次声明过的分项按 done/skip 落库，**没声明的分项
            # 保持原样**——一台只做 nl 的机器不该把别人已经做好的 tag 抹掉。
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            for k in PARTS:
                if k not in parts:
                    continue
                c.execute(f"UPDATE packages SET {k}_status=?, {k}_by=?, updated_at=?"
                          " WHERE repo=? AND id=?",
                          (parts[k], lease["device_id"], now_iso, lease["repo"], lease["pkg"]))
            row = _row(c, "SELECT * FROM packages WHERE repo=? AND id=?",
                       (lease["repo"], lease["pkg"])) or {}
            if not missing_parts(row):
                # 两侧都齐了才算整包完成：面板的"进行中/剩余"仍按 status 看，
                # 单侧完成的包必须留在可派工状态里，否则没人会来补另一侧。
                c.execute("UPDATE packages SET status='completed', completed_at=?, note=?"
                          " WHERE repo=? AND id=?",
                          (now_iso, (body.get("stats") or "")[:STATS_NOTE_LIMIT],
                           lease["repo"], lease["pkg"]))
            else:
                c.execute("UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                          " downloaded_at=NULL, note=? WHERE repo=? AND id=?",
                          ((body.get("stats") or "")[:STATS_NOTE_LIMIT], lease["repo"], lease["pkg"]))
                if on_event:
                    left = ",".join(missing_parts(row))
                    on_event(c, "v3_partial", lease["user_id"], lease["device_id"],
                             f"repo={lease['repo']}; pkg={lease['pkg']};"
                             f" did={','.join(parts) or '-'}; missing={left}")
        else:
            # 旧口径：整包完成。两侧都记 done，免得以后分项统计把它当成缺项。
            c.execute("UPDATE packages SET status='completed', completed_at=?, note=?, updated_at=?,"
                      " tag_status=CASE WHEN tag_status='' THEN 'done' ELSE tag_status END,"
                      " nl_status=CASE WHEN nl_status='' THEN 'done' ELSE nl_status END"
                      " WHERE repo=? AND id=?",
                      (time.strftime("%Y-%m-%dT%H:%M:%S%z"), (body.get("stats") or "")[:STATS_NOTE_LIMIT],
                       time.strftime("%Y-%m-%dT%H:%M:%S%z"), lease["repo"], lease["pkg"]))
    else:
        c.execute("UPDATE leases SET status='failed', closed_reason=? WHERE lease_id=?",
                  (str(body.get("reason") or "failed")[:100], lease_id))
        if lease["kind"] in ("active", "offline"):
            c.execute("UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                      " downloaded_at=NULL, updated_at=? WHERE repo=? AND id=?",
                      (time.strftime("%Y-%m-%dT%H:%M:%S%z"), lease["repo"], lease["pkg"]))
        # 部分失败仍保留这一轮已通过验收的完整侧，另一侧继续派工。
        for part in set(part_leases.parts(lease)) & (verified or set()):
            c.execute(f'UPDATE packages SET {part}_status=\'done\', {part}_by=? WHERE repo=? AND id=?',
                      (device_id, lease['repo'], lease['pkg']))
    # skip 不算完成：它只是"这台机器不做这一项"，包仍留在池子里等合适的机器。
    if on_event:
        on_event(c, "v3_done" if ok else "v3_fail", lease["user_id"], lease["device_id"],
                 ("parts=" + ",".join(f"{k}:{v}" for k, v in sorted(parts.items()))
                  if parts else ""))
    # 最终进度随申报落账（若客户端带了 progress 字段；旧客户端不带则保留原值）
    c.execute("UPDATE leases SET progress_json=?, last_progress_at=? WHERE lease_id=?",
              (json.dumps(progress, ensure_ascii=False), now, lease_id))
    part_leases.refresh(c, lease["repo"], lease["pkg"])
    if lease["kind"] == "offline":
        offline_tasks.refresh(c,device_id)
        return {"ok":True,"leases":[],"lease_id":None}
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
        from scheduling.allocation import forget
        forget(c, user_id=user_id)
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
        if lease["kind"] in ("active", "offline"):
            c.execute("UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                      " downloaded_at=NULL, updated_at=? WHERE repo=? AND id=?",
                      (time.strftime("%Y-%m-%dT%H:%M:%S%z"), lease["repo"], lease["pkg"]))
        if on_event:
            on_event(c, "v3_release_device", lease["user_id"], device_id)
    c.execute("UPDATE devices SET status='offline' WHERE device_id=?", (device_id,))
    package_queue.clear_device(c,device_id)
    offline_tasks.refresh(c,device_id)
    return {"released": len(leases), "device_id": device_id}


def prune_devices(c: sqlite3.Connection, days: float = 30, on_event=None) -> dict:
    """归档长期没心跳、且手上没有 open 租约的设备（只改状态，不删数据）。"""
    cutoff = time.time() - days * 86400
    rows = _rows(c, "SELECT d.device_id FROM devices d WHERE d.last_seen<?"
                    " AND d.status!='archived' AND NOT EXISTS (SELECT 1 FROM leases l"
                    " WHERE l.device_id=d.device_id AND l.status='open')", (cutoff,))
    for row in rows:
        c.execute("UPDATE devices SET status='archived' WHERE device_id=?", (row["device_id"],))
        package_queue.clear_device(c,row["device_id"])
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
    user_id = _sid(body.get('user_id'), 'user_id')
    device = _row(c, "SELECT * FROM devices WHERE device_id=?", (device_id,))
    if device is None:
        raise AuthError("设备未登记")
    lease = _row(c, "SELECT * FROM leases WHERE lease_id=?", (lease_id,))
    if lease is None or lease["device_id"] != device_id:
        raise LeaseError("租约不存在或不属于这台设备")
    if device['user_id'] != user_id or lease['user_id'] != user_id:
        raise AuthError('租约与当前用户不匹配')
    _require_device(c, user_id, device_id, body.get('secret'))
    if body.get('expected_kind') and lease['kind'] != body['expected_kind']:
        raise LeaseError('预取分片已转为当前任务，请刷新后使用停止并释放')
    if body.get('reason') == 'user_release':
        part_leases.release_targets(c, lease)
    if lease['kind'] == 'active' and body.get('reason') == 'user_release':
        from managed.automation import intent
        intent(c, device_id, 'stop', reason='manual')
    if lease['status'] != 'open':
        left = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'", (device_id,))
        return {'ok': True, 'replay': True, 'released': lease_id, 'leases': [_lease_view(c, l) for l in left]}
    if lease["kind"] == "offline":
        offline_tasks.authenticate(c,__import__(__name__),lease,body.get("secret"))
    c.execute("UPDATE leases SET status='released', closed_reason=? WHERE lease_id=?",
              (str(body.get("reason") or "client_release")[:60], lease_id))
    if lease["kind"] in ("active", "offline"):
        c.execute("UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                  " downloaded_at=NULL, updated_at=? WHERE repo=? AND id=? AND status='claimed'",
                  (time.strftime("%Y-%m-%dT%H:%M:%S%z"), lease["repo"], lease["pkg"]))
    if on_event:
        on_event(c, "v3_release", lease["user_id"], device_id)
    c.execute("DELETE FROM package_queue WHERE repo=? AND pkg=? AND device_id=?",
              (lease["repo"],lease["pkg"],device_id))
    offline_tasks.refresh(c,device_id)
    left = _rows(c, "SELECT * FROM leases WHERE device_id=? AND status='open'", (device_id,))
    return {"ok": True, "released": lease_id,
            "leases": [_lease_view(c, l) for l in left]}


def _ping_impl(c: sqlite3.Connection, body: dict) -> dict:
    """成功 ping 更新在线状态、进度并续期有效在线租约，不重新分配任务。"""
    if not isinstance(body, dict):
        raise AuthError("请求体必须是 JSON 对象")
    user_id = _sid(body.get("user_id"), "user_id")
    device_id = _sid(body.get("device_id"), "device_id")
    secret = body.get("secret")
    if secret is not None and (not isinstance(secret, str) or len(secret) > MAX_SECRET):
        raise AuthError("secret 类型或长度非法")
    device = _require_device(c, user_id, device_id, secret)
    lease_lifetime.expire(c)
    lease_lifetime.renew(c, device_id)
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
    # 顺手回一份池子余量（SPEC-parts §7，契约只增）：客户端的调度中心顶部要显示
    # "还剩多少片、缺 tag 多少、缺 nl 多少"，而 ping 本来就是最小流量的周期拍，
    # 再开一个端点要多吃一次限流配额。
    return {"ok": True,
            "status": derived_status(last["last_seen"] if last else None),
            "tier": device["tier"],
            "parts": parts_summary(c)}


def device_json_progress(c: sqlite3.Connection, device_id: str) -> str | None:
    """该设备 open active 租约的当前进度快照（EWMA 增量判定的前值来源）。"""
    row = c.execute("SELECT progress_json FROM leases WHERE device_id=? AND kind='active'"
                    " AND status='open' ORDER BY issued_at DESC LIMIT 1", (device_id,)).fetchone()
    return row["progress_json"] if row else None


def ping(c: sqlite3.Connection, body: dict) -> dict:
    with _Tx(c):
        return _ping_impl(c, body)


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
        "status": derived_status(device.get("last_seen"), now),
        "last_seen": device["last_seen"],
        "last_seen_ago": round(now - (device["last_seen"] or now), 1),
        "renewal_seconds": lease_lifetime.SECONDS,
        "leases": [_lease_view(c, l, p) for l in leases],
        # 池子余量（SPEC-parts §7，只增）：本机 Web 的「调度中心」顶部要显示
        # "总剩余 / 缺 NL / 缺 TAG"，它本来就在拉 status，顺带带回去，不另开请求。
        "parts": parts_summary(c),
        # 工作区（2026-11-13，只增）：worker.py show 要能回答"我为什么没活干"。
        # handshake 已经带这份，status 是只读入口，同样不能缺。
        "workspace": workspace_view(c, device),
    }


def reserve_targets(c: sqlite3.Connection, body: dict, on_event=None) -> dict:
    if not isinstance(body, dict):raise AuthError("请求体必须是对象")
    items=body.get("items")
    if items is None:
        files=body.get("files")
        if not isinstance(files,list):raise AuthError("请选择分包")
        items=[{"repo":body.get("repo"),"id":name} for name in files]
    result=package_queue.enqueue(c,__import__(__name__),{**body,"items":items},on_event=on_event)
    return {**result,"repo":body.get("repo"),"user_id":body["user_id"],"files":[x["id"] for x in items]}


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


def parts_summary(c: sqlite3.Connection, repo: str | None = None) -> dict:
    """分项统计（SPEC-parts §6）：还剩多少包、其中缺 tag / 缺 nl 各多少。

    口径（与 missing_parts 完全一致，只是压进 SQL，40 万包不能拉进 Python 数）：
      * remaining  = 还没"整包完成"的包数（available + claimed），也就是池子里
                     还需要人去做的量；
      * missing_tag = remaining 里 tag 这一侧还没落账的（含 skip——skip 是
                      "这台机器不做"，不是"做好了"）；
      * missing_nl  = 同上，nl 一侧；
      * done        = 两侧都齐的包数（= status completed），纯参考。
    claimed 的包算进 remaining（它确实还没交），但不算"缺"的紧急程度——
    这里不分紧急度，只给数量，选择权留给人。
    """
    where, args = (" AND p.repo=?", (repo,)) if repo else ("", ())
    cnt = part_count_expr("p")
    row = c.execute(
        "SELECT COUNT(*) AS n,"
        f" COALESCE(SUM(CASE WHEN p.status='completed' THEN 1 ELSE 0 END),0) AS done,"
        f" COALESCE(SUM({cnt['missing_tag']}),0) AS mt,"
        f" COALESCE(SUM({cnt['missing_nl']}),0) AS mn"
        " FROM packages p WHERE 1=1" + where, args).fetchone()
    total = row["n"] or 0
    done = row["done"] or 0
    return {
        "repo": repo,
        "total": total,
        "done": done,
        "remaining": max(0, total - done),
        "missing_tag": row["mt"] or 0,
        "missing_nl": row["mn"] or 0,
    }


def overview(c: sqlite3.Connection) -> dict:
    """看板：设备、租约、池子余量、分项缺口。"""
    devices = _rows(c, "SELECT * FROM devices ORDER BY last_seen DESC")
    leases = _rows(c, "SELECT * FROM leases WHERE status='open'")
    total = c.execute("SELECT COUNT(*) n FROM packages").fetchone()["n"]
    avail = c.execute("SELECT COUNT(*) n FROM packages WHERE status='available'").fetchone()["n"]
    busy = len({(l["repo"], l["pkg"]) for l in leases})
    return {"packages_total": total, "available": avail, "reserved_or_active": busy,
            "free_for_assign": max(0, avail - busy),
            "parts": parts_summary(c),
            "devices": [device_view(d, time.time()) for d in devices],
            "leases": [{**_lease_view(c, l), "user_id": l["user_id"],
                        "device_id": l["device_id"], "issued_at": l["issued_at"]}
                       for l in leases]}


def pool(c: sqlite3.Connection) -> dict:
    """客户端要的"池子还剩多少"（SPEC-parts §7）：**只出数，不出包**。

    之所以做成独立动作而不是塞进 status：status 是"我这台设备的租约"，
    它是"整池子"，两者速率不同（前者每 5 秒、后者每 30 秒就够），
    混在一起会让状态接口变重。也不带任何包名——谁都能看余量，
    但拿不到清单就不会出现"跳过派工直接去抢某个包"。
    """
    cur = parts_summary(c)
    repos = _rows(c, "SELECT repo FROM repos WHERE enabled=1 ORDER BY repo")
    return {
        "ok": True,
        "parts": cur,
        "repos": [{"repo": r["repo"], **parts_summary(c, r["repo"])} for r in repos],
        "ts": time.time(),
    }


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
        from scheduling.allocation import forget
        forget(c, device_id=device_id)
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
    overdue = overdue_leases(c, p, now)
    open_keys = {(r['repo'], r['pkg']) for r in overdue}
    # resolve 回填：有提醒记录但已无 open 租约 → 标记已解决（不物理删除）
    resolved = 0
    for row in _rows(c, "SELECT id, repo, pkg FROM notifications"
                        " WHERE kind='overdue' AND resolved_at IS NULL"):
        if (row["repo"], row["pkg"]) not in open_keys:
            c.execute("UPDATE notifications SET resolved_at=?, resolved_by='auto'"
                      " WHERE id=? AND resolved_at IS NULL", (now, row["id"]))
            resolved += 1
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
