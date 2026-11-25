#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fox-label 服务端 CLI —— 多仓库打标包认领/分发协调器（纯标准库，无第三方依赖）。

数据模型（v2）
-------------
仓库是一级实体：repos 表登记 HF 仓库（类型、包匹配正则、公共清单）；
packages 主键为 (repo, id)，同一分片名可以出现在不同仓库；
repo_grants 支持按仓库授权（为空表示该密钥可用全部仓库）。

子命令
------
  repo add      登记仓库并建立包索引（首个仓库也可用 init）
  repo list     列出所有仓库
  repo sync     重新拉取仓库清单，补录新增文件（--prune 清理已消失的未认领包）
  repo enable / disable / remove
  init          等价于 repo add（兼容旧用法）
  serve         启动 HTTP 服务（端口默认 31831）
  enroll        签发用户密钥（默认全部仓库，可 --repo 限定）
  keys / revoke
  release       管理员强制释放已认领的包
  reopen        管理员把已完成的包打回
  status        看板（可按 --repo 过滤，含按用户统计）
  export        导出审计事件

状态机
------
  未认领 available ──claim──> 已认领 claimed ──submit──> 已完成 completed
        ^                          │                          │
        │                          │<──── reopen(管理员) ─────┘
        └──── release(本人/管理员) ─┘
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import ipaddress
import os
import json
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote
from urllib.request import Request, urlopen

import dispatch as v3
from scheduling import progress as package_progress
import artifacts as artifact_core
from delivery.service import Delivery
from delivery.routes import handle as delivery_handle
from managed import store as managed_store
from managed.contracts import ManagedError
from managed.routes import handle_poll
from history import store as history_store
from history.routes import handle as history_handle
from skips import store as skip_store
from skips.routes import handle as handle_skips
from diagnostics import store as execution_results
from assignments import store as assignments
from assignments import offline as offline_tasks
from scheduling import routes as scheduling_routes
from source_catalog import routes as source_routes

DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "state"
DEFAULT_PORT = 31831
DEFAULT_INCLUDE = r"^shard-\d+\.tar$"

# 工作分派参数：心跳 120s，租约取 3 倍心跳（丢一两次不误伤），预锁更宽松
def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


HEARTBEAT_SEC = _env_int("FOXLABEL_HEARTBEAT", 120)
LEASE_TTL = _env_int("FOXLABEL_LEASE_TTL", 360)
PRELOCK_TTL = _env_int("FOXLABEL_PRELOCK_TTL", 600)
# 申报制（SPEC-declare）：SWEEP_SEC 后台回收循环已拆除——open 租约永久有效，
# FOXLABEL_SWEEP 环境变量不再有任何效果（读面兼容：解析后丢弃，避免旧 systemd
# 单元带着该变量启动时报错）。
DEFAULT_MANIFEST = "manifest.jsonl"

STATUS_LABEL = {"available": "未认领", "claimed": "已认领", "completed": "已完成"}
# /packages?status= 只做「中文别名 → 英文状态码」的反查；英文值由
# STATUS_HINT.get(qs_, qs_) 的兜底原样匹配。注意不能把英文键也并进来
# （t13 复审 F-B2R：那样 status=completed 会被映射成「已完成」再与
# status 列比较 → 永远空结果，500 变成更隐蔽的静默空列表）。
STATUS_HINT = {v: k for k, v in STATUS_LABEL.items()}

# ------------------------------------------------ 管理写通道（v3）常量
# 这里是 v3 唯一新增的"写面"：/admin/v1/*。客户端端点（/api/v1/*、/packages、
# /repos、/me、/events、/health）一个字段都不改，见 SPEC-v3 §11。
ADMIN_VERSION = "3.0.0"
ADMIN_MAX_BODY = 256 * 1024          # 写请求体上限（面板侧同值，SPEC §2.2）
CONFIRM_TTL = _env_int("FOXLABEL_CONFIRM_TTL", 120)
CONFIRM_TTL = max(1, min(120, CONFIRM_TTL))   # 生产不得 >120s，测试钩子只允许更短
BATCH_THRESHOLD = _env_int("FOXLABEL_BATCH_THRESHOLD", 5)
MAX_BATCH = 100
WRITE_LOG_MAX_BYTES = _env_int("FOXLABEL_WRITE_LOG_MAX_BYTES", 10 * 1024 * 1024)
WRITE_LOG_KEEP = max(1, _env_int("FOXLABEL_WRITE_LOG_KEEP", 5))
WRITE_IDEM_TTL = 86400               # 幂等缓存保留 24h（SPEC §6.1）
WRITE_IDEM_GC_SEC = 3600             # 每小时清理一次
IMPACT_ITEMS_MAX = 200               # 影响面清单展示上限（哈希始终覆盖全集）
SCAN_LINES_MAX = 100_000             # 写日志扫描/导出上限


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def write_enabled() -> bool:
    """服务端写开关：每次读环境变量，测试里给子进程设 FOXLABEL_WRITE_ENABLED=0 即生效。"""
    return _env_flag("FOXLABEL_WRITE_ENABLED", True)


def admin_local_only() -> bool:
    return _env_flag("FOXLABEL_ADMIN_LOCAL_ONLY", True)

SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS repos (
    repo            TEXT PRIMARY KEY,
    repo_type       TEXT NOT NULL DEFAULT 'dataset',
    include_pat     TEXT NOT NULL DEFAULT '^shard-\\d+\\.tar$',
    manifest        TEXT,
    manifest_sha256 TEXT,
    manifest_size   INTEGER,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    synced_at       TEXT
);
CREATE TABLE IF NOT EXISTS packages (
    repo          TEXT NOT NULL,
    id            TEXT NOT NULL,
    size          INTEGER NOT NULL DEFAULT 0,
    sha256        TEXT,
    status        TEXT NOT NULL DEFAULT 'available',
    owner         TEXT,
    claimed_at    TEXT,
    completed_at  TEXT,
    downloaded_at TEXT,
    note          TEXT,
    updated_at    TEXT NOT NULL,
    -- 租约（工作分派）：claimed 的包由某设备独占，到期未续则回池
    lease_device  TEXT,
    lease_user    TEXT,
    lease_until   REAL,
    last_beat     REAL,
    progress      TEXT,
    -- 预锁：提前留给某台设备的下一个包，别人看不见也拿不到
    prelock_device TEXT,
    prelock_user   TEXT,
    prelock_until  REAL,
    PRIMARY KEY (repo, id)
);
CREATE TABLE IF NOT EXISTS repo_grants (
    key_hash TEXT NOT NULL,
    repo     TEXT NOT NULL,
    PRIMARY KEY (key_hash, repo)
);
CREATE TABLE IF NOT EXISTS keys (
    hash       TEXT PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    role       TEXT NOT NULL DEFAULT 'user',
    all_repos  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    last_seen  TEXT
);
CREATE TABLE IF NOT EXISTS events (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    kind   TEXT NOT NULL,
    repo   TEXT,
    pkg    TEXT,
    actor  TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_pkg_status ON packages(repo, status);
CREATE INDEX IF NOT EXISTS idx_pkg_owner ON packages(owner);
CREATE INDEX IF NOT EXISTS idx_pkg_lease ON packages(lease_device);
CREATE INDEX IF NOT EXISTS idx_pkg_prelock ON packages(prelock_device);
"""

# v3 写通道专用表：幂等缓存。只被 /admin/v1/execute 使用，客户端不可见。
SCHEMA_WRITE = """
CREATE TABLE IF NOT EXISTS write_idem (
    k        TEXT PRIMARY KEY,      -- sha256(actor|action|idempotency_key)
    ts       REAL NOT NULL,
    response TEXT NOT NULL          -- {"status": 200, "body": {...}}
);
CREATE INDEX IF NOT EXISTS idx_write_idem_ts ON write_idem(ts);
"""


# ---------------------------------------------------------------- 小工具

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def pad(text, width: int) -> str:
    text = str(text)
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(0, width - w)


def disp_width(text) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def fit(text, width: int) -> str:
    """按显示宽度截断并补齐，避免长仓库名撑破表格。"""
    text = str(text)
    if disp_width(text) <= width:
        return pad(text, width)
    out, w = [], 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > width - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "~" + " " * (width - w - 1)


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def read_hf_token(explicit: str | None = None) -> str | None:
    import os
    if explicit:
        return explicit.strip()
    env = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if env:
        return env.strip()
    for p in ("~/.cache/huggingface/token", "~/.huggingface/token"):
        f = Path(p).expanduser()
        if f.is_file():
            return f.read_text(encoding="utf-8").strip()
    return None


# ------------------------------------------------- HF 凭据（加密落盘）与文件树缓存

HF_TOKEN_FILE = "hf_token.bin"       # Fernet 密文
HF_TOKEN_KEYFILE = "hf_token.key"    # 本机密钥，600
TREE_CACHE_DIR = "tree-cache"
TREE_CACHE_TTL = 6 * 3600            # 6 小时：不天天拉，也不至于永远用旧数据


def _hf_fernet(store: "Store"):
    """取（或首次生成）本机密钥。延迟 import：没装 cryptography 的机器
    只会在"要保存 token"时报错，不影响其它功能。"""
    from cryptography.fernet import Fernet
    path = Path(store.dir) / HF_TOKEN_KEYFILE
    if path.is_file():
        raw = path.read_bytes().strip()
        if raw:
            return Fernet(raw)
    key = Fernet.generate_key()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return Fernet(key)


def hf_token_save(store: "Store", token: str) -> None:
    """保存 HF token（密文）。明文只在内存里出现，不落盘、不回显。"""
    token = (token or "").strip()
    if not token:
        raise Conflict("token 不能为空")
    if len(token) > 512:
        raise Conflict("token 过长（最多 512 字符）")
    data = _hf_fernet(store).encrypt(token.encode("utf-8"))
    path = Path(store.dir) / HF_TOKEN_FILE
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def hf_token_load(store: "Store") -> str | None:
    path = Path(store.dir) / HF_TOKEN_FILE
    if not path.is_file():
        return None
    try:
        raw = _hf_fernet(store).decrypt(path.read_bytes()).decode("utf-8").strip()
        return raw or None
    except Exception:                      # 换机器/密钥丢了：当成没配，别把服务搞崩
        return None


def mask_token(token: str) -> str:
    """面板上只显示 hf_ 后前三位与末三位——够认是哪把钥匙，不够拿来用。"""
    if not token:
        return ""
    body = token[3:] if token.startswith("hf_") else token
    if len(body) <= 6:
        return "hf_" + "*" * max(3, len(body))
    return "hf_" + body[:3] + "*" * 6 + body[-3:]


def read_hf_token_for(store: "Store", explicit: str | None = None) -> str | None:
    """面板里配的优先（显式配置压过环境），其余沿用老来源。"""
    if explicit:
        return explicit.strip()
    saved = hf_token_load(store)
    if saved:
        return saved
    return read_hf_token()


def hf_token_status(store: "Store") -> dict:
    saved = hf_token_load(store)
    if saved:
        return {"configured": True, "source": "panel", "masked": mask_token(saved)}
    env = read_hf_token()
    if env:
        return {"configured": True, "source": "env", "masked": mask_token(env)}
    return {"configured": False, "source": None, "masked": ""}


def _tree_cache_path(store: "Store", repo: str, repo_type: str) -> Path:
    key = hashlib.sha256(("%s\t%s" % (repo_type, repo)).encode("utf-8")).hexdigest()[:16]
    return Path(store.dir) / TREE_CACHE_DIR / ("%s.json.gz" % key)


def tree_cache_meta(store: "Store", repo: str, repo_type: str) -> dict | None:
    path = _tree_cache_path(store, repo, repo_type)
    if not path.is_file():
        return None
    st = path.stat()
    return {"bytes": st.st_size,
            "fetched_at": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                          .astimezone().isoformat(timespec="seconds"),
            "age_sec": int(max(0, time.time() - st.st_mtime))}


def tree_cache_load(store: "Store", repo: str, repo_type: str) -> list[dict] | None:
    path = _tree_cache_path(store, repo, repo_type)
    if not path.is_file():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        tree = data.get("tree")
        return tree if isinstance(tree, list) and tree else None
    except (OSError, ValueError):
        return None


def tree_cache_store(store: "Store", repo: str, repo_type: str, tree: list[dict]) -> None:
    path = _tree_cache_path(store, repo, repo_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump({"repo": repo, "repo_type": repo_type, "fetched_at": now_iso(),
                   "count": len(tree), "tree": tree}, fh)
    tmp.replace(path)


_TREE_BG_LOCK = threading.Lock()
_TREE_BG: set[str] = set()


def tree_refresh_bg(store: "Store", repo: str, repo_type: str) -> bool:
    """后台重新拉文件树并更新包索引；已经有线程在跑就跳过。

    这是"有人来要包时顺手刷一下"的落地：调用方立刻拿到现有缓存，
    刷新在后台做完，绝不卡住要包的那台机器。
    """
    key = "%s:%s" % (repo_type, repo)
    with _TREE_BG_LOCK:
        if key in _TREE_BG:
            return False
        _TREE_BG.add(key)

    def _run() -> None:
        try:
            token = read_hf_token_for(store)
            if not token:
                return
            tree = fetch_tree(repo, repo_type, token)
            if not tree:
                return
            tree_cache_store(store, repo, repo_type, tree)
            with store._write_lock:
                repo_sync(store, repo, tree)
        except Exception:                  # 后台线程：失败只影响缓存新鲜度
            pass
        finally:
            with _TREE_BG_LOCK:
                _TREE_BG.discard(key)

    threading.Thread(target=_run, name="tree-refresh", daemon=True).start()
    return True


def repo_refresh_data(store: "Store", repo: str, *, force: bool = False) -> dict:
    """面板上的「刷新数据」：有缓存就先用缓存，过期了再后台补。

    三种结果都告诉调用方，前端才好决定要不要转圈：
      source=cache  + refreshing=true  → 数据是旧的，后台正在更新
      source=cache  + refreshing=false → 缓存还新鲜
      source=hf                        → 首次没有缓存，同步拉完（会慢）
    """
    row = repo_row(store, repo)
    if not row:
        raise NotFound("仓库 %s 未登记" % repo)
    repo_type = row["repo_type"]
    meta = tree_cache_meta(store, repo, repo_type)
    tree = tree_cache_load(store, repo, repo_type)

    if tree is not None and not force:
        st = repo_sync(store, repo, tree)
        stale = (meta or {}).get("age_sec", 0) > TREE_CACHE_TTL
        return {"repo": repo, "added": st["added"], "files": st["files"],
                "pruned": st["pruned"], "source": "cache",
                "cached_at": (meta or {}).get("fetched_at"),
                "refreshing": bool(stale) and tree_refresh_bg(store, repo, repo_type)}

    token = read_hf_token_for(store)
    if not token:
        raise Conflict("尚未配置 HF token：先在面板里填一次")
    try:
        fresh = fetch_tree(repo, repo_type, token)
    except HTTPError as e:
        raise Conflict("读取仓库清单失败：HTTP %s（检查仓库名/权限/gated 审批）" % e.code)
    except URLError as e:
        raise Conflict("网络错误：%s" % e.reason)
    if not fresh:
        raise Conflict("仓库清单为空，已中止（避免误删）")
    tree_cache_store(store, repo, repo_type, fresh)
    st = repo_sync(store, repo, fresh)
    return {"repo": repo, "added": st["added"], "files": st["files"],
            "pruned": st["pruned"], "source": "hf", "cached_at": now_iso(),
            "refreshing": False}


def split_ref(ref: str) -> tuple[str | None, str]:
    """支持 'repo/namespace/name' 形式的包引用：返回 (repo, pkg)。

    包名本身不含 '/'，所以从右往左找第一个不含 '/' 的段之前的全部内容视为仓库。
    例：'MatchaCuratio/Anime-Fox768-400k/shard-000001.tar'
        -> ('MatchaCuratio/Anime-Fox768-400k', 'shard-000001.tar')
    """
    if "/" not in ref:
        return None, ref
    repo, pkg = ref.rsplit("/", 1)
    return repo, pkg


# ---------------------------------------------------------------- 存储层

class Store:
    def __init__(self, data_dir: Path | str, *, artifact_dir: Path | str | None = None,
                 artifact_limits: dict | None = None):
        self.dir = Path(data_dir).expanduser().resolve()
        self.artifacts = None
        self.delivery = None
        if artifact_dir is not None:
            artifact_path = Path(artifact_dir).expanduser().resolve()
            if (artifact_path == self.dir or artifact_path in self.dir.parents
                    or self.dir in artifact_path.parents):
                raise ValueError("产物目录必须与调度数据目录分离，不能相同或互相包含")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "db.sqlite3"
        self.audit_path = self.dir / "audit.jsonl"
        # 写日志（v3 轨②）：只增、按大小轮转、轮转只改名/归档
        self.writes = WriteLog(self.dir)
        # 可重入：状态变更持有写锁期间还会调用 event() 写审计
        self._write_lock = threading.RLock()
        # 已存在的库先做建表/迁移，保证任何子命令都能直接跑
        if self.db_path.exists():
            self.init_schema()
        if artifact_dir is not None:
            self.artifacts = artifact_core.ArtifactStore(artifact_dir, **(artifact_limits or {}))
            self.delivery = Delivery(self.dir, artifact_dir)

    def conn(self) -> sqlite3.Connection:
        wait_ms = int(os.environ.get("FOXLABEL_DB_TIMEOUT_MS", "15000"))
        c = sqlite3.connect(self.db_path, timeout=wait_ms / 1000, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(f"PRAGMA busy_timeout={wait_ms}")
        return c

    # -- 建表与迁移 -------------------------------------------------
    def init_schema(self) -> None:
        c = self.conn()
        # 先建表 → 迁移 → 再建索引：v1 的 packages 没有 repo 列，
        # 若先建索引会因 no such column 直接失败，迁移永远跑不到。
        c.executescript(SCHEMA_TABLES)
        self._migrate_v1(c)
        self._ensure_lease_columns(c)
        c.executescript(SCHEMA_INDEXES)
        c.executescript(SCHEMA_WRITE)       # v3 写通道的幂等表（独立存储，客户端不可见）
        legacy = c.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                           " AND name='devices'").fetchone()
        if legacy and "first_seen" not in (legacy["sql"] or ""):
            n = c.execute("SELECT COUNT(*) n FROM devices").fetchone()["n"]
            if n == 0:              # 只清空表，绝不动有数据的表
                c.execute("DROP TABLE devices")
        v3.ensure_schema(c)          # v3：users / devices / leases
        managed_store.ensure_schema(c)
        history_store.ensure_schema(c)
        skip_store.ensure_schema(c)
        c.close()

    def _migrate_v1(self, c: sqlite3.Connection) -> None:
        """v1（单仓库、packages 无 repo 列）→ v2（多仓库、复合主键）。"""
        kcols = {r["name"] for r in c.execute("PRAGMA table_info(keys)")}
        if kcols and "all_repos" not in kcols:
            c.execute("ALTER TABLE keys ADD COLUMN all_repos INTEGER NOT NULL DEFAULT 0")
            c.execute("UPDATE keys SET all_repos=1 WHERE hash NOT IN"
                      " (SELECT key_hash FROM repo_grants)")
        cols = {r["name"] for r in c.execute("PRAGMA table_info(packages)")}
        if not cols or "repo" in cols:
            # 无表或已是 v2：补 events.repo 列，并回填仓库的清单元数据
            ecols = {r["name"] for r in c.execute("PRAGMA table_info(events)")}
            if ecols and "repo" not in ecols:
                c.execute("ALTER TABLE events ADD COLUMN repo TEXT")
            self._backfill_manifest(c)
            return

        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='packages_v1'").fetchone():
            raise SystemExit("检测到上次迁移残留的 packages_v1 表：迁移中途失败。"
                             "请先备份 db.sqlite3，核对后再删除该表重试。")

        legacy_repo = None
        row = c.execute("SELECT v FROM meta WHERE k='repo'").fetchone()
        if row:
            legacy_repo = row["v"]
        legacy_type = "dataset"
        row = c.execute("SELECT v FROM meta WHERE k='repo_type'").fetchone()
        if row:
            legacy_type = row["v"]

        if not legacy_repo:
            # 空库没有任何包，直接重建即可
            c.execute("DROP TABLE packages")
            c.executescript(SCHEMA_TABLES)
            ecols2 = {r["name"] for r in c.execute("PRAGMA table_info(events)")}
            if ecols2 and "repo" not in ecols2:
                c.execute("ALTER TABLE events ADD COLUMN repo TEXT")
            return

        ts = now_iso()
        c.execute("INSERT OR IGNORE INTO repos(repo,repo_type,include_pat,created_at) VALUES(?,?,?,?)",
                  (legacy_repo, legacy_type, DEFAULT_INCLUDE, ts))
        c.execute("ALTER TABLE packages RENAME TO packages_v1")
        c.executescript(SCHEMA_TABLES)  # 重新创建 packages（含新结构）
        c.execute("UPDATE packages_v1 SET size=0 WHERE size IS NULL")
        c.execute(
            "INSERT INTO packages(repo,id,size,sha256,status,owner,claimed_at,completed_at,"
            "downloaded_at,note,updated_at) "
            "SELECT ?,id,size,sha256,status,owner,claimed_at,completed_at,downloaded_at,note,updated_at "
            "FROM packages_v1", (legacy_repo,))
        moved = c.execute("SELECT COUNT(*) n FROM packages").fetchone()["n"]
        origin = c.execute("SELECT COUNT(*) n FROM packages_v1").fetchone()["n"]
        if moved != origin:
            raise SystemExit(f"迁移行数不一致（{origin} → {moved}），已中止，请检查 db.sqlite3")
        c.execute("DROP TABLE packages_v1")
        ecols = {r["name"] for r in c.execute("PRAGMA table_info(events)")}
        if "repo" not in ecols:
            c.execute("ALTER TABLE events ADD COLUMN repo TEXT")
        c.execute("UPDATE events SET repo=? WHERE repo IS NULL AND pkg IS NOT NULL", (legacy_repo,))
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version','2')")
        self._backfill_manifest(c)

    def _ensure_lease_columns(self, c: sqlite3.Connection) -> None:
        """老库补上租约/预锁列（幂等）。"""
        cols = {r["name"] for r in c.execute("PRAGMA table_info(packages)")}
        for name, decl in (("lease_device", "TEXT"), ("lease_user", "TEXT"),
                           ("lease_until", "REAL"), ("last_beat", "REAL"),
                           ("progress", "TEXT"), ("prelock_device", "TEXT"),
                           ("prelock_user", "TEXT"), ("prelock_until", "REAL")):
            if cols and name not in cols:
                c.execute(f"ALTER TABLE packages ADD COLUMN {name} {decl}")

    def _backfill_manifest(self, c) -> None:
        """v1 的清单信息存在 meta 里，迁移后需要搬到 repos 表。"""
        total = c.execute("SELECT COUNT(*) n FROM repos").fetchone()["n"]
        if total != 1:
            return  # 只有「本来就只有这一个仓库」的 v1 库才可安全回填
        repos = [r["repo"] for r in c.execute("SELECT repo FROM repos WHERE manifest IS NULL")]
        if not repos:
            return
        mf = {r["k"]: r["v"] for r in c.execute(
            "SELECT k,v FROM meta WHERE k IN ('manifest','manifest_sha256','manifest_size')")}
        if not mf.get("manifest"):
            return
        legacy = c.execute("SELECT v FROM meta WHERE k='repo'").fetchone()
        if legacy and legacy["v"] != repos[0]:
            return
        c.execute("UPDATE repos SET manifest=?, manifest_sha256=?, manifest_size=? WHERE repo=?",
                  (mf["manifest"], mf.get("manifest_sha256"),
                   int(mf.get("manifest_size") or 0), repos[0]))

    # -- meta
    def get_meta(self, k: str, default=None):
        c = self.conn()
        row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        c.close()
        return row["v"] if row else default

    def set_meta(self, k: str, v: str) -> None:
        c = self.conn()
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        c.close()

    # -- 事件
    def event(self, conn, kind: str, repo, pkg, actor, detail) -> int | None:
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO events(ts,kind,repo,pkg,actor,detail) VALUES(?,?,?,?,?,?)",
            (ts, kind, repo, pkg, actor, detail))
        seq = cur.lastrowid
        rec = {"ts": ts, "kind": kind, "repo": repo, "pkg": pkg, "actor": actor, "detail": detail}
        with self._write_lock:
            try:
                with self.audit_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError as e:
                # 审计文件不可写时只告警：events 表才是权威记录，不能拖垮业务事务
                print(f"[warn] 写审计文件失败：{e}", file=sys.stderr)
        return seq


# ---------------------------------------------------------------- 写日志（轨②）

def _log_csv_cell(value) -> str:
    """RFC 4180 + 公式注入防护（与面板 CSV 规则一致）。"""
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    if not s:
        return ""
    if s[0] in "=+-@\t\r":
        s = "'" + s
    if '"' in s or "," in s or "\r" in s or "\n" in s:
        s = '"' + s.replace('"', '""') + '"'
    return s


def _to_epoch(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


class WriteLog:
    """轨② `state/writes.jsonl`：写操作逐条落盘，append-only。

    为什么不用 SQLite 存：审计要能被外部工具直接 grep/tail，并且必须在数据库
    损坏时仍然留痕。代码里对日志文件只有 `open(..., "a")` 与轮转用的
    `os.replace`/gzip 归档，**没有 unlink/truncate**（只增不改，SPEC §7.4）。
    """

    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.path = self.dir / "writes.jsonl"
        self.archive = self.dir / "writes-archive"
        self.max_bytes = WRITE_LOG_MAX_BYTES
        self.keep = WRITE_LOG_KEEP
        self._lock = threading.Lock()
        try:                    # 目录只读也不能让只读子命令（status/list）直接崩
            self._ensure_dirs()
            self._ensure_file()
        except OSError as e:
            print(f"[warn] 初始化写日志失败（写操作会降级为 logged=false）：{e}",
                  file=sys.stderr)

    # -- 文件/轮转 --------------------------------------------------
    def _ensure_dirs(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def _ensure_file(self) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)

    def files(self) -> list[Path]:
        """活跃 → .1 → … → .(KEEP-1) → 归档（新→旧）。"""
        out: list[Path] = []
        for i in range(self.keep):
            p = self.path if i == 0 else self.path.with_name(self.path.name + f".{i}")
            if p.exists():
                out.append(p)
        try:
            if self.archive.is_dir():
                out.extend(sorted(self.archive.glob("writes-*.jsonl.gz"), reverse=True))
        except OSError:
            pass
        return out

    def _archive_file(self, src: Path) -> bool:
        """把一份轮出的日志 gzip 归档。失败返回 False（中止轮转，绝不丢历史）。"""
        try:
            self.archive.mkdir(parents=True, exist_ok=True)
            os.chmod(self.archive, 0o700)
            stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(src.stat().st_mtime))
            dest = self.archive / f"writes-{stamp}.jsonl.gz"
            n = 1
            while dest.exists():
                dest = self.archive / f"writes-{stamp}-{n}.jsonl.gz"
                n += 1
            tmp = dest.with_name(dest.name + ".tmp")
            with src.open("rb") as fin, gzip.open(tmp, "wb") as fout:
                while True:
                    chunk = fin.read(1 << 20)
                    if not chunk:
                        break
                    fout.write(chunk)
            # 归档与活日志同权限（0600）：gzip.open 按 umask 创建会是 0644，
            # 审计内容含 actor/IP，不能比 source 更宽松；替换后再兜底一次 chmod。
            os.chmod(tmp, 0o600)
            os.replace(tmp, dest)
            os.chmod(dest, 0o600)
            return True
        except OSError as e:
            print(f"[warn] 归档写日志失败（本轮不轮转）：{e}", file=sys.stderr)
            return False

    def _rotate(self) -> bool:
        if self.keep > 1:
            last = self.path.with_name(self.path.name + f".{self.keep - 1}")
            if last.exists() and not self._archive_file(last):
                return False
            for i in range(self.keep - 2, 0, -1):
                src = self.path.with_name(self.path.name + f".{i}")
                if src.exists():
                    os.replace(src, self.path.with_name(self.path.name + f".{i + 1}"))
            if self.path.exists():
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        else:
            if self.path.exists() and not self._archive_file(self.path):
                return False
            tmp = self.path.with_name(self.path.name + ".new." + secrets.token_hex(4))
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            os.replace(tmp, self.path)          # 内容已在归档里，这里只是换空文件
        self._ensure_file()
        return True

    @staticmethod
    def _shrink(rec: dict) -> bytes:
        """单行超过阈值时的兜底：截断 error/params，保证文件不会无限膨胀。"""
        slim = dict(rec)
        if isinstance(slim.get("error"), str):
            slim["error"] = slim["error"][:200]
        params = slim.get("params")
        if isinstance(params, dict):
            slim["params"] = {k: (str(v)[:80] if not isinstance(v, (int, float, bool)) else v)
                              for k, v in list(params.items())[:5]}
        return json.dumps(slim, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"

    def append(self, rec: dict) -> bool:
        """追加一行。失败只告警并返回 False（业务不回滚、响应标 logged=false）。"""
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        with self._lock:
            try:
                # 目录不可写时连轮转/归档都做不了，按"落盘失败"处理（业务不回滚）
                if not os.access(self.dir, os.W_OK):
                    print(f"[warn] 写日志目录不可写：{self.dir}", file=sys.stderr)
                    return False
                self._ensure_file()
                size = self.path.stat().st_size
                if size and size + len(line) > self.max_bytes:
                    self._rotate()
                if len(line) > self.max_bytes:
                    line = self._shrink(rec)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return True
            except OSError as e:
                print(f"[warn] 写 writes.jsonl 失败：{e}", file=sys.stderr)
                return False

    # -- 读取 ------------------------------------------------------
    def _iter(self):
        for path in self.files():
            opener = gzip.open if path.suffix == ".gz" else open
            try:
                with opener(path, "rb") as fh:
                    while True:
                        raw = fh.readline()
                        if not raw:
                            break
                        raw = raw.rstrip(b"\r\n")
                        if not raw.strip():
                            continue
                        try:
                            rec = json.loads(raw.decode("utf-8"))
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if isinstance(rec, dict):
                            yield rec, raw
            except (OSError, EOFError):
                continue

    def _load(self, max_rows: int = SCAN_LINES_MAX) -> tuple[list[tuple[dict, bytes]], bool]:
        rows: list[tuple[dict, bytes]] = []
        truncated = False
        for rec, raw in self._iter():
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append((rec, raw))
        return rows, truncated

    def _match(self, rec: dict, action: str, actor: str, result: str, q: str,
               batch_id: str, write_id: str, from_ts: float | None,
               to_ts: float | None) -> bool:
        if action and rec.get("action") != action:
            return False
        if actor and rec.get("actor") != actor:
            return False
        if result and rec.get("result") != result:
            return False
        if batch_id and rec.get("batch_id") != batch_id:
            return False
        if write_id and rec.get("write_id") != write_id:
            return False
        ts = _to_epoch(rec.get("ts_epoch"))
        if from_ts is not None and (ts is None or ts < from_ts):
            return False
        if to_ts is not None and (ts is None or ts > to_ts):
            return False
        if q:
            needle = q.strip().lower()
            if needle:
                target = rec.get("target") or {}
                hay = " ".join(str(x) for x in [
                    target.get("repo"), target.get("id"), target.get("lease_id"),
                    target.get("device_id"), target.get("name"), target.get("user_id"),
                    rec.get("action"), rec.get("error"), rec.get("actor")] if x)
                if needle not in hay.lower():
                    return False
        return True

    @staticmethod
    def _sort_key(item: tuple[dict, bytes]):
        rec = item[0]
        return (_to_epoch(rec.get("ts_epoch")) or 0.0, str(rec.get("write_id") or ""))

    def query(self, *, limit: int = 25, offset: int = 0, action: str = "", actor: str = "",
              result: str = "", q: str = "", batch_id: str = "", write_id: str = "",
              order: str = "desc", from_ts: float | None = None,
              to_ts: float | None = None) -> dict:
        rows, truncated = self._load()
        matched = [r for r in rows if self._match(r[0], action, actor, result, q,
                                                  batch_id, write_id, from_ts, to_ts)]
        matched.sort(key=self._sort_key, reverse=(order != "asc"))
        total = len(matched)
        page = matched[offset:offset + limit] if limit > 0 else []
        out = {"total": total, "items": [rec for rec, _ in page], "truncated": truncated,
               "scanned": len(rows)}
        if not rows:
            out["note"] = "写日志尚未创建"
        return out

    def find(self, write_id: str) -> dict | None:
        for rec, _ in self._iter():
            if rec.get("write_id") == write_id:
                return rec
        return None

    def export(self, fmt: str = "jsonl", **filters) -> tuple[bytes, int, bool]:
        """导出。JSONL 与磁盘行**逐字节一致**（不重新序列化、不改字段顺序）。"""
        rows, truncated = self._load()
        matched = [r for r in rows if self._match(r[0], filters.get("action", ""),
                                                  filters.get("actor", ""),
                                                  filters.get("result", ""),
                                                  filters.get("q", ""),
                                                  filters.get("batch_id", ""),
                                                  filters.get("write_id", ""),
                                                  filters.get("from_ts"),
                                                  filters.get("to_ts"))]
        matched.sort(key=self._sort_key)
        if len(matched) > SCAN_LINES_MAX:
            matched = matched[:SCAN_LINES_MAX]
            truncated = True
        if fmt == "csv":
            cols = ("ts", "actor", "remote_ip", "action", "target", "result",
                    "impact_count", "confirm_token_id", "batch_id", "ms")
            lines = [",".join(cols)]
            for rec, _ in matched:
                cells = []
                for col in cols:
                    value = rec.get(col)
                    if col == "target":
                        value = json.dumps(value or {}, ensure_ascii=False,
                                           separators=(",", ":"))
                    cells.append(_log_csv_cell(value))
                lines.append(",".join(cells))
            body = ("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8")
        else:
            body = b"\n".join(raw for _, raw in matched)
            if matched:
                body += b"\n"
        return body, len(matched), truncated


# ---------------------------------------------------------------- 业务错误

# 每个密钥 last_seen 的内存节流表（避免每个请求都写库）
_LAST_SEEN: dict[str, float] = {}


class PayloadTooLarge(Exception):
    pass


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


# ---------------------------------------------------------------- 仓库

def fetch_tree(repo: str, repo_type: str, token: str) -> list[dict]:
    """读取仓库清单。HF tree API 每页上限 1000 条并用 Link 头翻页，
    不翻页会对大仓库只拿到首页——配合 --prune 就是数据灾难。"""
    prefix = "/datasets" if repo_type == "dataset" else ""
    url = f"https://huggingface.co/api{prefix}/{repo}/tree/main?recursive=true"
    out: list[dict] = []
    for _ in range(500):                       # 硬上限，防跑飞
        req = Request(url, headers={"Authorization": f"Bearer {token}"})
        with urlopen(req, timeout=60) as r:
            out.extend(json.loads(r.read().decode("utf-8")))
            link = r.headers.get("Link") or ""
        nxt = None
        for part in link.split(","):
            if 'rel="next"' in part:
                nxt = part.split(";")[0].strip().strip("<>")
        if not nxt:
            break
        url = nxt
    return out


def repo_row(store: Store, repo: str) -> dict | None:
    c = store.conn()
    row = c.execute("SELECT * FROM repos WHERE repo=?", (repo,)).fetchone()
    c.close()
    return dict(row) if row else None


def repo_rows(store: Store, only_enabled: bool = False) -> list[dict]:
    c = store.conn()
    sql = "SELECT * FROM repos" + (" WHERE enabled=1" if only_enabled else "") + " ORDER BY repo"
    rows = [dict(r) for r in c.execute(sql)]
    c.close()
    for r in rows:
        r["summary"] = summary(store, repo=r["repo"])
    return rows


def apply_tree(store: Store, repo: str, tree: list[dict], include_pat: str,
               manifest_name: str, prune: bool = False) -> dict:
    """按仓库清单补录/更新包。返回统计。"""
    try:
        pat = re.compile(include_pat)
    except re.error as e:
        raise Conflict(f"包匹配正则非法：{e}")
    files = [e for e in tree if e.get("type") == "file" and pat.search(e["path"])]
    if not files:
        # 清单拉空（权限异常/分页截断）时绝不能继续，否则 --prune 会清库
        raise Conflict("仓库清单中没有任何匹配文件，已中止（避免误删）")
    manifest = next((e for e in tree if e.get("type") == "file" and e["path"] == manifest_name), None)

    c = store.conn()
    added = 0
    seen = set()
    existing = c.execute("SELECT COUNT(*) n FROM packages WHERE repo=?", (repo,)).fetchone()["n"]
    if prune and existing >= 20 and len(files) * 2 < existing:
        c.close()
        raise Conflict(f"清单条目数骤降（{existing} → {len(files)}），拒绝执行 --prune；"
                       f"请确认仓库是否被清空或 tree 分页是否完整")
    c.execute("BEGIN IMMEDIATE")
    for e in files:
        seen.add(e["path"])
        sha = (e.get("lfs") or {}).get("oid") or e.get("xetHash")
        row = c.execute("SELECT sha256, status FROM packages WHERE repo=? AND id=?",
                        (repo, e["path"])).fetchone()
        if row is None:
            c.execute("INSERT INTO packages(repo,id,size,sha256,status,updated_at) "
                      "VALUES(?,?,?,?,'available',?)",
                      (repo, e["path"], e.get("size", 0), sha, now_iso()))
            added += 1
        else:
            # 仓库侧改了文件内容：已认领/已完成的包必须留痕，否则难以追责
            if sha and row["sha256"] and sha != row["sha256"] and row["status"] != "available":
                store.event(c, "pkg_changed", repo, e["path"], "system",
                            f"内容变化（状态 {STATUS_LABEL[row['status']]}），旧 {row['sha256'][:12]}…")
            c.execute("UPDATE packages SET size=?, sha256=COALESCE(?,sha256) WHERE repo=? AND id=?",
                      (e.get("size", 0), sha, repo, e["path"]))

    pruned = 0
    if prune:
        rows = c.execute("SELECT id FROM packages WHERE repo=? AND status='available'", (repo,)).fetchall()
        dropped = []
        for r in rows:
            if r["id"] in seen:
                continue
            cur = c.execute("DELETE FROM packages WHERE repo=? AND id=? AND status='available'",
                            (repo, r["id"]))
            if cur.rowcount:
                pruned += 1
                dropped.append(r["id"])
        if dropped:
            store.event(c, "repo_prune", repo, None, "admin",
                        f"清理 {len(dropped)} 个未认领包：" + "、".join(dropped[:10])
                        + ("…" if len(dropped) > 10 else ""))

    if manifest:
        sha = (manifest.get("lfs") or {}).get("oid") or manifest.get("xetHash") or ""
        c.execute("UPDATE repos SET manifest=?, manifest_sha256=?, manifest_size=? WHERE repo=?",
                  (manifest["path"], sha, manifest.get("size", 0), repo))
    c.execute("UPDATE repos SET synced_at=? WHERE repo=?", (now_iso(), repo))
    c.execute("COMMIT")
    c.close()
    return {"files": len(files), "added": added, "pruned": pruned}


def repo_add(store: Store, repo: str, repo_type: str, include_pat: str, manifest_name: str,
             tree: list[dict]) -> dict:
    store.init_schema()
    c = store.conn()
    exists = c.execute("SELECT 1 FROM repos WHERE repo=?", (repo,)).fetchone()
    if exists:
        c.close()
        raise Conflict(f"仓库 {repo} 已登记；如需补录新文件请用 repo sync")
    try:
        re.compile(include_pat)
    except re.error as e:
        c.close()
        raise Conflict(f"包匹配正则非法：{e}")
    c.execute("INSERT INTO repos(repo,repo_type,include_pat,created_at) VALUES(?,?,?,?)",
              (repo, repo_type, include_pat, now_iso()))
    store.event(c, "repo_add", repo, None, "admin", f"{repo_type}, include={include_pat}")
    c.close()
    try:
        return apply_tree(store, repo, tree, include_pat, manifest_name)
    except Exception:
        # 补偿：包索引建立失败就撤掉仓库行，别留一个永久不可用的仓库
        c2 = store.conn()
        c2.execute("DELETE FROM repos WHERE repo=?", (repo,))
        c2.close()
        raise


def repo_sync(store: Store, repo: str, tree: list[dict], prune: bool = False) -> dict:
    row = repo_row(store, repo)
    if not row:
        raise NotFound(repo)
    st = apply_tree(store, repo, tree, row["include_pat"], row["manifest"] or DEFAULT_MANIFEST, prune)
    c = store.conn()
    store.event(c, "repo_sync", repo, None, "admin",
                f"files={st['files']} added={st['added']} pruned={st['pruned']}")
    c.close()
    return st


def repo_set_enabled(store: Store, repo: str, enabled: bool, actor: str = "admin",
                     audit_extra: str | None = None) -> None:
    c = store.conn()
    cur = c.execute("UPDATE repos SET enabled=? WHERE repo=?", (1 if enabled else 0, repo))
    if cur.rowcount == 0:
        c.close()
        raise NotFound(repo)
    detail = audit_extra if audit_extra else None
    seq = store.event(c, "repo_enable" if enabled else "repo_disable", repo, None, actor, detail)
    c.close()
    return seq


def repo_remove(store: Store, repo: str, force: bool = False) -> None:
    c = store.conn()
    if not c.execute("SELECT 1 FROM repos WHERE repo=?", (repo,)).fetchone():
        c.close()
        raise NotFound(repo)
    busy = c.execute("SELECT COUNT(*) n FROM packages WHERE repo=? AND status<>'available'",
                     (repo,)).fetchone()["n"]
    if busy and not force:
        c.close()
        raise Conflict(f"仓库 {repo} 仍有 {busy} 个已认领/已完成的包；确认要删除请加 --force")
    c.execute("DELETE FROM packages WHERE repo=?", (repo,))
    c.execute("DELETE FROM repos WHERE repo=?", (repo,))
    c.execute("DELETE FROM repo_grants WHERE repo=?", (repo,))
    store.event(c, "repo_remove", repo, None, "admin", f"force={force}")
    c.close()


# ---------------------------------------------------------------- 业务操作

def _pkg_row(c, repo: str, pkg: str):
    return c.execute("SELECT * FROM packages WHERE repo=? AND id=?", (repo, pkg)).fetchone()


def claim(store: Store, repo: str, pkg: str, actor: str) -> dict:
    with store._write_lock:
        c = store.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM leases WHERE repo=? AND pkg=? AND status='open'",
                         (repo, pkg)).fetchone():
                raise Conflict(f"包 {pkg} 已被 v3 调度占用（请改用 /api/v1 接口）")
            if c.execute("SELECT 1 FROM package_targets WHERE repo=? AND pkg=?",
                         (repo, pkg)).fetchone():
                raise Conflict(f"包 {pkg} 已定向派给指定用户，请使用 v3 worker 领取")
            row = _pkg_row(c, repo, pkg)
            if row is None:
                raise NotFound(f"{repo}/{pkg}")
            if row["status"] != "available":
                raise Conflict(f"包 {pkg} 当前为 {STATUS_LABEL[row['status']]}"
                               + (f"，认领人 {row['owner']}" if row["owner"] else ""))
            ts = now_iso()
            c.execute("UPDATE packages SET note=NULL, status='claimed', owner=?, claimed_at=?,"
                      " downloaded_at=NULL, updated_at=? WHERE repo=? AND id=?",
                      (actor, ts, ts, repo, pkg))
            store.event(c, "claim", repo, pkg, actor, None)
            c.execute("COMMIT")
            return {"repo": repo, "id": pkg, "status": "claimed", "owner": actor, "claimed_at": ts}
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()


def release(store: Store, repo: str, pkg: str, actor: str, is_admin: bool,
            reason: str | None = None, audit_extra: str | None = None) -> dict:
    with store._write_lock:
        c = store.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = _pkg_row(c, repo, pkg)
            if row is None:
                raise NotFound(f"{repo}/{pkg}")
            if row["status"] != "claimed":
                raise Conflict(f"包 {pkg} 不是已认领状态（当前 {STATUS_LABEL[row['status']]}）")
            if not is_admin and row["owner"] != actor:
                raise Forbidden(f"包 {pkg} 由 {row['owner']} 认领，你不能释放")
            ts = now_iso()
            c.execute("UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                      " downloaded_at=NULL, updated_at=? WHERE repo=? AND id=?", (ts, repo, pkg))
            detail = reason or f"原认领人 {row['owner']}"
            if audit_extra:
                detail = f"{detail}; {audit_extra}"
            seq = store.event(c, "admin_release" if is_admin else "release", repo, pkg, actor,
                              detail)
            c.execute("COMMIT")
            out = {"repo": repo, "id": pkg, "status": "available",
                   "previous_owner": row["owner"]}
            if audit_extra:          # 管理路径才带 event_seq（客户端端点字段零变化）
                out["event_seq"] = seq
            return out
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()


def submit(store: Store, repo: str, pkg: str, actor: str, note: str | None = None) -> dict:
    with store._write_lock:
        c = store.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = _pkg_row(c, repo, pkg)
            if row is None:
                raise NotFound(f"{repo}/{pkg}")
            if row["status"] == "completed":
                raise Conflict(f"包 {pkg} 已经是已完成状态")
            if row["status"] != "claimed":
                raise Conflict(f"包 {pkg} 未认领，不能提交（当前 {STATUS_LABEL[row['status']]}）")
            if row["owner"] != actor:
                raise Forbidden(f"包 {pkg} 由 {row['owner']} 认领，你不能提交")
            ts = now_iso()
            c.execute("UPDATE packages SET status='completed', completed_at=?, note=?,"
                      " updated_at=? WHERE repo=? AND id=?", (ts, note, ts, repo, pkg))
            store.event(c, "submit", repo, pkg, actor, note)
            c.execute("COMMIT")
            return {"repo": repo, "id": pkg, "status": "completed", "completed_at": ts}
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()


def reopen(store: Store, repo: str, pkg: str, to_status: str = "claimed",
           reason: str | None = None, owner: str | None = None,
           actor: str = "admin", audit_extra: str | None = None) -> dict:
    if to_status not in ("claimed", "available"):
        raise Conflict("to_status 只能是 claimed 或 available")
    with store._write_lock:
        c = store.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = _pkg_row(c, repo, pkg)
            if row is None:
                raise NotFound(f"{repo}/{pkg}")
            if row["status"] != "completed":
                raise Conflict(f"包 {pkg} 不是已完成状态（当前 {STATUS_LABEL[row['status']]}）")
            ts = now_iso()
            if to_status == "claimed":
                # v2 契约：打回给原认领人重做（owner 不动）。v3 调度会把这种
                # "claimed 但没有 open 租约"的包重新纳入可派工池，不会变成死包。
                c.execute("UPDATE packages SET note=NULL, status='claimed', completed_at=NULL,"
                          " downloaded_at=NULL, updated_at=? WHERE repo=? AND id=?", (ts, repo, pkg))
            else:
                c.execute("UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                          " completed_at=NULL, downloaded_at=NULL, updated_at=? WHERE repo=? AND id=?",
                          (ts, repo, pkg))
            detail = f"-> {STATUS_LABEL[to_status]}" + (f"; {reason}" if reason else "")
            if audit_extra:
                detail = f"{detail}; {audit_extra}"
            seq = store.event(c, "reopen", repo, pkg, actor, detail)
            c.execute("COMMIT")
            out = {"repo": repo, "id": pkg, "status": to_status, "owner": row["owner"]}
            if audit_extra:          # 管理路径才带 event_seq（客户端端点字段零变化）
                out["event_seq"] = seq
            return out
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()


def mark_downloaded(store: Store, repo: str, pkg: str, actor: str) -> dict:
    with store._write_lock:
        c = store.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = _pkg_row(c, repo, pkg)
            if row is None:
                raise NotFound(f"{repo}/{pkg}")
            if row["status"] != "claimed":
                raise Conflict(f"包 {pkg} 不在已认领状态")
            if row["owner"] != actor:
                raise Forbidden(f"包 {pkg} 由 {row['owner']} 认领")
            ts = now_iso()
            c.execute("UPDATE packages SET downloaded_at=?, updated_at=? WHERE repo=? AND id=?",
                      (ts, ts, repo, pkg))
            store.event(c, "downloaded", repo, pkg, actor, None)
            c.execute("COMMIT")
            return {"repo": repo, "id": pkg, "downloaded_at": ts}
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()


# ---------------------------------------------------------------- 查询

def package_rows(store: Store, repo: str | None = None, owner: str | None = None) -> list[dict]:
    c = store.conn()
    sql = ("SELECT p.*, " + package_progress.columns() + ", t.user_id AS reserved_for FROM packages p"
           " JOIN repos r ON r.repo=p.repo"
           + package_progress.JOIN +
           " LEFT JOIN package_targets t ON t.repo=p.repo AND t.pkg=p.id WHERE 1=1")
    args: list = []
    if repo:
        sql += " AND p.repo=?"
        args.append(repo)
    if owner:
        sql += " AND p.owner=?"
        args.append(owner)
    sql += " ORDER BY p.repo, p.id"
    rows = [dict(r) for r in c.execute(sql, args)]
    c.close()
    for r in rows:
        package_progress.decorate(r)
    return rows


def summary(store: Store, repo: str | None = None) -> dict:
    """用 SQL 聚合，避免把整张表读成 Python 对象（40 万包时是几百 MB）。

    分项字段（SPEC-parts §6，只增）：missing_tag / missing_nl 是"还没落账的那一侧"，
    口径与 v3.missing_parts 一致。老库迁移上来的包 status='completed' 时
    分项为空也算齐，所以改造前已完成的 135 片不会被误报成"全缺 tag"。
    """
    c = store.conn()
    where, args = ("WHERE repo=?", (repo,)) if repo else ("", ())
    cnt = v3.part_count_expr("p")
    row = c.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS s,"
        " COALESCE(SUM(status='available'),0) AS a,"
        " COALESCE(SUM(status='claimed'),0) AS cl,"
        " COALESCE(SUM(status='completed'),0) AS co,"
        " COALESCE(SUM(downloaded_at IS NOT NULL),0) AS d,"
        f" COALESCE(SUM({cnt['missing_tag']}),0) AS mt,"
        f" COALESCE(SUM({cnt['missing_nl']}),0) AS mn,"
        " COUNT(DISTINCT repo) AS r FROM packages p " + where, args).fetchone()
    progress = package_progress.counts(c, repo)
    c.close()
    return {**progress, "repos": 1 if repo else (row["r"] or 0), "total": row["n"],
            "available": row["a"], "claimed": row["cl"], "completed": row["co"],
            "downloaded": row["d"], "bytes_total": row["s"],
            "missing_tag": row["mt"] or 0, "missing_nl": row["mn"] or 0}


def user_stats(store: Store, repo: str | None = None) -> list[dict]:
    c = store.conn()
    sql = ("SELECT owner, COUNT(*) n,"
           " SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) done,"
           " SUM(CASE WHEN downloaded_at IS NOT NULL THEN 1 ELSE 0 END) dl"
           " FROM packages WHERE owner IS NOT NULL")
    args: list = []
    if repo:
        sql += " AND repo=?"
        args.append(repo)
    sql += " GROUP BY owner ORDER BY owner"
    rows = [dict(r) for r in c.execute(sql, args)]
    c.close()
    return rows


def key_grants(store: Store, kh: str) -> set[str] | None:
    """返回密钥可用的仓库集合；None 表示显式授予全部仓库。

    注意：空集 set() 表示「没有任何仓库权限」，必须在 _allowed 里判否。
    绝不能用「没有授权行 = 全部」这种 fail-open 推断：repo_remove 会删掉授权行，
    那样会把受限密钥静默升级为全仓库访问。
    """
    c = store.conn()
    row = c.execute("SELECT all_repos FROM keys WHERE hash=?", (kh,)).fetchone()
    rows = {r["repo"] for r in c.execute("SELECT repo FROM repo_grants WHERE key_hash=?", (kh,))}
    c.close()
    if row and row["all_repos"]:
        return None
    return rows


# ================================================================ 管理写通道（v3）
# 只新增 /admin/v1/* 与 state/writes.jsonl，不改任何客户端端点/字段（SPEC-v3 §11）。


class AdminError(Exception):
    """写通道的结构化错误：status/code/message/extra 直接映射成响应体。"""

    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}

    def body(self) -> dict:
        out = {"ok": False, "error": self.message, "code": self.code}
        out.update(self.extra)
        return out


def _artifact_store(store: Store) -> artifact_core.ArtifactStore:
    if store.artifacts is None:
        raise artifact_core.ArtifactError(403, "artifacts_disabled", "未启用产物接收目录")
    return store.artifacts


def check_artifact_binding(c: sqlite3.Connection, metadata: dict) -> dict:
    """Read-only provenance check. Never clear/reopen/reassign a dispatch row."""
    m = metadata
    package = c.execute("SELECT p.*,r.enabled FROM packages p JOIN repos r ON r.repo=p.repo"
                        " WHERE p.repo=? AND p.id=?", (m["repo"], m["pkg"])).fetchone()
    lease = c.execute("SELECT * FROM leases WHERE lease_id=?", (m["lease_id"],)).fetchone()
    device = c.execute("SELECT user_id FROM devices WHERE device_id=?", (m["device_id"],)).fetchone()
    user = c.execute("SELECT status FROM users WHERE user_id=?", (m["user_id"],)).fetchone()
    if not package or not lease or not device or not user:
        raise artifact_core.ArtifactError(404, "binding_not_found", "产物的包、租约或设备不存在")
    if not package["enabled"] or user["status"] != "active":
        raise artifact_core.ArtifactError(409, "binding_inactive", "仓库或申报身份当前不可接收")
    if (lease["repo"] != m["repo"] or lease["pkg"] != m["pkg"]
            or lease["device_id"] != m["device_id"] or lease["user_id"] != m["user_id"]
            or device["user_id"] != m["user_id"]):
        raise artifact_core.ArtifactError(409, "binding_mismatch", "租约、仓库、分片和设备归属不一致")
    # Status alone is not a lock: even available can have a foreign prelock.
    other = c.execute("SELECT 1 FROM leases WHERE repo=? AND pkg=? AND status='open'"
                      " AND lease_id<>? LIMIT 1", (m["repo"], m["pkg"], m["lease_id"])).fetchone()
    if other:
        raise artifact_core.ArtifactError(409, "package_occupied", "分片存在其他开放租约，拒绝产物接收")
    latest = c.execute("SELECT lease_id FROM leases WHERE repo=? AND pkg=?"
                       " ORDER BY issued_at DESC,rowid DESC LIMIT 1", (m["repo"], m["pkg"])).fetchone()
    if not latest or latest["lease_id"] != m["lease_id"]:
        raise artifact_core.ArtifactError(409, "stale_artifact_lease", "不是该包最新轮次的租约，拒绝旧轮次补传")
    if lease["kind"] != "active" or lease["status"] not in ("open", "done"):
        raise artifact_core.ArtifactError(409, "lease_not_uploadable", "仅当前 active/open 或本人 done 租约可上传")
    if package["owner"] != m["user_id"]:
        raise artifact_core.ArtifactError(409, "owner_changed", "分片当前不属于申报用户")
    if lease["status"] == "open":
        if package["status"] != "claimed":
            raise artifact_core.ArtifactError(409, "package_state_changed", "开放租约与包状态不一致")
        for part in v3.PARTS:
            if package[f"{part}_status"] == "done" and package[f"{part}_by"] != m["device_id"]:
                raise artifact_core.ArtifactError(409, "mixed_artifact_ownership", "已有分项不归本设备，有限版不接收混合归属归档")
    else:
        # v1 is a whole-package archive, not a merge of deliveries from several devices.
        if package["status"] != "completed":
            raise artifact_core.ArtifactError(409, "package_not_completed", "done 补传要求包仍是本人已完成状态")
        if any(package[f"{part}_status"] != "done" or package[f"{part}_by"] != m["device_id"]
               for part in v3.PARTS):
            raise artifact_core.ArtifactError(409, "artifact_provenance_incomplete", "分项完成归属不足，不能自动认作本设备的完整归档")
    return {"lease_status": lease["status"], "package_status": package["status"],
            "owner": package["owner"], "tag_status": package["tag_status"],
            "nl_status": package["nl_status"], "tag_by": package["tag_by"],
            "nl_by": package["nl_by"], "latest_lease_id": latest["lease_id"],
            "device_user": device["user_id"], "repo_enabled": package["enabled"]}


@contextlib.contextmanager
def artifact_binding_guard(store: Store, metadata: dict):
    # BEGIN IMMEDIATE also fences external CLI writers, but only during checks
    # and publication. This transaction never updates a dispatch business row.
    with store._write_lock:
        if not write_enabled():
            raise artifact_core.ArtifactError(403, "readonly", "服务端写通道已关闭，拒绝产物发布")
        _artifact_store(store)
        c = store.conn()
        try:
            with v3._Tx(c):
                check_artifact_binding(c, metadata)
                yield
        finally:
            c.close()


def artifact_admin_metadata(store: Store, target: dict, params: dict) -> dict:
    try:
        return artifact_core.normalize_metadata(target, params, _artifact_store(store).max_bytes)
    except artifact_core.ArtifactError as e:
        raise AdminError(e.status, e.code, e.message) from e


def admin_authorize_artifact(store: Store, target: dict, params: dict, actor: str) -> dict:
    metadata = artifact_admin_metadata(store, target, params)
    try:
        return _artifact_store(store).authorize(
            metadata, actor, lambda m: artifact_binding_guard(store, m))
    except artifact_core.ArtifactError as e:
        raise AdminError(e.status, e.code, e.message) from e


ACTION_SPECS: dict[str, dict] = {
    "artifact.authorize": {"kind": "artifact", "level": "danger", "label": "授权单包产物上传",
                           "multi": False, "priority": "P1", "cacheable": False,
                           "params": ("user_id", "device_id", "filename", "size", "sha256")},
    # kind     目标身份种类；level 确认档位；multi 是否支持批量；params 允许的参数键
    "package.reopen": {"kind": "package", "level": "danger", "label": "打回重做",
                       "multi": True, "priority": "P0", "params": ("to", "reason")},
    "package.release": {"kind": "package", "level": "danger", "label": "释放包",
                        "multi": True, "priority": "P1", "params": ("reason",)},
    "package.cancel_reservation": {"kind": "package", "level": "danger", "label": "取消待派",
                                   "multi": True, "priority": "P1", "params": ()},
    "package.assign": {"kind": "package", "level": "normal", "label": "指定分片给设备",
                       "multi": False, "priority": "P1",
                       "params": ("device_id", "replace_prelock")},
    "lease.release": {"kind": "lease", "level": "danger", "label": "释放租约",
                      "multi": True, "priority": "P0", "params": ("reason",)},
    "device.unassign": {"kind": "device", "level": "danger", "label": "解绑设备",
                        "multi": False, "priority": "P0", "params": ("reason",)},
    # 强行解绑：在"作废租约"之外把 devices 行也删掉，断开 device_id → user_id 的
    # 绑定。客户端换账号/重装后一直被 401 挡住的死结就靠这条解。
    "device.forget": {"kind": "device", "level": "danger", "label": "强制解绑设备",
                      "multi": False, "priority": "P1", "params": ("reason",)},
    "repo.enable": {"kind": "repo", "level": "normal", "label": "启用仓库",
                    "multi": True, "priority": "P0", "params": ()},
    "repo.disable": {"kind": "repo", "level": "danger", "label": "停用仓库",
                     "multi": True, "priority": "P0", "params": ()},
    "key.revoke": {"kind": "key", "level": "key", "label": "吊销密钥",
                   "multi": True, "priority": "P1", "params": ("reason",)},
    "user.rekey": {"kind": "user", "level": "key", "label": "重签密钥",
                   "multi": False, "priority": "P2", "params": (),
                   # 响应含一次性 secret：不进幂等缓存，重放只回"不可重放"墓碑
                   "cacheable": False},
    # 清掉一个用户的调度身份（租约+设备+用户行）。删完这台/这台机器再握手就是
    # 新用户，secret 以客户端本地那份为准——"以本地为准"的落地动作。
    "user.forget": {"kind": "user", "level": "danger", "label": "清空用户身份",
                    "multi": False, "priority": "P1", "params": ("reason",)},
    # 工作区（2026-11-13）：把"哪些仓库的活归谁做"变成面板上的配置。
    # 这四个动作只改归属，**不打断在跑的租约**——派工每次现读配置，
    # 已经发出去的包做完为止，下一个包才按新的仓库列表挑。
    "workspace.save": {"kind": "workspace", "level": "normal", "label": "保存工作区",
                       "multi": False, "priority": "P1",
                       "params": ("name", "repos", "note", "allow_manual_claim")},
    "workspace.assign": {"kind": "workspace", "level": "normal", "label": "指派工作区",
                         "multi": False, "priority": "P1",
                         "params": ("user_id", "device_id")},
    "workspace.unassign": {"kind": "workspace", "level": "normal", "label": "解除工作区归属",
                           "multi": False, "priority": "P1",
                           "params": ("user_id", "device_id")},
    # 删工作区会顺手解绑挂在上面的人，不然那些机器会永远派不到包（静默空转）
    "workspace.remove": {"kind": "workspace", "level": "danger", "label": "删除工作区",
                         "multi": False, "priority": "P0", "params": ("reason",)},
    # 仓库登记与数据刷新（2026-11-13）：面板上"添加仓库 / 刷新数据"
    "repo.add": {"kind": "repo", "level": "normal", "label": "添加仓库",
                 "multi": False, "priority": "P1",
                 "params": ("repo_type", "include", "manifest", "prune")},
    "repo.sync": {"kind": "repo", "level": "normal", "label": "强制刷新包缓存",
                  "multi": True, "priority": "P1", "params": ("force",)},
    # HF 凭据：密文落盘在数据目录，动作只记"已设置/未设置"，明文不进审计
    "hf.token": {"kind": "token", "level": "key", "label": "更新 HF Token",
                 "multi": False, "priority": "P0", "params": ("token",),
                 "cacheable": False},
}

# 影响面哈希的规范化字段（SPEC §3.4 逐字实现：测试按同一算法复算）。
KIND_FIELDS = {
    "artifact": ("repo", "id", "lease_id", "user_id", "device_id", "lease_status",
                 "package_status", "owner", "tag_status", "nl_status", "tag_by", "nl_by",
                 "latest_lease_id", "device_user", "repo_enabled", "artifact_state"),
    "package": ("repo", "id", "status"),
    "lease": ("lease_id", "repo", "pkg", "kind", "status"),
    "device": ("device_id", "status"),
    "repo": ("repo", "enabled"),
    "key": ("name", "role", "revoked"),
    "user": ("user_id", "status"),
    # 仓库列表不参与影响面哈希（顺序会抖），它绑在 params_hash 里
    "workspace": ("ws_id",),
    "token": ("name", "configured"),
}
TARGET_ID_FIELDS = {
    "artifact": ("repo", "id", "lease_id"),
    "package": ("repo", "id"),
    "lease": ("lease_id",),
    "device": ("device_id",),
    "repo": ("repo",),
    "key": ("name",),
    "user": ("user_id",),
    "workspace": ("ws_id",),
    "token": ("name",),
}


def _canon(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def impact_hash(action: str, items: list[dict], mutations: int) -> str:
    rows = []
    for it in items:
        kind = it["kind"]
        fields = KIND_FIELDS[kind]
        if action == "package.cancel_reservation" and kind == "package":
            fields += ("reserved_for", "assigned_at", "reservation_actor")
        rows.append([kind] + [str(it.get(f, "")) for f in fields])
    rows.sort()
    digest = hashlib.sha256(_canon({"action": action, "mutations": mutations,
                                    "items": rows}).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def target_hash(action: str, targets: list[dict]) -> str:
    kind = ACTION_SPECS[action]["kind"]
    fields = TARGET_ID_FIELDS[kind]
    rows = [[kind] + [str(t.get(f, "")) for f in fields] for t in targets]
    rows.sort()
    digest = hashlib.sha256(_canon({"action": action, "targets": rows}).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def params_hash(action: str, params: dict) -> str:
    """把"确认时看到的参数"也绑进 token。

    评审 F1：只绑 action/targets/impact 不够——reopen 的 to=claimed 与 to=available
    影响面（items）完全相同，token 却能换个 params 直接执行成功。params 必须参与绑定。
    """
    payload = {"action": action, "params": params or {}}
    digest = hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def _clean_ip(value) -> str:
    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        return "unknown"
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return "unknown"


def _norm_params(action: str, raw) -> dict:
    spec = ACTION_SPECS[action]
    allowed = set(spec["params"])
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AdminError(400, "bad_request", "params 必须是 JSON 对象")
    unknown = set(raw) - allowed
    if unknown:
        raise AdminError(400, "bad_request",
                         f"params 存在未知字段：{', '.join(sorted(unknown))}")
    out: dict = {}
    if "allow_manual_claim" in allowed and "allow_manual_claim" in raw:
        if type(raw["allow_manual_claim"]) is not bool:
            raise AdminError(400, "bad_request", "手动领取选项必须为布尔值")
        out["allow_manual_claim"] = raw["allow_manual_claim"]
    if "to" in allowed:
        to = raw.get("to", "claimed")
        if to not in ("claimed", "available"):
            raise AdminError(400, "bad_request", "参数 to 只能是 claimed 或 available")
        out["to"] = to
    if "reason" in allowed:
        reason = raw.get("reason")
        if reason is not None:
            if not isinstance(reason, str):
                raise AdminError(400, "bad_request", "参数 reason 必须是字符串")
            cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", reason).strip()[:200]
            if cleaned:
                out["reason"] = cleaned
    # 工作区的几个参数（2026-11-13）：都是短文本，截断比报错好——
    # 面板上来回敲名字时超长一位就整单失败，体验很差。
    for key, limit in (("name", 64), ("note", 200), ("repo_type", 16),
                       ("include", 200), ("manifest", 200), ("token", 512),
                       ("user_id", v3.MAX_ID), ("device_id", v3.MAX_ID)):
        if key in allowed:
            value = raw.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise AdminError(400, "bad_request", f"参数 {key} 必须是字符串")
                cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()[:limit]
                if cleaned:
                    out[key] = cleaned
    if "repos" in allowed:
        repos = raw.get("repos")
        if repos is not None:
            if not isinstance(repos, list) or any(not isinstance(x, str) for x in repos):
                raise AdminError(400, "bad_request", "参数 repos 必须是字符串数组")
            # 空数组是**有效值**：明确表示"这个工作区没有任何仓库"，不是"不限制"。
            out["repos"] = [x.strip() for x in repos if x.strip()][:200]
    if action == "artifact.authorize":
        for key in ("filename", "sha256"):
            value = raw.get(key)
            if not isinstance(value, str) or not value.strip():
                raise AdminError(400, "bad_metadata", f"缺少或非法的 {key}")
            out[key] = value.strip().lower() if key == "sha256" else value.strip()
        size = raw.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise AdminError(400, "bad_size", "size 必须是正整数")
        out["size"] = size
    for key in ("prune", "force", "replace_prelock"):
        if key in allowed:
            value = raw.get(key)
            if value is not None:
                if not isinstance(value, bool):
                    raise AdminError(400, "bad_request", "参数 %s 必须是布尔值" % key)
                out[key] = value
    return out


def _norm_targets(action: str, raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise AdminError(400, "bad_request", "targets 必须是非空数组")
    if len(raw) > MAX_BATCH:
        raise AdminError(400, "batch_too_large", f"单次最多 {MAX_BATCH} 个目标",
                         {"max_batch": MAX_BATCH})
    if action == "artifact.authorize" and len(raw) != 1:
        raise AdminError(400, "single_artifact_required", "产物授权每次只允许一个分片")
    fields = TARGET_ID_FIELDS[ACTION_SPECS[action]["kind"]]
    out: list[dict] = []
    seen: set[tuple] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise AdminError(400, "bad_request", "targets 的每一项都必须是对象")
        target: dict = {}
        for field in fields:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise AdminError(400, "bad_request", f"目标缺少字段 {field}")
            value = value.strip()
            if len(value) > 512:
                raise AdminError(400, "bad_request", f"目标字段 {field} 过长（最多 512 字符）")
            target[field] = value
        key = tuple(target[f] for f in fields)
        if key in seen:                 # 同一目标重复出现：去重保序、日志只记一次
            continue
        seen.add(key)
        out.append(target)
    if not out:
        raise AdminError(400, "bad_request", "targets 为空")
    return out


def _pkg_label(row: dict) -> str:
    label = STATUS_LABEL.get(row.get("status"), row.get("status") or "")
    owner = row.get("owner")
    text = f"{row.get('repo')}/{row.get('id')}"
    if label:
        text += f" · {label}"
    if owner:
        text += f" · {owner}"
    return text


def _item_key(item: dict) -> tuple:
    kind = str(item.get("kind") or "")
    return (kind,) + tuple(str(item.get(f, "")) for f in TARGET_ID_FIELDS.get(kind, ()))


def _sort_item(item: dict) -> tuple:
    primary = (item.get("repo") or item.get("lease_id") or item.get("device_id")
               or item.get("name") or item.get("user_id") or "")
    return (str(item.get("kind") or ""), str(primary), str(item.get("id") or ""))


def compute_impact(store: Store, action: str, targets: list[dict],
                   params: dict) -> tuple[dict, list[tuple[int, int]], list[dict]]:
    """只读：算出影响面。预览与执行共用同一函数，保证"重算结果可逐字比对"。"""
    spec = ACTION_SPECS[action]
    kind = spec["kind"]
    items: list[dict] = []
    warnings: list[str] = []
    per_target: list[tuple[int, int]] = []
    counts = {"targets": len(targets), "packages": 0, "leases": 0, "devices": 0,
              "repos": 0, "keys": 0, "users": 0}
    mutations = 0
    seen_dev: set[str] = set()
    seen_user: set[str] = set()
    c = store.conn()
    try:
        for target in targets:
            if kind == "artifact":
                metadata = artifact_admin_metadata(store, target, params)
                try:
                    binding = check_artifact_binding(c, metadata)
                    collection = _artifact_store(store).list_for_package(metadata["repo"], metadata["pkg"])
                except artifact_core.ArtifactError as e:
                    raise AdminError(e.status, e.code, e.message) from e
                items.append({"kind": "artifact", **target, **binding,
                              "user_id": metadata["user_id"], "device_id": metadata["device_id"],
                              "artifact_state": _canon(collection["items"]),
                              "label": f"{metadata['repo']}/{metadata['pkg']} → {metadata['device_id']}"})
                counts["artifacts"] = counts.get("artifacts", 0) + 1
                counts["packages"] += 1
                counts["leases"] += 1
                mutations += 1
                per_target.append((3, 1))
            elif kind == "package":
                row = c.execute("SELECT * FROM packages WHERE repo=? AND id=?",
                                (target["repo"], target["id"])).fetchone()
                if row is None:
                    raise AdminError(404, "not_found", f"包不存在：{target['repo']}/{target['id']}")
                row = dict(row)
                items.append({"kind": "package", "repo": row["repo"], "id": row["id"],
                              "status": row["status"], "owner": row["owner"],
                              "label": _pkg_label(row)})
                counts["packages"] += 1
                mutations += 1
                per_target.append((1, 1))
                label = STATUS_LABEL.get(row["status"], row["status"])
                if action == "package.cancel_reservation":
                    from assignments.cancellation import snapshot, CancellationError
                    try:
                        items[-1].update(snapshot(c, row['repo'], row['id']))
                    except CancellationError as e:
                        raise AdminError(409, "conflict", str(e)) from e
                if action == "package.reopen" and row["status"] != "completed":
                    warnings.append(f"{row['repo']}/{row['id']} 当前状态为 {label}，执行时将返回 409")
                if action == "package.release":
                    if row["status"] != "claimed":
                        warnings.append(f"{row['repo']}/{row['id']} 当前状态为 {label}，执行时将返回 409")
                    lease = c.execute(
                        "SELECT lease_id FROM leases WHERE repo=? AND pkg=? AND status='open' LIMIT 1",
                        (row["repo"], row["id"])).fetchone()
                    if lease:
                        counts["leases"] += 1
                        warnings.append(f"包 {row['repo']}/{row['id']} 存在 open 租约 "
                                        f"{lease['lease_id']}，建议改用「释放租约」")
                        per_target[-1] = (2, 1)
                if action == "package.assign":
                    device_id = params.get("device_id")
                    if not device_id:
                        raise AdminError(400, "bad_request", "请选择目标设备")
                    device = c.execute("SELECT * FROM devices WHERE device_id=?",
                                       (device_id,)).fetchone()
                    if device is None:
                        raise AdminError(404, "not_found", f"设备不存在：{device_id}")
                    device = dict(device)
                    reserved = c.execute("SELECT user_id FROM package_targets"
                                         " WHERE repo=? AND pkg=?",
                                         (row["repo"], row["id"])).fetchone()
                    if reserved and reserved["user_id"] != device["user_id"]:
                        warnings.append(f"该分片已定向给用户 {reserved['user_id']}，执行时将返回 409")
                    items.append({"kind": "device", "device_id": device_id,
                                  "status": device["status"],
                                  "label": f"目标设备 {device_id}（{device['user_id']}）"})
                    counts["devices"] += 1
                    per_target[-1] = (2, 1)
                    if row["status"] != "available":
                        warnings.append("该分片不是未认领状态，执行时将返回 409")
                    if v3._wanted_parts(device) and v3._part_rank(
                            row, v3._wanted_parts(device), v3._device_pref(device)) >= 2:
                        warnings.append("目标设备不能处理该分片缺失的分项，执行时将返回 409")
                    ws = v3.workspace_of(c, device)
                    if ws is not None and row["repo"] not in ws["repos"]:
                        warnings.append("目标设备的工作区不包含该仓库，执行时将返回 409")
                    if c.execute("SELECT 1 FROM leases WHERE repo=? AND pkg=?"
                                 " AND status='open'",
                                 (row["repo"], row["id"])).fetchone():
                        warnings.append("该分片已有租约，执行时将返回 409")
                    held = c.execute("SELECT lease_id,repo,pkg,kind FROM leases"
                                     " WHERE device_id=? AND status='open'",
                                     (device_id,)).fetchall()
                    prelocks = [x for x in held if x["kind"] == "prelock"]
                    if prelocks and params.get("replace_prelock"):
                        old = prelocks[0]
                        items.append({"kind": "lease", "lease_id": old["lease_id"],
                                      "label": f"释放原预锁 {old['repo']}/{old['pkg']}"})
                        counts["leases"] += 1
                        mutations += 1
                        per_target[-1] = (3, 2)
                        warnings.append(f"将释放该设备原预锁片 {old['repo']}/{old['pkg']}")
                    elif prelocks:
                        warnings.append("目标设备已有预锁片；勾选替换旧预锁后可继续")
                    if len(held) - len(prelocks) > 1 or len(prelocks) > 1:
                        warnings.append("目标设备租约数量异常，执行时将返回 409")
            elif kind == "lease":
                lease = c.execute("SELECT * FROM leases WHERE lease_id=?",
                                  (target["lease_id"],)).fetchone()
                if lease is None:
                    raise AdminError(404, "not_found", f"租约不存在：{target['lease_id']}")
                lease = dict(lease)
                pkg = c.execute("SELECT status, owner FROM packages WHERE repo=? AND id=?",
                                (lease["repo"], lease["pkg"])).fetchone()
                pkg = dict(pkg) if pkg else None
                items.append({"kind": "lease", "lease_id": lease["lease_id"],
                              "repo": lease["repo"], "pkg": lease["pkg"],
                              "kind_lease": lease["kind"], "status": lease["status"],
                              "device_id": lease["device_id"], "user_id": lease["user_id"],
                              "label": (f"租约 {lease['lease_id']} · {lease['repo']}/"
                                        f"{lease['pkg']} · 设备 {lease['device_id']}")})
                if pkg is not None:
                    items.append({"kind": "package", "repo": lease["repo"], "id": lease["pkg"],
                                  "status": pkg["status"], "owner": pkg["owner"],
                                  "label": _pkg_label({"repo": lease["repo"],
                                                       "id": lease["pkg"], **pkg})})
                    counts["packages"] += 1
                counts["leases"] += 1
                if lease["device_id"]:
                    seen_dev.add(lease["device_id"])
                if lease["user_id"]:
                    seen_user.add(lease["user_id"])
                mutations += 1
                per_target.append((1 + (1 if pkg else 0), 1))
                if lease["status"] != "open":
                    warnings.append(f"租约 {lease['lease_id']} 当前状态为 "
                                    f"{lease['status']}，执行时将返回 409")
            elif kind == "device":
                dev = c.execute("SELECT * FROM devices WHERE device_id=?",
                                (target["device_id"],)).fetchone()
                if dev is None:
                    raise AdminError(404, "not_found", f"设备不存在：{target['device_id']}")
                dev = dict(dev)
                leases = [dict(r) for r in c.execute(
                    "SELECT * FROM leases WHERE device_id=? AND status='open'",
                    (dev["device_id"],))]
                items.append({"kind": "device", "device_id": dev["device_id"],
                              "status": dev["status"], "user_id": dev["user_id"],
                              "label": f"设备 {dev['device_id']} · {dev['status']}"
                                       f" · 用户 {dev['user_id'] or '-'}"})
                pkg_n = 0
                for lease in leases:
                    items.append({"kind": "lease", "lease_id": lease["lease_id"],
                                  "repo": lease["repo"], "pkg": lease["pkg"],
                                  "kind_lease": lease["kind"], "status": lease["status"],
                                  "device_id": lease["device_id"],
                                  "user_id": lease["user_id"],
                                  "label": (f"租约 {lease['lease_id']} · {lease['repo']}/"
                                            f"{lease['pkg']}")})
                    pkg = c.execute("SELECT status, owner FROM packages WHERE repo=? AND id=?",
                                    (lease["repo"], lease["pkg"])).fetchone()
                    if pkg is not None and lease["kind"] in ("active", "offline"):
                        pkg_n += 1
                        items.append({"kind": "package", "repo": lease["repo"],
                                      "id": lease["pkg"], "status": pkg["status"],
                                      "owner": pkg["owner"],
                                      "label": _pkg_label({"repo": lease["repo"],
                                                           "id": lease["pkg"], **dict(pkg)})})
                counts["devices"] += 1
                counts["leases"] += len(leases)
                counts["packages"] += pkg_n
                # 设备动作 multi=False，单目标；改绑删除也算一次变更，否则"没有租约
                # 的设备"影响面是 0，确认框会退化成"请输入 0 以确认"
                mut_now = len(leases) + (1 if action == "device.forget" else 0)
                mutations += mut_now
                if action == "device.forget" and not leases:
                    warnings.append(f"设备 {dev['device_id']} 当前没有在跑的租约，"
                                    f"本次只会删除它与用户 {dev['user_id']} 的绑定")
                per_target.append((1 + len(leases) + pkg_n, mut_now))
            elif kind == "repo":
                row = c.execute("SELECT * FROM repos WHERE repo=?", (target["repo"],)).fetchone()
                if row is None:
                    # 添加仓库时它本来就还不存在；其余动作才按 404 处理
                    if action != "repo.add":
                        raise AdminError(404, "not_found", f"仓库不存在：{target['repo']}")
                    items.append({"kind": "repo", "repo": target["repo"], "enabled": True,
                                  "label": f"仓库 {target['repo']} · 新登记"})
                    counts["repos"] += 1
                    mutations += 1
                    per_target.append((1, 1))
                    token = hf_token_status(store)
                    if not token["configured"]:
                        warnings.append("尚未配置 HF token：执行时会失败，先在面板里填一次")
                    continue
                row = dict(row)
                enabled = bool(row["enabled"])
                items.append({"kind": "repo", "repo": row["repo"], "enabled": enabled,
                              "label": f"仓库 {row['repo']} · {'启用' if enabled else '停用'}"})
                counts["repos"] += 1
                mutations += 1
                per_target.append((1, 1))
                if action == "repo.enable" and enabled:
                    warnings.append(f"仓库 {row['repo']} 已是启用状态，执行时将返回 409")
                if action == "repo.disable" and not enabled:
                    warnings.append(f"仓库 {row['repo']} 已是停用状态，执行时将返回 409")
                if action == "repo.add":
                    warnings.append(f"仓库 {row['repo']} 已经登记，执行时将返回 409")
                if action == "repo.sync":
                    meta = tree_cache_meta(store, row["repo"], row["repo_type"])
                    total = c.execute("SELECT COUNT(*) n FROM packages WHERE repo=?",
                                      (row["repo"],)).fetchone()["n"]
                    counts["packages"] += total
                    per_target[-1] = (1 + total, 1)
                    if params.get("force"):
                        warnings.append("将直接从 HF 重新读取完整文件树并补录分片；已执行及已完成记录保留")
                    elif meta is None:
                        warnings.append("还没有缓存，首次刷新需要联网拉文件树（会慢一些）")
                    else:
                        warnings.append(f"上次拉取于 {meta['fetched_at']}；"
                                        f"有缓存时先用缓存，过期了才后台补")
                if action == "repo.disable":
                    claimed = c.execute(
                        "SELECT COUNT(*) n FROM packages WHERE repo=? AND status<>'available'",
                        (row["repo"],)).fetchone()["n"]
                    open_leases = c.execute(
                        "SELECT COUNT(*) n FROM leases WHERE repo=? AND status='open'",
                        (row["repo"],)).fetchone()["n"]
                    devs = c.execute(
                        "SELECT COUNT(DISTINCT device_id) n FROM leases WHERE repo=? AND status='open'",
                        (row["repo"],)).fetchone()["n"]
                    counts["packages"] += claimed
                    counts["leases"] += open_leases
                    counts["devices"] += devs
                    per_target[-1] = (1 + claimed + open_leases + devs, 1)
            elif kind == "token":
                st = hf_token_status(store)
                items.append({"kind": "token", "name": "hf", "configured": st["configured"],
                              "label": ("HF Token · 已配置（%s）" % st["masked"]
                                        if st["configured"] else "HF Token · 未配置")})
                if st["source"] == "env":
                    warnings.append("当前生效的是环境变量里的 token；保存后会以面板这份为准")
                mutations += 1
                per_target.append((1, 1))
            elif kind == "key":
                row = c.execute("SELECT name, role, revoked FROM keys WHERE name=?",
                                (target["name"],)).fetchone()
                if row is None:
                    raise AdminError(404, "not_found", f"密钥不存在：{target['name']}")
                row = dict(row)
                revoked = bool(row["revoked"])
                items.append({"kind": "key", "name": row["name"], "role": row["role"],
                              "revoked": revoked,
                              "label": f"密钥 {row['name']} · {row['role']}"
                                       + ("（已吊销）" if revoked else "")})
                counts["keys"] += 1
                mutations += 1
                per_target.append((1, 1))
                if revoked:
                    warnings.append(f"密钥 {row['name']} 已被吊销，执行时将返回 409")
            elif kind == "user":
                row = c.execute("SELECT user_id, status FROM users WHERE user_id=?",
                                (target["user_id"],)).fetchone()
                if row is None:
                    raise AdminError(404, "not_found", f"用户不存在：{target['user_id']}")
                row = dict(row)
                devs = [dict(r) for r in c.execute(
                    "SELECT device_id FROM devices WHERE user_id=?", (row["user_id"],))]
                open_leases = c.execute(
                    "SELECT COUNT(*) n FROM leases l JOIN devices d ON d.device_id=l.device_id"
                    " WHERE d.user_id=? AND l.status='open'", (row["user_id"],)).fetchone()["n"]
                items.append({"kind": "user", "user_id": row["user_id"],
                              "status": row["status"],
                              "label": f"用户 {row['user_id']} · {row['status']}"
                                       f" · {len(devs)} 台设备"})
                counts["users"] += 1
                counts["devices"] += len(devs)
                mut_now = 1
                if action == "user.forget":
                    # 解绑用户会连带删设备、作废在跑租约，影响面必须如实计入
                    counts["leases"] += open_leases
                    mut_now += len(devs) + open_leases
                    for dev in devs:
                        items.append({"kind": "device", "device_id": dev["device_id"],
                                      "user_id": row["user_id"],
                                      "label": f"设备 {dev['device_id']}（随用户一并解绑）"})
                mutations += mut_now
                per_target.append((1 + len(devs) + open_leases, mut_now))
            elif kind == "workspace":
                ws_id = target["ws_id"]
                row = c.execute("SELECT * FROM workspaces WHERE ws_id=?",
                                (ws_id,)).fetchone()
                if row is None and action != "workspace.save":
                    raise AdminError(404, "not_found", f"工作区不存在：{ws_id}")
                row = dict(row) if row else None
                try:
                    cur_repos = json.loads((row or {}).get("repos") or "[]")
                except ValueError:
                    cur_repos = []
                items.append({"kind": "workspace", "ws_id": ws_id,
                              "label": ("工作区 %s · 新建" % ws_id if row is None
                                        else "工作区 %s · %d 个仓库"
                                             % (ws_id, len(cur_repos)))})
                # 挂在这台工作区上的设备会被这次配置变更直接影响，逐台列出来
                bound = [dict(r) for r in c.execute(
                    "SELECT device_id, user_id, status FROM devices WHERE workspace_id=?",
                    (ws_id,))]
                bound_users = [dict(r) for r in c.execute(
                    "SELECT user_id, status FROM users WHERE workspace_id=?", (ws_id,))]
                for dev in bound:
                    items.append({"kind": "device", "device_id": dev["device_id"],
                                  "user_id": dev["user_id"], "status": dev["status"],
                                  "label": f"设备 {dev['device_id']} · 挂在工作区 {ws_id}"})
                counts["devices"] += len(bound)
                counts["users"] += len(bound_users)

                if action == "workspace.save":
                    repos = params.get("repos")
                    if repos is None:
                        repos = cur_repos
                    known = {r["repo"] for r in c.execute("SELECT repo FROM repos")}
                    missing = [r for r in repos if r not in known]
                    if missing:
                        warnings.append("这些仓库还没登记，执行时会返回 400："
                                        + "、".join(missing[:5]))
                    elif not repos:
                        warnings.append("这个工作区没有仓库：绑上去的设备一个包都拿不到"
                                        "（不会退回「不限制」）")
                    elif sorted(repos) == sorted(cur_repos):
                        warnings.append("仓库列表与当前一致，本次只更新名称/备注")
                    mutations += 1
                    per_target.append((1 + len(bound) + len(bound_users), 1))
                elif action == "workspace.remove":
                    if not bound and not bound_users:
                        warnings.append("这个工作区上没有绑定任何账号或设备，删除不影响派工")
                    else:
                        warnings.append(f"删除会同时解绑 {len(bound)} 台设备、"
                                        f"{len(bound_users)} 个账号，它们将回到「不限制仓库」")
                    mutations += 1 + len(bound) + len(bound_users)
                    per_target.append((1 + len(bound) + len(bound_users),
                                       1 + len(bound) + len(bound_users)))
                else:                               # assign / unassign
                    user_id = params.get("user_id")
                    device_id = params.get("device_id")
                    if not user_id and not device_id:
                        raise AdminError(400, "bad_request", "要指定 user_id 或 device_id")
                    mut_now = 0
                    if device_id:
                        dev = c.execute("SELECT device_id, user_id, status FROM devices"
                                        " WHERE device_id=?", (device_id,)).fetchone()
                        if dev is None:
                            raise AdminError(404, "not_found", f"设备不存在：{device_id}")
                        dev = dict(dev)
                        items.append({"kind": "device", "device_id": dev["device_id"],
                                      "user_id": dev["user_id"], "status": dev["status"],
                                      "label": f"设备 {dev['device_id']} · 用户 "
                                               f"{dev['user_id'] or '-'}"})
                        mut_now += 1
                    if user_id:
                        usr = c.execute("SELECT user_id, status FROM users WHERE user_id=?",
                                        (user_id,)).fetchone()
                        if usr is None:
                            raise AdminError(404, "not_found", f"用户不存在：{user_id}")
                        usr = dict(usr)
                        items.append({"kind": "user", "user_id": usr["user_id"],
                                      "status": usr["status"],
                                      "label": f"用户 {usr['user_id']} · {usr['status']}"})
                        mut_now += 1
                    if action == "workspace.unassign":
                        warnings.append("解除归属后该账号/设备回到「不限制仓库」，"
                                        "会开始抢所有启用仓库的包")
                    mutations += mut_now
                    per_target.append((mut_now, mut_now))
            else:                                   # pragma: no cover - 目录常量写错时兜底
                raise AdminError(400, "bad_request", f"未知动作：{action}")
    finally:
        c.close()

    if kind == "lease":
        counts["devices"] = len(seen_dev)
        counts["users"] = len(seen_user)

    items.sort(key=_sort_item)
    impact = {
        "summary": _impact_summary(action, targets, params, counts, mutations),
        "mutations": mutations,
        "batch_required": mutations > BATCH_THRESHOLD,
        "counts": counts,
        "total": sum(v for k, v in counts.items() if k != "targets"),
        "items": items[:IMPACT_ITEMS_MAX],
        "truncated": len(items) > IMPACT_ITEMS_MAX,
        "hash": impact_hash(action, items, mutations),   # 哈希覆盖全集，不受截断影响
        "warnings": warnings,
    }
    return impact, per_target, items


def _impact_summary(action: str, targets: list[dict], params: dict,
                    counts: dict, mutations: int) -> str:
    if action == "artifact.authorize":
        return (f"授权 {mutations} 份归档（{params.get('size', 0)} 字节）上传给设备 "
                f"{params.get('device_id', '-')}；不更改派工或完成状态")
    if action == "package.assign":
        t = targets[0]
        tail = "；同时释放原预锁片" if counts.get("leases") else ""
        return (f"将 {t['repo']}/{t['id']} 定向派给设备 {params.get('device_id', '-')}"
                f"；设备下一次领工时接收{tail}")
    if action == "package.reopen":
        tail = "保留原认领人" if params.get("to", "claimed") == "claimed" else "退回公共池"
        return f"打回 {mutations} 个已完成包（{tail}）"
    if action == "package.release":
        return f"释放 {mutations} 个已认领包，包回到公共池"
    if action == "package.cancel_reservation":
        return f"取消 {mutations} 个分片的待派记录，分片重新参与调度"
    if action == "lease.release":
        return f"释放 {mutations} 条租约，{counts['packages']} 个包回到公共池"
    if action == "device.unassign":
        dev = targets[0]["device_id"] if targets else "-"
        return (f"解绑设备 {dev}：作废 {mutations} 条租约，"
                f"{counts['packages']} 个包退回公共池")
    if action == "device.forget":
        dev = targets[0]["device_id"] if targets else "-"
        return (f"强制解绑设备 {dev}：作废 {counts['leases']} 条租约、"
                f"{counts['packages']} 个包退回公共池，并删除该设备的用户绑定")
    if action == "repo.enable":
        names = "、".join(t["repo"] for t in targets)
        return f"启用仓库 {names}"
    if action == "repo.disable":
        names = ("、".join(t["repo"] for t in targets) if len(targets) > 1
                 else (targets[0]["repo"] if targets else "-"))
        return (f"停用仓库 {names}：将停止派工，影响 {counts['packages']} 个处理中包、"
                f"{counts['leases']} 条租约、{counts['devices']} 台设备")
    if action == "key.revoke":
        return f"吊销 {mutations} 个密钥（立即生效、不可撤销）"
    if action == "user.rekey":
        return f"解除 {mutations} 个用户的 secret 绑定（下次握手以客户端本地 secret 为准）"
    if action == "user.forget":
        who = targets[0]["user_id"] if targets else "-"
        return (f"清空用户 {who}：删除 {counts['devices']} 台设备、作废 "
                f"{counts['leases']} 条租约、删除用户行（下次握手按本机 secret 重新登记）")
    if action == "workspace.save":
        who = targets[0]["ws_id"] if targets else "-"
        return (f"保存工作区 {who}：{counts['devices']} 台设备、{counts['users']} 个账号"
                f"将按新的仓库列表派工（在跑的包做完才切换）")
    if action == "workspace.assign":
        who = targets[0]["ws_id"] if targets else "-"
        return f"指派到工作区 {who}：{mutations} 个目标，下一个包生效"
    if action == "workspace.unassign":
        return ("解除工作区归属：目标回到「不限制仓库」，"
                "会开始抢所有启用仓库的包")
    if action == "workspace.remove":
        who = targets[0]["ws_id"] if targets else "-"
        return (f"删除工作区 {who}：解绑 {counts['devices']} 台设备、"
                f"{counts['users']} 个账号")
    return f"{action}：影响 {mutations} 项"


class _ConfirmStore:
    """confirm token 登记表：只存 sha256(token)，进程内、带锁、过期即清。

    重启即全部失效（安全默认）：token 不落盘，writes.jsonl 里只写 token_id。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def _gc(self, now: float) -> None:
        dead = [k for k, v in self._items.items() if v["expires_at"] < now - 5]
        for key in dead:
            self._items.pop(key, None)

    def issue(self, rec: dict) -> tuple[str, str, float]:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        token_id = "ct_" + digest[:12]
        now = time.time()
        item = dict(rec, token_id=token_id, created_at=now,
                    expires_at=now + CONFIRM_TTL, used=False)
        with self._lock:
            self._gc(now)
            self._items[digest] = item
        return token, token_id, item["expires_at"]

    def check(self, token, action: str, actor: str, session_fp: str,
              client_ip: str) -> dict:
        if not isinstance(token, str) or not token.strip():
            raise AdminError(428, "confirm_required", "需要二次确认，请先预览影响面")
        digest = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
        with self._lock:
            rec = self._items.get(digest)
            if rec is None:
                raise AdminError(428, "confirm_required", "二次确认无效，请重新确认")
            now = time.time()
            if rec["used"]:
                raise AdminError(428, "confirm_replayed", "二次确认已使用，请重新确认")
            if now > rec["expires_at"]:
                self._items.pop(digest, None)
                raise AdminError(428, "confirm_expired", "二次确认已过期，请重新确认")
            if rec["action"] != action:
                raise AdminError(400, "action_mismatch", "确认的动作与请求不一致",
                                 {"diff": {"action": {"confirmed": rec["action"],
                                                      "actual": action}}})
            if (rec["actor"] != actor or rec["session_fp"] != session_fp
                    or rec["client_ip"] != client_ip):
                raise AdminError(428, "confirm_actor_mismatch",
                                 "二次确认与当前会话不匹配，请重新确认")
            rec["used"] = True                  # 执行开始即原子占用，无论成败都不再可用
            return dict(rec)


CONFIRM_STORE = _ConfirmStore()


def _idem_digest(actor: str, action: str, key: str) -> str:
    return hashlib.sha256(f"{actor}|{action}|{key}".encode("utf-8")).hexdigest()


def _idem_marker(actor: str, key: str) -> str:
    return hashlib.sha256(f"{actor}|*|{key}".encode("utf-8")).hexdigest()


def _idem_get(store: Store, actor: str, action: str, key: str) -> dict | None:
    c = store.conn()
    marker = c.execute("SELECT response FROM write_idem WHERE k=?",
                       (_idem_marker(actor, key),)).fetchone()
    row = c.execute("SELECT response FROM write_idem WHERE k=?",
                    (_idem_digest(actor, action, key),)).fetchone()
    c.close()
    if marker is not None:
        try:
            seen = json.loads(marker["response"]).get("action")
        except (ValueError, AttributeError):
            seen = None
        if seen and seen != action:
            raise AdminError(409, "idempotency_conflict", "幂等键已用于不同请求")
    if row is None:
        return None
    try:
        return json.loads(row["response"])
    except ValueError:
        return None


def _idem_put(store: Store, actor: str, action: str, key: str, payload: dict) -> None:
    c = store.conn()
    c.execute("INSERT OR IGNORE INTO write_idem(k,ts,response) VALUES(?,?,?)",
              (_idem_marker(actor, key), time.time(),
               json.dumps({"action": action}, ensure_ascii=False)))
    c.execute("INSERT OR REPLACE INTO write_idem(k,ts,response) VALUES(?,?,?)",
              (_idem_digest(actor, action, key), time.time(),
               json.dumps(payload, ensure_ascii=False)))
    c.close()


def idem_gc(store: Store) -> int:
    c = store.conn()
    cur = c.execute("DELETE FROM write_idem WHERE ts < ?", (time.time() - WRITE_IDEM_TTL,))
    n = cur.rowcount
    c.close()
    return n


# ---- 业务包装：复用既有实现，不重写业务逻辑 ----------------------------

def admin_release_leases(store: Store, lease_ids: list[str], actor: str,
                         reason: str = "admin_release",
                         audit_extra: str | None = None) -> dict:
    """管理路径释放任意租约。SQL 语义与 dispatch._release_lease_impl 完全一致。"""
    with store._write_lock:
        c = store.conn()
        released: list[dict] = []
        back = 0
        try:
            c.execute("BEGIN IMMEDIATE")
            for lease_id in lease_ids:
                row = c.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
                if row is None:
                    raise NotFound(f"租约不存在：{lease_id}")
                lease = dict(row)
                if lease["status"] != "open":
                    raise Conflict(f"租约 {lease_id} 已 {lease['status']}，无需释放")
                c.execute("UPDATE leases SET status='released', closed_reason=? WHERE lease_id=?",
                          ((reason or "admin_release")[:60], lease_id))
                c.execute("DELETE FROM package_queue WHERE repo=? AND pkg=?",(lease["repo"],lease["pkg"]))
                offline_tasks.refresh(c,lease["device_id"])
                package_back = False
                if lease["kind"] in ("active", "offline"):
                    cur = c.execute(
                        "UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                        " downloaded_at=NULL, updated_at=? WHERE repo=? AND id=? AND status='claimed'",
                        (now_iso(), lease["repo"], lease["pkg"]))
                    package_back = bool(cur.rowcount)
                    back += 1 if package_back else 0
                detail = f"lease={lease_id}; admin=1"
                if audit_extra:
                    detail = f"{detail}; {audit_extra}"
                seq = store.event(c, "v3_release", lease["repo"], lease["pkg"], actor, detail)
                released.append({"lease_id": lease_id, "released": True,
                                 "package_back": package_back, "event_seq": seq})
            c.execute("COMMIT")
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()
    return {"released": released, "packages_back": back}


def admin_unassign_device(store: Store, device_id: str, actor: str,
                          reason: str = "admin_release",
                          audit_extra: str | None = None) -> dict:
    """按设备解绑 = 作废该设备全部 open 租约。

    直接复用 dispatch.release_device（不改其实现）；为拿到带 repo/pkg 的审计，
    调用前先快照 open 租约，调用后自己补事件（不传 on_event）。
    """
    with store._write_lock:
        c = store.conn()
        try:
            dev = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
            if dev is None:
                raise NotFound(f"设备不存在：{device_id}")
            leases = [dict(r) for r in c.execute(
                "SELECT * FROM leases WHERE device_id=? AND status='open'", (device_id,))]
            res = v3.release_device(c, device_id, reason or "admin_release", on_event=None)
            seq = None
            for lease in leases:
                detail = (f"device={device_id}; lease={lease['lease_id']}; "
                          f"reason={reason or 'admin_release'}")
                if audit_extra:
                    detail = f"{detail}; {audit_extra}"
                seq = store.event(c, "v3_release_device", lease["repo"], lease["pkg"],
                                  actor, detail)
            detail = f"device={device_id}; released={res['released']}"
            if audit_extra:
                detail = f"{detail}; {audit_extra}"
            seq = store.event(c, "v3_release_device", None, None, actor, detail)
            return {"device_id": device_id, "released": res["released"],
                    "packages_back": len([x for x in leases if x["kind"] in ("active", "offline")]),
                    "event_seq": seq}
        finally:
            c.close()


def admin_forget_device(store: Store, device_id: str, actor: str,
                        reason: str = "admin_forget",
                        audit_extra: str | None = None) -> dict:
    """强行解绑：作废租约 + 删除 devices 行（断掉 device_id → user_id 绑定）。

    与 admin_unassign_device 的差别只在最后一步：那边设备仍然"属于"某个用户，
    客户端换账号再握手会被"设备已绑定到用户 X"挡回去，管理员除了手改数据库
    没有别的路。这里把绑定擦掉，下一个 handshake 就是干净的新登记。
    """
    with store._write_lock:
        c = store.conn()
        try:
            dev = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
            if dev is None:
                raise NotFound(f"设备不存在：{device_id}")
            dev = dict(dev)
            leases = [dict(r) for r in c.execute(
                "SELECT * FROM leases WHERE device_id=? AND status='open'", (device_id,))]
            res = v3.forget_device(c, device_id, reason or "admin_forget", on_event=None)
            seq = None
            for lease in leases:
                detail = (f"device={device_id}; lease={lease['lease_id']}; "
                          f"user={dev['user_id']}; reason={reason or 'admin_forget'}")
                if audit_extra:
                    detail = f"{detail}; {audit_extra}"
                seq = store.event(c, "v3_device_forget", lease["repo"], lease["pkg"],
                                  actor, detail)
            detail = (f"device={device_id}; user={dev['user_id']}; "
                      f"released={res['released']}; binding=deleted")
            if audit_extra:
                detail = f"{detail}; {audit_extra}"
            seq = store.event(c, "v3_device_forget", None, None, actor, detail)
            return {"device_id": device_id, "user_id": dev["user_id"],
                    "released": res["released"],
                    "packages_back": len([x for x in leases if x["kind"] in ("active", "offline")]),
                    "forgotten": True, "event_seq": seq}
        finally:
            c.close()


def admin_creds(store: Store) -> dict:
    """面板 readonly 渲染用：token 状态（只给掩码）、缓存年龄、动作能力。

    不带仓库列表——那是 /admin/v1/workspaces 的事，两边各管各的。
    """
    status = hf_token_status(store)
    c = store.conn()
    try:
        caches = []
        for r in c.execute("SELECT repo, repo_type FROM repos ORDER BY repo"):
            meta = tree_cache_meta(store, r["repo"], r["repo_type"])
            caches.append({"repo": r["repo"],
                           "cached": meta is not None,
                           "cached_at": (meta or {}).get("fetched_at"),
                           "age_sec": (meta or {}).get("age_sec"),
                           "stale": ((meta or {}).get("age_sec") or 0) > TREE_CACHE_TTL})
    finally:
        c.close()
    return {"ok": True, "hf_token": status, "ttl_sec": TREE_CACHE_TTL,
            "crypto": _has_crypto(), "caches": caches, "generated_at": now_iso()}


def _has_crypto() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def _ws_call(fn, *args, **kwargs):
    """dispatch 层的校验异常是"输入不合法"（仓库没登记、账号不存在），
    必须转成 400 —— 直接漏出去会被兜底成 500「服务端内部错误」，
    面板上看不出是自己填错了。"""
    try:
        return fn(*args, **kwargs)
    except v3.AuthError as e:
        raise AdminError(400, "bad_request", str(e)) from e


def _ws_params(store: Store, ws_id: str, params: dict) -> dict:
    """把"没填的字段"补成当前值。

    预览与执行必须看到同一份 repos：影响面里用的是现库的值，执行时若
    ws_create 收到 None 就会把仓库列表清空——一次保存名字的误操作就能
    让整组机器停工。所以这里显式兜底。
    """
    out = dict(params)
    if "repos" not in out:
        c = store.conn()
        try:
            row = c.execute("SELECT repos FROM workspaces WHERE ws_id=?", (ws_id,)).fetchone()
        finally:
            c.close()
        try:
            out["repos"] = json.loads((row or {})["repos"] or "[]") if row else []
        except (ValueError, KeyError):
            out["repos"] = []
    if "name" not in out:
        c = store.conn()
        try:
            row = c.execute("SELECT name FROM workspaces WHERE ws_id=?", (ws_id,)).fetchone()
        finally:
            c.close()
        if row is not None:
            out["name"] = row["name"]
    return out


def admin_workspace_save(store: Store, ws_id: str, params: dict, actor: str,
                         audit_extra: str | None = None) -> dict:
    merged = _ws_params(store, ws_id, params)
    with store._write_lock:
        c = store.conn()
        try:
            ws = _ws_call(v3.ws_create, c, ws_id, merged.get("name") or "",
                          merged.get("repos"), merged.get("note") or "", merged.get("allow_manual_claim"))
            detail = (f"ws={ws['ws_id']}; repos={len(ws['repos'])}; "
                      f"users={len(ws['users'])}; devices={len(ws['devices'])}; "
                      f"list={','.join(ws['repos'][:5])}")
            if audit_extra:
                detail = f"{detail}; {audit_extra}"
            seq = store.event(c, "workspace_save", None, None, actor, detail)
            return {"ws_id": ws["ws_id"], "name": ws["name"], "repos": ws["repos"],
                    "users": ws["users"], "devices": ws["devices"], "allow_manual_claim":bool(ws["allow_manual_claim"]), "event_seq": seq}
        finally:
            c.close()


def admin_assign_package(store: Store, repo: str, pkg: str, device_id: str,
                         actor: str, audit_extra: str = "",
                         replace_prelock: bool = False) -> dict:
    """管理员定向派一片；复用 worker 已认识的 active/prelock 租约协议。"""
    with store._write_lock:
        c = store.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            package = c.execute("SELECT p.*, r.enabled FROM packages p JOIN repos r"
                                " ON r.repo=p.repo WHERE p.repo=? AND p.id=?",
                                (repo, pkg)).fetchone()
            if package is None:
                raise NotFound(f"包不存在：{repo}/{pkg}")
            package = dict(package)
            if not package["enabled"]:
                raise Conflict("仓库已停用，请先启用")
            if package["status"] != "available":
                raise Conflict("分片不是未认领状态，不能定向派工")
            if c.execute("SELECT 1 FROM leases WHERE repo=? AND pkg=? AND status='open'",
                         (repo, pkg)).fetchone():
                raise Conflict("分片已有开放租约，请先释放现有租约")
            device = c.execute("SELECT * FROM devices WHERE device_id=?",
                               (device_id,)).fetchone()
            if device is None:
                raise NotFound(f"设备不存在：{device_id}")
            device = dict(device)
            if device["status"] in ("blocked", "archived") or device_id.startswith("offline-"):
                raise Conflict("目标设备已停用或属于离线任务，请选择普通设备")
            queued = c.execute("SELECT device_id FROM package_queue WHERE repo=? AND pkg=?",
                               (repo, pkg)).fetchone()
            if queued and queued["device_id"] != device_id:
                raise Conflict("分片已进入其他设备的待处理队列，请先归还该队列")
            target = c.execute("SELECT user_id FROM package_targets WHERE repo=? AND pkg=?",
                               (repo, pkg)).fetchone()
            if target and target["user_id"] != device["user_id"]:
                raise Conflict(f"分片已定向给用户 {target['user_id']}，不能派给其他设备")
            ws = v3.workspace_of(c, device)
            if ws is not None and repo not in ws["repos"]:
                raise Conflict("目标设备的工作区不包含该仓库")
            want = v3._wanted_parts(device)
            if want and v3._part_rank(package, want, v3._device_pref(device)) >= 2:
                raise Conflict("目标设备不能处理该分片缺失的分项")
            held = [dict(r) for r in c.execute(
                "SELECT lease_id,repo,pkg,kind FROM leases WHERE device_id=? AND status='open'",
                (device_id,))]
            prelocks = [r for r in held if r["kind"] == "prelock"]
            active = [r for r in held if r["kind"] == "active"]
            if len(active) > 1 or len(prelocks) > 1:
                raise Conflict("目标设备租约数量异常，请先检查租约")
            if prelocks and not replace_prelock:
                raise Conflict("设备已有预锁片；勾选替换旧预锁或等待处理完成")
            replaced = prelocks[0] if prelocks else None
            if replaced:
                c.execute("UPDATE leases SET status='released', closed_reason=?"
                          " WHERE lease_id=? AND status='open'",
                          ("admin_replace_prelock", replaced["lease_id"]))
            kind = "prelock" if active else "active"
            attempt = c.execute("SELECT COUNT(*) n FROM leases WHERE repo=? AND pkg=?",
                                (repo, pkg)).fetchone()["n"] + 1
            lease = v3._open_lease(c, device_id, package, kind, attempt)
            detail = f"device={device_id}; lease={lease['lease_id']}; kind={kind}"
            if replaced:
                detail += f"; replaced={replaced['lease_id']}:{replaced['repo']}/{replaced['pkg']}"
            if audit_extra:
                detail += f"; {audit_extra}"
            seq = store.event(c, "v3_manual_assign", repo, pkg, actor, detail)
            c.execute("COMMIT")
            return {"repo": repo, "id": pkg, "device_id": device_id,
                    "lease_id": lease["lease_id"], "kind": kind,
                    "replaced_lease_id": replaced["lease_id"] if replaced else None,
                    "event_seq": seq}
        except Exception:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.close()


def admin_workspace_assign(store: Store, ws_id: str, params: dict, actor: str,
                           audit_extra: str | None = None) -> dict:
    with store._write_lock:
        c = store.conn()
        try:
            ws = _ws_call(v3.ws_assign, c, ws_id, params.get("user_id"),
                          params.get("device_id"))
            detail = (f"ws={ws_id}; user={params.get('user_id') or '-'}; "
                      f"device={params.get('device_id') or '-'}")
            if audit_extra:
                detail = f"{detail}; {audit_extra}"
            seq = store.event(c, "workspace_assign", None, None, actor, detail)
            return {"ws_id": ws["ws_id"], "users": ws["users"],
                    "devices": ws["devices"], "event_seq": seq}
        finally:
            c.close()


def admin_workspace_unassign(store: Store, params: dict, actor: str,
                             audit_extra: str | None = None) -> dict:
    with store._write_lock:
        c = store.conn()
        try:
            msg = _ws_call(v3.ws_unassign, c, params.get("user_id"),
                           params.get("device_id"))
            detail = (f"user={params.get('user_id') or '-'}; "
                      f"device={params.get('device_id') or '-'}; {msg}")
            if audit_extra:
                detail = f"{detail}; {audit_extra}"
            seq = store.event(c, "workspace_unassign", None, None, actor, detail)
            return {"user_id": params.get("user_id"),
                    "device_id": params.get("device_id"),
                    "message": msg, "event_seq": seq}
        finally:
            c.close()


def admin_workspace_remove(store: Store, ws_id: str, actor: str, reason: str | None = None,
                           audit_extra: str | None = None) -> dict:
    with store._write_lock:
        c = store.conn()
        try:
            msg = _ws_call(v3.ws_remove, c, ws_id)
            detail = f"ws={ws_id}; {msg}"
            if reason:
                detail = f"{detail}; reason={reason}"
            if audit_extra:
                detail = f"{detail}; {audit_extra}"
            seq = store.event(c, "workspace_remove", None, None, actor, detail)
            return {"ws_id": ws_id, "message": msg, "event_seq": seq}
        finally:
            c.close()


def admin_repo_add(store: Store, repo: str, params: dict, actor: str,
                   audit_extra: str | None = None) -> dict:
    """面板上"添加仓库"：登记 + 首次拉文件树建索引。

    跑在 HTTP 线程里且可能几十秒（HF 翻页），所以 _admin_post 那边对这个
    动作单独放宽了读超时；有缓存时走缓存，几乎立刻返回。
    """
    repo = (repo or "").strip()
    if not repo or repo.count("/") < 1:
        raise AdminError(400, "bad_request", "仓库名要写成 owner/name")
    with store._write_lock:
        c = store.conn()
        try:
            row = c.execute("SELECT repo FROM repos WHERE repo=?", (repo,)).fetchone()
        finally:
            c.close()
    if row is not None:
        raise Conflict(f"仓库 {repo} 已登记，用「强制刷新包缓存」更新")
    repo_type = (params.get("repo_type") or "dataset").strip()
    include = (params.get("include") or ".").strip()
    manifest = (params.get("manifest") or DEFAULT_MANIFEST).strip()

    token = read_hf_token_for(store)
    if not token:
        raise Conflict("尚未配置 HF token：先在面板里填一次")
    try:
        tree = fetch_tree(repo, repo_type, token)
    except HTTPError as e:
        raise Conflict("读取仓库清单失败：HTTP %s（检查仓库名/权限/gated 审批）" % e.code)
    except URLError as e:
        raise Conflict("网络错误：%s" % e.reason)
    if not tree:
        raise Conflict("仓库清单为空，未登记")
    tree_cache_store(store, repo, repo_type, tree)
    with store._write_lock:
        try:
            st = repo_add(store, repo, repo_type, include, manifest, tree)
        except Conflict:
            raise
        c = store.conn()
        try:
            detail = (f"repo={repo}; type={repo_type}; include={include}; "
                      f"files={st['files']}; added={st['added']}")
            if audit_extra:
                detail = f"{detail}; {audit_extra}"
            seq = store.event(c, "repo_add", repo, None, actor, detail)
        finally:
            c.close()
    return {"repo": repo, "added": st["added"], "files": st["files"],
            "pruned": 0, "source": "hf", "event_seq": seq}


def admin_repo_sync(store: Store, repo: str, params: dict, actor: str,
                    audit_extra: str | None = None) -> dict:
    """「刷新数据」：有缓存先用缓存（后台再补），没缓存才同步拉。"""
    with store._write_lock:
        res = repo_refresh_data(store, repo, force=bool(params.get("force")))
        c = store.conn()
        try:
            detail = (f"repo={repo}; source={res['source']}; files={res['files']}; "
                      f"added={res['added']}; pruned={res['pruned']}; "
                      f"refreshing={res['refreshing']}")
            if audit_extra:
                detail = f"{detail}; {audit_extra}"
            seq = store.event(c, "repo_sync", repo, None, actor, detail)
        finally:
            c.close()
    res["event_seq"] = seq
    return res


def admin_token_save(store: Store, token: str | None, actor: str,
                     audit_extra: str | None = None) -> dict:
    token = (token or "").strip()
    if not token:
        raise AdminError(400, "bad_request", "缺少参数 token")
    try:
        hf_token_save(store, token)
    except Conflict as e:
        raise AdminError(400, "bad_request", str(e))
    except ImportError:
        raise AdminError(500, "no_crypto", "服务端缺少 cryptography，无法加密保存")
    masked = mask_token(token)
    c = store.conn()
    try:
        detail = f"token={masked}; encrypted=fernet"
        if audit_extra:
            detail = f"{detail}; {audit_extra}"
        seq = store.event(c, "hf_token_set", None, None, actor, detail)
    finally:
        c.close()
    return {"masked": masked, "configured": True, "event_seq": seq}


def admin_repo_toggle(store: Store, repo: str, enabled: bool, actor: str,
                      audit_extra: str | None = None) -> dict:
    """仓库启停。先 SELECT 当前值判冲突：SQLite 对"同值更新"rowcount 仍为 1。"""
    c = store.conn()
    row = c.execute("SELECT enabled FROM repos WHERE repo=?", (repo,)).fetchone()
    c.close()
    if row is None:
        raise NotFound(f"仓库不存在：{repo}")
    if bool(row["enabled"]) == enabled:
        raise Conflict(f"仓库 {repo} 已是{'启用' if enabled else '停用'}状态")
    seq = repo_set_enabled(store, repo, enabled, actor=actor, audit_extra=audit_extra)
    return {"repo": repo, "enabled": enabled, "event_seq": seq}


def admin_revoke_key(store: Store, name: str, actor: str,
                     audit_extra: str | None = None) -> dict:
    with store._write_lock:
        c = store.conn()
        row = c.execute("SELECT role, revoked FROM keys WHERE name=?", (name,)).fetchone()
        if row is None:
            c.close()
            raise NotFound(f"密钥不存在：{name}")
        if row["revoked"]:
            c.close()
            raise Conflict(f"密钥 {name} 已被吊销")
        if row["role"] == "admin":
            left = c.execute("SELECT COUNT(*) n FROM keys WHERE role='admin' AND revoked=0"
                             ).fetchone()["n"]
            if left <= 1:
                c.close()
                raise Forbidden("不能吊销最后一个管理员密钥")
        c.execute("UPDATE keys SET revoked=1 WHERE name=?", (name,))
        detail = f"key={name}; role={row['role']}"
        if audit_extra:
            detail = f"{detail}; {audit_extra}"
        seq = store.event(c, "revoke", None, None, actor, detail)
        c.close()
    return {"name": name, "revoked": True, "event_seq": seq}


def admin_user_rekey(store: Store, user_id: str, actor: str,
                     audit_extra: str | None = None) -> dict:
    """解除 secret 绑定（不再签发新的）：下次握手以客户端本地那份为准。"""
    with store._write_lock:
        c = store.conn()
        try:
            v3.reset_secret(c, user_id)
        except v3.AuthError as e:
            c.close()
            raise NotFound(str(e))
        detail = f"user={user_id}; unbind=1"
        if audit_extra:
            detail = f"{detail}; {audit_extra}"
        seq = store.event(c, "v3_reset_secret", None, None, actor, detail)
        c.close()
    return {"user_id": user_id, "secret": "", "unbound": True, "event_seq": seq}


def admin_forget_user(store: Store, user_id: str, actor: str,
                      reason: str = "admin_forget",
                      audit_extra: str | None = None) -> dict:
    """清空一个用户的调度身份：作废租约 → 删设备 → 删用户行。

    比 forget_device 更彻底：那个只断一台机器，这个把名字也放掉，之后任何机器
    用这个 user_id 握手都按新用户登记（secret 以本机为准）。
    """
    with store._write_lock:
        c = store.conn()
        try:
            row = c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                raise NotFound(f"用户不存在：{user_id}")
            leases = [dict(r) for r in c.execute(
                "SELECT l.* FROM leases l JOIN devices d ON d.device_id=l.device_id"
                " WHERE d.user_id=? AND l.status='open'", (user_id,))]
            res = v3.forget_user(c, user_id, reason or "admin_forget", on_event=None)
            seq = None
            for lease in leases:
                detail = (f"user={user_id}; lease={lease['lease_id']}; "
                          f"device={lease['device_id']}; reason={reason or 'admin_forget'}")
                if audit_extra:
                    detail = f"{detail}; {audit_extra}"
                seq = store.event(c, "v3_user_forget", lease["repo"], lease["pkg"], actor, detail)
            detail = (f"user={user_id}; devices={res['devices']}; "
                      f"released={res['released']}; identity=deleted")
            if audit_extra:
                detail = f"{detail}; {audit_extra}"
            seq = store.event(c, "v3_user_forget", None, None, actor, detail)
            return {"user_id": user_id, "devices": res["devices"],
                    "released": res["released"], "forgotten": True, "event_seq": seq}
        finally:
            c.close()


# ---- 单个目标执行 ------------------------------------------------------

def _exec_one(store: Store, action: str, target: dict, params: dict,
              ctx: dict, batch_id: str, reservation: dict | None = None) -> dict:
    actor = ctx["actor"]
    extra = f"panel=1; token={ctx['token_id']}; batch={batch_id}"
    if action == "artifact.authorize":
        return admin_authorize_artifact(store, target, params, actor)
    if action == "package.cancel_reservation":
        from assignments.cancellation import cancel, CancellationError
        c = store.conn()
        try:
            return cancel(c, store, target['repo'], target['id'], actor, reservation or {}, extra)
        except CancellationError as e:
            raise Conflict(str(e)) from e
        finally:
            c.close()
    if action == "package.assign":
        return admin_assign_package(store, target["repo"], target["id"],
                                    params["device_id"], actor, extra,
                                    params.get("replace_prelock", False))
    if action == "package.reopen":
        res = reopen(store, target["repo"], target["id"], params.get("to", "claimed"),
                     params.get("reason"), None, actor=actor, audit_extra=extra)
        return {"repo": res["repo"], "id": res["id"], "status": res["status"],
                "owner": res.get("owner"), "event_seq": res.get("event_seq")}
    if action == "package.release":
        res = release(store, target["repo"], target["id"], actor, True,
                      params.get("reason"), audit_extra=extra)
        return {"repo": res["repo"], "id": res["id"], "status": res["status"],
                "previous_owner": res.get("previous_owner"),
                "event_seq": res.get("event_seq")}
    if action == "lease.release":
        res = admin_release_leases(store, [target["lease_id"]], actor,
                                   params.get("reason") or "admin_release", extra)
        item = res["released"][0]
        return {"lease_id": item["lease_id"], "released": True,
                "package_back": item["package_back"], "event_seq": item["event_seq"]}
    if action == "device.unassign":
        res = admin_unassign_device(store, target["device_id"], actor,
                                    params.get("reason") or "admin_release", extra)
        return {"device_id": res["device_id"], "released": res["released"],
                "packages_back": res["packages_back"], "event_seq": res["event_seq"]}
    if action == "device.forget":
        res = admin_forget_device(store, target["device_id"], actor,
                                  params.get("reason") or "admin_forget", extra)
        return {"device_id": res["device_id"], "user_id": res["user_id"],
                "released": res["released"], "packages_back": res["packages_back"],
                "forgotten": True, "event_seq": res["event_seq"]}
    if action in ("repo.enable", "repo.disable"):
        res = admin_repo_toggle(store, target["repo"], action == "repo.enable", actor, extra)
        return {"repo": res["repo"], "enabled": res["enabled"],
                "event_seq": res["event_seq"]}
    if action == "key.revoke":
        res = admin_revoke_key(store, target["name"], actor, extra)
        return {"name": res["name"], "revoked": True, "event_seq": res["event_seq"]}
    if action == "user.rekey":
        res = admin_user_rekey(store, target["user_id"], actor, extra)
        return {"user_id": res["user_id"], "secret": res["secret"],
                "unbound": res.get("unbound"), "event_seq": res["event_seq"]}
    if action == "user.forget":
        res = admin_forget_user(store, target["user_id"], actor,
                                params.get("reason") or "admin_forget", extra)
        return {"user_id": res["user_id"], "devices": res["devices"],
                "released": res["released"], "forgotten": True,
                "event_seq": res["event_seq"]}
    if action == "workspace.save":
        res = admin_workspace_save(store, target["ws_id"], params, actor, extra)
        return {"ws_id": res["ws_id"], "name": res["name"], "repos": res["repos"],
                "users": res["users"], "devices": res["devices"],
                "event_seq": res["event_seq"]}
    if action == "workspace.assign":
        res = admin_workspace_assign(store, target["ws_id"], params, actor, extra)
        return {"ws_id": res["ws_id"], "users": res["users"], "devices": res["devices"],
                "event_seq": res["event_seq"]}
    if action == "workspace.unassign":
        res = admin_workspace_unassign(store, params, actor, extra)
        return {"user_id": res["user_id"], "device_id": res["device_id"],
                "message": res["message"], "event_seq": res["event_seq"]}
    if action == "workspace.remove":
        res = admin_workspace_remove(store, target["ws_id"], actor,
                                     params.get("reason"), extra)
        return {"ws_id": res["ws_id"], "message": res["message"],
                "event_seq": res["event_seq"]}
    if action == "repo.add":
        res = admin_repo_add(store, target["repo"], params, actor, extra)
        return {"repo": res["repo"], "added": res["added"], "files": res["files"],
                "pruned": res["pruned"], "source": res["source"],
                "event_seq": res["event_seq"]}
    if action == "repo.sync":
        res = admin_repo_sync(store, target["repo"], params, actor, extra)
        return {"repo": res["repo"], "added": res["added"], "files": res["files"],
                "pruned": res["pruned"], "source": res["source"],
                "cached_at": res.get("cached_at"),
                "refreshing": res.get("refreshing", False),
                "event_seq": res["event_seq"]}
    if action == "hf.token":
        return admin_token_save(store, params.get("token"), actor, extra)
    raise AdminError(400, "bad_request", f"未知动作：{action}")


_TS_LOCK = threading.Lock()
_TS_LAST = [0.0]


def _next_ts_epoch() -> float:
    """写日志的时间戳必须严格单调：同一毫秒内多条写若时间相同，导出按时间排序
    就会把行序打乱，破坏"导出 JSONL 与磁盘逐字一致"。加 1ms 递增兜底。"""
    with _TS_LOCK:
        now = round(time.time(), 3)
        if now <= _TS_LAST[0]:
            now = round(_TS_LAST[0] + 0.001, 3)
        _TS_LAST[0] = now
        return now


def _write_id() -> str:
    return "W" + time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)


