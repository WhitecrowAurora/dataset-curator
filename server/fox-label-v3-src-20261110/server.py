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
import hashlib
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
SWEEP_SEC = _env_int("FOXLABEL_SWEEP", 30)
DEFAULT_MANIFEST = "manifest.jsonl"

STATUS_LABEL = {"available": "未认领", "claimed": "已认领", "completed": "已完成"}

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
    def __init__(self, data_dir: Path | str):
        self.dir = Path(data_dir).expanduser().resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "db.sqlite3"
        self.audit_path = self.dir / "audit.jsonl"
        # 可重入：状态变更持有写锁期间还会调用 event() 写审计
        self._write_lock = threading.RLock()
        # 已存在的库先做建表/迁移，保证任何子命令都能直接跑
        if self.db_path.exists():
            self.init_schema()

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
        legacy = c.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                           " AND name='devices'").fetchone()
        if legacy and "first_seen" not in (legacy["sql"] or ""):
            n = c.execute("SELECT COUNT(*) n FROM devices").fetchone()["n"]
            if n == 0:              # 只清空表，绝不动有数据的表
                c.execute("DROP TABLE devices")
        v3.ensure_schema(c)          # v3：users / devices / leases
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
    def event(self, conn, kind: str, repo, pkg, actor, detail) -> None:
        ts = now_iso()
        conn.execute(
            "INSERT INTO events(ts,kind,repo,pkg,actor,detail) VALUES(?,?,?,?,?,?)",
            (ts, kind, repo, pkg, actor, detail))
        rec = {"ts": ts, "kind": kind, "repo": repo, "pkg": pkg, "actor": actor, "detail": detail}
        with self._write_lock:
            try:
                with self.audit_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError as e:
                # 审计文件不可写时只告警：events 表才是权威记录，不能拖垮业务事务
                print(f"[warn] 写审计文件失败：{e}", file=sys.stderr)


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


def repo_set_enabled(store: Store, repo: str, enabled: bool) -> None:
    c = store.conn()
    cur = c.execute("UPDATE repos SET enabled=? WHERE repo=?", (1 if enabled else 0, repo))
    if cur.rowcount == 0:
        c.close()
        raise NotFound(repo)
    store.event(c, "repo_enable" if enabled else "repo_disable", repo, None, "admin", None)
    c.close()


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
            reason: str | None = None) -> dict:
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
            store.event(c, "admin_release" if is_admin else "release", repo, pkg, actor,
                        reason or f"原认领人 {row['owner']}")
            c.execute("COMMIT")
            return {"repo": repo, "id": pkg, "status": "available", "previous_owner": row["owner"]}
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
           reason: str | None = None, owner: str | None = None) -> dict:
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
            store.event(c, "reopen", repo, pkg, "admin",
                        f"-> {STATUS_LABEL[to_status]}" + (f"; {reason}" if reason else ""))
            c.execute("COMMIT")
            return {"repo": repo, "id": pkg, "status": to_status, "owner": row["owner"]}
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
    sql = ("SELECT p.* FROM packages p JOIN repos r ON r.repo=p.repo WHERE 1=1")
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
        r["status_label"] = STATUS_LABEL.get(r["status"], r["status"])
    return rows


