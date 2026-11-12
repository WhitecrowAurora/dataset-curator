#!/usr/bin/env python3
"""fox-panel：fox-label 的 Web 面板（独立进程，不耦合服务端代码）。

设计原则
--------
* **不耦合**：只通过 HTTP 调用 fox-label 的接口，不 import server.py、
  不直接读它的 SQLite。服务端挂了，面板只显示"上游不可用"，自身不崩。
* **自带鉴权**：账号 + PBKDF2 密码 + HMAC 签名会话 Cookie，登录失败限速。
* **读为主、写有闸**：v2 的只读面逐字保留；v3 新增的写通道（/api/write/*）
  只做代理，真正的强制点（confirm token、影响面哈希、审计、写日志）都在
  fox-label 的 /admin/v1/*。面板侧负责：写开关、CSRF（双提交 + Origin）、
  幂等、限流、失败不半写、写后清缓存。admin key 只在面板进程内使用，
  永不出现在 HTML / JS / 日志 / 响应里。

端口默认 31832（紧邻服务端 31831），默认只监听 127.0.0.1。
"""

from __future__ import annotations

import base64
import getpass
import gzip
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "3.0.0"
DEFAULT_PORT = 31832
DEFAULT_UPSTREAM = "http://127.0.0.1:31831"
CONFIG = Path(os.environ.get("FOXLABEL_PANEL_CONFIG")
              or Path.home() / ".config" / "fox-panel" / "config.json")
MAX_BODY = 64 * 1024
_CLEAR_COOKIE = "fp_sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
SESSION_HOURS = 12
CACHE_TTL = 2.0            # 上游结果缓存，防止刷新风暴
UPSTREAM_TIMEOUT = 6.0

# ---- v3 写通道（SPEC-v3 §2/§4/§5/§6/§7）
WRITE_MAX_BODY = 256 * 1024          # 写请求体上限（§2.2）
WRITE_CONFIRM_TTL = 120              # 与服务端默认一致；meta 里以服务端为准
BATCH_THRESHOLD = 5                  # >5 必须数量确认（§3.5）
MAX_BATCH = 100
WRITE_UPSTREAM_TIMEOUT = 20.0        # 写请求超时（读仍是 6s，§6.5）
IDEM_TTL = 600                       # 面板幂等缓存 10 分钟
CSRF_COOKIE = "fp_csrf"
CSRF_MAX_AGE = SESSION_HOURS * 3600

# 动作目录：面板路由 ←→ 服务端 action。字段与 /api/meta.write.actions 同构。
WRITE_ACTIONS = {
    "package.reopen": {"path": "/api/write/reopen", "label": "打回重做",
                       "level": "danger", "target": "package", "multi": True,
                       "priority": "P0"},
    "package.release": {"path": "/api/write/package-release", "label": "释放包",
                        "level": "danger", "target": "package", "multi": True,
                        "priority": "P1"},
    "lease.release": {"path": "/api/write/lease-release", "label": "释放租约",
                      "level": "danger", "target": "lease", "multi": True,
                      "priority": "P0"},
    "device.unassign": {"path": "/api/write/device-unassign", "label": "解绑设备",
                        "level": "danger", "target": "device", "multi": False,
                        "priority": "P0"},
    "device.forget": {"path": "/api/write/device-forget", "label": "强制解绑设备",
                      "level": "danger", "target": "device", "multi": False,
                      "priority": "P1"},
    "repo.enable": {"path": "/api/write/repo-enable", "label": "启用仓库",
                    "level": "normal", "target": "repo", "multi": True,
                    "priority": "P0"},
    "repo.disable": {"path": "/api/write/repo-disable", "label": "停用仓库",
                     "level": "danger", "target": "repo", "multi": True,
                     "priority": "P0"},
    "key.revoke": {"path": "/api/write/key-revoke", "label": "吊销密钥",
                   "level": "key", "target": "key", "multi": True, "priority": "P1"},
    "user.rekey": {"path": "/api/write/user-rekey", "label": "重签密钥",
                   "level": "key", "target": "user", "multi": False, "priority": "P2"},
    "user.forget": {"path": "/api/write/user-forget", "label": "清空用户身份",
                    "level": "danger", "target": "user", "multi": False,
                    "priority": "P1"},
}
WRITE_PATH_ACTION = {spec["path"]: aid for aid, spec in WRITE_ACTIONS.items()}
TARGET_FIELDS = {"package": ("repo", "id"), "lease": ("lease_id",),
                 "device": ("device_id",), "repo": ("repo",), "key": ("name",),
                 "user": ("user_id",)}

# ---- v2 静态资源与列表/导出约束（值集中在这里，前端通过 /api/meta 取，别在两处写死）
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"          # 前端产物目录；缺失时回退内置字符串
REFRESH_MS = 15_000
DEFAULT_PAGE_SIZE = 50
PAGE_SIZES = (25, 50, 100, 200)
AUDIT_PAGE_SIZE = 25
EXPORT_MAX_ROWS = 100_000           # CSV 一次最多导出行数
MAX_SCAN_ROWS = 200_000             # 全量拉取上限：再大就只排前 20 万行并置 truncated
STATUS_LABEL = {"available": "未认领", "claimed": "处理中", "completed": "已完成"}
STATUS_VALUES = tuple(STATUS_LABEL)
SORT_DEFAULT_ORDER = {"updated": "desc", "size": "desc", "id": "asc"}
SORT_LABEL = {"updated": "更新时间", "size": "大小", "id": "包名"}
CSV_COLUMNS = ("repo", "id", "size", "sha256", "status", "owner",
               "claimed_at", "completed_at", "downloaded_at", "updated_at")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'")

# 全量扫描（/api/packages、导出、单包兜底）在这台 1C/1GB 小机器上是重活：
# 一次 20 万行能吃到几百 MiB，所以既缓存又限流，别让并发各扫一遍。
SCAN_TTL = CACHE_TTL         # 同一 (repo,status) 的扫描结果复用 2 秒
SCAN_MAX_PARALLEL = 1        # 1C/1GB 机器：全量扫描串行，第二个排队（避免 2×670MiB）
SCAN_CACHE_MAX = 8           # 缓存条目上限
SCAN_CACHE_MAX_ROWS = 120_000  # 缓存里的总行数预算：20 万行一条就够吃几百 MiB，
                               # 超预算的扫描干脆不缓存（只影响复用，不影响正确性）


# ------------------------------------------------------------------ 配置

def die(code: int, msg: str) -> None:
    print(f"[X] {msg}", file=sys.stderr)
    raise SystemExit(code)


def load_cfg() -> dict:
    if not CONFIG.exists():
        die(2, f"还没有初始化：{CONFIG} 不存在，先跑 "
               f"`python3 panel.py init --user admin`")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save_cfg(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG)


def hash_password(password: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations).hex()