def _batch_id() -> str:
    return "B" + time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)


def _target_text(action: str, target: dict) -> str:
    kind = ACTION_SPECS[action]["kind"]
    if kind == "artifact":
        return f"{target.get('repo')}/{target.get('id')}; lease={target.get('lease_id')}"
    if kind == "package":
        return f"{target.get('repo')}/{target.get('id')}"
    if kind == "lease":
        return f"lease={target.get('lease_id')}"
    if kind == "device":
        return f"device={target.get('device_id')}"
    if kind == "repo":
        return f"repo={target.get('repo')}"
    if kind == "key":
        return f"key={target.get('name')}"
    if kind == "workspace":
        return f"workspace={target.get('ws_id')}"
    return f"user={target.get('user_id')}"


def _log_write_error(store: Store, action: str, target: dict, code: int,
                     error: str, ctx: dict, batch_id: str) -> None:
    """失败审计：admin_write_error（repo/pkg 为 NULL，只对 admin 可见）。"""
    detail = (f"action={action}; target={_target_text(action, target)}; code={code}; "
              f"error={(error or '')[:200]}; token={ctx['token_id']}; batch={batch_id}")
    c = store.conn()
    try:
        store.event(c, "admin_write_error", None, None, ctx["actor"], detail)
    finally:
        c.close()