def summary(store: Store, repo: str | None = None) -> dict:
    """用 SQL 聚合，避免把整张表读成 Python 对象（40 万包时是几百 MB）。"""
    c = store.conn()
    where, args = ("WHERE repo=?", (repo,)) if repo else ("", ())
    row = c.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS s,"
        " COALESCE(SUM(status='available'),0) AS a,"
        " COALESCE(SUM(status='claimed'),0) AS cl,"
        " COALESCE(SUM(status='completed'),0) AS co,"
        " COALESCE(SUM(downloaded_at IS NOT NULL),0) AS d,"
        " COUNT(DISTINCT repo) AS r FROM packages " + where, args).fetchone()
    c.close()
    return {"repos": 1 if repo else (row["r"] or 0), "total": row["n"],
            "available": row["a"], "claimed": row["cl"], "completed": row["co"],
            "downloaded": row["d"], "bytes_total": row["s"]}


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

    def _json(self, code: int, obj) -> None:
        if getattr(self, "_responded", False):
            return
        self._responded = True
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0:
            return {}
        if n > self.MAX_BODY:
            # 先把超出的请求体吃掉（有上限）再回 413，否则服务端直接关连接，
            # 客户端看到的是 connection reset 而不是明确的 413。
            left = min(n, self.MAX_BODY * 4)
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
            raise PayloadTooLarge()
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    def _auth(self) -> dict | None:
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            return None
        key = h[7:].strip()
        if not key:
            return None
        kh = key_hash(key)
        c = self.store.conn()
        row = c.execute("SELECT name, role, revoked FROM keys WHERE hash=?", (kh,)).fetchone()
        if row is None or row["revoked"]:
            c.close()
            return None
        now = time.time()
        if now - _LAST_SEEN.get(kh, 0) > 60:      # 每个密钥每分钟最多写一次
            _LAST_SEEN[kh] = now
            c.execute("UPDATE keys SET last_seen=? WHERE hash=?", (now_iso(), kh))
        c.close()
        return {"name": row["name"], "role": row["role"],
                "grants": key_grants(self.store, kh)}

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
        try:
            if method == "GET":
                self._do_get()
            else:
                self._do_post()
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

    def _do_get(self) -> None:
        raw = self.path
        path = unquote(raw.split("?", 1)[0]).rstrip("/") or "/"
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
                "SELECT p.*, r.repo_type FROM packages p JOIN repos r ON r.repo = p.repo"
                " WHERE p.repo=? AND p.id=?", (repo, pkg)).fetchone()
            c.close()
            if row is None:
                return self._json(404, {"error": f"包不存在：{repo}/{pkg}"})
            out = dict(row)
            out["status_label"] = STATUS_LABEL.get(out["status"], out["status"])
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
                rows = [r for r in rows if r["status"] == STATUS_HINT.get(qs_, qs_)]
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
                body = v3.work(c, dev) if dev else v3.overview(c)
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

        # ---- v3 调度接口：身份是 user_id + secret，和 flk 密钥体系并行 ----
        seg = [x for x in path.split("/") if x]
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

    def _v3_event(self, c, kind: str, user: str, device: str) -> None:
        self.store.event(c, kind, None, None, user or "system",
                         f"device={device}" if device else None)

    def _v3_open(self, c, lease: dict) -> None:
        self.store.event(c, "v3_assign", lease["repo"], lease["pkg"], lease["user_id"],
                         f"{lease['kind']} attempt={lease['attempt']} device={lease['device_id']}")

    def _v1_post(self, action: str, body: dict) -> None:
        p = v3.params()
        # 派工是"读空闲包→插入租约"的复合操作，必须整体串行：
        # 否则并发握手会各自读到同一个空闲包（TOCTOU），把一片派给多人。
        with self.store._write_lock:
            return self._v1_post_locked(action, body, p)

    def _v1_post_locked(self, action: str, body: dict, p: dict) -> None:
        c = self.store.conn()
        common = {"on_open": self._v3_open, "on_event": self._v3_event}
        try:
            if action == "release":
                res = v3.release_lease(c, body, on_event=self._v3_event)
            elif action == "status":
                res = v3.status(c, body)          # 只读，不派工
            elif action == "handshake":
                res = v3.handshake(c, p, body, **common)
                res["server_id"] = self._server_id()
            elif action == "heartbeat":
                res = v3.heartbeat(c, p, body, **common)
            elif action in ("done", "fail"):
                res = v3.finish(c, p, body, ok=(action == "done"), **common)
            else:
                c.close()
                return self._json(404, {"error": f"未知动作 {action}"})
            c.close()
            self._note(f"/api/v1/{action}", 200, str(body.get("device_id") or ""))
            return self._json(200, res)
        except v3.AuthError as e:
            c.close()
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
        self.bus = Broadcaster(store)

    def server_close(self) -> None:
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


def cmd_serve(args) -> int:
    store = Store(args.data_dir)
    if getattr(args, "server_id", None):
        c = store.conn()
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('server_id',?)", (args.server_id,))
        c.close()
    if not store.db_path.exists():
        print("尚未初始化，请先运行 server.py repo add", file=sys.stderr)
        return 2
    store.init_schema()
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
    store = Store(args.data_dir)
    c = store.conn()
    try:
        secret = v3.reset_secret(c, args.user_id)
    except v3.AuthError as e:
        c.close()
        print(f"错误：{e}", file=sys.stderr)
        return 1
    store.event(c, "v3_reset_secret", None, None, args.user_id, "管理员重置 secret")
    c.close()
    print(f"已为 {args.user_id} 重签 secret（旧的全部失效）")
    print(f"secret={secret}")
    print(f"请让该用户执行：worker.py login --server <地址> --user-id {args.user_id}"
          f" --secret {secret}")
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
    expired = c.execute("SELECT COUNT(*) n FROM leases WHERE status='open' AND expires_at<?",
                        (time.time(),)).fetchone()["n"]
    print(f"死包（claimed 但无租约，v2/v3 都领不到）: {len(stuck)}")
    for r in stuck[:10]:
        print(f"  - {r['repo']}/{r['id']}  owner={r['owner'] or '-'}")
    print(f"孤儿租约（包已不存在）: {len(orphan)}")
    print(f"幽灵租约（设备已不存在）: {len(ghosts)}")
    print(f"已超时未回收的 open 租约: {expired}")
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


def cmd_leases(args) -> int:
    store = Store(args.data_dir)
    c = store.conn()
    ov = v3.overview(c)
    c.close()
    if not ov["leases"]:
        print("当前没有进行中的租约")
        return 0
    print(pad("租约", 14) + pad("类型", 8) + pad("包", 24) + pad("用户", 12)
          + pad("设备", 14) + pad("剩余秒", 8) + "进度")
    print("-" * 100)
    for l in ov["leases"]:
        print(fit(l["lease_id"], 14) + pad(l["kind"], 8) + fit(l["package"], 24)
              + fit(l["user_id"], 12) + fit(l["device_id"], 14)
              + pad(l["expires_in"], 8) + json.dumps(l["progress"], ensure_ascii=False)[:24])
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