def _init_backup_path(target: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = target.with_name(f"{target.name}.bak-{stamp}")
    n = 1
    while backup.exists():
        backup = target.with_name(f"{target.name}.bak-{stamp}-{n}")
        n += 1
    return backup


def cmd_init(args) -> int:
    """建面板账号。

    事故防护（t9 复审）：**已存在的配置一律拒绝覆盖**，与文件权限无关。
    覆盖需要两个独立开关同时满足：命令行 --force 且环境变量 FOXLABEL_ALLOW_INIT=1；
    即使满足，也会先把旧配置备份成 config.json.bak-<ts>（600）再写新文件。
    """
    target = CONFIG
    if target.exists():
        allow_env = os.environ.get("FOXLABEL_ALLOW_INIT", "").strip() == "1"
        if not (args.force and allow_env):
            die(2, (
                f"拒绝覆盖已有配置：{target}\n"
                f"  文件已存在（面板账号/密钥都在里面），init 不会覆盖它。\n"
                f"  如确需重建：先手工备份（例如 cp {target} {target}.bak-$(date +%s)），\n"
                f"  再显式设置 FOXLABEL_ALLOW_INIT=1 并同时加 --force 重跑；\n"
                f"  即使两个开关都满足，也会先把旧配置备份成 "
                f"{target.name}.bak-<ts>（600）再写新配置。"))
        backup = _init_backup_path(target)
        try:
            shutil.copy2(target, backup)
            os.chmod(backup, 0o600)
        except OSError as e:
            die(2, f"备份旧配置失败，已中止（{target} 未改动）：{e}")
        print(f"已备份旧配置 → {backup}（600）")
    password = args.password
    if not password:
        password = getpass.getpass("面板登录密码：")
        if password != getpass.getpass("再输一次："):
            die(2, "两次输入不一致")
    if len(password) < 8:
        die(2, "密码至少 8 位（面板等于管理员权限，别用弱密码）")
    salt = secrets.token_bytes(16)
    iterations = 200_000
    cfg = {
        "username": args.user,
        "salt": salt.hex(),
        "pwd_hash": hash_password(password, salt, iterations),
        "iterations": iterations,
        "secret": secrets.token_hex(32),
        "upstream": args.upstream.rstrip("/"),
        "admin_key_file": args.admin_key_file,
        "audit_file": args.audit_file,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    save_cfg(cfg)
    print(f"已写入 {CONFIG}（600）")
    print(f"  账号   : {cfg['username']}")
    print(f"  上游   : {cfg['upstream']}")
    # 启动日志只打文件名不打绝对路径：日志会被 journal/转发，家目录结构不必外泄
    print(f"  密钥文件: {Path(cfg['admin_key_file']).name}")
    print("启动： python3 panel.py serve")
    return 0


def cmd_passwd(args) -> int:
    cfg = load_cfg()
    password = args.password or getpass.getpass("新密码：")
    if len(password) < 8:
        die(2, "密码至少 8 位")
    salt = secrets.token_bytes(16)
    cfg.update({"salt": salt.hex(),
                "pwd_hash": hash_password(password, salt, cfg["iterations"]),
                "secret": secrets.token_hex(32)})   # 同时让所有旧会话失效
    save_cfg(cfg)
    print("密码已更新，旧会话全部失效")
    return 0


# ------------------------------------------------------------------ 上游

class UpstreamError(Exception):
    """上游故障。code 保留 HTTP 状态码（None 表示连不上/超时），
    让上层能把 404/403/409 映射成不同的面板错误，而不是一律 503。"""

    def __init__(self, msg: str, code: int | None = None, body: bytes = b""):
        super().__init__(msg)
        self.code = code
        self.body = body


class Upstream:
    """fox-label 的只读客户端。admin key 只在这里使用。"""

    def __init__(self, base: str, key_file: str | None):
        self.base = base.rstrip("/")
        self.key_file = key_file
        self._key = ""
        self._key_at = 0.0
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, bytes]] = {}
        # 申报制（SPEC-declare §3.5）：/admin/v1/overdue 聚合的短缓存（30 秒）
        self._overdue_cache: tuple[float, dict] | None = None

    def key(self) -> str:
        with self._lock:
            now = time.time()
            if self._key and now - self._key_at < 5:      # 允许轮换密钥
                return self._key
            if not self.key_file:
                raise UpstreamError("未配置 admin_key_file")
            try:
                self._key = Path(self.key_file).expanduser().read_text(
                    encoding="utf-8").strip()
            except OSError as e:
                # 只报文件名不报完整路径：错误体是给浏览器看的，
                # 不要把管理员的家目录结构泄出去
                raise UpstreamError(
                    f"上游鉴权失败：读不到管理员密钥 {Path(self.key_file).name}"
                    f"（{e.strerror or type(e).__name__}）") from e
            self._key_at = now
            return self._key

    def get(self, path: str, cache: bool = True) -> dict:
        now = time.time()
        if cache:
            hit = self._cache.get(path)
            if hit and now - hit[0] < CACHE_TTL:
                return json.loads(hit[1])
        req = urllib.request.Request(self.base + path,
                                     headers={"Authorization": f"Bearer {self.key()}"})
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            # 保留状态码与响应体：上层要靠它区分"包不存在"和"上游炸了"
            try:
                detail = e.read()[:4096]
            except Exception:                       # noqa: BLE001 - 读不到就算了
                detail = b""
            raise UpstreamError(f"上游返回 {e.code}", code=e.code,
                                body=detail) from e
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            raise UpstreamError(f"上游不可达：{e}") from e
        if cache:
            self._cache[path] = (now, raw)
        try:
            return json.loads(raw)
        except ValueError as e:
            # 上游返回了 HTML 错误页之类：当上游故障处理，别让面板 500
            raise UpstreamError(f"上游返回的不是合法 JSON（{len(raw)} 字节）") from e

    def summary(self) -> dict:
        return self.get("/packages?limit=1")

    def repos(self) -> dict:
        return self.get("/repos")

    def packages(self, repo: str, status: str, q: str, page: int, size: int) -> dict:
        params = {"limit": size, "offset": (page - 1) * size}
        if repo:
            params["repo"] = repo
        if status:
            params["status"] = status
        return self.get("/packages?" + urllib.parse.urlencode(params), cache=False)

    def package(self, repo: str, pkg_id: str) -> dict:
        """上游单包接口 GET /packages/<id>?repo=：40 万包的仓库靠翻页会翻不到，
        有这条路由就必须先用它（旧版上游没有该路由，由调用方退回分页扫描）。"""
        params = {"repo": repo} if repo else {}
        path = "/packages/" + urllib.parse.quote(pkg_id, safe="")
        if params:
            path += "?" + urllib.parse.urlencode(params)
        return self.get(path, cache=False)

    def work(self) -> dict:
        return self.get("/api/v1/work", cache=False)

    def overdue(self) -> dict:
        # 逾期清单不做缓存：t2 聚合端点自带「每日首个请求落一条提醒」的副作用，
        # 打到缓存上会拖慢首条提醒的落库时机（SPEC-declare §3.4）；
        # 但也不要每次刷新面板都穿透——30 秒本地缓存足够面板提示语义。
        hit = self._overdue_cache
        now = time.time()
        if hit and now - hit[0] < 30:
            return hit[1]
        data = self.get("/admin/v1/overdue", cache=False)
        self._overdue_cache = (now, data)
        return data

    # ---- v3 写通道 -------------------------------------------------
    def invalidate(self) -> None:
        """写成功后清上游缓存：下一次读取必须立刻反映新状态（§6.3）。"""
        with self._lock:
            self._cache.clear()

    def post(self, path: str, body: dict, headers: dict | None = None,
             timeout: float = WRITE_UPSTREAM_TIMEOUT) -> tuple[int, bytes, dict]:
        """写请求。4xx 也当"有响应"返回：服务端的 428/400-diff 必须原样给浏览器。"""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs = {"Authorization": f"Bearer {self.key()}",
                "Content-Type": "application/json; charset=utf-8"}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            try:
                payload = e.read()
            except Exception:                   # noqa: BLE001 - 读不到就算了
                payload = b""
            return e.code, payload, {k.lower(): v for k, v in (e.headers or {}).items()}
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            raise UpstreamError(f"上游不可达：{e}") from e

    def raw(self, path: str, timeout: float = UPSTREAM_TIMEOUT) -> tuple[int, bytes, dict]:
        """原样取上游响应体（写日志导出要逐字节一致，不能过 json.loads）。"""
        req = urllib.request.Request(self.base + path,
                                     headers={"Authorization": f"Bearer {self.key()}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            try:
                payload = e.read()
            except Exception:                   # noqa: BLE001
                payload = b""
            return e.code, payload, {k.lower(): v for k, v in (e.headers or {}).items()}
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            raise UpstreamError(f"上游不可达：{e}") from e


def write_enabled(cfg: dict) -> bool:
    """面板写开关：环境变量 FOXLABEL_PANEL_WRITE=0 强制只读，其次看配置（默认开）。"""
    raw = os.environ.get("FOXLABEL_PANEL_WRITE")
    if raw is not None and str(raw).strip().lower() in ("0", "false", "no", "off", ""):
        return False
    value = cfg.get("write_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return bool(value)


class WriteLimit:
    """写通道令牌桶（线程安全）：容量 10、补 20/分钟 → 连点第 11 次必 429。"""

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


WRITE_LIMIT = WriteLimit(10, 20)        # 执行：10 桶 / 20 每分钟
PREVIEW_LIMIT = WriteLimit(20, 60)      # 预览：60 每分钟
WRITE_READ_LIMIT = WriteLimit(60, 120)  # 写日志/通知/导出：120 每分钟

# 面板幂等缓存：key=(actor, action, idempotency_key) → (ts, status, body)
_IDEM_LOCK = threading.Lock()
_IDEM_CACHE: dict[tuple[str, str, str], tuple[float, int, bytes]] = {}


def idem_lookup(actor: str, action: str, key: str) -> tuple[int, bytes] | None:
    with _IDEM_LOCK:
        hit = _IDEM_CACHE.get((actor, action, key))
        if hit is None:
            return None
        if time.time() - hit[0] > IDEM_TTL:
            _IDEM_CACHE.pop((actor, action, key), None)
            return None
        return hit[1], hit[2]


def idem_store(actor: str, action: str, key: str, status: int, body: bytes) -> None:
    with _IDEM_LOCK:
        now = time.time()
        for k in [k for k, v in _IDEM_CACHE.items() if now - v[0] > IDEM_TTL]:
            _IDEM_CACHE.pop(k, None)
        if len(_IDEM_CACHE) > 512:
            _IDEM_CACHE.clear()
        _IDEM_CACHE[(actor, action, key)] = (now, status, body)


# ------------------------------------------------------------------ 拒绝留痕（F4）

DENIED_LOG_ENV = "FOXLABEL_PANEL_DENIED_LOG"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


DENIED_LOG_MAX_BYTES = _env_int("FOXLABEL_PANEL_DENIED_LOG_MAX_BYTES",
                                10 * 1024 * 1024)
DENIED_LOG_KEEP = min(9, _env_int("FOXLABEL_PANEL_DENIED_LOG_KEEP", 5))
DENY_FLOOD_MAX = _env_int("FOXLABEL_PANEL_DENY_FLOOD", 20)
DENY_FLOOD_WINDOW = 60.0
_DENIED_LOCK = threading.Lock()
_DENIED_MAX_ROWS = 100_000
_FLOOD_LOCK = threading.Lock()
_FLOOD: dict[str, list] = {}


def denied_log_path(cfg: dict | None = None) -> Path:
    """面板拒绝留痕文件：默认与面板配置同目录（生产 ~/.config/fox-panel/）。"""
    raw = os.environ.get(DENIED_LOG_ENV)
    if raw:
        return Path(raw).expanduser()
    if cfg and cfg.get("denied_log_file"):
        return Path(str(cfg["denied_log_file"])).expanduser()
    return CONFIG.parent / "writes-denied.jsonl"


def _denied_archive_dir(path: Path) -> Path:
    return path.parent / "writes-denied-archive"


def _chmod_quiet(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _denied_archive(src: Path, path: Path) -> bool:
    """把一份轮出的拒绝日志 gzip 归档（0600），失败返回 False（不丢历史）。"""
    try:
        arch = _denied_archive_dir(path)
        arch.mkdir(parents=True, exist_ok=True)
        _chmod_quiet(arch, 0o700)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(src.stat().st_mtime))
        dest = arch / f"writes-denied-{stamp}.jsonl.gz"
        n = 1
        while dest.exists():
            dest = arch / f"writes-denied-{stamp}-{n}.jsonl.gz"
            n += 1
        tmp = dest.with_name(dest.name + ".tmp")
        with src.open("rb") as fin, gzip.open(tmp, "wb") as fout:
            while True:
                chunk = fin.read(1 << 20)
                if not chunk:
                    break
                fout.write(chunk)
        _chmod_quiet(tmp, 0o600)
        os.replace(tmp, dest)
        _chmod_quiet(dest, 0o600)
        return True
    except OSError as e:
        sys.stderr.write(f"[warn] 归档拒绝留痕失败：{e}\n")
        return False


def _rotate_denied(path: Path) -> bool:
    """与 writes.jsonl 同策略：KEEP 份滚动 + 最老一份 gzip 归档，只增不删。"""
    if DENIED_LOG_KEEP > 1:
        last = path.with_name(path.name + f".{DENIED_LOG_KEEP - 1}")
        if last.exists() and not _denied_archive(last, path):
            return False
        for i in range(DENIED_LOG_KEEP - 2, 0, -1):
            src = path.with_name(path.name + f".{i}")
            if src.exists():
                os.replace(src, path.with_name(path.name + f".{i + 1}"))
        if path.exists():
            os.replace(path, path.with_name(path.name + ".1"))
    else:
        if path.exists() and not _denied_archive(path, path):
            return False
        tmp = path.with_name(path.name + ".new." + secrets.token_hex(4))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        os.replace(tmp, path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    return True


def deny_append(rec: dict, cfg: dict | None = None) -> bool:
    """append-only 追加一行；失败只告警（留痕失败绝不能影响请求本身）。"""
    path = denied_log_path(cfg)
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    with _DENIED_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            size = path.stat().st_size if path.exists() else 0
            if size and size + len(line) > DENIED_LOG_MAX_BYTES:
                _rotate_denied(path)      # 归档失败不阻塞写入（与 writes.jsonl 一致）
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            _chmod_quiet(path, 0o600)
            return True
        except OSError as e:
            sys.stderr.write(f"[warn] 写拒绝留痕失败：{e}\n")
            return False


def _denied_files(path: Path) -> list[Path]:
    files: list[Path] = []
    if path.exists():
        files.append(path)
    for i in range(1, DENIED_LOG_KEEP):
        p = path.with_name(path.name + f".{i}")
        if p.exists():
            files.append(p)
    try:
        files.extend(sorted(_denied_archive_dir(path).glob("writes-denied-*.jsonl.gz"),
                            reverse=True))
    except OSError:
        pass
    return files


def deny_rows(cfg: dict | None = None) -> list[dict]:
    path = denied_log_path(cfg)
    rows: list[dict] = []
    for f in _denied_files(path):
        opener = gzip.open if f.suffix == ".gz" else open
        try:
            with _DENIED_LOCK, opener(f, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        rows.append(rec)
                    if len(rows) >= _DENIED_MAX_ROWS:
                        return rows
        except OSError:
            continue
    return rows


def deny_query(cfg: dict | None, *, limit: int = 25, offset: int = 0,
               actor: str = "", reason: str = "", path: str = "",
               order: str = "desc") -> dict:
    rows = deny_rows(cfg)
    if actor:
        rows = [r for r in rows if r.get("actor") == actor]
    if reason:
        rows = [r for r in rows
                if r.get("reason") == reason or str(r.get("code")) == reason]
    if path:
        rows = [r for r in rows if r.get("path") == path]
    rows.sort(key=lambda r: (r.get("ts_epoch") or 0, str(r.get("write_id") or "")),
              reverse=(order != "asc"))
    total = len(rows)
    page = rows[offset:offset + limit] if limit > 0 else []
    return {"ok": True, "generated_at": now_iso(), "total": total, "limit": limit,
            "offset": offset, "items": page, "source": "panel-denied",
            "filters": {"actor": actor, "reason": reason, "path": path, "order": order},
            **({"note": "还没有被拒绝的写请求"} if not rows and total == 0 else {})}


HISTORY_SCAN_CAP = 2000


def _fmt_held_js(seconds: float | None) -> str:
    """降级页（无 /static/app.js）的已持时长格式化，与 web fmtHeld 同口径。"""
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分钟"
    if s < 86400:
        return f"{s // 3600} 小时"
    return f"{s // 86400} 天 {s % 86400 // 3600} 小时"


def _epoch_or(v, dflt: float = 0.0) -> float:
    """notifications.first_reminded_at 是 REAL（epoch 秒），给 float/None 兜底。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dflt
    return f if f == f else dflt          # 过滤 NaN


def _iso_or(v) -> str | None:
    """REAL epoch 秒 → 面板通知统一的时间字符串；无效值给 None。"""
    f = _epoch_or(v, -1.0)
    if f < 0:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f))


def _ts_param(q: dict, name: str) -> float | None:
    raw = (q.get(name) or [""])[0].strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _filter_denied(rows: list[dict], q: dict) -> list[dict]:
    """合并视图里，拒绝行也要遵守同一套筛选（actor/action/q/from/to）。"""
    actor = (q.get("actor") or [""])[0].strip()
    action = (q.get("action") or [""])[0].strip()
    keyword = (q.get("q") or [""])[0].strip().lower()
    from_ts, to_ts = _ts_param(q, "from"), _ts_param(q, "to")
    out: list[dict] = []
    for r in rows:
        if actor and r.get("actor") != actor:
            continue
        if action and action != "write.denied":
            continue                     # 明确按某动作筛 → 拒绝行不参与
        if keyword:
            blob = " ".join(str(r.get(k) or "")
                            for k in ("reason", "error", "path", "actor", "action"))
            if keyword not in blob.lower():
                continue
        ts = r.get("ts_epoch") or 0
        if from_ts is not None and ts < from_ts:
            continue
        if to_ts is not None and ts > to_ts:
            continue
        out.append(r)
    return out


# ---- 拒绝洪水保护（N4）：连续被拒 > N 次/分钟 → 直接 429，并只补一条 429 留痕
def deny_flood_check(key: str) -> tuple[bool, bool]:
    """返回 (是否已被限流, 本窗口是否还需要补一条 429 留痕)。"""
    now = time.time()
    with _FLOOD_LOCK:
        st = _FLOOD.get(key)
        if not st or now - st[0] > DENY_FLOOD_WINDOW:
            _FLOOD[key] = [now, 0, False]
            return False, False
        if st[1] >= DENY_FLOOD_MAX:
            log_once = not st[2]
            st[2] = True
            return True, log_once
        return False, False


def deny_flood_bump(key: str) -> None:
    now = time.time()
    with _FLOOD_LOCK:
        st = _FLOOD.get(key)
        if not st or now - st[0] > DENY_FLOOD_WINDOW:
            _FLOOD[key] = [now, 1, False]
        else:
            st[1] += 1
        if len(_FLOOD) > 2048:
            for k in [k for k, v in _FLOOD.items()
                      if now - v[0] > DENY_FLOOD_WINDOW]:
                _FLOOD.pop(k, None)


# ------------------------------------------------------------------ 会话

class Sessions:
    """无状态会话：cookie = base64(exp).hmac。改不了也伪造不了。"""

    def __init__(self, secret_hex: str):
        self.key = bytes.fromhex(secret_hex)

    def issue(self, user: str) -> str:
        exp = int(time.time() + SESSION_HOURS * 3600)
        payload = f"{user}|{exp}"
        sig = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode().rstrip("=")

    def check(self, token: str) -> str | None:
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
            user, exp_s, sig = raw.rsplit("|", 2)
            payload = f"{user}|{exp_s}"
            want = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, want):
                return None
            if int(exp_s) < time.time():
                return None
            return user
        except (ValueError, TypeError):
            return None


class RateLimit:
    """登录失败限速：单 IP 5 次锁 5 分钟，全局 30 次锁 1 分钟。"""

    def __init__(self):
        self.fails: dict[str, list] = {}
        self.lock = threading.Lock()
        self.global_fails = 0
        self.global_until = 0.0

    def blocked(self, ip: str) -> float:
        with self.lock:
            now = time.time()
            if now < self.global_until:
                return self.global_until - now
            n, until = self.fails.get(ip, (0, 0.0))
            return max(0.0, until - now)

    def fail(self, ip: str) -> None:
        with self.lock:
            now = time.time()
            n, _ = self.fails.get(ip, (0, 0.0))
            n += 1
            wait = min(300, 30 * (2 ** max(0, n - 1))) if n >= 5 else 0
            self.fails[ip] = (n, now + wait if wait else 0.0)
            self.global_fails += 1
            if self.global_fails >= 30:
                self.global_until = now + 60
                self.global_fails = 0

    def ok(self, ip: str) -> None:
        with self.lock:
            self.fails.pop(ip, None)


# ------------------------------------------------------------------ 聚合
# 面板只做"把上游几个接口拼成页面要的形状"，不复制业务判断逻辑。

def build_summary(up: Upstream, cfg: dict) -> dict:
    pk = up.summary()
    work = up.work()
    repos = up.repos()
    leases = work.get("leases") or []
    pre = [l for l in leases if l.get("kind") == "prelock"]
    act = [l for l in leases if l.get("kind") == "active"]
    return {
        "ok": True,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "totals": {
            "packages": pk.get("summary", {}).get("total", 0),
            "available": pk.get("summary", {}).get("available", 0),
            "claimed": pk.get("summary", {}).get("claimed", 0),
            "completed": pk.get("summary", {}).get("completed", 0),
            "bytes_total": pk.get("summary", {}).get("bytes_total", 0),
            "repos": pk.get("summary", {}).get("repos", 0),
            "devices_online": len([d for d in work.get("devices") or []
                                   if d.get("status") != "archived"]),
            "active_leases": len(act),
            "prelock_leases": len(pre),
            "free_for_assign": work.get("free_for_assign", 0),
        },
        "repos": [{"repo": r.get("repo"), "type": r.get("repo_type"),
                   "enabled": bool(r.get("enabled")),
                   "summary": r.get("summary") or {}} for r in repos.get("repos") or []],
        "devices": [{"device_id": d.get("device_id"), "user_id": d.get("user_id"),
                     "tier": d.get("tier"), "speed": round(d.get("speed") or 0, 4),
                     "status": d.get("status"), "last_seen": d.get("last_seen"),
                     "client_ver": d.get("client_ver"),
                     # SPEC-link §3：新增 6 字段只增不改，上游旧版本没有时为 null
                     "gpu_name": d.get("gpu_name"), "gpu_vram_mb": d.get("gpu_vram_mb"),
                     "mode": d.get("mode"), "hw_report_at": d.get("hw_report_at"),
                     "first_seen": d.get("first_seen"),
                     "local_pkgs_count": d.get("local_pkgs_count"),
                     # F1（t8 终审）：行展开需要明细数组，只留 count 会恒显示空
                     "local_pkgs": d.get("local_pkgs") or []}
                    for d in work.get("devices") or []],
        "leases": [{"lease_id": l.get("lease_id"), "kind": l.get("kind"),
                    "repo": l.get("repo"), "package": l.get("package"),
                    "device_id": l.get("device_id"), "user_id": l.get("user_id"),
                    # 申报制（SPEC-declare §8）：expires_in 恒 null（字段保留）；
                    # 新增 held_seconds/overdue 只增透传
                    "expires_in": l.get("expires_in"),
                    "held_seconds": l.get("held_seconds"),
                    "overdue": l.get("overdue"),
                    "overdue_seconds": l.get("overdue_seconds"),
                    # 申报制（SPEC-declare §3）：已持时长插值需要 claim_at（epoch 秒）；
                    # fuse_exceeded 是 30 天保险丝越界标记（红色特殊提示，非回收）
                    "claim_at": l.get("claim_at"),
                    "fuse_exceeded": l.get("fuse_exceeded"),
                    "progress": l.get("progress") or {}} for l in leases],
    }


def upstream_msg(err: UpstreamError) -> str:
    """契约要求所有上游故障文案以"上游"开头（前端据此显示错误态）。"""
    msg = str(err)
    return msg if msg.startswith("上游") else "上游不可用：" + msg


def now_iso() -> str:
    """带时区偏移的 ISO 8601（面板本地时区）：前端要显示"数据生成时刻"，
    无偏移的裸时间会被浏览器当 UTC 解释，差 8 小时。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class _ScanFlight:
    """一次全量扫描的"班机"：并发请求共享同一个结果，避免 8 个线程各扫一遍
    （1C/1GB 机器上 20 万行 × 8 会直接把内存打满）。"""

    __slots__ = ("done", "rows", "meta", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.rows: list[dict] | None = None
        self.meta: dict = {}
        self.error: str | None = None


_SCAN_LOCK = threading.Lock()
_SCAN_SLOTS = threading.BoundedSemaphore(SCAN_MAX_PARALLEL)
_SCAN_CACHE: dict[tuple[str, str, str], tuple[float, list[dict], dict]] = {}
_SCAN_STATE: dict = {"bad_at": 0.0, "inflight": {}}


def _scan_fetch(up: Upstream, repo: str = "", status: str = "") -> tuple[list[dict], dict]:
    """真正去上游分页拉全量（limit=1000，上限 MAX_SCAN_ROWS 行）。
    顺便留下首块响应里的 summary/repos/page.matched：v1 的 /api/packages 是整包透传，
    v2 不能把这些字段弄丢（§6.0 只做加法）。"""
    out: list[dict] = []
    meta: dict = {"summary": {}, "repos": [], "matched": None}
    offset, size, first = 0, 1000, True
    while offset < MAX_SCAN_ROWS:
        params = {"limit": size, "offset": offset}
        if repo:
            params["repo"] = repo
        if status:
            params["status"] = status
        chunk = up.get("/packages?" + urllib.parse.urlencode(params), cache=False)
        rows = chunk.get("packages") or []
        if first:
            meta = {"summary": chunk.get("summary") or {},
                    "repos": chunk.get("repos") or [],
                    "matched": (chunk.get("page") or {}).get("matched")}
            first = False
        out.extend(rows)
        if len(rows) < size:
            break
        offset += size
    return out, meta


def _scan_packages(up: Upstream, repo: str = "",
                   status: str = "") -> tuple[list[dict], dict]:
    """带 2 秒 TTL 缓存 + single-flight 的全量扫描。
    - 缓存 key 含 (上游地址, repo, status)：换上游/换筛选立刻失效；
    - 一旦某次上游调用失败就清空缓存并戳 bad_at，之后不再用"旧数据"冒充成功，
      保证"上游刚挂 → 下一次请求立刻 503"而不是先吐 2 秒旧页面；
    - 并发请求共享同一次扫描结果（single-flight），并用信号量限制同时进行的扫描数。"""
    key = (up.base, repo, status)
    now = time.time()
    with _SCAN_LOCK:
        hit = _SCAN_CACHE.get(key)
        if hit and now - hit[0] < SCAN_TTL and hit[0] >= _SCAN_STATE["bad_at"]:
            return list(hit[1]), dict(hit[2])
        flight = _SCAN_STATE["inflight"].get(key)
        leader = flight is None
        if leader:
            flight = _ScanFlight()
            _SCAN_STATE["inflight"][key] = flight

    if not leader:
        if flight.done.wait(UPSTREAM_TIMEOUT * 3):
            if flight.error:
                raise UpstreamError(flight.error)
            return list(flight.rows or []), dict(flight.meta)
        # 等太久（罕见）：不冒险返回空数据，自己直连扫一次，宁慢不 500
        return _scan_fetch(up, repo, status)

    got_slot = _SCAN_SLOTS.acquire(timeout=UPSTREAM_TIMEOUT * 2)
    if not got_slot:
        # 宁可让客户端稍后重试，也不要两个全量扫描同时把 1GB 内存吃掉
        flight.error = "上游繁忙：面板正在并发扫描全量包清单，请稍后重试"
        flight.done.set()
        with _SCAN_LOCK:
            if _SCAN_STATE["inflight"].get(key) is flight:
                _SCAN_STATE["inflight"].pop(key, None)
        raise UpstreamError(flight.error)
    try:
        rows, meta = _scan_fetch(up, repo, status)
    except Exception as e:                       # noqa: BLE001 - 记录给等待者后原样抛出
        _scan_invalidate()
        flight.error = str(e) if isinstance(e, UpstreamError) else "上游查询失败"
        raise
    else:
        with _SCAN_LOCK:
            _SCAN_CACHE[key] = (now, rows, meta)
            _trim_scan_cache()
        flight.rows, flight.meta = rows, meta
        return list(rows), dict(meta)
    finally:
        if got_slot:
            _SCAN_SLOTS.release()
        flight.done.set()
        with _SCAN_LOCK:
            if _SCAN_STATE["inflight"].get(key) is flight:
                _SCAN_STATE["inflight"].pop(key, None)


def _trim_scan_cache() -> None:
    """按"总行数预算"淘汰，而不是只按条目数：一条 20 万行的缓存本身就有几百 MiB，
    留 8 条等于自杀。超预算的大扫描插进来会被立刻淘汰（只在本次请求里用）。"""
    while _SCAN_CACHE:
        total_rows = sum(len(v[1]) for v in _SCAN_CACHE.values())
        if total_rows <= SCAN_CACHE_MAX_ROWS and len(_SCAN_CACHE) <= SCAN_CACHE_MAX:
            return
        oldest = min(_SCAN_CACHE, key=lambda k: _SCAN_CACHE[k][0])
        _SCAN_CACHE.pop(oldest, None)


def _scan_invalidate() -> None:
    """上游一旦报错就作废扫描缓存：宁可让下一次请求重新去撞 503，
    也不能在"上游已经挂了"的时候继续拿旧数据冒充成功。"""
    with _SCAN_LOCK:
        _SCAN_CACHE.clear()
        _SCAN_STATE["bad_at"] = time.time()


def _all_packages(up: Upstream, repo: str = "", status: str = "") -> list[dict]:
    """只要行、不要元信息时的入口（异常体检 / 单包兜底扫描）。"""
    return _scan_packages(up, repo, status)[0]


def _filter_packages(rows: list[dict], repo: str, status: str, q: str) -> list[dict]:
    """q 在面板侧过滤，且必须在分页之前做：上游没有 q 参数，
    若放到分页之后过滤，page.total 就会与实际剩余行数打架、导出条数也对不上。"""
    needle = q.strip().lower()
    out: list[dict] = []
    for p in rows:
        if repo and p.get("repo") != repo:
            continue
        if status and p.get("status") != status:
            continue
        if needle:
            ident = str(p.get("id") or "").lower()
            owner = str(p.get("owner") or "").lower()
            if needle not in ident and needle not in owner:
                continue
        out.append(p)
    return out


def _sort_packages(rows: list[dict], sort_by: str, order: str) -> list[dict]:
    """稳定排序：同 key（例如 updated_at 全相同）时保持上游原始顺序，
    否则既有断言 page=2&size=2 → shard-00002/shard-00003 会挂。
    Python 的 sort 即使 reverse=True 也保持稳定性，所以只给主键、不掺下标，
    这样 asc/desc 两个方向都不会把等键行倒过来。"""
    def key_of(p: dict):
        if sort_by == "size":
            try:
                return int(p.get("size") or 0)
            except (TypeError, ValueError):
                return 0
        if sort_by == "id":
            return str(p.get("id") or "")
        return str(p.get("updated_at") or "")   # updated：ISO 8601 字符串可直接比大小

    return sorted(rows, key=key_of, reverse=(order == "desc"))


class BadRequest(ValueError):
    """参数不合法（带具体文案）。继承 ValueError，让 `_api()` 既有的
    `except ValueError → 400` 依然兜底，同时能把 SPEC 原句透给前端。"""


def _sort_params(q: dict) -> tuple[str, str]:
    """sort/order 解析：缺省用 SORT_DEFAULT_ORDER[by]，显式传非法值则 400。
    区别对待是有意的——不传是正常用法，拼错枚举是客户端 bug，
    静默回落会让"点了排序没反应"变成难以排查的哑失败。"""
    raw_sort = ((q.get("sort") or [""])[0] or "").strip().lower()
    if not raw_sort:
        by = "updated"
    elif raw_sort in SORT_DEFAULT_ORDER:
        by = raw_sort
    else:
        raise BadRequest("sort 只能是 updated、size、id 之一")
    raw_order = ((q.get("order") or [""])[0] or "").strip().lower()
    if not raw_order:
        order = SORT_DEFAULT_ORDER[by]
    elif raw_order in ("asc", "desc"):
        order = raw_order
    else:
        raise BadRequest("order 只能是 asc 或 desc")
    return by, order


def _int_param(q: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = (q.get(name) or [""])[0].strip()
    if not raw:
        return default
    value = int(raw)                            # 非法交给上层 → 400
    return max(lo, min(hi, value))


def build_packages(up: Upstream, repo: str, status: str, q: str, page: int,
                   size: int, sort_by: str, order: str,
                   overdue_first: bool = False) -> dict:
    """列表页：拉全量 → 过滤 → 稳定排序 → 切片。只排当前页是不合格的，
    因为"大小降序"必须跨页正确（否则第 2 页的最大值可能比第 1 页还大）。
    summary/repos 与 page.matched/limit/offset 是 v1 就有的透传字段，
    v2 保留（§6.0 只做加法）。

    申报制（SPEC-declare §3.5）：overdue_first=True 时把逾期包
    （服务端 overdue 标记）排在当前排序之前——只是“逾期优先”的选择性
    置顶，不改变用户所选的排序键本身。"""
    raw, meta = _scan_packages(up, repo, status)
    rows = _sort_packages(_filter_packages(raw, repo, status, q), sort_by, order)
    # 申报制（SPEC-declare §3.5）：行级 overdue 标记 + 逾期优先置顶
    try:
        ovd_items = up.overdue().get("items") or []
    except UpstreamError:
        ovd_items = []
    overdue_keys = {(it.get("repo"), it.get("pkg"))
                    for it in ovd_items if it.get("still_open")}
    for r in rows:
        r["overdue"] = (r.get("status") == "claimed" and
                        (r.get("repo"), r.get("id")) in overdue_keys)
    if overdue_first and overdue_keys:
        rows = sorted(rows, key=lambda r: (
            0 if (r.get("repo"), r.get("id")) in overdue_keys else 1,))
    total = len(rows)
    start = (page - 1) * size
    matched = meta.get("matched")
    return {
        "ok": True,
        "repo_filter": repo,
        "generated_at": now_iso(),
        "sort": {"by": sort_by, "order": order},
        "overdue_first": bool(overdue_first),
        "page": {"page": page, "size": size,
                 # matched：上游按 repo/status 匹配到的行数（不含面板侧 q 过滤）
                 "matched": matched if isinstance(matched, int) else total,
                 # total：面板过滤（repo/status/q）之后的真实行数，分页与导出都以它为准
                 "total": total,
                 "pages": max(1, (total + size - 1) // size),
                 "limit": size,
                 "offset": start,
                 "truncated": len(raw) >= MAX_SCAN_ROWS},
        "summary": meta.get("summary") or {},
        "repos": meta.get("repos") or [],
        "packages": rows[start:start + size],
    }


# ------------------------------------------------------------------ 单包详情

class PackageNotFound(Exception):
    pass


class PackageAmbiguous(Exception):
    pass


def _shape_package(row: dict) -> dict:
    """把上游行收敛成契约字段：上游字段若有缺失/多出的都不外泄，
    前端抽屉只依赖这 12 个键，不用写一堆 ?? 兜底。"""
    status = row.get("status") or ""
    return {
        "repo": row.get("repo"),
        "id": row.get("id"),
        "size": row.get("size") or 0,
        "sha256": row.get("sha256") or None,
        "status": status,
        # 状态文案固定走面板词表（SPEC §2.3）：上游 status_label 给的是「已认领」，
        # 与列表页/徽章的「处理中」不一致，透传会让同一状态一页里出现两种说法
        "status_label": STATUS_LABEL.get(status, status),
        "owner": row.get("owner"),
        "claimed_at": row.get("claimed_at"),
        "completed_at": row.get("completed_at"),
        "downloaded_at": row.get("downloaded_at"),
        "note": row.get("note"),
        "updated_at": row.get("updated_at"),
    }


def _lease_of(up: Upstream, repo: str, pkg_id: str) -> dict | None:
    """租约只是详情的附加信息：上游 work 接口挂了不该让整条详情变 503。"""
    try:
        work = up.work()
    except UpstreamError:
        return None
    for lease in work.get("leases") or []:
        if lease.get("repo") == repo and lease.get("package") == pkg_id:
            return {"lease_id": lease.get("lease_id"), "kind": lease.get("kind"),
                    "device_id": lease.get("device_id"),
                    "user_id": lease.get("user_id"),
                    "expires_in": lease.get("expires_in"),
                    # 申报制（SPEC-declare §3.2）：抽屉渲染「已持有时长」需要这些字段
                    "held_seconds": lease.get("held_seconds"),
                    "overdue": lease.get("overdue"),
                    "overdue_seconds": lease.get("overdue_seconds"),
                    "fuse_exceeded": lease.get("fuse_exceeded"),
                    "claim_at": lease.get("claim_at"),
                    "last_beat_at": lease.get("last_beat_at"),
                    "progress": lease.get("progress") or {}}
    return None


# 歧义文案用面板固定句子，不透传上游文本：上游的措辞/字段一变，
# 前端和测试都会跟着抖，而这里只需要"哪个 id、可选哪些 repo"。
_AMBIGUOUS = "包名在多个仓库存在，请指定 repo：{repos}"


def _scan_hits(up: Upstream, pkg_id: str, repo: str = "") -> list[dict]:
    """扫描找出同名包（可按 repo 收窄）。歧义判定与单包兜底都靠它。"""
    rows = _all_packages(up, repo)
    return [r for r in rows
            if r.get("id") == pkg_id and (not repo or r.get("repo") == repo)]


def _ambiguous_from(up: Upstream, pkg_id: str) -> PackageAmbiguous:
    """拼歧义错误：仓库列表自己扫出来，不用上游 500/409 的错误文本。"""
    try:
        names = sorted({str(r.get("repo")) for r in _scan_hits(up, pkg_id)})
    except UpstreamError:
        names = []
    return PackageAmbiguous(_AMBIGUOUS.format(repos=", ".join(names) or pkg_id))


def find_package(up: Upstream, repo: str, pkg_id: str) -> dict:
    """定位单个包：先打上游单包路由，拿不到再退回分页扫描。
    为什么两条路：旧版 fox-label 没有 /packages/<id>，而大仓库里目标包
    可能在 200 页之外，所以既不能只翻页、也不能只信任单包路由。
    另外 repo 为空时上游遇到同名包会 500（真实服务端把 Conflict 落到通用
    except），所以 5xx 也走扫描，把"歧义"还原成正确的 400。"""
    try:
        return _shape_package(up.package(repo, pkg_id))
    except UpstreamError as e:
        if e.code is None:
            raise                               # 连不上/超时 → 503，别再扫描一遍
        if e.code == 409:
            raise _ambiguous_from(up, pkg_id) from None
        if e.code == 403:
            # 仓库停用/无权：统一 404，避免把"这个仓库存在"泄露给未授权者
            raise PackageNotFound(f"包不存在：{repo}/{pkg_id}" if repo
                                  else f"包不存在：{pkg_id}") from None
        if e.code in (404, 405):
            pass                                # 路由不存在/包不在 → 扫描兜底
        elif not repo:
            pass                                # 无 repo 的 5xx：可能就是上游把歧义炸成 500
        else:
            raise                               # 有 repo 的 5xx/401 → 交给上层映射 503

    hits = _scan_hits(up, pkg_id, repo)
    if not hits:
        raise PackageNotFound(f"包不存在：{repo}/{pkg_id}" if repo
                              else f"包不存在：{pkg_id}")
    repos = sorted({str(r.get("repo")) for r in hits})
    if not repo and len(repos) > 1:
        raise PackageAmbiguous(_AMBIGUOUS.format(repos=", ".join(repos)))
    return _shape_package(hits[0])


# ------------------------------------------------------------------ CSV 导出

def _csv_cell(value) -> str:
    """RFC 4180 转义 + 公式注入防护：Excel 会把 = + - @ 开头的单元格当公式执行，
    所以先加前导单引号（此时该字段必然含特殊字符或被包裹），再按需加引号。"""
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


def build_csv(rows: list[dict]) -> bytes:
    """BOM + CRLF + 固定 10 列。BOM 是给 Excel 认 UTF-8 用的；CRLF 是 RFC 4180
    的要求。返回 bytes：编码问题要在这一层收口，不能指望 HTTP 层。"""
    lines = [",".join(CSV_COLUMNS)]
    for p in rows:
        cells = []
        for col in CSV_COLUMNS:
            value = p.get(col)
            if col == "size":
                # 未知大小（None）写空串而不是 0：0 会被 Excel 当"空文件"，
                # 与"大小未上报"是两件事（§6.3 空值规则）
                if value is None or value == "":
                    cells.append("")
                    continue
                try:
                    cells.append(str(int(value)))
                    continue
                except (TypeError, ValueError):
                    pass
            cells.append(_csv_cell(value))
        lines.append(",".join(cells))
    return ("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8")


# ------------------------------------------------------------------ meta

def server_write_meta(up: Upstream) -> tuple[dict | None, str | None]:
    """读服务端 /admin/v1/meta 的 write 配置。返回 (配置, 不可用原因)。

    服务端还是 v2（没有 /admin/v1/*）时必须优雅降级：write.enabled=false，
    UI 不渲染一套必然失败的入口（SPEC §2.4），但**不能**把 upstream_ok 弄坏。
    """
    try:
        data = up.get("/admin/v1/meta")
    except UpstreamError as e:
        if e.code == 404:
            return None, "服务端未提供写通道（版本不匹配）"
        return None, "上游不可用，写通道暂不可用"
    write = data.get("write")
    return (write if isinstance(write, dict) else {}), None


def _write_meta(up: Upstream, cfg: dict) -> dict:
    srv, reason = server_write_meta(up)
    panel_on = write_enabled(cfg)
    out = {
        "enabled": False,
        "upstream_ready": srv is not None,
        "readonly_reason": None,
        "confirm_ttl_sec": WRITE_CONFIRM_TTL,
        "batch_threshold": BATCH_THRESHOLD,
        "max_batch": MAX_BATCH,
        "actions": [{"id": aid, "path": spec["path"], "label": spec["label"],
                     "level": spec["level"], "target": spec["target"],
                     "multi": spec["multi"], "priority": spec["priority"]}
                    for aid, spec in WRITE_ACTIONS.items()],
    }
    if srv:
        for key in ("confirm_ttl_sec", "batch_threshold", "max_batch"):
            try:
                if srv.get(key):
                    out[key] = int(srv[key])
            except (TypeError, ValueError):
                pass
    if not panel_on:
        out["readonly_reason"] = "面板处于只读模式"
    elif srv is None:
        out["readonly_reason"] = reason
    elif srv.get("enabled") is False:
        out["readonly_reason"] = "服务端写通道已关闭"
    else:
        out["enabled"] = True
    return out


def build_meta(up: Upstream, cfg: dict) -> dict:
    """前端启动只需要一次请求就知道全部枚举/分页/刷新间隔，
    省掉"页面先硬编码 50 行、后端改了再对不上"的老问题。
    上游挂了也要 200：整页降级比整页 503 有用。"""
    repos: list[dict] = []
    upstream_ok, upstream_error = True, None
    try:
        raw = up.repos()
        repos = [{"repo": r.get("repo"), "type": r.get("repo_type"), "enabled": True}
                 for r in raw.get("repos") or []]
    except UpstreamError as e:
        upstream_ok, upstream_error = False, upstream_msg(e)
    try:
        health = up.get("/health")
        if health.get("ok") is False:
            upstream_ok = False
            upstream_error = upstream_error or "上游健康检查未通过"
    except UpstreamError:
        # 旧版上游没有 /health（返回 404 也是 UpstreamError）：以 /repos 的结果为准
        pass
    return {
        "ok": True,
        "version": VERSION,
        "upstream": up.base,                    # 只有 base，admin key 永不出现在这里
        "upstream_ok": upstream_ok,
        "upstream_error": upstream_error,
        "generated_at": now_iso(),
        "refresh_ms": REFRESH_MS,
        "default_page_size": DEFAULT_PAGE_SIZE,
        "page_sizes": list(PAGE_SIZES),
        "audit_page_size": AUDIT_PAGE_SIZE,
        "export_max_rows": EXPORT_MAX_ROWS,
        "statuses": [{"value": v, "label": STATUS_LABEL[v]} for v in STATUS_VALUES],
        "sorts": [{"value": k, "label": SORT_LABEL[k],
                   "default_order": SORT_DEFAULT_ORDER[k]} for k in SORT_DEFAULT_ORDER],
        "repos": repos,
        "features": {"drawer": True, "export": True, "sort": True, "theme": True},
        "audit": {"enabled": bool(cfg.get("audit_file"))},
        # v3 只新增这一个顶层对象；features/audit 逐字不变（旧测试用 == 断言）
        "write": _write_meta(up, cfg),
    }


def build_anomalies(up: Upstream) -> dict:
    """异常体检：完全由两个只读接口推导，不碰服务端数据库。"""
    all_pk = {"packages": _all_packages(up)}
    work = up.work()
    pkgs = {(p["repo"], p["id"]): p for p in all_pk.get("packages") or []}
    leases = work.get("leases") or []
    opened = {(l["repo"], l["package"]) for l in leases}
    devices = {d["device_id"] for d in work.get("devices") or []}

    stuck = [{"repo": p["repo"], "package": p["id"], "owner": p.get("owner")}
             for p in pkgs.values()
             if p.get("status") == "claimed" and (p["repo"], p["id"]) not in opened]
    orphan = [{"lease_id": l["lease_id"], "repo": l.get("repo"), "package": l.get("package")}
              for l in leases if (l.get("repo"), l.get("package")) not in pkgs]
    ghost = [{"lease_id": l["lease_id"], "device_id": l.get("device_id")}
             for l in leases if l.get("device_id") not in devices]
    return {"ok": True, "stuck": stuck, "orphan": orphan, "ghost": ghost}


def build_audit(cfg: dict, tail: int = 25) -> dict:
    path = cfg.get("audit_file")
    if not path:
        return {"ok": True, "enabled": False, "lines": []}
    p = Path(path).expanduser()
    if not p.exists():
        return {"ok": True, "enabled": True, "lines": [], "note": "审计文件不存在"}
    try:
        with p.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            data = fh.read().decode("utf-8", "replace").splitlines()
    except OSError as e:
        return {"ok": False, "error": f"读审计失败：{e}", "lines": []}
    out = []
    for line in data[-tail:]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        out.append({"ts": e.get("ts"), "kind": e.get("kind"),
                    "repo": e.get("repo"), "pkg": e.get("pkg"),
                    "actor": e.get("actor"), "detail": e.get("detail")})
    return {"ok": True, "enabled": True, "lines": out}


# ------------------------------------------------------------------ 写日志视图 / 通知中心

def build_repos_admin(up: Upstream) -> dict:
    """全部仓库（含停用）+ 计数：启用/停用入口的数据源。

    服务端有 /admin/v1/repos 就用它；否则退回 /repos（v2 上游），仍然 200 可用。
    """
    try:
        data = up.get("/admin/v1/repos", cache=False)
    except UpstreamError as e:
        if e.code != 404:
            raise
        legacy = up.repos()
        rows = []
        for r in legacy.get("repos") or []:
            summary = r.get("summary") or {}
            rows.append({"repo": r.get("repo"), "repo_type": r.get("repo_type"),
                         "enabled": bool(r.get("enabled", True)),
                         "packages": summary.get("total", 0),
                         "claimed": summary.get("claimed", 0),
                         "open_leases": 0})
        return {"ok": True, "repos": rows, "source": "legacy",
                "generated_at": now_iso()}
    rows = data.get("repos") if isinstance(data.get("repos"), list) else []
    return {"ok": True, "repos": rows, "source": "admin",
            "generated_at": data.get("generated_at") or now_iso()}


def _target_label(rec: dict) -> str:
    target = rec.get("target") or {}
    kind = target.get("kind") or ""
    if kind == "panel":
        return str(target.get("path") or rec.get("path") or "写请求")
    if kind == "package":
        return f"{target.get('repo')}/{target.get('id')}"
    if kind == "lease":
        return f"租约 {target.get('lease_id')}"
    if kind == "device":
        return f"设备 {target.get('device_id')}"
    if kind == "repo":
        return f"仓库 {target.get('repo')}"
    if kind == "key":
        return f"密钥 {target.get('name')}"
    if kind == "user":
        return f"用户 {target.get('user_id')}"
    return "目标"


def build_notifications(rows: list[dict], limit: int = 20) -> dict:
    """把写日志原始行按 batch_id 聚合成通知条目（与写日志同源，不另存状态）。"""
    groups: dict[str, dict] = {}
    order: list[str] = []
    for rec in rows:                                   # 服务端已按时间倒序
        bid = str(rec.get("batch_id") or rec.get("write_id") or "")
        group = groups.get(bid)
        if group is None:
            group = {"batch_id": bid, "rows": [], "ts_epoch": 0.0,
                     "write_id": rec.get("write_id"), "action": rec.get("action"),
                     "ts": rec.get("ts")}
            groups[bid] = group
            order.append(bid)
        group["rows"].append(rec)
        ts = rec.get("ts_epoch") or 0.0
        if ts >= group["ts_epoch"]:
            group["ts_epoch"] = ts
            group["write_id"] = rec.get("write_id")
            group["ts"] = rec.get("ts")

    items: list[dict] = []
    for bid in order:
        group = groups[bid]
        members = group["rows"]
        ok_count = sum(1 for r in members if r.get("result") == "ok")
        fail_count = len(members) - ok_count
        if any(r.get("result") == "denied" for r in members):
            result = "denied"
        else:
            result = "ok" if fail_count == 0 else ("error" if ok_count == 0 else "partial")
        action = group["action"] or ""
        if action == "write.denied":
            label = "写入被拒绝"
            level = "danger"
        elif action == "overdue.notice":
            # 申报制（SPEC-declare §3.5）：逾期未申报提醒（逐包一条）
            label = "逾期未申报"
            level = "danger"
        else:
            label = (WRITE_ACTIONS.get(action) or {}).get("label") or action or "写操作"
            level = (WRITE_ACTIONS.get(action) or {}).get("level") or "danger"
        title = (f"{label}：{_target_label(members[0])}" if len(members) == 1
                 else f"{label} {len(members)} 个：成功 {ok_count}，失败 {fail_count}")
        error = next((r.get("error") for r in members if r.get("error")), None)
        items.append({
            "id": "N-" + bid,
            "kind": "single" if len(members) == 1 else "batch",
            "ts": group["ts"],
            "ts_epoch": group["ts_epoch"],
            "action": action,
            "action_label": label,
            "level": level,
            "title": title,
            "result": result,
            "ok_count": ok_count,
            "failed_count": fail_count,
            "impact_count": sum(int(r.get("impact_count") or 0) for r in members),
            "write_id": group["write_id"],
            "batch_id": bid,
            "audit_link": (f"#/audit?tab=denied&write={group['write_id']}"
                           if action == "write.denied"
                           else f"#/audit?tab=writes&write={group['write_id']}"),
            "error": error,
        })
    items.sort(key=lambda x: (x["ts_epoch"], str(x.get("write_id") or "")), reverse=True)
    latest = None
    if items:
        latest = {"ts_epoch": items[0]["ts_epoch"], "write_id": items[0]["write_id"]}
    return {"ok": True, "generated_at": now_iso(), "upstream_ok": True,
            "items": items[:limit], "unread": len(items), "latest": latest}


def empty_notifications(note: str = "") -> dict:
    out = {"ok": True, "generated_at": now_iso(), "upstream_ok": False,
           "items": [], "unread": 0, "latest": None}
    if note:
        out["note"] = note
    return out


# ------------------------------------------------------------------ 页面

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>fox-label 面板</title>
<link rel="stylesheet" href="/static/app.css">
</head><body>
<header>
  <h1>fox-label<span>面板</span></h1>
  <div class="hdr-right">
    <span id="upstream" class="pill">上游检测中…</span>
    <label class="chk"><input type="checkbox" id="auto" checked> 自动刷新</label>
    <button id="refresh" class="btn">刷新</button>
    <form method="post" action="/logout" class="inline">
      <button class="btn ghost" type="submit">登出</button>
    </form>
  </div>
</header>

<nav id="tabs">
  <button data-tab="overview" class="on">概览</button>
  <button data-tab="packages">包列表</button>
  <button data-tab="devices">设备与租约</button>
  <button data-tab="anomalies">异常体检</button>
  <button data-tab="audit">审计</button>
</nav>

<main>
  <section id="tab-overview">
    <div class="cards" id="cards"></div>
    <h2>仓库</h2>
    <table id="repo-table"><thead><tr>
      <th>仓库</th><th>类型</th><th>启用</th><th>总数</th><th>未认领</th>
      <th>处理中</th><th>已完成</th><th>容量</th>
    </tr></thead><tbody></tbody></table>
  </section>

  <section id="tab-packages" hidden>
    <div class="filters">
      <select id="f-repo"><option value="">全部仓库</option></select>
      <select id="f-status">
        <option value="">全部状态</option>
        <option value="available">未认领</option>
        <option value="claimed">处理中</option>
        <option value="completed">已完成</option>
      </select>
      <input id="f-q" placeholder="搜索包名 / 认领人">
      <span id="page-info" class="muted"></span>
      <button id="prev" class="btn">上一页</button>
      <button id="next" class="btn">下一页</button>
    </div>
    <table id="pkg-table"><thead><tr>
      <th>仓库</th><th>包</th><th>大小</th><th>状态</th><th>认领人</th>
      <th>已下载</th><th>更新时间</th>
    </tr></thead><tbody></tbody></table>
  </section>

  <section id="tab-devices" hidden>
    <h2>设备</h2>
    <table id="dev-table"><thead><tr>
      <th>设备</th><th>用户</th><th>档位</th><th>速度(张/秒)</th>
      <th>状态</th><th>最近心跳</th><th>版本</th>
    </tr></thead><tbody></tbody></table>
    <h2>进行中的租约</h2>
    <table id="lease-table"><thead><tr>
      <th>类型</th><th>包</th><th>用户</th><th>设备</th><th>剩余</th><th>进度</th>
    </tr></thead><tbody></tbody></table>
  </section>

  <section id="tab-anomalies" hidden>
    <div class="cards" id="anom-cards"></div>
    <h2>死包（claimed 但无 active 租约）</h2>
    <table id="stuck-table"><thead><tr><th>仓库</th><th>包</th><th>认领人</th>
    </tr></thead><tbody></tbody></table>
    <h2>孤儿 / 幽灵租约</h2>
    <table id="orphan-table"><thead><tr><th>租约</th><th>包</th><th>设备</th>
    </tr></thead><tbody></tbody></table>
  </section>

  <section id="tab-audit" hidden>
    <table id="audit-table"><thead><tr>
      <th>时间</th><th>动作</th><th>仓库</th><th>包</th><th>操作者</th><th>详情</th>
    </tr></thead><tbody></tbody></table>
  </section>
</main>
<footer class="muted">fox-panel v__VER__ · 只读面板 · 数据来自 __UPSTREAM__</footer>
<script src="/static/app.js"></script>
</body></html>
"""

CSS = """*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",
 "Noto Sans CJK SC",sans-serif;background:#0f1115;color:#e6e9ef}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;
 padding:12px 20px;background:#161a22;border-bottom:1px solid #242a36;position:sticky;top:0;z-index:5}
h1{margin:0;font-size:17px;letter-spacing:.5px}
h1 span{color:#7c8798;font-weight:400;margin-left:6px}
h2{font-size:14px;color:#9aa6b8;margin:22px 0 8px;font-weight:600}
nav{display:flex;gap:4px;padding:10px 20px 0;background:#12161d;border-bottom:1px solid #242a36;
 position:sticky;top:57px;z-index:4}
nav button{background:none;border:0;color:#8b96a8;padding:9px 14px;cursor:pointer;
 border-bottom:2px solid transparent;font-size:13px}
nav button.on{color:#e6e9ef;border-bottom-color:#4c8dff}
main{padding:8px 20px 40px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.card{background:#161a22;border:1px solid #242a36;border-radius:8px;padding:12px 14px}
.card .k{color:#7c8798;font-size:12px}
.card .v{font-size:22px;font-weight:600;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px;background:#12161d;
 border:1px solid #242a36;border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #1d222b;white-space:nowrap}
th{color:#8b96a8;font-weight:600;background:#161a22;font-size:12px}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#171c25}
.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
select,input{background:#161a22;color:#e6e9ef;border:1px solid #2b3240;border-radius:6px;
 padding:6px 9px;font-size:13px}
input{min-width:200px}
.btn{background:#233start;background:#23324a;color:#dce5f5;border:1px solid #33415c;
 border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px}
.btn:hover{background:#2b3c5b}
.btn.ghost{background:transparent}
.pill{font-size:12px;padding:3px 9px;border-radius:99px;background:#1d232d;color:#8b96a8}
.pill.ok{background:#12301f;color:#5fd08a}
.pill.bad{background:#3a1618;color:#ff7b72}
.muted{color:#6b7686;font-size:12px}
.inline{display:inline}
.chk{font-size:12px;color:#8b96a8;display:flex;align-items:center;gap:5px}
footer{padding:14px 20px;border-top:1px solid #242a36}
.tag{border-radius:4px;padding:1px 7px;font-size:12px}
.s-available{background:#1d232d;color:#9aa6b8}
.s-claimed{background:#2a2110;color:#e3b341}
.s-completed{background:#12301f;color:#5fd08a}
.empty{color:#6b7686;padding:10px}
"""

JS = r"""const $ = (s, r) => (r || document).querySelector(s);
const state = { page: 1, size: 50, repo: '', status: '', q: '', tab: 'overview' };
let timer = null;

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}
function fmtBytes(n) {
  if (!n) return '-';
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v < 10 ? 1 : 0) + ' ' + u[i];
}
function fmtTime(t) {
  if (!t) return '-';
  if (typeof t === 'number') return new Date(t * 1000).toLocaleString('zh-CN');
  return String(t).replace('T', ' ').slice(0, 19);
}
/* 申报制：lease 不再有 expires_in，改渲染「已持有时长」（服务端 held_seconds） */
function fmtHeldLite(s) {
  if (s == null) return '-';
  s = Math.round(s);
  if (s < 60) return s + ' 秒';
  if (s < 3600) return Math.floor(s / 60) + ' 分钟';
  if (s < 86400) return Math.floor(s / 3600) + ' 小时';
  return Math.floor(s / 86400) + ' 天 ' + Math.floor(s % 86400 / 3600) + ' 小时';
}
function fillRows(tbody, rows, cols) {
  tbody.replaceChildren();
  if (!rows.length) {
    const tr = el('tr'), td = el('td', '（无数据）', 'empty');
    td.colSpan = cols; tr.appendChild(td); tbody.appendChild(tr);
    return;
  }
  for (const r of rows) {
    const tr = el('tr');
    for (const c of r) {
      const td = el('td');
      if (c instanceof Node) td.appendChild(c); else td.textContent = c == null ? '-' : String(c);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

async function api(path) {
  const r = await fetch(path, { credentials: 'same-origin' });
  if (r.status === 401) { location.href = '/login'; throw new Error('unauth'); }
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

function setUpstream(ok, text) {
  const p = $('#upstream');
  p.className = 'pill ' + (ok ? 'ok' : 'bad');
  p.textContent = text;
}

async function loadOverview() {
  let d;
  try { d = await api('/api/summary'); setUpstream(true, '上游正常'); }
  catch (e) { setUpstream(false, '上游不可用：' + e.message); return; }
  const t = d.totals;
  const cards = [
    ['总包数', t.packages], ['未认领', t.available], ['处理中', t.claimed],
    ['已完成', t.completed], ['active 租约', t.active_leases],
    ['预锁', t.prelock_leases], ['在线设备', t.devices_online],
    ['可分配', t.free_for_assign], ['总容量', fmtBytes(t.bytes_total)],
  ];
  const box = $('#cards'); box.replaceChildren();
  for (const [k, v] of cards) {
    const c = el('div', null, 'card');
    c.appendChild(el('div', k, 'k'));
    c.appendChild(el('div', v, 'v'));
    box.appendChild(c);
  }
  fillRows($('#repo-table tbody'), (d.repos || []).map(r => {
    const s = r.summary || {};
    return [r.repo, r.type, r.enabled ? '是' : '否',
            s.total ?? '-', s.available ?? '-', s.claimed ?? '-', s.completed ?? '-',
            fmtBytes(s.bytes_total)];
  }), 8);
  const sel = $('#f-repo');
  const cur = sel.value;
  sel.replaceChildren(el('option', '全部仓库'));
  sel.firstChild.value = '';
  for (const r of d.repos || []) {
    const o = el('option', r.repo); o.value = r.repo; sel.appendChild(o);
  }
  sel.value = cur || '';
}

async function loadPackages() {
  const q = new URLSearchParams({ page: state.page, size: state.size,
    repo: state.repo, status: state.status, q: state.q });
  const d = await api('/api/packages?' + q);
  const rows = (d.packages || []).filter(p => {
    if (!state.q) return true;
    const needle = state.q.toLowerCase();
    return (p.id || '').toLowerCase().includes(needle)
        || (p.owner || '').toLowerCase().includes(needle);
  }).map(p => {
    const st = el('span', p.status === 'available' ? '未认领'
      : p.status === 'claimed' ? '处理中' : p.status === 'completed' ? '已完成' : p.status,
      'tag s-' + p.status);
    return [p.repo, p.id, fmtBytes(p.size), st, p.owner || '-',
            p.downloaded_at ? '是' : '-', fmtTime(p.updated_at)];
  });
  fillRows($('#pkg-table tbody'), rows, 7);
  const pg = d.page || {};
  $('#page-info').textContent =
    `第 ${pg.page || state.page} 页 · 共 ${pg.total ?? '?'} 个 · 每页 ${state.size}`;
}

async function loadDevices() {
  const d = await api('/api/summary');
  fillRows($('#dev-table tbody'), (d.devices || []).map(x => [
    x.device_id, x.user_id, x.tier, x.speed, x.status,
    fmtTime(x.last_seen), x.client_ver || '-']), 7);
  fillRows($('#lease-table tbody'), (d.leases || []).map(l => [
    l.kind === 'active' ? '处理中' : '预锁',
    (l.repo || '') + '/' + (l.package || ''),
    l.user_id, l.device_id,
    (l.held_seconds != null ? fmtHeldLite(l.held_seconds) : '-'),
    JSON.stringify(l.progress || {}).slice(0, 60)]), 6);
}

async function loadAnomalies() {
  const d = await api('/api/anomalies');
  const box = $('#anom-cards'); box.replaceChildren();
  for (const [k, v] of [['死包', d.stuck.length], ['孤儿租约', d.orphan.length],
                        ['幽灵租约', d.ghost.length]]) {
    const c = el('div', null, 'card');
    c.appendChild(el('div', k, 'k'));
    c.appendChild(el('div', v, 'v'));
    box.appendChild(c);
  }
  fillRows($('#stuck-table tbody'), (d.stuck || []).map(x =>
    [x.repo, x.package, x.owner || '-']), 3);
  fillRows($('#orphan-table tbody'),
    (d.orphan || []).map(x => [x.lease_id, (x.repo || '') + '/' + (x.package || ''), '-'])
      .concat((d.ghost || []).map(x => [x.lease_id, '（幽灵）', x.device_id])), 3);
}

async function loadAudit() {
  const d = await api('/api/audit');
  if (!d.enabled) {
    fillRows($('#audit-table tbody'), [], 6);
    $('#audit-table').previousElementSibling;
    return;
  }
  fillRows($('#audit-table tbody'), (d.lines || []).reverse().map(e => [
    fmtTime(e.ts), e.kind, e.repo || '-', e.pkg || '-', e.actor || '-',
    (e.detail || '').slice(0, 60)]), 6);
}

async function refresh() {
  try {
    if (state.tab === 'overview') await loadOverview();
    else if (state.tab === 'packages') { await loadOverview(); await loadPackages(); }
    else if (state.tab === 'devices') await loadDevices();
    else if (state.tab === 'anomalies') await loadAnomalies();
    else if (state.tab === 'audit') await loadAudit();
  } catch (e) { setUpstream(false, e.message); }
}

function switchTab(name) {
  state.tab = name;
  for (const b of document.querySelectorAll('#tabs button'))
    b.classList.toggle('on', b.dataset.tab === name);
  for (const id of ['overview', 'packages', 'devices', 'anomalies', 'audit'])
    $('#tab-' + id).hidden = (id !== name);
  refresh();
}

document.querySelectorAll('#tabs button').forEach(b =>
  b.addEventListener('click', () => switchTab(b.dataset.tab)));
$('#refresh').addEventListener('click', refresh);
$('#auto').addEventListener('change', e => {
  if (timer) { clearInterval(timer); timer = null; }
  if (e.target.checked) timer = setInterval(refresh, 15000);
});
$('#f-repo').addEventListener('change', e => { state.repo = e.target.value; state.page = 1; refresh(); });
$('#f-status').addEventListener('change', e => { state.status = e.target.value; state.page = 1; refresh(); });
$('#f-q').addEventListener('input', e => { state.q = e.target.value.trim(); state.page = 1; refresh(); });
$('#prev').addEventListener('click', () => { if (state.page > 1) { state.page--; refresh(); } });
$('#next').addEventListener('click', () => { state.page++; refresh(); });

timer = setInterval(refresh, 15000);
refresh();
"""

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · fox-label 面板</title>
<link rel="stylesheet" href="/static/app.css"></head>
<body class="login">
<form method="post" action="/login" class="loginbox">
  <h1>fox-label 面板</h1>
  <label>账号<input name="user" autocomplete="username" required></label>
  <label>密码<input name="password" type="password" autocomplete="current-password" required></label>
  __ERROR__
  <button class="btn" type="submit">登录</button>
  <p class="muted">只读面板 · 账号密码由管理员通过 <code>panel.py init</code> 设置</p>
</form>
</body></html>
"""


# ------------------------------------------------------------------ HTTP

class PanelHandler(BaseHTTPRequestHandler):
    server_version = f"fox-panel/{VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # --- 由 main() 注入 ---
    cfg: dict = {}
    sessions: Sessions | None = None
    up: Upstream | None = None
    limit: RateLimit | None = None

    # ---------- 工具
    _cfg_mtime: float = 0.0

    def _clear_cookie(self) -> str:
        return f"fp_sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{self._cookie_attrs()}"

    def _refresh_cfg(self) -> None:
        """配置被改过（比如跑了 panel.py passwd）就热加载：改密码立即生效，
        不用记得重启服务；旧会话因为签名密钥变了当场失效。"""
        try:
            mtime = CONFIG.stat().st_mtime
        except OSError:
            return
        if mtime == PanelHandler._cfg_mtime:
            return
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
            sessions = Sessions(cfg["secret"])
        except (OSError, ValueError, KeyError) as e:
            sys.stderr.write(f"[warn] 配置热加载失败，沿用旧配置：{e}\n")
            PanelHandler._cfg_mtime = mtime
            return
        PanelHandler.cfg, PanelHandler.sessions = cfg, sessions
        PanelHandler._cfg_mtime = mtime

    def log_message(self, fmt: str, *args) -> None:
        msg = re.sub(r"(secret|key|password)=[^&\s]+", r"\1=***", fmt % args)
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} "
                         f"{msg}\n")

    def _ip(self) -> str:
        """限速用的客户端 IP。只在直连方是本机（反代）时才信任 X-Forwarded-For，
        否则任何人伪造一个头就能绕过限速。"""
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1", "localhost"):
            xff = self.headers.get("X-Forwarded-For") or ""
            first = xff.split(",")[0].strip()
            if first:
                return first
        return peer

    def _cookie_attrs(self) -> str:
        """走 HTTPS（反代告诉我们）就给 cookie 加 Secure。"""
        proto = (self.headers.get("X-Forwarded-Proto") or "").lower()
        return "; Secure" if proto == "https" else ""

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict[str, str] | None = None) -> None:
        """所有响应都从这里出去：安全头一次写全。用 dict 合并而不是重复
        send_header，避免同一响应出现两个 Cache-Control（no-store + no-cache）。"""
        headers = {
            "Content-Type": ctype,
            "Content-Length": str(len(body)),
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        }
        headers.update(extra or {})
        headers["Content-Security-Policy"] = CSP
        if code == HTTPStatus.NOT_MODIFIED:      # 304 不能带体，也别带 Content-Length
            headers.pop("Content-Length", None)
        self.send_response(code)
        for k, v in headers.items():
            if isinstance(v, (list, tuple)):     # 多条 Set-Cookie 需要重复同名头
                for item in v:
                    self.send_header(k, item)
            else:
                self.send_header(k, v)
        self.end_headers()
        if body and code != HTTPStatus.NOT_MODIFIED:
            self.wfile.write(body)

    def _json(self, code: int, obj: dict, extra: dict[str, str] | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", extra)

    def _html(self, code: int, text: str, extra: dict[str, str] | None = None) -> None:
        self._send(code, text.encode(), "text/html; charset=utf-8", extra)

    def _redirect(self, where: str, extra: dict[str, str] | None = None) -> None:
        self._send(HTTPStatus.FOUND, b"", "text/plain",
                   {"Location": where, **(extra or {})})

    # ---------- 静态资源（web/ 优先，缺失回退内置字符串）
    def _web_text(self, name: str) -> tuple[str, int]:
        """读 web/<name>，同时给出用于 ETag 的 mtime_ns。
        文件不存在/IO/解码失败都抛异常，由调用方决定是 503 还是回退。"""
        base = WEB_DIR.resolve()
        path = (base / name).resolve()
        # 只允许 web/ 目录内的固定文件名，杜绝 ../ 穿越（名字是常量也照样校验）
        if path.parent != base:
            raise OSError("非法资源名")
        st = path.stat()
        return path.read_text(encoding="utf-8"), st.st_mtime_ns

    def _web_ready(self) -> bool:
        """web/ 里只要已经有前端产物就算"前端已交付"。空目录（前端刚建目录还没写文件）
        视同未交付，继续回退内置页面——否则部署中间态会整页 503。"""
        if not WEB_DIR.is_dir():
            return False
        try:
            return any((WEB_DIR / n).is_file() for n in
                       ("index.html", "login.html", "app.css", "app.js"))
        except OSError:
            return False

    def _static(self, name: str, fallback: str, ctype: str,
                subs: dict[str, str] | None = None,
                cache: str = "no-cache", soft: bool = False) -> None:
        """静态资源统一出口：ETag "<mtime_ns>-<size>" + Cache-Control，命中
        If-None-Match 就 304 空体。web/ 未交付时用内置字符串，保证旧行为不破。
        cache 默认 no-cache（css/js 要能被复用又随时可校验）；HTML 页面仍用
        no-store——页面里带着上游地址和版本，不该被缓存，也保住 v1 的既有断言。
        soft=True 时缺文件回退内置字符串（登录页专用：登录入口永远不能 503）。"""
        if self._web_ready():
            try:
                text, mtime_ns = self._web_text(name)
            except (OSError, UnicodeDecodeError):
                if not soft:
                    # 前端产物在、但这个文件读不了：明确 503，绝不返回半截文件
                    return self._json(503, {"error": f"静态资源缺失：{name}"})
                text, mtime_ns = fallback, self._self_mtime_ns()
        else:
            text, mtime_ns = fallback, self._self_mtime_ns()
        for key, value in (subs or {}).items():
            text = text.replace(key, value)
        body = text.encode("utf-8")
        etag = f'"{mtime_ns}-{len(body)}"'
        if self._etag_match(etag):
            return self._send(HTTPStatus.NOT_MODIFIED, b"", ctype,
                              {"ETag": etag, "Cache-Control": cache})
        self._send(200, body, ctype, {"ETag": etag, "Cache-Control": cache})

    @staticmethod
    def _self_mtime_ns() -> int:
        """内置字符串没有自己的 mtime：拿 panel.py 的 mtime 当版本号，
        代码一改 ETag 就变，浏览器不会继续用旧的 304。"""
        try:
            return Path(__file__).resolve().stat().st_mtime_ns
        except OSError:
            return 0

    def _etag_match(self, etag: str) -> bool:
        """If-None-Match 可能是逗号分隔的列表，也可能是 *。"""
        raw = (self.headers.get("If-None-Match") or "").strip()
        if not raw:
            return False
        if raw == "*":
            return True
        return etag in [t.strip() for t in raw.split(",")]

    def _login_body(self, error_html: str) -> str:
        """登录页也走 web/login.html，占位符 {{ERROR}} 与旧内置的 __ERROR__ 都支持，
        免得前端/后端在占位符名字上打架。"""
        text = LOGIN_PAGE
        if self._web_ready():
            try:
                loaded, _ = self._web_text("login.html")
                text = loaded
            except (OSError, UnicodeDecodeError):
                text = LOGIN_PAGE
        return text.replace("{{ERROR}}", error_html).replace("__ERROR__", error_html)

    def _cookie_user(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "fp_sid":
                return self.sessions.check(v)
        return None

    def _body(self, limit: int = MAX_BODY) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        if n > limit:
            raise ValueError("body too large")
        return self.rfile.read(n)

    # ---------- CSRF / 来源（写通道专用）
    def _cookie_raw(self, name: str) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _csrf_token(self) -> str | None:
        return self._cookie_raw(CSRF_COOKIE)

    def _csrf_cookie(self, token: str) -> str:
        # 不加 HttpOnly：前端要把 fp_csrf 放进 X-CSRF-Token（双提交）
        return (f"{CSRF_COOKIE}={token}; Path=/; Max-Age={CSRF_MAX_AGE}; "
                f"SameSite=Lax{self._cookie_attrs()}")

    def _expected_origin(self) -> str | None:
        """期望源：Host 必须在白名单内（防 DNS rebinding），scheme 只信本机反代头。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return None
        allowed = (host == "label.scvxzf.asia" or host == "127.0.0.1"
                   or host.startswith("127.0.0.1:") or host == "[::1]"
                   or host.startswith("[::1]:"))
        if not allowed:
            return None
        peer = self.client_address[0] if self.client_address else ""
        proto = ((self.headers.get("X-Forwarded-Proto") or "").lower()
                 if peer in ("127.0.0.1", "::1") else "")
        return f"{proto or 'http'}://{host}"

    def _origin_ok(self) -> bool:
        expected = self._expected_origin()
        if not expected:
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin:
            return origin == expected
        referer = (self.headers.get("Referer") or "").strip()
        if not referer:
            return False                       # Origin 与 Referer 都缺 → 拒绝
        parsed = urllib.parse.urlparse(referer)
        return f"{parsed.scheme}://{parsed.netloc}".lower() == expected

    def _fetch_site_ok(self) -> bool:
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        return site in ("", "same-origin", "none")

    def _session_fp(self) -> str:
        """会话指纹：confirm token 绑定「actor + 会话」，换登录不能用旧 token。"""
        sid = self._cookie_raw("fp_sid") or ""
        return hashlib.sha256(sid.encode()).hexdigest()[:16]

    def _csrf_response(self, include_cookie: bool = True) -> None:
        token = self._csrf_token() or secrets.token_hex(16)
        extra = {"Set-Cookie": self._csrf_cookie(token)} if include_cookie else None
        return self._json(200, {"ok": True, "csrf": token,
                                "header": "X-CSRF-Token"}, extra)

    # ---------- 路由
    def do_GET(self) -> None:                       # noqa: N802
        self._refresh_cfg()
        path = urllib.parse.urlparse(self.path).path
        # 管理面在会话校验之前直接 404：公网 catch-all 打到面板时必须是确定、可断言的
        # 404，而不是把未登录访客重定向到登录页（SPEC §1.2）。
        if path == "/admin" or path.startswith("/admin/"):
            return self._json(404, {"error": "not found"})
        # 静态资源在登录校验之前：登录页自己也要 CSS
        if path == "/static/app.css":
            return self._static("app.css", CSS, "text/css; charset=utf-8")
        if path == "/static/app.js":
            return self._static("app.js", JS, "application/javascript; charset=utf-8")
        if path == "/login":
            if self._cookie_user():
                return self._redirect("/")
            # 与 css/js 一致带 ETag/304；HTML 仍 no-store（带版本与错误位）。
            # soft=True：web/login.html 还没交付时回退内置登录页，登录口不能 503。
            return self._static("login.html", LOGIN_PAGE,
                                "text/html; charset=utf-8",
                                {"{{ERROR}}": "", "__ERROR__": ""},
                                cache="no-store", soft=True)
        if path == "/logout":
            return self._redirect("/login", {"Set-Cookie": [
                self._clear_cookie(), f"{CSRF_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax"]})

        if not self._cookie_user():
            if path.startswith("/api/"):
                return self._json(401, {"error": "未登录"})
            return self._redirect("/login")

        if path == "/":
            # 两套占位符都替换：{{VERSION}}/{{UPSTREAM}} 是 SPEC v2 写法，
            # __VER__/__UPSTREAM__ 是 v1 内置页面的写法，前端不用改常量名。
            esc_upstream = html.escape(self.up.base)
            return self._static("index.html", PAGE, "text/html; charset=utf-8", {
                "{{VERSION}}": VERSION, "{{UPSTREAM}}": esc_upstream,
                "__VER__": VERSION, "__UPSTREAM__": esc_upstream}, cache="no-store")

        if path.startswith("/api/"):
            return self._api(path)

        self._html(404, "<h1>404</h1>")

    def do_HEAD(self) -> None:                      # noqa: N802
        """只回头部：给探活/监控用（不返回页面内容）。"""
        path = urllib.parse.urlparse(self.path).path
        code = 200 if path in ("/", "/login", "/static/app.css", "/static/app.js") else 404
        self._send(code, b"", "text/html; charset=utf-8")

    def do_POST(self) -> None:                      # noqa: N802
        self._refresh_cfg()
        path = urllib.parse.urlparse(self.path).path
        if path == "/admin" or path.startswith("/admin/"):
            return self._json(404, {"error": "not found"})
        if path == "/login":
            return self._login()
        if path == "/logout":
            return self._redirect("/login", {"Set-Cookie": [
                _CLEAR_COOKIE, f"{CSRF_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax"]})
        if path == "/api/write/preview" or path in WRITE_PATH_ACTION:
            return self._write_post(path)
        if path in ("/api/write/history", "/api/write/export", "/api/notifications"):
            return self._json(405, {"error": "方法不允许"}, {"Allow": "GET"})
        self._json(404, {"error": "not found"})

    # 写通道与只读审计面只接受 POST/GET；其它方法一律 405（不给"删除日志"留口）
    def do_PUT(self) -> None:                       # noqa: N802
        self._method_guard()

    def do_DELETE(self) -> None:                    # noqa: N802
        self._method_guard()

    def do_PATCH(self) -> None:                     # noqa: N802
        self._method_guard()

    def _method_guard(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/write/") or path == "/api/notifications":
            allow = "GET" if path in ("/api/write/history", "/api/write/export",
                                      "/api/notifications") else "POST"
            return self._json(405, {"error": "方法不允许"}, {"Allow": allow})
        if path == "/admin" or path.startswith("/admin/"):
            return self._json(404, {"error": "not found"})
        self.send_error(501, "Unsupported method (%r)" % self.command)

    # ---------- 具体处理
    def _login(self) -> None:
        ip = self._ip()
        wait = self.limit.blocked(ip)
        if wait > 0:
            return self._html(429, self._login_body(
                f'<p class="err">尝试过多，请 {int(wait) + 1} 秒后再试</p>'))
        try:
            data = urllib.parse.parse_qs(self._body().decode("utf-8", "replace"))
        except ValueError:
            return self._html(413, "请求体过大")
        user = (data.get("user") or [""])[0].strip()
        pwd = (data.get("password") or [""])[0]
        cfg = self.cfg
        want = cfg["pwd_hash"]
        salt = bytes.fromhex(cfg["salt"])
        got = hash_password(pwd, salt, int(cfg.get("iterations", 200_000)))
        # 用户名与密码都要对；错误信息不区分，避免枚举账号
        ok = hmac.compare_digest(user, cfg["username"]) and hmac.compare_digest(got, want)
        if not ok:
            self.limit.fail(ip)
            return self._html(401, self._login_body(
                '<p class="err">账号或密码不正确</p>'))
        self.limit.ok(ip)
        token = self.sessions.issue(user)
        csrf = secrets.token_hex(16)          # 每次登录轮换（双提交）
        # 一次响应下发两个 Cookie：fp_sid（会话，HttpOnly）+ fp_csrf（双提交，JS 可读）。
        # 兼容性：旧的脚本/测试有的按"第一个 Set-Cookie"取会话、有的按"最后一个"取，
        # 所以 fp_sid 前后各发一次（值相同，浏览器最终一致）；新前端 get_all 也能拿全。
        sid_cookie = (f"fp_sid={token}; Path=/; Max-Age={SESSION_HOURS*3600}; "
                      f"HttpOnly; SameSite=Lax{self._cookie_attrs()}")
        self._redirect("/", {
            "Set-Cookie": [sid_cookie, self._csrf_cookie(csrf), sid_cookie]})

    def _api(self, path: str) -> None:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        # 写路径只有 POST：GET 打写端点 → 405（前端不会这么用，但脚本可能）
        if path == "/api/write/preview" or path in WRITE_PATH_ACTION:
            return self._json(405, {"error": "方法不允许"}, {"Allow": "POST"})
        try:
            if path == "/api/summary":
                return self._json(200, build_summary(self.up, self.cfg))
            if path == "/api/meta":
                return self._json(200, build_meta(self.up, self.cfg))
            if path == "/api/packages":
                repo = (q.get("repo") or [""])[0].strip()
                status = (q.get("status") or [""])[0].strip()
                text_q = (q.get("q") or [""])[0][:200]
                page = _int_param(q, "page", 1, 1, 10 ** 9)
                size = _int_param(q, "size", DEFAULT_PAGE_SIZE, 1, 200)
                sort_by, order = _sort_params(q)
                # 申报制（SPEC-declare §3.5）：overdue_first=1 时逾期包优先
                return self._json(200, build_packages(
                    self.up, repo, status, text_q, page, size, sort_by, order,
                    overdue_first=((q.get("overdue_first") or [""])[0] == "1")))
            if path == "/api/package":
                return self._package(q)
            if path == "/api/export.csv":
                return self._export(q)
            if path == "/api/anomalies":
                return self._json(200, build_anomalies(self.up))
            if path == "/api/audit":
                return self._json(200, build_audit(self.cfg))
            # ---- v3：写通道的只读面 ----
            if path == "/api/csrf":
                return self._csrf_response()
            if path == "/api/repos":
                return self._json(200, build_repos_admin(self.up))
            if path == "/api/keys":
                return self._admin_json_or_empty("/admin/v1/keys", "keys")
            if path == "/api/users":
                return self._admin_json_or_empty("/admin/v1/users", "users")
            if path == "/api/write/history":
                return self._write_history(q)
            if path == "/api/write/denied":
                return self._write_denied(q)
            if path == "/api/write/export":
                return self._write_export(q)
            if path == "/api/notifications":
                return self._notifications(q)
            if path == "/api/overdue":
                return self._overdue()
        except UpstreamError as e:
            # 契约：503 的文案一律以"上游"开头（前端/测试都按这个前缀判断）
            _scan_invalidate()
            return self._json(503, {"error": upstream_msg(e)})
        except BadRequest as e:
            # 枚举类参数（sort/order）用 SPEC 原句，前端能直接展示
            return self._json(400, {"error": str(e)})
        except ValueError:
            return self._json(400, {"error": "参数不合法"})
        return self._json(404, {"error": "not found"})

    # ---------- v3 写通道：只读面 ----------
    def _admin_json_or_empty(self, upstream_path: str, key: str) -> None:
        """密钥/用户列表：服务端没有该接口时优雅降级为空列表（不 503 整页）。"""
        try:
            data = self.up.get(upstream_path, cache=False)
        except UpstreamError as e:
            if e.code != 404:
                raise
            return self._json(200, {"ok": True, key: [], "enabled": False,
                                    "note": "服务端未提供写通道"})
        return self._json(200, data)

    def _write_read_limit(self) -> bool:
        actor = self._cookie_user() or "-"
        wait = WRITE_READ_LIMIT.take(f"{actor}|{self._ip()}")
        if wait <= 0:
            return True
        secs = max(1, int(wait + 0.999))
        self._json(429, {"error": "操作过于频繁，请稍后再试", "code": "rate_limited",
                         "retry_after": secs}, {"Retry-After": str(secs)})
        return False

    @staticmethod
    def _write_query(q: dict) -> str:
        """把写日志筛选参数原样转给服务端（面板不二次加工口径）。"""
        allowed = ("limit", "offset", "action", "actor", "result", "q",
                   "batch_id", "write_id", "order", "from", "to", "format")
        parts = []
        for name in allowed:
            raw = (q.get(name) or [""])[0]
            if raw == "":
                continue
            parts.append((name, raw[:200]))
        return urllib.parse.urlencode(parts)

    @staticmethod
    def _flag(q: dict, name: str, default: bool = False) -> bool:
        raw = (q.get(name) or [""])[0].strip().lower()
        if not raw:
            return default
        return raw not in ("0", "false", "no", "off")

    def _write_denied(self, q: dict) -> None:
        """被拒绝的写请求（F4）：与写日志同一套行结构，独立轨、只增不改。"""
        if not self._write_read_limit():
            return
        try:
            limit = _int_param(q, "limit", 25, 1, 200)
            offset = _int_param(q, "offset", 0, 0, 10 ** 9)
        except ValueError:
            return self._json(400, {"error": "参数 limit/offset 必须是整数"})
        order = (q.get("order") or ["desc"])[0].strip().lower()
        if order not in ("asc", "desc"):
            return self._json(400, {"error": "order 只能是 asc 或 desc"})
        data = deny_query(self.cfg, limit=limit, offset=offset,
                          actor=(q.get("actor") or [""])[0].strip()[:64],
                          reason=(q.get("reason") or [""])[0].strip()[:64],
                          path=(q.get("path") or [""])[0].strip()[:200],
                          order=order)
        return self._json(200, data)

    def _history_scan(self, qs_base: str, cap: int = HISTORY_SCAN_CAP
                      ) -> tuple[list[dict], int]:
        """合并排序需要全集：按 200/页 把业务写日志拉全（有上限，防上游过大）。"""
        rows: list[dict] = []
        offset = 0
        total = 0
        while True:
            qs = (qs_base + "&" if qs_base else "") + f"limit=200&offset={offset}"
            data = self.up.get("/admin/v1/writes?" + qs, cache=False)
            items = data.get("items") or []
            total = int(data.get("total") or 0)
            rows.extend(items)
            offset += len(items)
            if not items or offset >= total or len(rows) >= cap:
                break
        return rows[:cap], total

    def _write_history(self, q: dict) -> None:
        if not self._write_read_limit():
            return
        try:
            limit = _int_param(q, "limit", 25, 1, 200)
            offset = _int_param(q, "offset", 0, 0, 10 ** 9)
        except ValueError:
            return self._json(400, {"error": "参数 limit 必须是整数"})
        order = (q.get("order") or ["desc"])[0].strip().lower()
        if order not in ("asc", "desc"):
            return self._json(400, {"error": "order 只能是 asc 或 desc"})
        result_filter = (q.get("result") or [""])[0].strip().lower()
        include_denied = self._flag(q, "include_denied", default=True)
        if result_filter and result_filter != "denied":
            include_denied = False

        # result=denied：只看拒绝轨
        if result_filter == "denied":
            denied = _filter_denied(deny_rows(self.cfg), q)
            denied.sort(key=lambda r: (r.get("ts_epoch") or 0,
                                       str(r.get("write_id") or "")),
                        reverse=(order != "asc"))
            page = denied[offset:offset + limit]
            return self._json(200, {
                "ok": True, "generated_at": now_iso(), "total": len(denied),
                "limit": limit, "offset": offset, "items": page,
                "source": "panel-denied", "denied_included": True,
                "denied_total": len(denied), "business_total": 0,
                "total_all": len(denied),
                "filters": {"result": result_filter, "order": order},
                **({"note": "还没有被拒绝的写请求"} if not denied else {}),
            })

        if not include_denied:
            qs = self._write_query(dict(q, limit=[str(limit)], offset=[str(offset)]))
            try:
                data = self.up.get("/admin/v1/writes" + (("?" + qs) if qs else ""),
                                   cache=False)
            except UpstreamError as e:
                if e.code == 404:
                    return self._json(503, {"error": "上游未提供写日志接口（版本不匹配）",
                                            "code": "upstream_error"})
                raise
            return self._json(200, data)

        # 合并视图（N3）：total = 业务行 + 拒绝行，items 永远 ≤ total，分页按同一份排序切片
        qs_base = self._write_query(dict(q, limit=["200"], offset=["0"]))
        try:
            rows, business_total = self._history_scan(qs_base)
        except UpstreamError as e:
            if e.code == 404:
                return self._json(503, {"error": "上游未提供写日志接口（版本不匹配）",
                                        "code": "upstream_error"})
            raise
        denied = _filter_denied(deny_rows(self.cfg), q)
        merged = list(rows) + denied
        merged.sort(key=lambda r: (r.get("ts_epoch") or 0,
                                   str(r.get("write_id") or "")),
                    reverse=(order != "asc"))
        total = len(merged)
        truncated = business_total > len(rows) or len(merged) >= HISTORY_SCAN_CAP
        page = merged[offset:offset + limit] if limit > 0 else []
        return self._json(200, {
            "ok": True, "generated_at": now_iso(), "total": total,
            "limit": limit, "offset": offset, "items": page,
            "source": "merged", "denied_included": True,
            "business_total": business_total, "denied_total": len(denied),
            "total_all": total, "truncated": truncated, "order": order,
            "pages": (total + limit - 1) // limit if limit > 0 else 0,
            "filters": {"result": result_filter, "order": order,
                        "actor": (q.get("actor") or [""])[0].strip()},
        })

    def _write_export(self, q: dict) -> None:
        if not self._write_read_limit():
            return
        fmt = (q.get("format") or ["jsonl"])[0].strip().lower()
        if fmt not in ("jsonl", "csv"):
            return self._json(400, {"error": "format 只能是 jsonl 或 csv"})
        qs = self._write_query(dict(q, format=[fmt]))
        try:
            status, body, headers = self.up.raw("/admin/v1/writes/export" + (("?" + qs) if qs else ""))
        except UpstreamError as e:
            return self._json(503, {"error": upstream_msg(e), "code": "upstream_error"})
        if status != 200:
            return self._json(status, {"error": "上游写日志导出失败", "code": "upstream_error"})
        extra = {"Cache-Control": "no-store",
                 "Content-Disposition": headers.get(
                     "content-disposition",
                     f'attachment; filename="fox-writes.{fmt}"'),
                 "X-Total-Rows": headers.get("x-total-rows", "0"),
                 "X-Truncated": headers.get("x-truncated", "0")}
        ctype = ("application/x-ndjson; charset=utf-8" if fmt == "jsonl"
                 else "text/csv; charset=utf-8")
        return self._send(200, body, ctype, extra)

    def _notifications(self, q: dict) -> None:
        if not self._write_read_limit():
            return
        try:
            limit = _int_param(q, "limit", 20, 1, 100)
        except ValueError:
            return self._json(400, {"error": "参数 limit 必须是整数"})
        params = {"limit": str(limit)}
        after = (q.get("after") or [""])[0].strip()
        after_id = (q.get("after_id") or [""])[0].strip()
        if after:
            params["after"] = after[:40]
        if after_id:
            params["after_id"] = after_id[:80]
        try:
            data = self.up.get("/admin/v1/notifications?"
                               + urllib.parse.urlencode(params), cache=False)
            # 逾期提醒走独立聚合端点（SPEC-declare §3.5），在这里合并进通知流，
            # 让现有未读角标天然把逾期卡片算进去
            ov = self.up.overdue()
            rows = list(rows) + [
                {"ts_epoch": _epoch_or(r.get("first_reminded_at")),
                 "ts": _iso_or(r.get("first_reminded_at")),
                 "batch_id": "overdue:" + str(r.get("repo")) + "/" + str(r.get("pkg")),
                 "write_id": "overdue:" + str(r.get("repo")) + "/" + str(r.get("pkg")),
                 "action": "overdue.notice",
                 "result": "ok" if r.get("still_open") else "denied",
                 "impact_count": 1,
                 "_overdue": r}
                for r in (ov.get("items") or [])]
        except UpstreamError:
            return self._json(200, empty_notifications("上游不可用，通知暂不可用"))
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        out = build_notifications(rows, limit)
        # 默认只给个计数（前端可显示"另有 N 条写请求被拒绝"），要明细再带 include_denied=1
        denied_all = deny_query(self.cfg, limit=1, offset=0)
        out["denied_total"] = denied_all.get("total") or 0
        if self._flag(q, "include_denied"):
            denied = deny_query(self.cfg, limit=limit, offset=0)["items"]
            if denied:
                out = build_notifications(list(rows) + denied, limit)
                out["denied_included"] = True
        return self._json(200, out)

    # ---------- v3 写通道：写代理 ----------
    def _overdue(self) -> None:
        """申报制（SPEC-declare §3.5）逾期中心数据源。

        只读聚合代理 /admin/v1/overdue（每日去重、resolve 回填、
        declare_hours 判定全部由 fox-label 服务端完成），面板不自算时间。
        """
        if not self._write_read_limit():
            return
        try:
            data = self.up.overdue()
        except UpstreamError:
            return self._json(200, {
                "ok": False, "upstream_ok": False, "items": [], "declare_hours": None,
                "error": "上游不可用，逾期提示暂不可用"})
        out = {
            "ok": True,
            "declare_hours": data.get("declare_hours"),
            "generated_at": data.get("generated_at"),
            "items": [it for it in (data.get("items") or [])
                      if isinstance(it, dict)][:200],
        }
        return self._json(200, out)

    def _deny(self, path: str, status: int, code_str: str, reason: str,
              actor: str | None = None, *, upstream_code: int | None = None,
              upstream_action: str | None = None, count: bool = True) -> None:
        """面板侧拒绝也要留痕（F4）：actor + remote_ip + path + status + reason。

        与 writes.jsonl 同一套行结构（append-only、字段名一致），但**独立文件**：
        SPEC §7.1 明确前置拒绝不能混进业务审计（否则可被刷日志污染），P2 建议的
        writes-denied.jsonl 就是这条轨。查询走 /api/write/denied，或
        /api/write/history?include_denied=1 / /api/notifications?include_denied=1。
        """
        try:
            clean_path = path.split("?", 1)[0]
            if actor is None:
                actor = self._cookie_user() or "-"
            rec = {
                "write_id": "D" + time.strftime("%Y%m%dT%H%M%S") + "-"
                            + secrets.token_hex(3),
                "ts": now_iso(),
                "ts_epoch": round(time.time(), 3),
                "actor": actor,
                "session_fp": (self._session_fp()
                               if self._cookie_raw("fp_sid") else "-"),
                "remote_ip": self._ip(),
                "path": clean_path,
                "reason": code_str,
                "upstream_code": upstream_code,
                "upstream_action": upstream_action,
                "action": "write.denied",
                "target": {"kind": "panel", "path": clean_path},
                "params": {"reason": code_str, "upstream_code": upstream_code},
                "result": "denied",
                "code": status,
                "error": (reason or "")[:200],
                "impact_count": 0,
                "mutations": 0,
                "confirm_token_id": None,
                "batch_id": None,
                "idempotency_key": (self.headers.get("Idempotency-Key")
                                    or "").strip()[:64] or None,
                "ms": 0,
                "logged": True,
                "panel_version": VERSION,
            }
            deny_append(rec, self.cfg)
            if count:
                deny_flood_bump(f"{actor}|{self._ip()}")
        except Exception as e:              # noqa: BLE001 - 留痕失败不影响响应
            sys.stderr.write(f"[warn] 拒绝留痕异常：{e}\n")

    # 只有"被拒绝的写请求"才留痕；428（等用户去预览）与 503（上游抖动）不算拒绝
    _DENY_LOG_CODES = (400, 403, 413, 415, 429)

    def _write_fail(self, code: int, error: str, code_str: str,
                    extra: dict | None = None) -> None:
        if code in self._DENY_LOG_CODES:
            self._deny(self.path, code, code_str, error)
        body = {"ok": False, "error": error, "code": code_str}
        body.update(extra or {})
        self._json(code, body)

    def _write_headers(self, idem_key: str | None = None) -> dict:
        """面板 → 服务端的内部头：只在服务端认定调用方为本机时才被信任。"""
        headers = {"X-Fox-Actor": self._cookie_user() or "",
                   "X-Fox-Session": self._session_fp(),
                   "X-Fox-Client-IP": self._ip(),
                   "X-Fox-Panel-Version": VERSION}
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        return headers

    @staticmethod
    def _check_targets(action: str, raw) -> str | None:
        if not isinstance(raw, list) or not raw:
            return "targets 必须是非空数组"
        if len(raw) > MAX_BATCH:
            return f"单次最多 {MAX_BATCH} 个目标"
        fields = TARGET_FIELDS[WRITE_ACTIONS[action]["target"]]
        for item in raw:
            if not isinstance(item, dict):
                return "targets 的每一项都必须是对象"
            for field in fields:
                value = item.get(field)
                if not isinstance(value, str) or not value.strip():
                    return f"目标缺少字段 {field}"
        return None

    def _write_post(self, path: str) -> None:
        """写请求统一入口：洪水闸 → 会话 → 写开关 → Content-Type → CSRF → Origin → 限流。"""
        actor = self._cookie_user()
        # N4：连续被拒 > N 次/分钟直接 429，并只补一条 429 留痕（防止 30×415 刷满日志）
        flood_key = f"{actor or '-'}|{self._ip()}"
        blocked, log_429 = deny_flood_check(flood_key)
        if blocked:
            if log_429:
                self._deny(self.path, 429, "rate_limited",
                           "拒绝次数过多，已临时限流（60 秒）",
                           actor=actor or "-")
            return self._json(429, {"ok": False, "error": "拒绝次数过多，请稍后再试",
                                    "code": "rate_limited", "retry_after": 60},
                              {"Retry-After": "60"})
        if not actor:
            # N5：未认证的写请求也进 denied 轨（actor 记为 "-"）
            self._deny(self.path, 401, "unauthenticated", "未登录", actor="-")
            return self._json(401, {"error": "未登录"})
        if not write_enabled(self.cfg):
            return self._write_fail(403, "面板处于只读模式", "readonly")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._write_fail(415, "Content-Type 必须是 application/json",
                                    "unsupported_media_type")
        cookie_csrf = self._csrf_token() or ""
        header_csrf = (self.headers.get("X-CSRF-Token") or "").strip()
        if not cookie_csrf or not header_csrf or not hmac.compare_digest(
                header_csrf, cookie_csrf):
            return self._write_fail(403, "CSRF 校验失败", "csrf_failed")
        if not self._fetch_site_ok() or not self._origin_ok():
            return self._write_fail(403, "来源不被信任", "origin_rejected")
        try:
            body_bytes = self._body(WRITE_MAX_BODY)
        except ValueError:
            return self._write_fail(413, "请求体过大", "payload_too_large")
        try:
            data = json.loads(body_bytes.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._write_fail(400, "请求体不是合法 JSON", "bad_request")
        if not isinstance(data, dict):
            return self._write_fail(400, "请求体必须是 JSON 对象", "bad_request")

        preview = path == "/api/write/preview"
        wait = (PREVIEW_LIMIT if preview else WRITE_LIMIT).take(f"{actor}|{self._ip()}")
        if wait > 0:
            secs = max(1, int(wait + 0.999))
            self._deny(self.path, 429, "rate_limited", "操作过于频繁，请稍后再试",
                       actor=actor)
            return self._json(429, {"ok": False, "error": "操作过于频繁，请稍后再试",
                                    "code": "rate_limited", "retry_after": secs},
                              {"Retry-After": str(secs)})
        if preview:
            return self._write_preview(data)
        return self._write_execute(WRITE_PATH_ACTION[path], data)

    def _write_preview(self, data: dict) -> None:
        action = data.get("action")
        if not isinstance(action, str) or action not in WRITE_ACTIONS:
            return self._write_fail(400, f"未知动作：{action}", "bad_request")
        bad = self._check_targets(action, data.get("targets"))
        if bad:
            code = "batch_too_large" if "最多" in bad else "bad_request"
            extra = {"max_batch": MAX_BATCH} if code == "batch_too_large" else None
            return self._write_fail(400, bad, code, extra)
        if data.get("params") is not None and not isinstance(data.get("params"), dict):
            return self._write_fail(400, "params 必须是 JSON 对象", "bad_request")
        payload = {"action": action, "targets": data.get("targets"),
                   "params": data.get("params") or {}}
        try:
            status, raw, headers = self.up.post("/admin/v1/preview", payload,
                                                self._write_headers())
        except UpstreamError as e:
            return self._write_fail(503, upstream_msg(e), "upstream_error")
        return self._pass_through_write(status, raw, headers, action)

    def _write_execute(self, action: str, data: dict) -> None:
        idem_key = (self.headers.get("Idempotency-Key") or "").strip()
        if not idem_key:
            return self._write_fail(400, "缺少 Idempotency-Key 请求头",
                                    "idempotency_key_required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", idem_key):
            return self._write_fail(400, "Idempotency-Key 必须是 8-64 位 [A-Za-z0-9_-]",
                                    "bad_request")
        bad = self._check_targets(action, data.get("targets"))
        if bad:
            code = "batch_too_large" if "最多" in bad else "bad_request"
            extra = {"max_batch": MAX_BATCH} if code == "batch_too_large" else None
            return self._write_fail(400, bad, code, extra)
        if data.get("params") is not None and not isinstance(data.get("params"), dict):
            return self._write_fail(400, "params 必须是 JSON 对象", "bad_request")
        token = data.get("confirm_token")
        if not isinstance(token, str) or not token.strip():
            # 面板先判"缺 token → 428"：上游抖动时也要给出确定语义（SPEC §4.3）
            return self._write_fail(428, "需要二次确认，请先预览影响面",
                                    "confirm_required")
        actor = self._cookie_user() or ""
        if action != "user.rekey":
            hit = idem_lookup(actor, action, idem_key)
            if hit is not None:
                status, body = hit
                return self._pass_through(status, body,
                                          {"idempotent-replay": "1"})
        payload = {"action": action, "targets": data.get("targets"),
                   "params": data.get("params") or {},
                   "confirm_token": token.strip()}
        if data.get("batch_confirm") is not None:
            payload["batch_confirm"] = data.get("batch_confirm")
        headers = self._write_headers(idem_key)
        try:
            status, raw, up_headers = self.up.post("/admin/v1/execute", payload, headers)
        except UpstreamError:
            # 只对连接错误自动重试一次，复用同一 Idempotency-Key（SPEC §6.5）
            try:
                status, raw, up_headers = self.up.post("/admin/v1/execute", payload,
                                                      headers)
            except UpstreamError as e2:
                return self._write_fail(
                    503, upstream_msg(e2) + "（结果未知，请到审计页确认后再操作）",
                    "upstream_error")
        # 写成功（含部分失败）立刻清缓存：下一次读必须反映新状态
        if status in (200, 404, 409):
            _scan_invalidate()
            self.up.invalidate()
        if status in (200, 404, 409) and action != "user.rekey":
            skip = False
            try:
                code_str = json.loads(raw.decode("utf-8") or "{}").get("code")
                # 这两类是"键用错了/不可重放"，缓存下来只会让用户永远看到同一个错误
                skip = code_str in ("idempotency_conflict", "secret_not_replayable")
            except (ValueError, UnicodeDecodeError):
                skip = False
            if not skip:
                idem_store(actor, action, idem_key, status, raw)
        return self._pass_through_write(status, raw, up_headers, action)

    def _pass_through_write(self, status: int, raw: bytes, headers: dict,
                            action: str) -> None:
        """N1：上游 400/403/429 也是"被拒绝的写请求"，带 actor/会话/上游码留痕后再透传。"""
        if status in (400, 403, 429):
            try:
                parsed = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                parsed = {}
            if isinstance(parsed, dict):
                code = str(parsed.get("code") or "") or f"upstream_{status}"
                self._deny(self.path, status, code,
                           str(parsed.get("error") or ""),
                           upstream_code=status, upstream_action=action)
        return self._pass_through(status, raw, headers)

    def _pass_through(self, status: int, raw: bytes, headers: dict) -> None:
        """把服务端响应（含 428/400 diff）原样交给浏览器；只补安全相关头。"""
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._json(502, {"ok": False, "error": "上游返回的不是合法 JSON",
                                    "code": "upstream_error"})
        extra = {}
        if (headers.get("idempotent-replay") or "") == "1":
            extra["Idempotent-Replay"] = "1"
        self._json(status, parsed, extra or None)

    def _package(self, q: dict) -> None:
        """单包详情。缺参先拦（不打上游），再定位，最后补租约摘要。"""
        pkg_id = (q.get("id") or [""])[0].strip()
        if not pkg_id:
            return self._json(400, {"error": "缺少参数 id"})
        if len(pkg_id) > 512:
            return self._json(400, {"error": "参数 id 过长（最多 512 字符）"})
        repo = (q.get("repo") or [""])[0].strip()
        try:
            package = find_package(self.up, repo, pkg_id)
        except PackageNotFound as e:
            return self._json(404, {"error": str(e)})
        except PackageAmbiguous as e:
            return self._json(400, {"error": str(e)})
        # 租约摘要只是附加信息，取不到就留 null，不让详情跟着 503
        package["lease"] = _lease_of(self.up, str(package.get("repo") or ""), pkg_id)
        return self._json(200, {"ok": True, "generated_at": now_iso(),
                                "package": package})

    def _export(self, q: dict) -> None:
        """CSV 导出：与列表页复用同一条 过滤 → 稳定排序 链路，
        保证"我看到的顺序"和"导出的顺序"一致。"""
        repo = (q.get("repo") or [""])[0].strip()
        status = (q.get("status") or [""])[0].strip()
        text_q = (q.get("q") or [""])[0][:200]
        sort_by, order = _sort_params(q)
        raw = _all_packages(self.up, repo, status)
        rows = _sort_packages(_filter_packages(raw, repo, status, text_q),
                              sort_by, order)
        truncated = len(raw) >= MAX_SCAN_ROWS or len(rows) > EXPORT_MAX_ROWS
        rows = rows[:EXPORT_MAX_ROWS]
        body = build_csv(rows)
        filename = "fox-packages-" + time.strftime("%Y%m%d-%H%M%S") + ".csv"
        return self._send(200, body, "text/csv; charset=utf-8", {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Total-Rows": str(len(rows)),
            "X-Truncated": "1" if truncated else "0",
        })


# ------------------------------------------------------------------ CLI

def cmd_serve(args) -> int:
    try:                       # systemd 下 stdout 是块缓冲，不 flush 就看不到启动日志
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    cfg = load_cfg()
    if args.upstream:
        cfg["upstream"] = args.upstream.rstrip("/")
    if args.admin_key_file:
        cfg["admin_key_file"] = args.admin_key_file
    PanelHandler.cfg = cfg
    PanelHandler.sessions = Sessions(cfg["secret"])
    PanelHandler.up = Upstream(cfg["upstream"], cfg.get("admin_key_file"))
    PanelHandler.limit = RateLimit()

    host, port = args.host, args.port
    try:
        srv = ThreadingHTTPServer((host, port), PanelHandler)
    except OSError as e:
        die(2, f"监听 {host}:{port} 失败：{e}")
    srv.daemon_threads = True
    print(f"fox-panel v{VERSION} 监听 http://{host}:{port}")
    print(f"  上游 : {cfg['upstream']}")
    print(f"  账号 : {cfg['username']}")
    print(f"  密钥 : {Path(cfg.get('admin_key_file') or '-').name}")
    # 启动自检：把"密钥读不到 / 上游连不上"当场说清楚，别等到打开页面才报错
    try:
        up = PanelHandler.up
        up.key()
        h = up.get("/health")
        print(f"  自检 : 上游正常 ✓ {json.dumps(h, ensure_ascii=False)}")
    except UpstreamError as e:
        print(f"  自检 : [warn] {e}（面板照常启动，页面上会显示上游不可用）")
    if host not in ("127.0.0.1", "localhost"):
        print("  [warn] 监听在非本地地址：请确认已有 TLS 反代或仅在可信内网使用")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="panel.py", description="fox-label 只读 Web 面板")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("init", help="初始化面板账号")
    a.add_argument("--user", default="admin")
    a.add_argument("--password", default=None, help="不填则交互输入（推荐）")
    a.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    a.add_argument("--admin-key-file", default=str(
        Path.home() / "文档/deepseek-harness/default-workspace/fox-label/state/admin.key"))
    a.add_argument("--audit-file", default=None, help="可选：审计 jsonl 路径")
    a.add_argument("--force", action="store_true",
                   help="覆盖已有配置（还必须设置 FOXLABEL_ALLOW_INIT=1；会先自动备份）")
    a.set_defaults(func=cmd_init)

    a = sub.add_parser("passwd", help="改密码（同时失效所有旧会话）")
    a.add_argument("--password", default=None)
    a.set_defaults(func=cmd_passwd)

    a = sub.add_parser("serve", help="启动面板")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=DEFAULT_PORT)
    a.add_argument("--upstream", default=None)
    a.add_argument("--admin-key-file", default=None)
    a.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