def _impact_diff(rec: dict, impact: dict, full_items: list[dict],
                 confirmed_mutations: int | None = None) -> dict:
    confirmed = rec.get("items") or []
    conf = {_item_key(it): it for it in confirmed}
    actual = {_item_key(it): it for it in full_items}
    added = [actual[k] for k in actual if k not in conf][:50]
    removed = [conf[k] for k in conf if k not in actual][:50]
    # 评审 F2：batch_confirm 数量不符时，diff.mutations.confirmed 必须回显"客户端确认的数字"，
    # 否则文案说"确认时 5 项"、diff 却显示 token 的 2/2，自相矛盾。
    by_client = confirmed_mutations is not None
    return {"mutations": {"confirmed": (confirmed_mutations if by_client
                                        else rec.get("mutations")),
                          "actual": impact["mutations"]},
            "confirmed_by": "batch_confirm" if by_client else "token",
            "added": added, "removed": removed,
            "truncated": len(added) >= 50 or len(removed) >= 50,
            "hash": {"confirmed": rec.get("impact_hash"), "actual": impact["hash"]}}


def _impact_mismatch(rec: dict, impact: dict, full_items: list[dict],
                     confirmed_mutations: int | None = None) -> AdminError:
    """拼 400 impact_mismatch：文案与 diff.mutations 必须来自同一个"确认数字"。"""
    confirmed = (rec.get("mutations") if confirmed_mutations is None
                 else confirmed_mutations)
    source = "批量确认" if confirmed_mutations is not None else "确认"
    if confirmed == impact["mutations"]:
        msg = (f"影响面已变化（目标状态与{source}时不同），请重新确认："
               f"{source}时 {confirmed} 项，当前 {impact['mutations']} 项，内容哈希不一致")
    else:
        msg = f"影响面与确认不一致：{source}时 {confirmed} 项，当前 {impact['mutations']} 项"
    return AdminError(400, "impact_mismatch", msg,
                      {"diff": _impact_diff(rec, impact, full_items, confirmed_mutations)})


# ---- 预览 / 执行 ------------------------------------------------------

def admin_preview(store: Store, action, targets_raw, params_raw,
                  ctx: dict) -> tuple[int, dict]:
    if not isinstance(action, str) or action not in ACTION_SPECS:
        raise AdminError(400, "bad_request", f"未知动作：{action}")
    params = _norm_params(action, params_raw)
    targets = _norm_targets(action, targets_raw)
    impact, _per, full_items = compute_impact(store, action, targets, params)
    th = target_hash(action, targets)
    ph = params_hash(action, params)
    token, token_id, expires = CONFIRM_STORE.issue({
        "action": action, "actor": ctx["actor"], "session_fp": ctx["session_fp"],
        "client_ip": ctx["client_ip"], "targets": targets, "target_hash": th,
        "params": params, "params_hash": ph,
        "impact_hash": impact["hash"], "mutations": impact["mutations"],
        "batch_required": impact["batch_required"], "items": full_items,
    })
    expires_iso = datetime.fromtimestamp(expires, timezone.utc).astimezone().isoformat(
        timespec="seconds")
    body = {
        "ok": True,
        "preview": {
            "action": action,
            "level": ACTION_SPECS[action]["level"],
            "targets": targets,
            "target_hash": th,
            "params": params,
            "params_hash": ph,
            "impact": impact,
            "warnings": impact.get("warnings") or [],
            "confirm_token": token,
            "confirm_token_id": token_id,
            "expires_at": expires_iso,
            "ttl_sec": CONFIRM_TTL,
        },
        "generated_at": now_iso(),
    }
    return 200, body


def admin_execute(store: Store, action, targets_raw, params_raw, confirm_token,
                  batch_confirm, ctx: dict) -> tuple[int, dict, dict]:
    started_at = time.time()
    if not isinstance(action, str) or action not in ACTION_SPECS:
        raise AdminError(400, "bad_request", f"未知动作：{action}")
    idem_key = ctx.get("idem_key") or ""
    if not idem_key:
        raise AdminError(400, "idempotency_key_required", "缺少 Idempotency-Key 请求头")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", idem_key):
        raise AdminError(400, "bad_request", "Idempotency-Key 必须是 8-64 位 [A-Za-z0-9_-]")

    cached = _idem_get(store, ctx["actor"], action, idem_key)
    if cached is not None:
        return cached.get("status", 200), cached.get("body", {}), {"Idempotent-Replay": "1"}

    params = _norm_params(action, params_raw)
    targets = _norm_targets(action, targets_raw)

    # 二次确认（服务端强制）：缺/过期/重放/会话不符 → 428；动作/目标/影响面不符 → 400
    rec = CONFIRM_STORE.check(confirm_token, action, ctx["actor"],
                              ctx["session_fp"], ctx["client_ip"])
    ctx = dict(ctx, token_id=rec["token_id"])
    if params_hash(action, params) != rec.get("params_hash"):
        raise AdminError(400, "params_mismatch", "确认的参数与请求不一致",
                         {"diff": {"params": {"confirmed": rec.get("params") or {},
                                              "actual": params}}})
    th = target_hash(action, targets)
    if th != rec["target_hash"]:
        raise AdminError(400, "target_mismatch", "确认的目标与请求不一致",
                         {"diff": {"target": {"confirmed": rec.get("targets") or [],
                                              "actual": targets}}})
    impact, per_target, full_items = compute_impact(store, action, targets, params)
    if impact["hash"] != rec["impact_hash"]:
        raise _impact_mismatch(rec, impact, full_items)

    # 批量阈值（>5 必须带计数确认；未达阈值时携带也校验）
    if impact["mutations"] > BATCH_THRESHOLD:
        if batch_confirm is None:
            raise AdminError(400, "batch_confirm_required",
                             f"批量操作超过 {BATCH_THRESHOLD} 条，需要数量确认",
                             {"threshold": BATCH_THRESHOLD,
                              "actual": impact["mutations"]})
    if batch_confirm is not None:
        if not isinstance(batch_confirm, dict):
            raise AdminError(400, "bad_request", "batch_confirm 必须是对象")
        if batch_confirm.get("target_hash") and batch_confirm["target_hash"] != th:
            raise AdminError(400, "target_mismatch", "批量的目标哈希与请求不一致",
                             {"diff": {"target": {"confirmed": batch_confirm.get("target_hash"),
                                                  "actual": th}}})
        wrong_count = batch_confirm.get("mutations") != impact["mutations"]
        wrong_hash = (batch_confirm.get("impact_hash")
                      and batch_confirm["impact_hash"] != impact["hash"])
        if wrong_count or wrong_hash:
            # 文案与 diff 都用客户端在 batch_confirm 里给的数字（F2）
            raise _impact_mismatch(rec, impact, full_items,
                                   confirmed_mutations=batch_confirm.get("mutations"))

    batch_id = _batch_id()
    results: list[dict] = []
    logged_all = True
    failures = 0
    reservations = {(item['repo'], item['id']): item for item in full_items
                    if action == 'package.cancel_reservation' and item['kind'] == 'package'}
    for idx, target in enumerate(targets):
        t0 = time.time()
        ok, code, error, extra = True, 200, None, {}
        try:
            with store._write_lock:
                extra = _exec_one(store, action, target, params, ctx, batch_id,
                                  reservations.get((target.get('repo'), target.get('id'))))
        except NotFound as e:
            ok, code, error = False, 404, str(e)
        except Conflict as e:
            ok, code, error = False, 409, str(e)
        except Forbidden as e:
            ok, code, error = False, 403, str(e)
        except AdminError as e:
            # 每目标失败不能整批 500：_exec_one 里把校验错误包成 AdminError
            # （仓库没登记、账号不存在…），这里要原样落成 400 并补审计。
            ok, code, error = False, e.status, e.message
            _log_write_error(store, action, target, code, error, ctx, batch_id)
        except sqlite3.OperationalError:
            ok, code, error = False, 503, "存储暂时不可用（busy/disk），请稍后重试"
        except Exception:                      # noqa: BLE001 - 单条失败不能拖垮整批
            ok, code, error = False, 500, "服务端内部错误"
        ms = int((time.time() - t0) * 1000)
        impact_count, mutations = per_target[idx] if idx < len(per_target) else (1, 0)
        write_id = _write_id()
        record = {
            "write_id": write_id,
            "ts": now_iso(),
            "ts_epoch": _next_ts_epoch(),
            "actor": ctx["actor"],
            "remote_ip": ctx["remote_ip"],
            "action": action,
            "target": {"kind": ACTION_SPECS[action]["kind"], **target},
            "params": ({"secret_issued": True} if action == "user.rekey" else params),
            "result": "ok" if ok else "error",
            "code": code,
            "error": None if ok else (error or "")[:200],
            "impact_count": impact_count,
            "mutations": mutations if ok else 0,
            "confirm_token_id": ctx["token_id"],
            "batch_id": batch_id,
            "idempotency_key": idem_key,
            "ms": ms,
            "logged": True,
            "panel_version": ctx.get("panel_version") or "server",
        }
        logged = store.writes.append(record)
        logged_all = logged_all and logged
        if not ok:
            failures += 1
            _log_write_error(store, action, target, code, error, ctx, batch_id)
        row = {"target": target, "ok": ok, "code": code, **extra, "write_id": write_id}
        if not ok:
            row["error"] = error
        results.append(row)

    total = len(targets)
    duration_ms = int((time.time() - started_at) * 1000)
    cacheable = ACTION_SPECS[action].get("cacheable", True)
    if total == 1 and failures:
        row = results[0]
        # 400 也要保留：参数/校验类失败（仓库没登记、账号不存在）本来就是
        # 面板填错了，回 500 会让人以为是服务端挂了，错误也进不了 _idem 语义。
        code_map = {400: "bad_request", 403: "forbidden", 404: "not_found",
                    409: "conflict", 429: "rate_limited",
                    503: "upstream_unavailable", 500: "internal"}
        status = row["code"] if row["code"] in code_map else 500
        body = {"ok": False, "error": row.get("error") or "写操作失败",
                "code": code_map.get(status, "error"), "action": action,
                "batch_id": batch_id, "write_id": row["write_id"],
                "logged": logged_all}
        if cacheable:
            _idem_put(store, ctx["actor"], action, idem_key,
                      {"status": status, "body": body})
        return status, body, {}

    body = {
        "ok": failures == 0,
        "action": action,
        "batch_id": batch_id,
        "total": total,
        "succeeded": total - failures,
        "failed": failures,
        "skipped": 0,
        "impact_count": impact["total"],
        "results": results,
        "duration_ms": duration_ms,
        "logged": logged_all,
        "generated_at": now_iso(),
    }
    if cacheable:
        _idem_put(store, ctx["actor"], action, idem_key, {"status": 200, "body": body})
    else:
        # One-time credentials never enter a replay response cache.
        upload_grant = action == "artifact.authorize"
        _idem_put(store, ctx["actor"], action, idem_key, {
            "status": 409,
            "body": {"ok": False,
                     "error": ("上传授权凭证不支持幂等重放，请重新预览授权" if upload_grant
                               else "重签类操作不支持幂等重放，请重新确认"),
                     "code": "upload_token_not_replayable" if upload_grant else "secret_not_replayable"}})
    return 200, body, {}


# ---- 只读管理接口 ------------------------------------------------------

def admin_meta(store: Store) -> dict:
    return {
        "ok": True,
        "version": ADMIN_VERSION,
        "write": {
            "enabled": write_enabled(),
            "confirm_ttl_sec": CONFIRM_TTL,
            "batch_threshold": BATCH_THRESHOLD,
            "max_batch": MAX_BATCH,
            "log": {"file": "writes.jsonl", "max_bytes": WRITE_LOG_MAX_BYTES,
                    "keep": WRITE_LOG_KEEP},
        },
        "actions": [{"id": aid, "label": spec["label"], "level": spec["level"],
                     "target": spec["kind"], "multi": spec["multi"],
                     "priority": spec["priority"],
                     "params": list(spec["params"])}
                    for aid, spec in ACTION_SPECS.items()
                     if aid != "artifact.authorize" or store.artifacts is not None],
        "artifacts": {"enabled": store.artifacts is not None,
                      "max_upload_bytes": store.artifacts.max_bytes if store.artifacts else None},
        "generated_at": now_iso(),
    }


def admin_repos(store: Store) -> dict:
    c = store.conn()
    rows = []
    for r in c.execute("SELECT repo, repo_type, enabled FROM repos ORDER BY repo"):
        repo = r["repo"]
        total = c.execute("SELECT COUNT(*) n FROM packages WHERE repo=?", (repo,)).fetchone()["n"]
        claimed = c.execute("SELECT COUNT(*) n FROM packages WHERE repo=? AND status<>'available'",
                            (repo,)).fetchone()["n"]
        leases = c.execute("SELECT COUNT(*) n FROM leases WHERE repo=? AND status='open'",
                           (repo,)).fetchone()["n"]
        rows.append({"repo": repo, "repo_type": r["repo_type"],
                     "enabled": bool(r["enabled"]), "packages": total,
                     "claimed": claimed, "open_leases": leases})
    c.close()
    return {"ok": True, "repos": rows, "generated_at": now_iso()}


def admin_keys(store: Store) -> dict:
    c = store.conn()
    grants: dict[str, list[str]] = {}
    for g in c.execute("SELECT key_hash, repo FROM repo_grants"):
        grants.setdefault(g["key_hash"], []).append(g["repo"])
    admins = c.execute("SELECT COUNT(*) n FROM keys WHERE role='admin' AND revoked=0"
                       ).fetchone()["n"]
    rows = []
    for r in c.execute("SELECT name, role, hash, revoked, created_at, last_seen"
                       " FROM keys ORDER BY role, name"):
        rows.append({"name": r["name"], "role": r["role"], "revoked": bool(r["revoked"]),
                     "created_at": r["created_at"], "last_seen": r["last_seen"],
                     "grants": sorted(grants.get(r["hash"], [])),
                     "is_last_admin": (r["role"] == "admin" and not r["revoked"]
                                       and admins <= 1)})
    c.close()
    return {"ok": True, "keys": rows, "generated_at": now_iso()}


def admin_users(store: Store) -> dict:
    c = store.conn()
    rows = []
    for r in c.execute("SELECT user_id, status, created_at, last_seen FROM users"
                       " ORDER BY user_id"):
        devs = c.execute("SELECT COUNT(*) n FROM devices WHERE user_id=?",
                         (r["user_id"],)).fetchone()["n"]
        rows.append({"user_id": r["user_id"], "status": r["status"], "devices": devs,
                     "created_at": r["created_at"], "last_seen": r["last_seen"]})
    c.close()
    return {"ok": True, "users": rows, "generated_at": now_iso()}


def admin_workspaces(store: Store) -> dict:
    """工作区读面（2026-11-13）：谁能做哪些仓库。

    一次把面板要的三块数据都吐出来，免得前端为"指派"再拼两个接口：
      workspaces   —— 工作区本身 + 仓库各自包数 + 已绑账号/设备
      devices/users—— 带 workspace_id 的全量，面板自己按有没有归属分栏
      repos        —— 已登记的仓库（新增工作区时只能从这里挑）
    包数取 packages 的实际行数，不是 HF 的文件总数——面板上这两个数字一旦混用，
    "配了工作区却没包可派"就会被看成"配置没生效"。
    """
    c = store.conn()
    try:
        pkg_n = {r["repo"]: r["n"] for r in c.execute(
            "SELECT repo, COUNT(*) n FROM packages GROUP BY repo")}
        counts = v3.part_count_expr('p')
        part_counts = {r['repo']: dict(r) for r in c.execute(
            f"SELECT p.repo,SUM({counts['missing_tag']}) missing_tag,SUM({counts['missing_nl']}) missing_nl,"
            " SUM(p.status='completed') completed"
            " FROM packages p GROUP BY p.repo")}
        repos = [{"repo": r["repo"], "enabled": bool(r["enabled"]),
                  "packages": pkg_n.get(r["repo"], 0), "synced_at": r["synced_at"]}
                 for r in c.execute(
                     "SELECT repo, enabled, synced_at FROM repos ORDER BY repo")]
        known = {r["repo"] for r in repos}
        workspaces = []
        for ws in v3.ws_list(c):
            repo_list = list(ws.get("repos") or [])
            workspaces.append({
                "ws_id": ws["ws_id"], "name": ws.get("name") or "",
                "note": ws.get("note"), "created_at": ws.get("created_at"),
                "repos": repo_list,
                "allow_manual_claim": bool(ws.get("allow_manual_claim",1)),
                "allocation_mode": ws.get('allocation_mode', 'auto'),
                "repo_devices": ws.get('repo_devices', {}),
                "preassignment_template": ws.get('preassignment_template', {}),
                # registered 看的是 repos 表，不是包数——登记了但一次都没 sync 过的
                # 仓库包数是 0，那种状态要能看出来（面板上显示"未同步"）。
                "repo_packages": [{"repo": rp, "packages": pkg_n.get(rp, 0),
                                   "registered": rp in known, 'missing_tag': part_counts.get(rp,{}).get('missing_tag',0),
                                   'missing_nl': part_counts.get(rp,{}).get('missing_nl',0),
                                   'completed': part_counts.get(rp,{}).get('completed',0)}
                                  for rp in repo_list],
                "packages": sum(pkg_n.get(rp, 0) for rp in repo_list),
                "users": list(ws.get("users") or []),
                "devices": list(ws.get("devices") or []),
            })
        devices = [{"device_id": r["device_id"], "user_id": r["user_id"],
                    "workspace_id": r["workspace_id"], "status": r["status"],
                    "last_seen": r["last_seen"], "tier": r["tier"]}
                   for r in c.execute("SELECT device_id, user_id, workspace_id, status,"
                                      " last_seen, tier FROM devices ORDER BY device_id")]
        users = [{"user_id": r["user_id"], "workspace_id": r["workspace_id"],
                  "status": r["status"]}
                 for r in c.execute("SELECT user_id, workspace_id, status FROM users"
                                    " ORDER BY user_id")]
        for device in devices:
            ws = v3.workspace_of(c, device)
            device['effective_workspace_id'] = ws['ws_id'] if ws else None
        for user in users:
            ws = v3.workspace_of(c, dict(user_id=user['user_id'], device_id=''))
            user['effective_workspace_id'] = ws['ws_id'] if ws else None
    finally:
        c.close()
    return {"ok": True, "workspaces": workspaces, "repos": repos,
            "devices": devices, "users": users, "generated_at": now_iso()}


class TokenBucket:
    """线程安全的令牌桶（写通道限流）。返回 0 表示放行，否则返回建议重试秒数。"""

    def __init__(self, capacity: int, per_minute: float):
        self.capacity = float(max(1, capacity))
        self.rate = float(per_minute) / 60.0
        self._lock = threading.Lock()
        self._state: dict[str, list[float]] = {}

    def take(self, key: str) -> float:
        with self._lock:
            now = time.time()
            if len(self._state) > 2048:
                self._state.clear()
            tokens, last = self._state.get(key, [self.capacity, now])
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self._state[key] = [tokens, now]
                return max(1.0, (1.0 - tokens) / self.rate) if self.rate > 0 else 60.0
            self._state[key] = [tokens - 1.0, now]
            return 0.0


ADMIN_EXEC_LIMIT = TokenBucket(30, 30)      # 直连兜底：30/分钟
ADMIN_PREVIEW_LIMIT = TokenBucket(30, 60)   # 预览：60/分钟
ADMIN_READ_LIMIT = TokenBucket(120, 120)    # 写日志/通知/导出：120/分钟
PING_LIMIT = TokenBucket(v3.PING_RATE_LIMIT, v3.PING_RATE_LIMIT)  # idle ping：10/分钟/设备


# ---------------------------------------------------------------- SSE 广播

class Broadcaster:
    def __init__(self, store: Store):
        self.store = store
        self.subs: set[queue.Queue] = set()
        self.lock = threading.Lock()
        c = store.conn()
        row = c.execute("SELECT COALESCE(MAX(seq),0) AS m FROM events").fetchone()
        c.close()
        self.last_seq = row["m"] if row else 0
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="broadcaster", daemon=True)
        self.thread.start()

    MAX_SUBS = 64

    def subscribe(self) -> queue.Queue | None:
        """订阅者上限：每条 SSE 连接占一个线程 + 一个队列，不设限会被拖垮。"""
        with self.lock:
            if len(self.subs) >= self.MAX_SUBS:
                return None
            q: queue.Queue = queue.Queue(maxsize=1000)
            self.subs.add(q)
            return q

    def stop(self) -> None:
        self._stop.set()

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subs.discard(q)

    def _run(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.4)
            try:
                c = self.store.conn()
                rows = c.execute(
                    "SELECT seq,ts,kind,repo,pkg,actor,detail FROM events WHERE seq>? ORDER BY seq",
                    (self.last_seq,)).fetchall()
                c.close()
            except sqlite3.Error:
                continue
            for r in rows:
                self.last_seq = r["seq"]
                payload = dict(r)
                with self.lock:
                    for q in list(self.subs):
                        try:
                            q.put_nowait(payload)
                        except queue.Full:
                            pass


# ---------------------------------------------------------------- HTTP

class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "fox-label/2.0"
    # 半截请求不得永久占用线程（StreamRequestHandler 会据此设置 socket 超时）
    def _parse_timeout() -> int:
        try:
            return max(5, int(os.environ.get("FOXLABEL_HTTP_TIMEOUT", "30")))
        except ValueError:
            return 30

    timeout = _parse_timeout()
    MAX_BODY = 64 * 1024

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def bus(self) -> Broadcaster:
        return self.server.bus  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        pass

    def _note(self, tag: str, code: int, extra: str = "") -> None:
        sys.stderr.write(f"[{datetime.now().strftime('%H:%M:%S')}] {tag} -> {code} {extra}\n")

    def _json(self, code: int, obj, extra: dict | None = None) -> None:
        if getattr(self, "_responded", False):
            return
        self._responded = True
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, limit: int | None = None, strict: bool = False) -> dict:
        limit = self.MAX_BODY if limit is None else limit
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0:
            return {}
        if n > limit:
            # 先把超出的请求体吃掉（有上限）再回 413，否则服务端直接关连接，
            # 客户端看到的是 connection reset 而不是明确的 413。
            left = min(n, limit * 4)
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
            raise PayloadTooLarge()
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            if strict:
                raise
            return {}

    def _auth(self, *, record_seen: bool = True) -> dict | None:
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            return None
        key = h[7:].strip()
        if not key:
            return None
        kh = key_hash(key)
        if record_seen:
            c = self.store.conn()
        else:
            c = sqlite3.connect(self.store.db_path.as_uri() + "?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA query_only=ON")
        row = c.execute("SELECT name, role, revoked FROM keys WHERE hash=?", (kh,)).fetchone()
        if row is None or row["revoked"]:
            c.close()
            return None
        now = time.time()
        if record_seen and now - _LAST_SEEN.get(kh, 0) > 60:      # 每个密钥每分钟最多写一次
            _LAST_SEEN[kh] = now
            c.execute("UPDATE keys SET last_seen=? WHERE hash=?", (now_iso(), kh))
        c.close()
        return {"name": row["name"], "role": row["role"],
                "grants": key_grants(self.store, kh) if record_seen else None}

    def _still_valid(self, who: dict) -> bool:
        """SSE 长连接在用：定期复查密钥是否已被吊销。"""
        c = self.store.conn()
        row = c.execute("SELECT revoked FROM keys WHERE name=? AND role=?",
                        (who["name"], who["role"])).fetchone()
        c.close()
        return bool(row) and not row["revoked"]

    @staticmethod
    def _q(path: str, name: str) -> str | None:
        if "?" not in path:
            return None
        for kv in path.split("?", 1)[1].split("&"):
            if kv.startswith(name + "="):
                return unquote(kv.split("=", 1)[1])
        return None

    def _allowed(self, who: dict, repo: str) -> bool:
        return who["role"] == "admin" or who["grants"] is None or repo in who["grants"]

    def _enabled(self) -> set[str]:
        c = self.store.conn()
        s = {r["repo"] for r in c.execute("SELECT repo FROM repos WHERE enabled=1")}
        c.close()
        return s

    def _resolve_repo(self, who: dict, pkg: str, query_repo: str | None) -> str:
        if query_repo:
            if not self._allowed(who, query_repo):
                raise Forbidden(f"无权访问仓库 {query_repo}")
            if query_repo not in self._enabled():
                raise Forbidden(f"仓库 {query_repo} 已停用")
            return query_repo
        c = self.store.conn()
        rows = [r["repo"] for r in c.execute("SELECT DISTINCT repo FROM packages WHERE id=?", (pkg,))]
        c.close()
        rows = [r for r in rows if self._allowed(who, r)]
        if not rows:
            raise NotFound(pkg)
        if len(rows) > 1:
            raise Conflict(f"包名 {pkg} 在多个仓库中存在，请显式指定：{', '.join(rows)}")
        if rows[0] not in self._enabled():
            raise Forbidden(f"仓库 {rows[0]} 已停用")
        return rows[0]

    # -- GET --------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        """统一异常兜底：任何未预期异常都返回 JSON 而不是直接掐断连接。"""
        self._responded = False
        try:
            if history_handle(self, v3, method, write_enabled):
                return
            if delivery_handle(self, v3, method, write_enabled):
                return
            if method == "GET":
                self._do_get()
            elif method == "PUT":
                self._do_artifact_put()
            else:
                self._do_post()
        except artifact_core.ArtifactError as e:
            self._note(f"{method} {self.path.split('?')[0]}", e.status, e.code)
            self._safe_json(e.status, e.body())
        except (BrokenPipeError, ConnectionResetError):
            pass
        except PayloadTooLarge:
            self._note(f"{method} {self.path.split('?')[0]}", 413, "body too large")
            self._safe_json(413, {"error": f"请求体超过 {self.MAX_BODY} 字节上限"})
        except TimeoutError:
            self._note(f"{method} {self.path.split('?')[0]}", 408, "read timeout")
        except Exception as e:  # 兜底，避免 traceback 泄漏到 stderr/连接
            self._note(f"{method} {self.path.split('?')[0]}", 500, f"{type(e).__name__}: {e}"[:140])
            self._safe_json(500, {"error": "服务端内部错误"})

    def _safe_json(self, code: int, obj) -> None:
        if getattr(self, "_responded", False):
            return          # 已经发过 200，补写 500 会把 HTTP 帧序搞乱
        try:
            self._json(code, obj)
        except Exception:
            pass

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    # 写日志/管理面只读：非 GET/POST 一律 405（不改其它路径的既有 501 行为）
    def do_PUT(self) -> None:  # noqa: N802
        path = unquote(self.path.split("?", 1)[0]).rstrip("/")
        if path.startswith("/api/v1/artifacts/"):
            return self._dispatch("PUT")
        self._admin_method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._admin_method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._admin_method_not_allowed()

    def _admin_method_not_allowed(self) -> None:
        path = unquote(self.path.split("?", 1)[0]).rstrip("/") or "/"
        if path == "/admin/v1" or path.startswith("/admin/v1/"):
            allow = ("GET" if path.startswith(("/admin/v1/writes", "/admin/v1/workspaces",
                                              "/admin/v1/creds"))
                     else "POST")
            return self._json(405, {"ok": False, "error": "方法不允许",
                                    "code": "method_not_allowed"}, {"Allow": allow})
        self.send_error(501, "Unsupported method (%r)" % self.command)

    # -- 管理写通道（v3，仅本机） -----------------------------------
    def _admin_identity(self, *, record_seen: bool = True) -> tuple[dict | None, int, dict | None]:
        peer = (self.client_address[0] if self.client_address else "") or ""
        local = peer in ("127.0.0.1", "::1", "localhost")
        if admin_local_only() and not local:
            return None, 403, {"ok": False, "error": "管理接口只接受本机调用",
                               "code": "forbidden"}
        who = self._auth(record_seen=record_seen)
        if who is None:
            return None, 401, {"ok": False, "error": "需要有效的 Bearer 密钥",
                               "code": "unauthorized"}
        if who["role"] != "admin":
            return None, 403, {"ok": False, "error": "需要管理员密钥", "code": "forbidden"}
        # X-Fox-* 头只在本机（面板进程）调用时信任，避免公网伪造 actor/IP
        actor = (self.headers.get("X-Fox-Actor") or "").strip() if local else ""
        session_fp = (self.headers.get("X-Fox-Session") or "").strip()[:64] if local else ""
        ip_raw = (self.headers.get("X-Fox-Client-IP") or "").strip() if local else ""
        client_ip = _clean_ip(ip_raw) if ip_raw else _clean_ip(peer)
        if client_ip == "unknown":
            client_ip = _clean_ip(peer)
        panel_version = (self.headers.get("X-Fox-Panel-Version") or "").strip()[:32] \
            if local else ""
        idem_key = (self.headers.get("Idempotency-Key") or "").strip()
        return {"actor": actor or who["name"], "session_fp": session_fp,
                "client_ip": client_ip, "remote_ip": client_ip,
                "panel_version": panel_version or "server", "idem_key": idem_key}, 0, None

    def _admin_rate(self, bucket: TokenBucket, ctx: dict) -> bool:
        wait = bucket.take(f"{ctx['actor']}|{ctx['client_ip']}")
        if wait <= 0:
            return True
        secs = max(1, int(wait + 0.999))
        self._json(429, {"ok": False, "error": "操作过于频繁，请稍后再试",
                         "code": "rate_limited", "retry_after": secs},
                   {"Retry-After": str(secs)})
        return False

    def _artifact_download(self, upload_id: str) -> None:
        row, fh = _artifact_store(self.store).open_download(upload_id)
        with fh:
            if not artifact_core.FILENAME_RE.fullmatch(row["filename"]):
                raise artifact_core.ArtifactError(503, "artifact_metadata_corrupt", "归档元数据不可用")
            self._responded = True
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(row["size"]))
            self.send_header("Content-Disposition", f'attachment; filename="{row["filename"]}"')
            self.send_header("X-Artifact-SHA256", row["sha256"])
            self.send_header("ETag", '"sha256:' + row["sha256"] + '"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                for chunk in iter(lambda: fh.read(artifact_core.CHUNK_BYTES), b""):
                    self.wfile.write(chunk)
            except (OSError, ConnectionError):
                self.close_connection = True
                self._note("artifact download", 499, "stream_interrupted")

    def _do_artifact_put(self) -> None:
        path = unquote(self.path.split("?", 1)[0]).rstrip("/")
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[:3] != ["api", "v1", "artifacts"]:
            return self._json(404, {"ok": False, "error": "未知上传路径"})
        if not write_enabled():
            return self._json(403, {"ok": False, "code": "readonly", "error": "服务端写通道已关闭"})
        if self.headers.get("Transfer-Encoding"):
            return self._json(400, {"ok": False, "code": "unsupported_transfer_encoding",
                                    "error": "上传须携带 Content-Length，不接受 chunked"})
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,13}", lengths[0].strip()):
            return self._json(411, {"ok": False, "code": "content_length_required",
                                    "error": "需要唯一的准确 Content-Length"})
        if (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/octet-stream":
            return self._json(415, {"ok": False, "code": "unsupported_media_type",
                                    "error": "上传正文须是 application/octet-stream"})
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        try:
            result = _artifact_store(self.store).receive(
                parts[3], token, self.rfile, int(lengths[0]),
                lambda m: artifact_binding_guard(self.store, m))
        except TimeoutError as e:
            raise artifact_core.ArtifactError(408, "upload_timeout", "上传接收超时，未发布") from e
        except (sqlite3.Error, OSError) as e:
            full = isinstance(e, OSError) and e.errno == 28
            raise artifact_core.ArtifactError(
                507 if full else 503, "storage_full" if full else "storage_unavailable",
                "产物存储空间不足" if full else "产物存储暂时不可用") from e
        return self._json(200, result)

    def _admin_get(self, path: str, raw: str) -> None:
        artifact_read = path == "/admin/v1/artifacts" or path.startswith("/admin/v1/artifacts/")
        ctx, code, err = self._admin_identity(record_seen=not artifact_read)
        if ctx is None:
            return self._json(code, err or {})
        if not self._admin_rate(ADMIN_READ_LIMIT, ctx):
            return
        sub = path[len("/admin/v1"):].strip("/")
        try:
            if sub == 'result-exports' or sub.startswith('result-exports/'):
                from result_exports.routes import read as read_result_exports
                return read_result_exports(self,raw)
            if sub == 'source-catalog':
                return self._json(200, source_routes.read(self.store, self._q(raw, 'repo'), self._q(raw, 'package'), self._q(raw, 'offset') or 0, self._q(raw, 'conflict_state') or 'all', self._q(raw,'format')=='jsonl'))
            if sub == 'dispatch-plan':
                from assignments.plans import load
                c = self.store.conn()
                try: return self._json(200, load(c, self._q(raw, 'id')))
                finally: c.close()
            if sub == 'scheduling':
                return self._json(200, scheduling_routes.read(self.store, v3))
            if sub == "managed/devices":
                if self._q(raw,'summary')=='throughput':
                    from managed.throughput import view
                    return self._json(200,view(self.store))
                return self._json(200, managed_store.view(self.store, self._q(raw, "device_id")))
            if sub == "history":
                return self._json(200, history_store.view(self.store))
            if sub == "workspace-packages":
                c = self.store.conn()
                try:
                    result = assignments.packages(c,v3,ws_id=self._q(raw,"ws_id"),offset=self._q(raw,"offset") or 0)
                finally:
                    c.close()
                return self._json(200,result)
            if sub == "offline-tasks":
                return self._json(200,offline_tasks.view(self.store,self._q(raw,"ws_id"),self._q(raw,"offset") or 0))
            if sub == "offline-manifest":
                return self._json(200,offline_tasks.manifest(self.store,self._q(raw,"id")))
            if sub == "skips":
                c = self.store.conn()
                try:
                    result = skip_store.view(c, self._q(raw, "repo"), self._q(raw, "package"),
                                             self._q(raw, "offset") or 0)
                except skip_store.SkipError as exc:
                    return self._json(400, {"error": str(exc)})
                finally:
                    c.close()
                return self._json(200, result)
            if sub == "execution-results":
                c = self.store.conn()
                try:
                    result = execution_results.view(c, self._q(raw, "repo"), self._q(raw, "package"))
                finally:
                    c.close()
                return self._json(200, result)
            if sub.startswith("delivery/") and self.store.delivery is None:
                return self._json(503, {"error": "未启用调度产物接收"})
            if sub == "artifacts":
                if self.store.artifacts is None:
                    return self._json(200, {"ok": True, "enabled": False, "items": [],
                                            "note": "未启用产物接收目录"})
                result = self.store.artifacts.list_for_package(
                    self._q(raw, "repo") or "", self._q(raw, "package") or "")
                if self.store.delivery:
                    result["deliveries"] = self.store.delivery.files(
                        self._q(raw, "repo") or "", self._q(raw, "package") or "")
                return self._json(200, result)
            if sub == "delivery/settings":
                return self._json(200, self.store.delivery.config())
            if sub == "delivery/browse":
                try:
                    return self._json(200, self.store.delivery.browse(self._q(raw, "path") or "/"))
                except (ValueError, OSError) as e:
                    return self._json(400, {"error": str(e)})
            if sub.startswith("artifacts/"):
                segments = sub.split("/")
                if len(segments) == 3 and segments[2] == "download":
                    return self._artifact_download(segments[1])
                return self._json(404, {"ok": False, "code": "artifact_not_found", "error": "未知产物路径"})
            if sub == "meta":
                return self._json(200, admin_meta(self.store))
            if sub == "repos":
                return self._json(200, admin_repos(self.store))
            if sub == "keys":
                return self._json(200, admin_keys(self.store))
            if sub == "users":
                return self._json(200, admin_users(self.store))
            if sub == "workspaces":
                return self._json(200, admin_workspaces(self.store))
            if sub == "creds":
                return self._json(200, admin_creds(self.store))
            if sub == "writes":
                return self._json(200, self._admin_writes(raw))
            if sub == "writes/export":
                return self._admin_export(raw)
            if sub.startswith("writes/"):
                write_id = sub[len("writes/"):]
                item = self.store.writes.find(write_id)
                if item is None:
                    return self._json(404, {"ok": False, "error": "写日志不存在",
                                            "code": "not_found"})
                return self._json(200, {"ok": True, "item": item})
            if sub == "notifications":
                return self._json(200, self._admin_notifications(raw))
            # 申报制（SPEC-declare §3.5）：逾期清单 + 每日去重通知惰性落地（只读）
            if sub == "overdue":
                with self.store._write_lock:
                    c = self.store.conn()
                    try:
                        v3.ensure_notifications_schema(c)
                        res = v3.record_overdue_notices(c)
                        view = v3.overdue_view(c)
                        if res["new_notices"] or res["resolved"]:
                            self.store.event(c, "overdue_notice", None, None, "system",
                                             f"new={res['new_notices']} resolved={res['resolved']}")
                        view["new_notices"] = res["new_notices"]
                        view["resolved_now"] = res["resolved"]
                    finally:
                        c.close()
                return self._json(200, view)
            if sub in ("preview", "execute"):
                return self._json(405, {"ok": False, "error": "请使用 POST",
                                        "code": "method_not_allowed"}, {"Allow": "POST"})
        except (v3.AuthError, ValueError) as e:
            return self._json(400,{"ok":False,"error":str(e)})
        except AdminError as e:
            return self._json(e.status, e.body())
        except artifact_core.ArtifactError as e:
            if self._responded:
                self.close_connection = True
                return
            return self._json(e.status, e.body())
        except (sqlite3.Error, OSError):
            if artifact_read:
                if self._responded:
                    self.close_connection = True
                    return
                return self._json(503, {"ok": False, "code": "storage_unavailable", "error": "产物索引暂时不可用"})
            raise
        return self._json(404, {"ok": False, "error": f"未知路径 {path}",
                                "code": "not_found"})

    def _admin_writes(self, raw: str) -> dict:
        def _int(name: str, default: int, lo: int, hi: int) -> int:
            value = self._q(raw, name)
            if value is None or value == "":
                return default
            try:
                return max(lo, min(hi, int(value)))
            except ValueError:
                raise AdminError(400, "bad_request", f"参数 {name} 必须是整数")

        order = (self._q(raw, "order") or "desc").strip().lower()
        if order not in ("asc", "desc"):
            raise AdminError(400, "bad_request", "order 只能是 asc 或 desc")
        result = (self._q(raw, "result") or "").strip()
        if result not in ("", "ok", "error"):
            raise AdminError(400, "bad_request", "result 只能是 ok 或 error")
        out = self.store.writes.query(
            limit=_int("limit", 25, 1, 200), offset=_int("offset", 0, 0, 10 ** 9),
            action=(self._q(raw, "action") or "").strip(),
            actor=(self._q(raw, "actor") or "").strip(),
            result=result,
            q=(self._q(raw, "q") or "").strip()[:200],
            batch_id=(self._q(raw, "batch_id") or "").strip(),
            write_id=(self._q(raw, "write_id") or "").strip(),
            order=order,
            from_ts=_to_epoch(self._q(raw, "from")), to_ts=_to_epoch(self._q(raw, "to")))
        out.update({"ok": True, "generated_at": now_iso(),
                    "limit": _int("limit", 25, 1, 200),
                    "offset": _int("offset", 0, 0, 10 ** 9),
                    "filters": {"action": (self._q(raw, "action") or ""),
                                "actor": (self._q(raw, "actor") or ""),
                                "result": result,
                                "q": (self._q(raw, "q") or ""),
                                "batch_id": (self._q(raw, "batch_id") or ""),
                                "write_id": (self._q(raw, "write_id") or ""),
                                "order": order}})
        return out

    def _admin_export(self, raw: str) -> None:
        fmt = (self._q(raw, "format") or "jsonl").strip().lower()
        if fmt not in ("jsonl", "csv"):
            raise AdminError(400, "bad_request", "format 只能是 jsonl 或 csv")
        result = (self._q(raw, "result") or "").strip()
        if result not in ("", "ok", "error"):
            raise AdminError(400, "bad_request", "result 只能是 ok 或 error")
        body, rows, truncated = self.store.writes.export(
            fmt=fmt, action=(self._q(raw, "action") or "").strip(),
            actor=(self._q(raw, "actor") or "").strip(), result=result,
            q=(self._q(raw, "q") or "").strip()[:200],
            batch_id=(self._q(raw, "batch_id") or "").strip(),
            write_id=(self._q(raw, "write_id") or "").strip(),
            from_ts=_to_epoch(self._q(raw, "from")), to_ts=_to_epoch(self._q(raw, "to")))
        ctype = ("application/x-ndjson; charset=utf-8" if fmt == "jsonl"
                 else "text/csv; charset=utf-8")
        filename = ("fox-writes-" + time.strftime("%Y%m%d-%H%M%S")
                    + (".jsonl" if fmt == "jsonl" else ".csv"))
        self._respond_raw(200, body, ctype, {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Total-Rows": str(rows),
            "X-Truncated": "1" if truncated else "0",
        })

    def _respond_raw(self, code: int, body: bytes, ctype: str,
                     extra: dict | None = None) -> None:
        if getattr(self, "_responded", False):
            return
        self._responded = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _admin_notifications(self, raw: str) -> dict:
        try:
            limit = max(1, min(100, int(self._q(raw, "limit") or 20)))
        except ValueError:
            raise AdminError(400, "bad_request", "参数 limit 必须是整数")
        after = _to_epoch(self._q(raw, "after"))
        after_id = (self._q(raw, "after_id") or "").strip()
        page = self.store.writes.query(limit=500, order="desc")
        rows = page.get("items") or []
        latest = rows[0] if rows else None
        if after is not None:
            def newer(rec: dict) -> bool:
                ts = _to_epoch(rec.get("ts_epoch")) or 0.0
                if ts > after:
                    return True
                if ts < after:
                    return False
                return bool(after_id) and str(rec.get("write_id") or "") > after_id
            rows = [r for r in rows if newer(r)]
        return {"ok": True, "generated_at": now_iso(), "rows": rows[:500],
                "latest": latest, "truncated": page.get("truncated", False)}

    def _admin_post(self, path: str, raw: str) -> None:
        ctx, code, err = self._admin_identity()
        if ctx is None:
            return self._json(code, err or {})
        sub = path[len("/admin/v1"):].strip("/")
        if sub in ("meta", "repos", "keys", "users", "workspaces", "creds", "writes",
                   "writes/export", "notifications", "overdue", "managed/devices") or sub.startswith("writes/"):
            return self._json(405, {"ok": False, "error": "请使用 GET",
                                    "code": "method_not_allowed"}, {"Allow": "GET"})
        if sub not in ("preview", "execute", "delivery/settings", "delivery/repos", "managed/commands", "history/check", "workspace-dispatch", "offline-export", "offline-cancel", "scheduling", "source-catalog"):
            return self._json(404, {"ok": False, "error": f"未知路径 {path}",
                                    "code": "not_found"})
        if not write_enabled():
            return self._json(403, {"ok": False, "error": "服务端写通道已关闭",
                                    "code": "readonly"})
        if not self._admin_rate(ADMIN_PREVIEW_LIMIT if sub == "preview"
                                else ADMIN_EXEC_LIMIT, ctx):
            return
        try:
            body = self._read_json(limit=ADMIN_MAX_BODY, strict=True)
        except PayloadTooLarge:
            return self._json(413, {"ok": False, "error": "请求体过大",
                                    "code": "payload_too_large"})
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "error": "请求体不是合法 JSON",
                                    "code": "bad_request"})
        if not isinstance(body, dict):
            return self._json(400, {"ok": False, "error": "请求体必须是 JSON 对象",
                                    "code": "bad_request"})
        try:
            if sub == 'source-catalog':
                return self._json(200, source_routes.write(self.store, v3, body, ctx['actor']))
            if sub == 'scheduling':
                return self._json(200, scheduling_routes.write(self.store, v3, body, ctx['actor']))
            if sub == "offline-export":
                from managed import contracts
                return self._json(200,offline_tasks.create(self.store,v3,contracts,body,ctx["actor"]))
            if sub == "offline-cancel":
                return self._json(200,offline_tasks.cancel(self.store,v3,body.get("id"),ctx["actor"]))
            if sub == "workspace-dispatch":
                from assignments.service import dispatch
                return self._json(200,dispatch(self.store,v3,body,ctx["actor"]))
            if sub == "managed/commands":
                return self._json(200, managed_store.enqueue(self.store, body, ctx["actor"]))
            if sub == "history/check":
                return self._json(200, history_store.request(self.store, v3, body, ctx["actor"]))
            if sub == "delivery/settings":
                if self.store.delivery is None:
                    return self._json(503, {"error": "未启用调度产物接收"})
                try:
                    return self._json(200, self.store.delivery.save(body))
                except (ValueError, OSError) as e:
                    return self._json(400, {"error": str(e)})
                except Exception:
                    return self._json(400, {"error": "HF 仓库不可访问，请检查 token 和仓库权限"})
            if sub == "delivery/repos":
                if self.store.delivery is None:
                    return self._json(503, {"error": "未启用调度产物接收"})
                try:
                    return self._json(200, self.store.delivery.repos(body.get("hf_token") or ""))
                except Exception:
                    return self._json(400, {"error": "获取 HF 仓库失败，请检查 token 和网络"})
            if sub == "preview":
                status, out = admin_preview(self.store, body.get("action"),
                                            body.get("targets"), body.get("params"), ctx)
            else:
                status, out, headers = admin_execute(
                    self.store, body.get("action"), body.get("targets"),
                    body.get("params"), body.get("confirm_token"),
                    body.get("batch_confirm"), ctx)
                return self._json(status, out, headers or None)
        except ManagedError as e:
            return self._json(e.status, e.body())
        except (v3.AuthError, v3.LeaseError, ValueError) as e:
            return self._json(409 if isinstance(e, v3.LeaseError) else 400, {"ok":False,"error":str(e)})
        except AdminError as e:
            self._note(f"POST {path}", e.status, e.code)
            return self._json(e.status, e.body())
        except sqlite3.OperationalError:
            return self._json(503, {"ok": False, "error": "存储暂时不可用（busy/disk），请稍后重试",
                                    "code": "unavailable"})
        self._json(status, out)

    def _do_get(self) -> None:
        raw = self.path
        path = unquote(raw.split("?", 1)[0]).rstrip("/") or "/"
        if path == "/admin/v1" or path.startswith("/admin/v1/"):
            return self._admin_get(path, raw)
        if path == "/health":
            # 未鉴权端点：只回廉价计数，绝不做全表装载（否则一个 GET 就能打爆内存）
            c = self.store.conn()
            repos = c.execute("SELECT COUNT(*) n FROM repos WHERE enabled=1").fetchone()["n"]
            pkgs = c.execute("SELECT COUNT(*) n FROM packages").fetchone()["n"]
            c.close()
            return self._json(200, {"ok": True, "repos": repos, "packages": pkgs})

        who = self._auth()
        if who is None:
            self._note(f"GET {path}", 401, "no/invalid key")
            return self._json(401, {"error": "需要有效的 Bearer 密钥"})

        if path == "/me":
            en = self._enabled()
            mine = package_rows(self.store, owner=who["name"])
            mine = [r for r in mine if self._allowed(who, r["repo"]) and r["repo"] in en]
            return self._json(200, {"name": who["name"], "role": who["role"],
                                    "grants": sorted(who["grants"]) if who["grants"] else None,
                                    "packages": mine})

        if path == "/repos":
            repos = [r for r in repo_rows(self.store)
                     if r["enabled"] and self._allowed(who, r["repo"])]
            return self._json(200, {"repos": repos})

        # GET /packages/<pkg>?repo=X —— 单包查询。大仓库不能靠 /packages 分页翻找，
        # 40 万包的仓库里目标包可能在第 200 页之外。
        parts = [seg for seg in path.split("/") if seg]
        if len(parts) == 2 and parts[0] == "packages":
            pkg = parts[1]
            try:
                repo = self._resolve_repo(who, pkg, self._q(raw, "repo"))
            except NotFound:
                return self._json(404, {"error": f"包不存在：{pkg}"})
            except Forbidden as e:
                return self._json(403, {"error": str(e)})
            c = self.store.conn()
            row = c.execute(
                "SELECT p.*, " + package_progress.columns() + ", r.repo_type, t.user_id AS reserved_for"
                " FROM packages p JOIN repos r ON r.repo = p.repo"
                + package_progress.JOIN +
                " LEFT JOIN package_targets t ON t.repo=p.repo AND t.pkg=p.id"
                " WHERE p.repo=? AND p.id=?", (repo, pkg)).fetchone()
            c.close()
            if row is None:
                return self._json(404, {"error": f"包不存在：{repo}/{pkg}"})
            out = dict(row)
            package_progress.decorate(out)
            return self._json(200, out)

        if path == "/packages":
            qr = self._q(raw, "repo")
            if qr and not self._allowed(who, qr):
                return self._json(403, {"error": f"无权访问仓库 {qr}"})
            en = self._enabled()
            qs_ = self._q(raw, "status")
            rows = package_rows(self.store, qr)
            rows = [r for r in rows if self._allowed(who, r["repo"]) and r["repo"] in en]
            if qs_:
                wanted = STATUS_HINT.get(qs_, qs_)
                if wanted in ("incomplete", "verifying", "unclaimed", "未完结", "待核验"):
                    wanted = {"unclaimed": "available", "未完结": "incomplete", "待核验": "verifying"}.get(wanted, wanted)
                    rows = [r for r in rows if r["display_status"] == wanted]
                else:
                    rows = [r for r in rows if r["status"] == wanted]
            total_matched = len(rows)
            try:
                limit = max(1, min(int(self._q(raw, "limit") or 500), 5000))
                offset = max(0, int(self._q(raw, "offset") or 0))
            except ValueError:
                limit, offset = 500, 0
            page = rows[offset:offset + limit]
            repos = [r for r in repo_rows(self.store)
                     if (not qr or r["repo"] == qr) and r["enabled"]
                     and self._allowed(who, r["repo"])]
            return self._json(200, {
                "repo_filter": qr,
                "repos": [{"repo": r["repo"], "repo_type": r["repo_type"],
                           "manifest": r["manifest"], "manifest_sha256": r["manifest_sha256"],
                           "manifest_size": r["manifest_size"]} for r in repos],
                "packages": page,
                "page": {"limit": limit, "offset": offset, "matched": total_matched,
                         "truncated": offset + limit < total_matched},
                "summary": summary(self.store, qr),
            })

        if path == "/api/v1/work":
            if who["role"] != "admin":
                return self._json(403, {"error": "仅管理员"})
            dev = self._q(raw, "device_id")
            c = self.store.conn()
            try:
                p = v3.params()
                body = v3.work(c, dev, p) if dev else v3.overview(c)
            except v3.AuthError as e:
                c.close()
                return self._json(404, {"error": str(e)})
            c.close()
            return self._json(200, body)

        if path == "/events":
            return self._sse(who)

        return self._json(404, {"error": f"未知路径 {path}"})

    # -- POST -------------------------------------------------------
    def _do_post(self) -> None:
        raw = self.path
        path = unquote(raw.split("?", 1)[0]).rstrip("/")

        # ---- 管理写通道（本机 + admin key）：必须在客户端路由之前分流 ----
        if path == "/admin/v1" or path.startswith("/admin/v1/"):
            return self._admin_post(path, raw)

        if path == "/api/v1/managed/poll":
            return handle_poll(self, v3, PayloadTooLarge)
        if path == "/api/v1/skips":
            return handle_skips(self, v3, PayloadTooLarge, write_enabled)

        # ---- v3 调度接口：身份是 user_id + secret，和 flk 密钥体系并行 ----
        seg = [x for x in path.split("/") if x]
        if len(seg) >= 3 and seg[:3] == ["api", "v1", "artifacts"]:
            return self._json(405, {"ok": False, "error": "产物正文请使用 PUT"}, {"Allow": "PUT"})
        if len(seg) >= 3 and seg[0] == "api" and seg[1] == "v1":
            return self._v1_post(seg[2], self._read_json())

        who = self._auth()
        if who is None:
            self._note(f"POST {path}", 401, "no/invalid key")
            return self._json(401, {"error": "需要有效的 Bearer 密钥"})

        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[0] == "packages":
            pkg, action = parts[1], parts[2]
            body = self._read_json()
            qr = self._q(raw, "repo")
            try:
                repo = self._resolve_repo(who, pkg, qr)
                is_admin = who["role"] == "admin"
                if action == "claim":
                    res = claim(self.store, repo, pkg, who["name"])
                elif action == "release":
                    res = release(self.store, repo, pkg, who["name"], is_admin, body.get("reason"))
                elif action == "submit":
                    res = submit(self.store, repo, pkg, who["name"], body.get("note"))
                elif action == "downloaded":
                    res = mark_downloaded(self.store, repo, pkg, who["name"])
                else:
                    return self._json(404, {"error": f"未知操作 {action}"})
                self._note(f"{action} {repo}/{pkg}", 200, f"by {who['name']}")
                return self._json(200, res)
            except NotFound as e:
                self._note(f"{action} {pkg}", 404, who["name"])
                return self._json(404, {"error": f"包不存在：{e}"})
            except Conflict as e:
                self._note(f"{action} {pkg}", 409, who["name"])
                return self._json(409, {"error": str(e)})
            except Forbidden as e:
                self._note(f"{action} {pkg}", 403, who["name"])
                return self._json(403, {"error": str(e)})

        self._note(f"POST {path}", 404, who["name"])
        return self._json(404, {"error": f"未知路径 {path}"})

    # -- v3 调度 ----------------------------------------------------
    def _server_id(self) -> str:
        c = self.store.conn()
        row = c.execute("SELECT v FROM meta WHERE k='server_id'").fetchone()
        c.close()
        return (row["v"] if row else "") or os.environ.get("FOXLABEL_SERVER_ID", "fox-01")

    def _v3_event(self, c, kind: str, user: str, device: str,
                  detail: str | None = None) -> None:
        self.store.event(c, kind, None, None, user or "system",
                         detail or (f"device={device}" if device else None))

    def _v3_open(self, c, lease: dict) -> None:
        self.store.event(c, "v3_assign", lease["repo"], lease["pkg"], lease["user_id"],
                         f"{lease['kind']} attempt={lease['attempt']} device={lease['device_id']}")

    def _v1_post(self, action: str, body: dict) -> None:
        if action == 'dispatch-plan':
            from assignments.plans import client
            if not isinstance(body, dict):return self._json(400, {'ok':False,'error':'请求体必须是 JSON 对象'})
            if body.get('action') in ('apply','clear') and not write_enabled():
                return self._json(403, {'ok':False,'error':'服务端写通道已关闭'})
            try: return self._json(200, client(self.store, v3, body))
            except v3.AuthError as exc: return self._json(401, {'ok':False,'error':str(exc)})
            except (ValueError, v3.LeaseError) as exc: return self._json(409, {'ok':False,'error':str(exc)})
            except (sqlite3.Error, OSError): return self._json(503, {'ok':False,'error':'分派存储暂时不可用，请重试'})
        p = v3.params()
        # 派工是"读空闲包→插入租约"的复合操作，必须整体串行：
        # 否则并发握手会各自读到同一个空闲包（TOCTOU），把一片派给多人。
        with self.store._write_lock:
            return self._v1_post_locked(action, body, p)

    def _v1_post_locked(self, action: str, body: dict, p: dict) -> None:
        c = self.store.conn()
        common = {"on_open": self._v3_open, "on_event": self._v3_event}
        try:
            if action == "workspace":
                result_device = assignments.identity(c,v3,body)
                res = assignments.packages(c,v3,device=result_device,offset=body.get("offset",0))
            elif action == "queue":
                res = assignments.queue_mode(c,v3,body)
            elif action == "offline-status":
                res = offline_tasks.status(c,v3,body)
            elif action == "release":
                res = v3.release_lease(c, body, on_event=self._v3_event)
            elif action == "status":
                res = v3.status(c, body)          # 只读，不派工
            elif action == "reserve":
                # 客户端按派工 JSON 批量保留：只写 package_targets，真正的
                # active/prelock 租约仍由 worker 自己领取。
                res = v3.reserve_targets(
                    c, body,
                    on_event=lambda conn, repo, pkg, user, device:
                    self.store.event(conn, "v3_target_reserve", repo, pkg, user,
                                     f"device={device}"))
            elif action == "handshake":
                res = v3.handshake(c, p, body, **common)
                res["server_id"] = self._server_id()
            elif action == "heartbeat":
                res = v3.heartbeat(c, p, body, **common)
            elif action == "ping":
                # idle ping（SPEC-link §2.2）：先限流（每设备每分钟 ≤10 次），再更新
                device_key = str(body.get("device_id") or body.get("user_id") or "?")
                retry = PING_LIMIT.take(device_key)
                if retry > 0:
                    c.close()
                    self._note("/api/v1/ping", 429, device_key[:60])
                    return self._json(429, {"error": "ping 超过限流（10 次/分钟）",
                                            "retry_after": round(retry, 1)})
                res = v3.ping(c, body)
            elif action in ("done", "fail"):
                # 上传后再完成申报必须经过逐图归并；没有启用上传的旧客户端保留原契约。
                if self.store.delivery:
                    with self.store.delivery.connect() as uploads_db:
                        uploaded = uploads_db.execute('SELECT 1 FROM files WHERE lease_id=? LIMIT 1',
                                                       (body.get('lease_id'),)).fetchone()
                    if uploaded:
                        from delivery.reconciliation import accept
                        # accept 复用已登记租约和设备；finish 继续验证请求身份与分项。
                        with v3._Tx(c):
                            delivery_lease = v3._row(c, 'SELECT * FROM leases WHERE lease_id=?', (body.get('lease_id'),))
                            delivery_device = assignments.identity(c, v3, body, update_parts=False)
                            if not delivery_lease or delivery_lease['device_id'] != delivery_device['device_id']:
                                raise v3.LeaseError('回传租约不属于这台设备')
                            if delivery_lease['status'] == 'open':
                                accept(self.store, v3, c, delivery_lease, delivery_device)
                res = v3.finish(c, p, body, ok=(action == "done"), **common)
            elif action == "pool":
                # 池子余量（SPEC-parts §7）：**只读、不需要身份**——它不含任何包名，
                # 谁都能看到"还剩几片、缺 tag 多少、缺 nl 多少"。做成独立动作是
                # 为了让客户端在池子空转（没有租约、不发 ping）时也能刷新顶部计数。
                # 复用同一个连接：它本来就已按 _Tx 打开，另开的连接在 WAL 下
                # 看不到未提交数据，数字会慢一拍。
                res = v3.pool(c)
            else:
                c.close()
                return self._json(404, {"error": f"未知动作 {action}"})
            c.close()
            self._note(f"/api/v1/{action}", 200, str(body.get("device_id") or ""))
            return self._json(200, res)
        except ValueError as e:
            c.close()
            return self._json(400,{"ok":False,"error":str(e)})
        except v3.AuthError as e:
            c.close()
            if isinstance(e, v3.HwError):
                # hw 字段非法是请求体校验错误：400 而不是 401（SPEC-link §1.1）
                self._note(f"/api/v1/{action}", 400, str(e)[:60])
                return self._json(400, {"error": str(e)})
            self._note(f"/api/v1/{action}", 401, str(e)[:60])
            return self._json(401, {"error": str(e)})
        except v3.LeaseError as e:
            c.close()
            self._note(f"/api/v1/{action}", 409, str(e)[:60])
            return self._json(409, {"error": str(e)})
        except sqlite3.OperationalError as e:
            # 磁盘满 / 库被独占 / 只读：属于"暂时不可用"，明确告诉客户端可以重试，
            # 而不是抛一个 500 让它以为请求本身有问题。
            c.close()
            low = str(e).lower()
            self._note(f"/api/v1/{action}", 503, str(e)[:60])
            if any(k in low for k in ("locked", "busy", "disk", "readonly", "unable to open")):
                return self._json(503, {"error": "存储暂时不可用（busy/disk），请稍后重试"})
            raise
        except Exception:
            c.close()
            raise

    # -- SSE --------------------------------------------------------
    def _sse(self, who: dict) -> None:
        try:
            since = int(self.path.split("since=", 1)[1].split("&")[0]) if "since=" in self.path else 0
        except (ValueError, IndexError):
            since = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        def visible(ev: dict) -> bool:
            """事件必须按订阅者的仓库授权过滤，否则受限用户能看到别仓库的活动。"""
            repo = ev.get("repo")
            if repo is None:
                return who["role"] == "admin"
            return self._allowed(who, repo)

        q = self.bus.subscribe()
        if q is None:
            msg = {"kind": "error", "detail": "订阅连接数超过上限，请稍后重试"}
            self.wfile.write(b"data: " + json.dumps(msg, ensure_ascii=False).encode() + b"\n\n")
            self.wfile.flush()
            return
        last_check = time.time()
        try:
            if since:
                c = self.store.conn()
                rows = c.execute("SELECT seq,ts,kind,repo,pkg,actor,detail FROM events"
                                 " WHERE seq>? ORDER BY seq LIMIT 500", (since,)).fetchall()
                c.close()
                for r in rows:
                    ev = dict(r)
                    if visible(ev):
                        self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b": connected\n\n")
            c = self.store.conn()
            cur = c.execute("SELECT COALESCE(MAX(seq),0) AS m FROM events").fetchone()["m"]
            c.close()
            self.wfile.write(b"data: " + json.dumps({"kind": "cursor", "seq": cur}).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    if time.time() - last_check > 20:
                        last_check = time.time()
                        if not self._still_valid(who):
                            break          # 密钥被吊销后长连接必须断开
                    continue
                if not visible(ev):
                    continue
                self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
                if time.time() - last_check > 20:
                    last_check = time.time()
                    if not self._still_valid(who):
                        break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.bus.unsubscribe(q)


class FoxServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128          # 默认 5，20~30 人同时操作会偶发握手被拒

    def handle_error(self, request, client_address) -> None:
        """客户端主动断开（SSE 关闭、超时）属正常情况，不打印 traceback。"""
        import sys as _sys
        et = _sys.exc_info()[0]
        if et and issubclass(et, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def __init__(self, addr, store: Store):
        super().__init__(addr, ApiHandler)
        self.store = store
        from result_exports.service import recover
        recover(store)
        self.bus = Broadcaster(store)
        from scheduling.maintenance import start
        self._maintenance = start(store, v3)
        from source_catalog.manager import Manager
        store.source_catalog = Manager(store, v3, read_hf_token_for)

    def server_close(self) -> None:
        self._maintenance.set()
        self.store.source_catalog.close()
        self.bus.stop()          # 广播线程必须随服务一起退出
        super().server_close()


# ---------------------------------------------------------------- CLI

def _admin_key_for(store: Store) -> tuple[str | None, Path]:
    """仅在首次创建时返回密钥（用于打印一次）；已存在则返回 None。"""
    admin_file = store.dir / "admin.key"
    if admin_file.is_file():
        return None, admin_file
    c = store.conn()
    row = c.execute("SELECT COUNT(*) n FROM keys WHERE role='admin'").fetchone()
    key = None
    if row["n"] == 0:
        key = "fla_" + secrets.token_urlsafe(32)
        existing = c.execute("SELECT hash FROM keys WHERE role='admin'").fetchone()
        if existing:
            c.close()
            return None, admin_file
        name = "admin" if not c.execute("SELECT 1 FROM keys WHERE name='admin'").fetchone() else "admin@console"
        c.execute("INSERT INTO keys(hash,name,role,all_repos,created_at)"
                  " VALUES(?,?,?,1,?)", (key_hash(key), name, "admin", now_iso()))
        store.event(c, "init", None, None, "system", "管理员密钥已签发")
    c.close()
    if key:
        fd = os.open(str(admin_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key + "\n")
    return key, admin_file


def cmd_repo_add(args) -> int:
    store = Store(args.data_dir)
    store.init_schema()
    if args.from_tree:
        tree = json.loads(Path(args.from_tree).read_text(encoding="utf-8"))
    else:
        token = read_hf_token(args.token)
        if not token:
            print("需要一个 HF token（--token / HF_TOKEN / ~/.cache/huggingface/token）", file=sys.stderr)
            return 2
        try:
            tree = fetch_tree(args.repo, args.repo_type, token)
        except HTTPError as e:
            print(f"读取仓库清单失败：HTTP {e.code}（检查仓库名/权限/gated 审批）", file=sys.stderr)
            return 1
        except URLError as e:
            print(f"网络错误：{e.reason}", file=sys.stderr)
            return 1
    try:
        st = repo_add(store, args.repo, args.repo_type, args.include, args.manifest_name, tree)
    except Conflict as e:
        print(str(e), file=sys.stderr)
        return 1
    key, admin_file = _admin_key_for(store)
    print(f"仓库     : {args.repo}（{args.repo_type}）")
    print(f"匹配规则 : {args.include}")
    print(f"新增包数 : {st['added']}（清单中共 {st['files']} 个文件）")
    row = repo_row(store, args.repo)
    print(f"公共清单 : {row['manifest'] or '（未找到，跳过）'}")
    print(f"数据目录 : {store.dir}")
    if key:
        print()
        print("=" * 62)
        print("管理员密钥（只显示这一次）：")
        print(f"  {key}")
        print(f"已同时保存到 {admin_file}（600 权限），请转移到安全位置。")
        print("=" * 62)
    return 0


def cmd_repo_list(args) -> int:
    store = Store(args.data_dir)
    rows = repo_rows(store)
    if not rows:
        print("尚未登记任何仓库")
        return 0
    print(pad("仓库", 44) + pad("类型", 9) + pad("包数", 6) + pad("未认领", 8)
          + pad("已认领", 8) + pad("已完成", 8) + "状态")
    print("-" * 100)
    for r in rows:
        s = r["summary"]
        print(fit(r["repo"], 44) + pad(r["repo_type"], 9) + pad(s["total"], 6)
              + pad(s["available"], 8) + pad(s["claimed"], 8) + pad(s["completed"], 8)
              + ("启用" if r["enabled"] else "停用"))
    return 0


def cmd_repo_sync(args) -> int:
    store = Store(args.data_dir)
    row = repo_row(store, args.repo)
    if not row:
        print(f"仓库 {args.repo} 未登记", file=sys.stderr)
        return 1
    if args.from_tree:
        tree = json.loads(Path(args.from_tree).read_text(encoding="utf-8"))
    else:
        token = read_hf_token(args.token)
        if not token:
            print("需要一个 HF token", file=sys.stderr)
            return 2
        try:
            tree = fetch_tree(args.repo, row["repo_type"], token)
        except HTTPError as e:
            print(f"读取仓库清单失败：HTTP {e.code}", file=sys.stderr)
            return 1
    st = repo_sync(store, args.repo, tree, args.prune)
    print(f"仓库 {args.repo}: 清单 {st['files']} 个文件，新增 {st['added']}，清理 {st['pruned']}")
    return 0


def cmd_repo_toggle(args, enabled: bool) -> int:
    store = Store(args.data_dir)
    try:
        repo_set_enabled(store, args.repo, enabled)
    except NotFound:
        print(f"仓库 {args.repo} 未登记", file=sys.stderr)
        return 1
    print(f"已{'启用' if enabled else '停用'} {args.repo}")
    return 0


def cmd_repo_remove(args) -> int:
    store = Store(args.data_dir)
    try:
        repo_remove(store, args.repo, args.force)
    except NotFound:
        print(f"仓库 {args.repo} 未登记", file=sys.stderr)
        return 1
    except Conflict as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"已删除 {args.repo}")
    return 0


def cmd_ws_add(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    try:
        ws = v3.ws_create(c, args.ws_id, args.name or args.ws_id,
                          args.repo or [], args.note or "")
    except v3.AuthError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        c.close()
    print(f"工作区 {ws['ws_id']}（{ws['name']}）")
    if ws["repos"]:
        print("  仓库：" + "  ".join(ws["repos"]))
    else:
        # 空工作区的设备拿不到任何包，必须说出来——否则管理员以为配好了，
        # 而那台机器只是安静地空转。
        print("  仓库：（空）—— 这个工作区的设备一个包都拿不到，"
              "用 workspace add 补 --repo", file=sys.stderr)
    if ws["users"] or ws["devices"]:
        print(f"  已绑定：{len(ws['users'])} 个账号、{len(ws['devices'])} 台设备")
    return 0


def cmd_ws_list(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    try:
        rows = v3.ws_list(c)
    finally:
        c.close()
    if not rows:
        print("尚未建立任何工作区（所有设备不限制仓库）")
        return 0
    print(pad("工作区", 20) + pad("名称", 20) + pad("仓库", 8)
          + pad("账号", 6) + "设备")
    print("-" * 80)
    for r in rows:
        print(fit(r["ws_id"], 20) + fit(r["name"], 20) + pad(len(r["repos"]), 8)
              + pad(len(r["users"]), 6) + str(len(r["devices"])))
    # 有仓库但没人挂 = 白建；有人挂但没仓库 = 那些人会空转。两种都值得提一句。
    orphan = [r["ws_id"] for r in rows if not r["users"] and not r["devices"]]
    empty = [r["ws_id"] for r in rows if not r["repos"]]
    if empty:
        print(f"\n[警告] 这些工作区没有仓库，绑上去的设备拿不到任何包：{' '.join(empty)}")
    if orphan:
        print(f"\n提示：这些工作区还没分配给任何人：{' '.join(orphan)}")
    return 0


def cmd_ws_show(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    try:
        ws = v3.ws_get(c, args.ws_id)
    except v3.AuthError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        c.close()
    print(f"工作区 {ws['ws_id']}（{ws['name']}）")
    print("  仓库  ：" + ("  ".join(ws["repos"]) if ws["repos"] else "（空，没有设备能拿到包）"))
    print("  账号  ：" + ("  ".join(ws["users"]) if ws["users"] else "（无）"))
    print("  设备  ：" + ("  ".join(ws["devices"]) if ws["devices"] else "（无）"))
    if ws.get("note"):
        print("  备注  ：" + str(ws["note"]))
    return 0


def cmd_ws_assign(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    try:
        ws = v3.ws_assign(c, args.ws_id, user_id=args.user, device_id=args.device)
    except v3.AuthError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        c.close()
    who = "  ".join([*([f"账号 {args.user}"] if args.user else []),
                     *([f"设备 {args.device}"] if args.device else [])])
    print(f"已把 {who} 指到工作区 {ws['ws_id']}（{len(ws['repos'])} 个仓库）")
    # 不改正在跑的包——这是设计，但用户需要知道"现在还没生效"这件事的边界。
    print("  提示：正在跑的包不受影响，派**下一个包**时才按新工作区挑。")
    return 0


def cmd_ws_unassign(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    try:
        msg = v3.ws_unassign(c, user_id=args.user, device_id=args.device)
    except v3.AuthError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        c.close()
    print(msg)
    return 0


def cmd_ws_remove(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    try:
        msg = v3.ws_remove(c, args.ws_id)
    except v3.AuthError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        c.close()
    print(msg)
    return 0


def cmd_serve(args) -> int:
    try:
        store = Store(args.data_dir, artifact_dir=getattr(args, "artifact_dir", None),
                      artifact_limits={
                          "max_bytes": getattr(args, "artifact_max_bytes", artifact_core.DEFAULT_MAX_BYTES),
                          "max_total_bytes": getattr(args, "artifact_total_bytes", artifact_core.DEFAULT_TOTAL_BYTES),
                          "min_free_bytes": getattr(args, "artifact_min_free_bytes", artifact_core.DEFAULT_MIN_FREE_BYTES),
                          "token_ttl": getattr(args, "artifact_token_ttl", artifact_core.DEFAULT_TOKEN_TTL),
                          "upload_timeout": getattr(args, "artifact_upload_timeout", artifact_core.DEFAULT_UPLOAD_TIMEOUT)})
    except (ValueError, OSError) as e:
        print(f"产物存储配置不可用：{type(e).__name__}", file=sys.stderr)
        return 2
    # 顺序要紧：先确认库存在、再建表、最后才写 server_id。
    # 原来写 server_id 在前面，store.conn() 会顺手把一个空 db 建出来，
    # 于是 INSERT 打到还没建的 meta 表上（no such table: meta），
    # 而且那个空文件会让下次启动跳过"尚未初始化"的检查，时好时坏。
    if not store.db_path.exists():
        print("尚未初始化，请先运行 server.py repo add", file=sys.stderr)
        return 2
    store.init_schema()
    try:                                     # 幂等缓存清理：启动一次，之后每小时一次
        idem_gc(store)
    except sqlite3.Error:
        pass
    if getattr(args, "server_id", None):
        c = store.conn()
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('server_id',?)", (args.server_id,))
        c.close()
    srv = FoxServer((args.host, args.port), store)
    repos = repo_rows(store)
    print(f"fox-label 服务已启动: http://{args.host}:{args.port}")
    print(f"仓库数量 : {len(repos)}")
    for r in repos:
        s = r["summary"]
        print(f"  - {r['repo']}: {s['total']} 包 / {human_size(s['bytes_total'])}"
              f"  未认领 {s['available']} / 已认领 {s['claimed']} / 已完成 {s['completed']}")
    print("Ctrl-C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


def cmd_enroll(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    if args.name in ("admin", "root", "system"):
        print(f"{args.name} 是保留名，请换一个用户名", file=sys.stderr)
        c.close()
        return 1
    row = c.execute("SELECT hash, revoked FROM keys WHERE name=?", (args.name,)).fetchone()
    if row and not row["revoked"]:
        print(f"用户 {args.name} 已存在；换密钥请先 revoke", file=sys.stderr)
        c.close()
        return 1
    known = {r["repo"] for r in c.execute("SELECT repo FROM repos")}
    for r in args.repo or []:
        if r not in known:
            print(f"仓库 {r} 未登记（已知：{', '.join(sorted(known)) or '无'}）", file=sys.stderr)
            c.close()
            return 1
    key = "flk_" + secrets.token_urlsafe(32)
    kh = key_hash(key)
    if row and row["revoked"]:
        # 之前吊销过：复用该行并撤销旧授权，避免受 UNIQUE(name) 卡死
        c.execute("DELETE FROM repo_grants WHERE key_hash=?", (row["hash"],))
        c.execute("UPDATE keys SET hash=?, revoked=0, all_repos=?, created_at=?, last_seen=NULL"
                  " WHERE name=?", (kh, 0 if args.repo else 1, now_iso(), args.name))
    else:
        c.execute("INSERT INTO keys(hash,name,role,all_repos,created_at) VALUES(?,?,'user',?,?)",
                  (kh, args.name, 0 if args.repo else 1, now_iso()))
    for r in args.repo or []:
        c.execute("INSERT OR IGNORE INTO repo_grants(key_hash,repo) VALUES(?,?)", (kh, r))
    store.event(c, "enroll", None, None, "admin",
                args.name + (f" [{'、'.join(args.repo)}]" if args.repo else " [全部仓库]"))
    c.close()
    print(f"用户: {args.name}")
    print(f"密钥: {key}")
    print(f"授权: {'、'.join(args.repo) if args.repo else '全部仓库'}")
    return 0


def cmd_devices(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    if getattr(args, "prune", False):
        res = v3.prune_devices(
            c, getattr(args, "days", 30),
            on_event=lambda cc, k, u, d: store.event(
                cc, k, None, None, u or "admin", f"device={d}" if d else None))
        print(f"已归档 {res['archived']} 台超过 {res['days']} 天没心跳的设备")
    ov = v3.overview(c)
    c.close()
    if not ov["devices"]:
        print("还没有设备连上来过")
        return 0
    cur = {}
    for lease in ov["leases"]:
        cur.setdefault(lease["device_id"], []).append(
            f"{lease['kind'][:4]}:{lease['package']}")
    print(pad("设备", 16) + pad("用户", 14) + pad("档位", 7) + pad("速度(张/秒)", 12)
          + pad("最近心跳", 20) + "当前租约")
    print("-" * 108)
    for d in ov["devices"]:
        speed = f"{d['speed']:.3f}" if d["speed"] else "-"
        seen = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["last_seen"] or 0))
        print(fit(d["device_id"], 16) + fit(d["user_id"], 14) + pad(d["tier"], 7)
              + pad(speed, 12) + pad(seen, 20) + ("、".join(cur.get(d["device_id"], [])) or "-"))
    print()
    print(f"包总数 {ov['packages_total']}  未处理 {ov['available']}  "
          f"占用中 {ov['reserved_or_active']}  可分配 {ov['free_for_assign']}")
    return 0


def cmd_reset_secret(args) -> int:
    """兼容入口：secret 已改为以客户端为准，这里只"解除绑定"。"""
    store = Store(args.data_dir)
    c = store.conn()
    try:
        v3.reset_secret(c, args.user_id)
    except v3.AuthError as e:
        c.close()
        print(f"错误：{e}", file=sys.stderr)
        return 1
    store.event(c, "v3_reset_secret", None, None, args.user_id, "管理员解除 secret 绑定")
    c.close()
    print(f"已解除 {args.user_id} 的 secret 绑定。")
    print("secret 现在以客户端本地那份为准：让对方直接重跑 login 即可，不需要转交任何密钥。")
    return 0


def cmd_doctor(args) -> int:
    """体检：找出"谁也领不到"的死包和不该存在的租约残留。"""
    store = Store(args.data_dir)
    c = store.conn()
    stuck = [dict(r) for r in c.execute(
        "SELECT repo,id,owner FROM packages p WHERE p.status='claimed' AND NOT EXISTS"
        " (SELECT 1 FROM leases l WHERE l.repo=p.repo AND l.pkg=p.id AND l.status='open')")]
    orphan = [dict(r) for r in c.execute(
        "SELECT l.lease_id,l.repo,l.pkg FROM leases l WHERE l.status='open' AND NOT EXISTS"
        " (SELECT 1 FROM packages p WHERE p.repo=l.repo AND p.id=l.pkg)")]
    ghosts = [dict(r) for r in c.execute(
        "SELECT l.lease_id,l.device_id FROM leases l WHERE l.status='open' AND NOT EXISTS"
        " (SELECT 1 FROM devices d WHERE d.device_id=l.device_id)")]
    # 申报制（SPEC-declare §2.2/§4）：无 TTL 过期判定；统计保险丝越界租约
    # （held ≥ lease_max_h，30 天），只提示不回收。
    fuse = v3.overdue_leases(c)
    fuse = [r for r in fuse if r["fuse_exceeded"]]
    print(f"死包（claimed 但无租约，v2/v3 都领不到）: {len(stuck)}")
    for r in stuck[:10]:
        print(f"  - {r['repo']}/{r['id']}  owner={r['owner'] or '-'}")
    print(f"孤儿租约（包已不存在）: {len(orphan)}")
    print(f"幽灵租约（设备已不存在）: {len(ghosts)}")
    print(f"保险丝越界的 open 租约（≥{v3.params()['lease_max_h']:.0f}h，请人工处理，不会自动回收）: {len(fuse)}")
    for r in fuse[:10]:
        print(f"  - {r['repo']}/{r['pkg']}  device={r['device_id']} held={r['held_seconds']/3600:.1f}h")
    if args.fix and stuck:
        with store._write_lock:
            for r in stuck:
                c.execute("UPDATE packages SET status='available', owner=NULL, claimed_at=NULL,"
                          " updated_at=? WHERE repo=? AND id=?",
                          (now_iso(), r["repo"], r["id"]))
                store.event(c, "doctor_fix", r["repo"], r["id"], "admin", "死包回池")
        print(f"已修复 {len(stuck)} 个死包 → 未认领")
    c.close()
    return 0 if not (stuck or orphan or ghosts) else 1


def cmd_unassign(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    res = v3.release_device(c, args.device, args.reason or "admin_release",
                            on_event=lambda cc, k, u, d: store.event(
                                cc, k, None, None, u or "admin", f"device={d}"))
    c.close()
    print(f"已作废设备 {res['device_id']} 的 {res['released']} 个租约，包已退回公共池")
    return 0


def cmd_forget(args) -> int:
    """强行解绑：--device 断一台机器，--user 清掉整个身份。"""
    store = Store(args.data_dir)
    try:
        if args.user:
            res = admin_forget_user(store, args.user, "admin",
                                    args.reason or "admin_forget")
            print(f"已清空用户 {res['user_id']}：删除 {res['devices']} 台设备，"
                  f"作废 {res['released']} 个租约")
            print("之后该 user_id 的握手会按本机 secret 重新登记，等于新用户。")
            return 0
        res = admin_forget_device(store, args.device, "admin",
                                  args.reason or "admin_forget")
    except NotFound as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    print(f"已强制解绑设备 {res['device_id']}（原用户 {res['user_id']}）："
          f"作废 {res['released']} 个租约，{res['packages_back']} 个包退回公共池")
    print("该设备下次握手会按新 user_id 重新登记。")
    return 0


def cmd_leases(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    ov = v3.overview(c)
    c.close()
    if not ov["leases"]:
        print("当前没有进行中的租约")
        return 0
    print(pad("租约", 14) + pad("类型", 8) + pad("包", 24) + pad("用户", 12)
          + pad("设备", 14) + pad("已持h", 8) + "进度")
    print("-" * 100)
    for l in ov["leases"]:
        print(fit(l["lease_id"], 14) + pad(l["kind"], 8) + fit(l["package"], 24)
              + fit(l["user_id"], 12) + fit(l["device_id"], 14)
              + pad(round(l["held_seconds"]/3600, 1), 8) + json.dumps(l["progress"], ensure_ascii=False)[:24])
    return 0


def cmd_keys(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    rows = c.execute("SELECT name,role,hash,created_at,revoked,last_seen FROM keys"
                     " ORDER BY role, name").fetchall()
    grants = {}
    for g in c.execute("SELECT key_hash, repo FROM repo_grants"):
        grants.setdefault(g["key_hash"], []).append(g["repo"])
    c.close()
    print(pad("名称", 16) + pad("角色", 7) + pad("状态", 8) + pad("授权仓库", 28)
          + pad("创建时间", 22) + "最近使用")
    print("-" * 104)
    for r in rows:
        gl = "全部" if not grants.get(r["hash"]) else "、".join(sorted(grants[r["hash"]]))
        print(fit(r["name"], 16) + pad(r["role"], 7)
              + pad("已吊销" if r["revoked"] else "有效", 8)
              + pad(gl, 28) + pad(r["created_at"] or "-", 22) + (r["last_seen"] or "-"))
    return 0


def cmd_revoke(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    row = c.execute("SELECT role FROM keys WHERE name=?", (args.name,)).fetchone()
    if row is None:
        print(f"没有名为 {args.name} 的密钥", file=sys.stderr)
        c.close()
        return 1
    if row["role"] == "admin":
        left = c.execute("SELECT COUNT(*) n FROM keys WHERE role='admin' AND revoked=0").fetchone()["n"]
        if left <= 1:
            print("不能吊销最后一个管理员密钥（会彻底失去管理入口）", file=sys.stderr)
            c.close()
            return 1
    cur = c.execute("UPDATE keys SET revoked=1 WHERE name=?", (args.name,))
    store.event(c, "revoke", None, None, "admin", args.name)
    c.close()
    print(f"已吊销 {args.name}")
    return 0


def cmd_release(args) -> int:
    store = Store(args.data_dir)
    repo, pkg = split_ref(args.package)
    repo = args.repo or repo
    if not repo:
        print("多仓库请用 --repo 指定，或用 repo/包名 形式", file=sys.stderr)
        return 2
    try:
        r = release(store, repo, pkg, "admin", True, args.reason)
    except NotFound:
        print(f"包 {repo}/{pkg} 不存在", file=sys.stderr)
        return 1
    except Conflict as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"已释放 {repo}/{pkg}（原认领人 {r['previous_owner']}）")
    return 0


def cmd_reopen(args) -> int:
    store = Store(args.data_dir)
    repo, pkg = split_ref(args.package)
    repo = args.repo or repo
    if not repo:
        print("多仓库请用 --repo 指定，或用 repo/包名 形式", file=sys.stderr)
        return 2
    try:
        r = reopen(store, repo, pkg, args.to, args.reason, getattr(args, "owner", None))
    except NotFound:
        print(f"包 {repo}/{pkg} 不存在", file=sys.stderr)
        return 1
    except Conflict as e:
        print(str(e), file=sys.stderr)
        return 1
    target = "已认领" if r["status"] == "claimed" else "未认领"
    print(f"已打回 {repo}/{pkg} → {target}"
          + (f"（认领人 {r['owner']}）" if r["status"] == "claimed" else ""))
    return 0


def cmd_status(args) -> int:
    store = Store(args.data_dir)
    s = summary(store, args.repo)
    rows = package_rows(store, args.repo)
    print(f"仓库: {args.repo or '（全部）'}")
    print(f"总计 {s['total']} 个包 / {human_size(s['bytes_total'])}   "
          f"未认领 {s['available']} · 已认领 {s['claimed']} · 已完成 {s['completed']}"
          f"   已下载 {s['downloaded']}")
    print()
    print(pad("仓库", 34) + pad("包", 20) + pad("大小", 10) + pad("状态", 9)
          + pad("认领人", 12) + pad("已下载", 8) + "更新时间")
    print("-" * 122)
    for r in rows:
        print(fit(r["repo"], 34) + fit(r["id"], 20) + pad(human_size(r["size"]), 10)
              + pad(r["status_label"], 9) + pad(r["owner"] or "-", 12)
              + pad("是" if r.get("downloaded_at") else "-", 8) + (r["updated_at"] or "-"))

    us = user_stats(store, args.repo)
    if us:
        print()
        print(pad("用户", 16) + pad("认领", 8) + pad("已完成", 9) + "已下载")
        print("-" * 42)
        for u in us:
            print(fit(u["owner"], 16) + pad(u["n"], 8) + pad(u["done"], 9) + str(u["dl"]))
    return 0


def cmd_export(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    sql = "SELECT seq,ts,kind,repo,pkg,actor,detail FROM events"
    args_l: list = []
    if args.repo:
        sql += " WHERE repo=? OR repo IS NULL"
        args_l.append(args.repo)
    sql += " ORDER BY seq"
    rows = c.execute(sql, args_l).fetchall()
    c.close()
    data = "".join(json.dumps(dict(r), ensure_ascii=False) + "\n" for r in rows)
    if args.out:
        Path(args.out).write_text(data, encoding="utf-8")
        print(f"已导出 {len(rows)} 条事件到 {args.out}")
    else:
        sys.stdout.write(data)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="server.py", description="fox-label 服务端（多仓库）")
    p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    sub = p.add_subparsers(dest="cmd", required=True)

    # repo 子命令组
    rp = sub.add_parser("repo", help="仓库管理")
    rsub = rp.add_subparsers(dest="repo_cmd", required=True)

    a = rsub.add_parser("add", help="登记仓库并建立包索引")
    a.add_argument("--repo", required=True)
    a.add_argument("--repo-type", default="dataset", choices=["dataset", "model"])
    a.add_argument("--include", default=DEFAULT_INCLUDE)
    a.add_argument("--manifest-name", default=DEFAULT_MANIFEST)
    a.add_argument("--token", default=None)
    a.add_argument("--from-tree", default=None)
    a.set_defaults(func=cmd_repo_add)

    a = rsub.add_parser("list", help="列出仓库")
    a.set_defaults(func=cmd_repo_list)

    a = rsub.add_parser("sync", help="补录新增文件")
    a.add_argument("--repo", required=True)
    a.add_argument("--prune", action="store_true", help="清理已从仓库消失且仍未被认领的包")
    a.add_argument("--token", default=None)
    a.add_argument("--from-tree", default=None)
    a.set_defaults(func=cmd_repo_sync)

    a = rsub.add_parser("enable", help="启用仓库")
    a.add_argument("--repo", required=True)
    a.set_defaults(func=lambda args: cmd_repo_toggle(args, True))

    a = rsub.add_parser("disable", help="停用仓库（保留数据）")
    a.add_argument("--repo", required=True)
    a.set_defaults(func=lambda args: cmd_repo_toggle(args, False))

    a = rsub.add_parser("remove", help="删除仓库及其包记录")
    a.add_argument("--repo", required=True)
    a.add_argument("--force", action="store_true")
    a.set_defaults(func=cmd_repo_remove)

    # workspace 子命令组（2026-11-13）：把"哪些仓库的活归谁做"变成配置。
    # 改造前所有启用仓库的包混着派，设备没法只做其中一个仓库。
    wp = sub.add_parser("workspace", help="工作区：仓库归属与分配")
    wsub = wp.add_subparsers(dest="ws_cmd", required=True)

    a = wsub.add_parser("add", help="新建工作区（同名再跑一次 = 更新仓库列表）")
    a.add_argument("--id", required=True, dest="ws_id", help="工作区 id（字母数字与 - _ .）")
    a.add_argument("--name", default="", help="显示名，默认同 id")
    a.add_argument("--repo", action="append", default=[],
                   help="能做的仓库，可重复；必须是已 repo add 过的")
    a.add_argument("--note", default="", help="备注")
    a.set_defaults(func=cmd_ws_add)

    a = wsub.add_parser("list", help="列出所有工作区")
    a.set_defaults(func=cmd_ws_list)

    a = wsub.add_parser("show", help="看某个工作区绑了谁")
    a.add_argument("--id", required=True, dest="ws_id")
    a.set_defaults(func=cmd_ws_show)

    a = wsub.add_parser("assign", help="把账号/设备指到工作区（下一个包生效）")
    a.add_argument("--id", required=True, dest="ws_id")
    a.add_argument("--user", default=None, help="账号（该账号所有设备默认用它）")
    a.add_argument("--device", default=None, help="单台设备覆盖（优先于账号）")
    a.set_defaults(func=cmd_ws_assign)

    a = wsub.add_parser("unassign", help="解除归属，回到不限制仓库")
    a.add_argument("--user", default=None)
    a.add_argument("--device", default=None)
    a.set_defaults(func=cmd_ws_unassign)

    a = wsub.add_parser("remove", help="删除工作区（同时解绑挂在上面的人）")
    a.add_argument("--id", required=True, dest="ws_id")
    a.set_defaults(func=cmd_ws_remove)

    # 兼容旧用法：init == repo add
    a = sub.add_parser("init", help="等价于 repo add（兼容旧用法）")
    a.add_argument("--repo", required=True)
    a.add_argument("--repo-type", default="dataset", choices=["dataset", "model"])
    a.add_argument("--include", default=DEFAULT_INCLUDE)
    a.add_argument("--manifest-name", default=DEFAULT_MANIFEST)
    a.add_argument("--token", default=None)
    a.add_argument("--from-tree", default=None)
    a.set_defaults(func=cmd_repo_add)

    a = sub.add_parser("serve", help="启动 HTTP 服务")
    a.add_argument("--artifact-dir", default=None, help="独立产物目录（不配置即关闭上传/回收功能）")
    a.add_argument("--artifact-max-bytes", type=int, default=artifact_core.DEFAULT_MAX_BYTES)
    a.add_argument("--artifact-total-bytes", type=int, default=artifact_core.DEFAULT_TOTAL_BYTES)
    a.add_argument("--artifact-min-free-bytes", type=int, default=artifact_core.DEFAULT_MIN_FREE_BYTES)
    a.add_argument("--artifact-token-ttl", type=int, default=artifact_core.DEFAULT_TOKEN_TTL)
    a.add_argument("--artifact-upload-timeout", type=float, default=artifact_core.DEFAULT_UPLOAD_TIMEOUT)
    a.add_argument("--host", default="127.0.0.1",
                   help="监听地址；对外提供服务请显式用 0.0.0.0 并加 TLS 反代")
    a.add_argument("--port", type=int, default=DEFAULT_PORT)
    a.add_argument("--server-id", default=None,
                   help="服务端标识（下发给客户端；也可用 FOXLABEL_SERVER_ID）")
    a.set_defaults(func=cmd_serve)

    a = sub.add_parser("enroll", help="签发用户密钥")
    a.add_argument("--name", required=True)
    a.add_argument("--repo", action="append", default=None,
                   help="限定可访问的仓库，可重复；省略表示全部仓库")
    a.set_defaults(func=cmd_enroll)

    a = sub.add_parser("devices", help="看设备与当前派工")
    a.add_argument("--prune", action="store_true", help="归档长期失联的设备")
    a.add_argument("--days", type=float, default=30, help="失联多少天算僵尸（默认 30）")
    a.set_defaults(func=cmd_devices)

    a = sub.add_parser("reset-secret", help="重签某用户的 secret（换机/重装/泄漏）")
    a.add_argument("--user-id", required=True)
    a.set_defaults(func=cmd_reset_secret)

    a = sub.add_parser("doctor", help="体检：死包 / 孤儿租约 / 幽灵租约")
    a.add_argument("--fix", action="store_true", help="把死包放回公共池")
    a.set_defaults(func=cmd_doctor)

    a = sub.add_parser("unassign", help="作废某台设备的全部租约（设备报废/换机）")
    a.add_argument("--device", required=True)
    a.add_argument("--reason", default=None)
    a.set_defaults(func=cmd_unassign)

    a = sub.add_parser("forget", help="强行解绑：--device 断一台机器，--user 清掉整个身份")
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--device", help="设备号：作废租约并删除该设备的用户绑定")
    g.add_argument("--user", help="用户：作废其全部租约、删设备、删用户行")
    a.add_argument("--reason", default=None)
    a.set_defaults(func=cmd_forget)

    a = sub.add_parser("leases", help="看进行中的租约")
    a.set_defaults(func=cmd_leases)

    a = sub.add_parser("keys", help="列出密钥")
    a.set_defaults(func=cmd_keys)

    a = sub.add_parser("revoke", help="吊销密钥")
    a.add_argument("--name", required=True)
    a.set_defaults(func=cmd_revoke)

    a = sub.add_parser("release", help="强制释放已认领的包")
    a.add_argument("--package", required=True, help="包名，或 repo/包名")
    a.add_argument("--repo", default=None, help="仓库（package 用裸包名时必填）")
    a.add_argument("--reason", default=None)
    a.set_defaults(func=cmd_release)

    a = sub.add_parser("reopen", help="把已完成的包打回")
    a.add_argument("--package", required=True, help="包名，或 repo/包名")
    a.add_argument("--repo", default=None)
    a.add_argument("--to", default="claimed", choices=["claimed", "available"],
                   help="claimed=打回给原认领人重做（默认）；available=直接回公共池")
    a.add_argument("--reason", default=None)
    a.set_defaults(func=cmd_reopen)

    a = sub.add_parser("status", help="打印看板")
    a.add_argument("--repo", default=None)
    a.set_defaults(func=cmd_status)

    a = sub.add_parser("export", help="导出审计事件")
    a.add_argument("--out", default=None)
    a.add_argument("--repo", default=None)
    a.set_defaults(func=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
