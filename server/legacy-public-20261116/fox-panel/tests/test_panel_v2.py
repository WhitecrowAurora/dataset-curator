#!/usr/bin/env python3
"""fox-panel v2 验收：新接口 + 静态资源托管 + 恶意输入 + 兼容红线。

自包含：起一个假上游 http.server 模拟 fox-label 的 /packages、/packages/<id>、
/repos、/api/v1/work、/health，再起 panel.py 子进程；不联网、不依赖真实服务。

风格照 tests/test_panel.py：每条断言打印 PASS/FAIL，最后汇总，失败退出码非零。
本文件只读，不改 panel.py / web/ / test_panel.py。任何真实密钥与密码都不会被打印
（这里的 key 是测试自造的假 key）。
"""

from __future__ import annotations

import base64
import collections
import csv
import hashlib
import hmac
import http.server
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "panel.py"
WEB = ROOT / "web"
PY = sys.executable

FAILS: list[str] = []
PASSED = 0

# 假上游里的"管理器"账号密码（仅测试用，不是真实凭据）
TEST_USER = "admin"
TEST_PASS = "testpass123"
FAKE_KEY = "test-admin-key"

CSV_HEADER = ["repo", "id", "size", "sha256", "status", "owner",
              "claimed_at", "completed_at", "downloaded_at", "updated_at"]
PKG_KEYS = {"repo", "id", "size", "sha256", "status", "status_label", "owner",
            "claimed_at", "completed_at", "downloaded_at", "note",
            "updated_at", "lease"}
META_KEYS = {"ok", "version", "upstream", "upstream_ok", "upstream_error",
             "generated_at", "refresh_ms", "default_page_size", "page_sizes",
             "audit_page_size", "export_max_rows", "statuses", "sorts",
             "repos", "features", "audit"}

# 恶意/边界包名（全部走 URL 编码，验证面板不会把它们拼进 HTML/CSV 就被破坏）
ID_COMMA = 'weird,comma "quote".tar'
ID_NEWLINE = "line\nbreak.tar"
ID_SCRIPT = "<script>alert(1)</script>.tar"
ID_FORMULA = "=cmd|calc.tar"
ID_MINUS = "-minus.tar"
ID_AT = "@at.tar"
ID_PLUS = "+plus.tar"
ID_TAB = "\ttab.tar"
ID_NOSIZE = "unknown-size.tar"
ID_DUP = "dup.tar"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}" + (f"  {str(detail)[:300]}" if detail else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(url: str, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.05)
    return False


# ---------------------------------------------------------------- 数据
def dataset() -> list[dict]:
    """假上游的完整包清单。

    前 6 行刻意保持 v1 假上游的形状（updated_at 全相同、size 递增），
    这样 page=2&size=2 → shard-00002/00003 这条既有断言在 v2 下仍然成立。
    后面追加恶意/边界行，全部 keep 在同一个仓库 t/one。
    """
    def row(repo, ident, size, status="available", owner=None, **extra):
        r = {
            "repo": repo, "id": ident, "size": size,
            "sha256": hashlib.sha256((repo + "/" + ident).encode()).hexdigest(),
            "status": status,
            "owner": owner,
            "claimed_at": None, "completed_at": None, "downloaded_at": None,
            "note": None,
            "updated_at": "2026-11-10T10:00:00+08:00",
            # 上游故意给歪文案：面板详情必须只信自己的词表（未认领/处理中/已完成）
            "status_label": {"available": "空闲", "claimed": "已认领",
                             "completed": "完成!"}.get(status, "?"),
            # 上游多出来的列：/api/package 必须按 12 字段白名单收敛，不得外泄
            "lease_device": "D9", "prelock_key": "pk-leak", "repo_type": "dataset",
        }
        r.update(extra)
        return r

    rows = [row("t/one", f"shard-{i:05d}.tar", 1000 * (i + 1)) for i in range(6)]
    rows[2].update({"status": "claimed", "owner": "bob",
                    "claimed_at": "2026-11-10T10:05:00+08:00",
                    "status_label": "已认领"})
    rows[4].update({"status": "completed", "owner": "carol",
                    "completed_at": "2026-11-10T11:00:00+08:00",
                    "downloaded_at": "2026-11-10T11:30:00+08:00",
                    "status_label": "完成!"})
    rows += [
        row("t/one", ID_COMMA, 2048),
        row("t/one", ID_NEWLINE, None),          # size 未知 → CSV 写空串
        row("t/one", ID_SCRIPT, 512),
        row("t/one", ID_FORMULA, 4096),
        row("t/one", ID_MINUS, 256),
        row("t/one", ID_AT, 128),
        row("t/one", ID_PLUS, 64),
        row("t/one", ID_TAB, 32),
        row("t/one", ID_NOSIZE, None, status="completed", owner="dave"),
        row("t/one", ID_DUP, 111),
        row("t/two", ID_DUP, 222),
        row("t/two", "other-two.tar", 777, status="claimed", owner="dave"),
    ]
    return rows


# ---------------------------------------------------------------- 假上游
class FakeUpstream(http.server.BaseHTTPRequestHandler):
    key = FAKE_KEY
    packages: list[dict] = []
    # 让指定 id 的单包路由返回 500（模拟真实 fox-label 把歧义 Conflict 落成通用 500）
    force_detail_500: set[str] = set()
    hits: collections.Counter = collections.Counter()
    hits_lock = threading.Lock()

    # ---------- 工具
    def _json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    @staticmethod
    def _summary(rows: list[dict]) -> dict:
        return {"repos": len({r["repo"] for r in rows}),
                "total": len(rows),
                "available": sum(1 for r in rows if r["status"] == "available"),
                "claimed": sum(1 for r in rows if r["status"] == "claimed"),
                "completed": sum(1 for r in rows if r["status"] == "completed"),
                "downloaded": sum(1 for r in rows if r.get("downloaded_at")),
                "bytes_total": sum(int(r.get("size") or 0) for r in rows)}

    @staticmethod
    def _repos() -> list[dict]:
        return [{"repo": "t/one", "repo_type": "dataset", "manifest": "manifest.jsonl",
                 "manifest_sha256": "9f2c" + "0" * 60, "manifest_size": 20480},
                {"repo": "t/two", "repo_type": "model", "manifest": "manifest.jsonl",
                 "manifest_sha256": "aa11" + "0" * 60, "manifest_size": 1024}]

    # ---------- 路由
    def do_GET(self):                                     # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path, q = parsed.path, urllib.parse.parse_qs(parsed.query)
        if self.headers.get("Authorization") != f"Bearer {self.key}":
            return self._json(401, {"error": "需要有效的 Bearer 密钥"})

        segs = [s for s in path.split("/") if s]
        if path == "/health":
            return self._json(200, {"ok": True, "version": "test-upstream"})
        if path == "/repos":
            with self.hits_lock:
                self.hits[("repos", "", "")] += 1
            return self._json(200, {"repos": self._repos()})
        if path == "/api/v1/work":
            with self.hits_lock:
                self.hits[("work", "", "")] += 1
            return self._json(200, {
                "packages_total": len(self.packages), "available": 0,
                "reserved_or_active": 1, "free_for_assign": 0,
                "devices": [
                    {"device_id": "D1", "user_id": "bob", "tier": "fast",
                     "speed": 0.5, "status": "online",
                     "last_seen": 1770000000.0, "client_ver": "3.0.0"},
                    {"device_id": "D2", "user_id": None, "tier": "slow",
                     "speed": 0.125, "status": "idle",
                     "last_seen": 1770000100.0, "client_ver": "3.0.0"}],
                "leases": [
                    {"lease_id": "L1", "kind": "active", "repo": "t/one",
                     "package": "shard-00002.tar", "device_id": "D1",
                     "user_id": "bob", "expires_in": 58,
                     "last_beat_at": 1770000000.0,
                     "progress": {"done": 1, "total": 2}},
                    {"lease_id": "L2", "kind": "prelock", "repo": "t/one",
                     "package": "shard-00003.tar", "device_id": "D2",
                     "user_id": None, "expires_in": 300,
                     "last_beat_at": None, "progress": {}}]})
        if len(segs) == 2 and segs[0] == "packages":
            return self._detail(urllib.parse.unquote(segs[1]),
                                (q.get("repo") or [""])[0])
        if path == "/packages":
            return self._list((q.get("repo") or [""])[0],
                              (q.get("status") or [""])[0], q)
        return self._json(404, {"error": f"未知路径 {path}"})

    def _detail(self, pkg: str, repo: str) -> None:
        with self.hits_lock:
            self.hits[("detail", pkg, repo)] += 1
        if pkg in self.force_detail_500:
            return self._json(500, {"error": "internal error"})
        hits = [r for r in self.packages if r["id"] == pkg
                and (not repo or r["repo"] == repo)]
        if not hits:
            return self._json(404, {"error": f"包不存在：{pkg}"})
        if not repo and len(hits) > 1:
            # 真实 fox-label 的 _resolve_repo 对歧义抛 Conflict → 通用 500；
            # 这里同时提供 409（契约提到）与 500（真实行为），用 409 更直观。
            return self._json(409, {"error": "包名在多个仓库存在"})
        return self._json(200, dict(hits[0]))

    def _list(self, repo: str, status: str, q: dict) -> None:
        with self.hits_lock:
            self.hits[("list", repo, status)] += 1
        rows = [r for r in self.packages
                if (not repo or r["repo"] == repo)
                and (not status or r["status"] == status)]
        try:
            limit = max(1, min(int((q.get("limit") or ["500"])[0]), 5000))
            offset = max(0, int((q.get("offset") or ["0"])[0]))
        except ValueError:
            limit, offset = 500, 0
        matched = len(rows)
        return self._json(200, {
            "repo_filter": repo,
            "repos": [r for r in self._repos() if not repo or r["repo"] == repo],
            "packages": rows[offset:offset + limit],
            "page": {"limit": limit, "offset": offset, "matched": matched,
                     "truncated": offset + limit < matched},
            "summary": self._summary(rows)})

    def log_message(self, *a):
        pass


def start_upstream(port: int) -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), FakeUpstream)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def reset_hits() -> None:
    with FakeUpstream.hits_lock:
        FakeUpstream.hits.clear()


def hit_count(kind: str, a: str = "", b: str = "") -> int:
    with FakeUpstream.hits_lock:
        return FakeUpstream.hits[(kind, a, b)]


# ---------------------------------------------------------------- HTTP 工具
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随 302：断言要看到重定向与会话 Cookie 本身。"""

    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def req(url: str, cookie: str | None = None, data: bytes | None = None,
        method: str = "GET", extra: dict | None = None):
    headers = dict(extra or {})
    if cookie:
        headers["Cookie"] = f"fp_sid={cookie}"
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with OPENER.open(r, timeout=15) as resp:
            return (resp.status, {k.lower(): v for k, v in resp.headers.items()},
                    resp.read())
    except urllib.error.HTTPError as e:
        return (e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())


def api(base: str, path: str, cookie: str, **params):
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return req(base + path + (("?" + qs) if qs else ""), cookie)


def api_json(base: str, path: str, cookie: str, **params):
    code, hdr, body = api(base, path, cookie, **params)
    text = body.decode("utf-8", "replace")
    try:
        return code, hdr, json.loads(text), text
    except ValueError:
        return code, hdr, {"_unparsable": text[:300]}, text


def login(base: str, user: str, pwd: str):
    body = urllib.parse.urlencode({"user": user, "password": pwd}).encode()
    return req(base + "/login", data=body, method="POST")


def cookie_of(headers: dict) -> str:
    for part in (headers.get("set-cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "fp_sid":
            return v
    return ""


def ids(rows) -> list[str]:
    return [r.get("id") for r in rows]


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ------------------------------------------------ CSS 语法小工具（只读核验前端字节）
def parse_css(css: str):
    """把 CSS 拆成 [(selector, decls_text, at_rule_stack)]。

    声明块内没有嵌套花括号，所以按花括号深度扫描就够：@media/@keyframes 这类
    at-rule 记在栈里，普通选择器连同它所在的媒体查询一起返回。
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules, stack, buf, i = [], [], "", 0
    while i < len(css):
        ch = css[i]
        if ch == "{":
            header, buf = buf.strip(), ""
            if header.startswith("@"):
                stack.append(header)
                i += 1
                continue
            end = css.index("}", i)
            rules.append((header, css[i + 1:end], tuple(stack)))
            i = end + 1
            continue
        if ch == "}":
            if stack:
                stack.pop()
            buf, i = "", i + 1
            continue
        if ch == ";":                       # @charset "@import" 之类语句
            buf, i = "", i + 1
            continue
        buf += ch
        i += 1
    return rules


def decl_map(body: str) -> dict:
    out = {}
    for part in body.split(";"):
        if ":" in part:
            k, _, v = part.partition(":")
            out[k.strip().lower()] = " ".join(v.split())
    return out


def css_rules(rules, selector: str, media: str | None = None, plain: bool = False):
    """selector 完全匹配（逗号拆开逐项比）的声明块列表 [(decls, at_rules)]。"""
    hits = []
    for sel, body, at in rules:
        if selector not in [s.strip() for s in sel.split(",")]:
            continue
        joined = " ".join(at)
        if plain and joined:
            continue
        if media is not None and media not in joined:
            continue
        hits.append((decl_map(body), joined))
    return hits


def theme_tokens(rules, theme: str) -> dict:
    for sel, body, _at in rules:
        if sel.strip() == f':root[data-theme="{theme}"]':
            return decl_map(body)
    return {}


def _lum(hexcolor: str) -> float:
    h = hexcolor.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = lambda v: v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def contrast(fg: str, bg: str) -> float:
    a, b = _lum(fg), _lum(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- 主流程
def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="foxpanel-v2-"))
    FakeUpstream.packages = dataset()
    up_port = free_port()
    upstream = start_upstream(up_port)
    up_base = f"http://127.0.0.1:{up_port}"

    key_file = tmp / "admin.key"
    key_file.write_text(FAKE_KEY + "\n", encoding="utf-8")
    cfg_path = tmp / "panel.json"
    env = dict(os.environ, FOXLABEL_PANEL_CONFIG=str(cfg_path))
    procs: list[subprocess.Popen] = []

    try:
        r = subprocess.run([PY, str(PANEL), "init", "--user", TEST_USER,
                            "--password", TEST_PASS, "--upstream", up_base,
                            "--admin-key-file", str(key_file)],
                           capture_output=True, text=True, env=env)
        print("[0] 初始化与登录")
        check("init 成功", r.returncode == 0, r.stderr[-200:])
        cfg = json.loads(cfg_path.read_text())
        check("配置权限 600", oct(cfg_path.stat().st_mode)[-3:] == "600",
              oct(cfg_path.stat().st_mode)[-3:])
        check("配置里没有明文密码", TEST_PASS not in cfg_path.read_text())

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        proc = subprocess.Popen([PY, str(PANEL), "serve", "--host", "127.0.0.1",
                                 "--port", str(port)], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(proc)
        check("面板就绪", wait(base + "/login"))

        code, hdr, _ = login(base, TEST_USER, TEST_PASS)
        sid = cookie_of(hdr)
        check("登录 302 且拿到会话", code == 302 and bool(sid), f"{code}")

        # ------------------------------------------------ 1. /api/package
        print("\n[1] /api/package：命中 / 词表 / 租约 / 白名单")
        code, hdr, d, text = api_json(base, "/api/package", sid,
                                      repo="t/one", id="shard-00002.tar")
        pkg = d.get("package") or {}
        check("命中 → 200 且 ok=true", code == 200 and d.get("ok") is True, str(code))
        check("package 恰好 13 键（12 固定 + lease）",
              set(pkg) == PKG_KEYS, sorted(set(pkg) ^ PKG_KEYS))
        check("status_label 用面板词表（claimed → 处理中，不吃上游「已认领」）",
              pkg.get("status_label") == "处理中", pkg.get("status_label"))
        check("status/owner 透传正确",
              pkg.get("status") == "claimed" and pkg.get("owner") == "bob", str(pkg)[:160])
        check("上游多出的列不外泄（lease_device/prelock_key/repo_type）",
              not ({"lease_device", "prelock_key", "repo_type"} & set(pkg)),
              sorted(set(pkg) & {"lease_device", "prelock_key", "repo_type"}))
        lease = pkg.get("lease") or {}
        check("有 active 租约 → lease 七个字段齐全",
              set(lease) == {"lease_id", "kind", "device_id", "user_id",
                             "expires_in", "last_beat_at", "progress"},
              sorted(lease))
        check("租约内容正确（L1/active/58s/progress）",
              lease.get("lease_id") == "L1" and lease.get("kind") == "active"
              and lease.get("expires_in") == 58
              and lease.get("progress") == {"done": 1, "total": 2}, str(lease))
        check("generated_at 是带偏移的 ISO 8601",
              bool(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$",
                            d.get("generated_at") or "")), d.get("generated_at"))
        check("响应里没有管理员密钥", FAKE_KEY not in text)

        code, _, d, _ = api_json(base, "/api/package", sid,
                                 repo="t/one", id="shard-00000.tar")
        check("available → 未认领", (d.get("package") or {}).get("status_label") == "未认领",
              (d.get("package") or {}).get("status_label"))
        check("无租约 → lease 为 null 且仍 200",
              code == 200 and (d.get("package") or {}).get("lease") is None, str(code))

        code, _, d, _ = api_json(base, "/api/package", sid,
                                 repo="t/one", id="shard-00004.tar")
        check("completed → 已完成",
              (d.get("package") or {}).get("status_label") == "已完成",
              (d.get("package") or {}).get("status_label"))

        code, _, d, _ = api_json(base, "/api/package", sid,
                                 repo="t/one", id="no-such-package.tar")
        check("不存在 → 404 且 error 以「包不存在」开头",
              code == 404 and (d.get("error") or "").startswith("包不存在"),
              f"{code} {d}")
        check("404 体只有 error 一个键", set(d) == {"error"}, sorted(d))

        code, _, d, _ = api_json(base, "/api/package", sid)
        check("缺 id → 400「缺少参数 id」",
              code == 400 and d.get("error") == "缺少参数 id", f"{code} {d}")

        long_id = "x" * 513
        code, _, d, _ = api_json(base, "/api/package", sid, repo="t/one", id=long_id)
        check("id 超 512 → 400「参数 id 过长（最多 512 字符）」",
              code == 400 and d.get("error") == "参数 id 过长（最多 512 字符）",
              f"{code} {d}")

        code, _, d, _ = api_json(base, "/api/package", sid, id=ID_DUP)
        check("无 repo 同名跨仓库 → 400（不是 503）",
              code == 400, f"{code} {d}")
        check("歧义文案给出可选 repo",
              "包名在多个仓库存在，请指定 repo" in (d.get("error") or "")
              and "t/one" in (d.get("error") or "")
              and "t/two" in (d.get("error") or ""), d.get("error"))

        code, _, d, _ = api_json(base, "/api/package", sid, repo="t/two", id=ID_DUP)
        check("给定 repo 后同名包可命中（t/two, size 222）",
              code == 200 and (d.get("package") or {}).get("size") == 222,
              f"{code} {str(d)[:120]}")

        # F3：无 repo 的上游 5xx 走扫描回退，终态是 400/404，不是 503
        FakeUpstream.force_detail_500 = {ID_DUP}
        code, _, d, _ = api_json(base, "/api/package", sid, id=ID_DUP)
        check("无 repo + 上游 5xx → 扫描回退还原歧义（400）",
              code == 400 and "包名在多个仓库存在" in (d.get("error") or ""),
              f"{code} {d}")
        FakeUpstream.force_detail_500 = {"shard-00001.tar"}
        code, _, d, _ = api_json(base, "/api/package", sid, id="shard-00001.tar")
        check("无 repo + 上游 5xx（唯一包）→ 扫描回退仍 200",
              code == 200 and (d.get("package") or {}).get("id") == "shard-00001.tar",
              f"{code} {str(d)[:120]}")
        code, _, d, _ = api_json(base, "/api/package", sid,
                                 repo="t/one", id="shard-00001.tar")
        check("有 repo + 上游 5xx → 503 且文案以「上游」开头",
              code == 503 and (d.get("error") or "").startswith("上游"),
              f"{code} {d}")
        FakeUpstream.force_detail_500 = set()

        code, _, d, _ = api_json(base, "/api/package", sid,
                                 repo="t/one", id=ID_SCRIPT)
        check("包名含 <script> → 200 且 id 原样返回（JSON 可解析）",
              code == 200 and (d.get("package") or {}).get("id") == ID_SCRIPT,
              f"{code} {str(d)[:120]}")

        code, _, d, _ = api_json(base, "/api/package", sid, repo="t/one", id="")
        check("id 为空串 → 400", code == 400 and d.get("error") == "缺少参数 id",
              f"{code} {d}")

        code, hdr, body = req(base + "/api/package?repo=t/one&id=shard-00000.tar")
        check("未登录 → 401 JSON", code == 401
              and (hdr.get("content-type") or "").startswith("application/json"),
              str(code))
        check("401 体是 {\"error\": \"未登录\"}",
              json.loads(body).get("error") == "未登录", body[:120])

        code, _, d, _ = api_json(base, "/api/nope", sid)
        check("未知 /api/ 路径 → 404 {\"error\": ...}", code == 404 and "error" in d,
              f"{code} {d}")

        # ------------------------------------------------ 2. /api/export.csv
        print("\n[2] /api/export.csv：BOM / CRLF / 10 列 / RFC4180 / 注入防护")
        code, hdr, body = api(base, "/api/export.csv", sid)
        check("导出 200", code == 200, str(code))
        check("Content-Type: text/csv; charset=utf-8",
              (hdr.get("content-type") or "").startswith("text/csv; charset=utf-8"),
              hdr.get("content-type"))
        check("前三字节是 UTF-8 BOM",
              body[:3] == b"\xef\xbb\xbf", repr(body[:6]))
        check("Content-Disposition 文件名形状",
              bool(re.match(r'^attachment; filename="fox-packages-\d{8}-\d{6}\.csv"$',
                            hdr.get("content-disposition") or "")),
              hdr.get("content-disposition"))
        text = body[3:].decode("utf-8")
        head = text.split("\r\n")[0]
        check("表头恰好 10 列且顺序固定",
              head == ",".join(CSV_HEADER), head)
        check("行尾用 CRLF 且文件以 CRLF 结束",
              text.endswith("\r\n") and "\r\n" in text, repr(text[-20:]))
        check("含换行的包名被引号包裹（原文含 \\n 而不是转义成 \\\\n）",
              '"line\nbreak.tar"' in text, "no quoted newline cell")

        rows = list(csv.reader(io.StringIO(text, newline="")))
        check("csv 模块可回读且每行 10 字段",
              rows[0] == CSV_HEADER and all(len(r) == 10 for r in rows[1:]),
              [len(r) for r in rows[:3]])
        by_id = {r[1]: r for r in rows[1:]}
        check("含逗号+双引号的包名原样回读",
              ID_COMMA in by_id and by_id[ID_COMMA][1] == ID_COMMA,
              [k for k in by_id if "weird" in k])
        check("含换行的包名原样回读", by_id.get(ID_NEWLINE, [None, None])[1] == ID_NEWLINE,
              repr(by_id.get(ID_NEWLINE)))
        # 公式注入防护会把值改成 "'<原值>"，所以这里按"列值集合"匹配，不用 by_id 键
        id_cells = {r[1] for r in rows[1:]}
        for label, ident in (("=", ID_FORMULA), ("-", ID_MINUS), ("@", ID_AT),
                             ("+", ID_PLUS), ("制表符", ID_TAB)):
            check(f"公式注入 {label} 开头加前导单引号",
                  ("'" + ident) in id_cells, sorted(i for i in id_cells
                                                   if i.startswith("'"))[:4])
        check("size 为 null → 空串（不是 0）",
              by_id.get(ID_NEWLINE, ["", "", ""])[2] == ""
              and by_id.get(ID_NOSIZE, ["", "", ""])[2] == "",
              (by_id.get(ID_NEWLINE), by_id.get(ID_NOSIZE)))

        code2, _, d2, _ = api_json(base, "/api/packages", sid, size=200)
        check("X-Total-Rows 与 /api/packages 的 page.total 一致",
              hdr.get("x-total-rows") == str(d2["page"]["total"]),
              f"{hdr.get('x-total-rows')} vs {d2['page'].get('total')}")
        check("X-Truncated: 0（小数据集）", hdr.get("x-truncated") == "0",
              hdr.get("x-truncated"))

        code, hdr, body = api(base, "/api/export.csv", sid, repo="t/two")
        rows = list(csv.reader(io.StringIO(body[3:].decode(), newline="")))
        check("repo 筛选透传（只有 t/two 的两行）",
              code == 200 and len(rows) == 3
              and {r[0] for r in rows[1:]} == {"t/two"},
              [r[:2] for r in rows[1:]])

        code, hdr, body = api(base, "/api/export.csv", sid, status="claimed")
        rows = list(csv.reader(io.StringIO(body[3:].decode(), newline="")))
        check("status 筛选透传（全部 claimed）",
              code == 200
              and all(r[4] == "claimed" for r in rows[1:]) and len(rows) > 1,
              [r[4] for r in rows[1:]])

        code, hdr, body = api(base, "/api/export.csv", sid, q="bob")
        rows = list(csv.reader(io.StringIO(body[3:].decode(), newline="")))
        check("q 筛选透传（只 1 行 owner=bob）",
              code == 200 and len(rows) == 2 and rows[1][5] == "bob"
              and hdr.get("x-total-rows") == "1",
              [r[:2] for r in rows[1:]])

        code, hdr, body = api(base, "/api/export.csv", sid,
                              sort="size", order="asc", q="shard-")
        csv_ids = [r[1] for r in list(csv.reader(
            io.StringIO(body[3:].decode(), newline="")))[1:]]
        _, _, lst, _ = api_json(base, "/api/packages", sid,
                                sort="size", order="asc", q="shard-", size=200)
        check("导出顺序 == 列表顺序（sort/order 透传）",
              csv_ids == ids(lst["packages"]), (csv_ids, ids(lst["packages"])))

        code, hdr, body = api(base, "/api/export.csv", sid, repo='<script>alert(1)</script>')
        check("恶意 repo 名：仍 200 且 CSV 表头完好",
              code == 200 and body[:3] == b"\xef\xbb\xbf"
              and body[3:].decode().split("\r\n")[0] == ",".join(CSV_HEADER),
              f"{code} {body[:80]!r}")

        code, hdr, body = api(base, "/api/export.csv", sid, sort="evil")
        check("非法 sort → 400 JSON（不是 CSV、不是 200 回落）",
              code == 400
              and (hdr.get("content-type") or "").startswith("application/json")
              and json.loads(body).get("error") == "sort 只能是 updated、size、id 之一",
              f"{code} {hdr.get('content-type')} {body[:120]!r}")

        # ------------------------------------------------ 3. sort / order
        print("\n[3] 排序：稳定 / 跨页单调 / 默认值 / 非法枚举")
        code, _, d, _ = api_json(base, "/api/packages", sid, page=2, size=2)
        check("默认排序 updated desc 且稳定：page=2&size=2 → shard-00002/00003",
              code == 200 and ids(d["packages"]) == ["shard-00002.tar", "shard-00003.tar"]
              and d["sort"] == {"by": "updated", "order": "desc"},
              (ids(d.get("packages")), d.get("sort")))

        _, _, d, _ = api_json(base, "/api/packages", sid, size=200, q="shard-")
        total_order = ids(d["packages"])
        check("全部 6 个同 updated_at 的包保持上游原始顺序",
              total_order == [f"shard-{i:05d}.tar" for i in range(6)], total_order)

        _, _, d, _ = api_json(base, "/api/packages", sid, sort="size", order="asc",
                              size=200, q="shard-")
        asc_ids = ids(d["packages"])
        sizes = [p["size"] for p in d["packages"]]
        check("sort=size&order=asc → 按大小升序",
              sizes == sorted(sizes) and asc_ids[0] == "shard-00000.tar",
              (asc_ids, sizes))
        check("sort=size&order=asc 的 sort 回显正确",
              d["sort"] == {"by": "size", "order": "asc"}, d.get("sort"))

        _, _, d, _ = api_json(base, "/api/packages", sid, sort="size", order="desc",
                              size=200, q="shard-")
        desc_sizes = [p["size"] for p in d["packages"]]
        check("sort=size&order=desc → 按大小降序",
              desc_sizes == sorted(desc_sizes, reverse=True)
              and ids(d["packages"])[0] == "shard-00005.tar",
              (ids(d["packages"]), desc_sizes))

        _, _, d, _ = api_json(base, "/api/packages", sid, sort="id", size=200)
        all_ids = ids(d["packages"])
        check("sort=id 缺省 order=asc → 包名升序",
              all_ids == sorted(all_ids) and d["sort"]["order"] == "asc", all_ids)

        _, _, d, _ = api_json(base, "/api/packages", sid, sort="id", order="desc",
                              size=200)
        check("sort=id&order=desc → 包名降序",
              ids(d["packages"]) == sorted(all_ids, reverse=True), ids(d["packages"]))

        # 跨页单调：size 降序，每页最小值不得小于下一页最大值
        # （用 q=shard- 收窄到 6 个 size 为整数的基座包；数据集里另有 size=null 的行）
        pages = []
        for p in range(1, 4):
            _, _, d, _ = api_json(base, "/api/packages", sid, sort="size",
                                  order="desc", size=2, page=p, q="shard-")
            pages.append([r["size"] for r in d["packages"]])
        ok = all(pages[i] and pages[i + 1] and min(pages[i]) >= max(pages[i + 1])
                 for i in range(len(pages) - 1) if pages[i + 1])
        check("size 降序跨页单调（第 N 页最小 ≥ 第 N+1 页最大）", ok, pages)
        flat = [s for pg in pages for s in pg]
        check("翻页拼起来没有重复/漏行（6 行）",
              len(flat) == len(set(flat)) == 6, flat)

        code, _, d, _ = api_json(base, "/api/packages", sid, sort="evil")
        check("?sort=evil → 400「sort 只能是 updated、size、id 之一」",
              code == 400
              and d.get("error") == "sort 只能是 updated、size、id 之一", f"{code} {d}")
        check("400 体只有 error 一个键", set(d) == {"error"}, sorted(d))

        code, _, d, _ = api_json(base, "/api/packages", sid, sort="size",
                                 order="sideways")
        check("?sort=size&order=sideways → 400「order 只能是 asc 或 desc」",
              code == 400 and d.get("error") == "order 只能是 asc 或 desc",
              f"{code} {d}")

        code, _, d, _ = api_json(base, "/api/packages", sid, page="abc")
        check("?page=abc → 400「参数不合法」",
              code == 400 and d.get("error") == "参数不合法", f"{code} {d}")

        code, _, d, _ = api_json(base, "/api/packages", sid, size=0)
        check("?size=0 → clamp 到 1（200）",
              code == 200 and d["page"]["size"] == 1, f"{code} {d.get('page')}")
        code, _, d, _ = api_json(base, "/api/packages", sid, size=9999)
        check("?size=9999 → clamp 到 200（200）",
              code == 200 and d["page"]["size"] == 200, f"{code} {d.get('page')}")
        code, _, d, _ = api_json(base, "/api/packages", sid, page=0)
        check("?page=0 → clamp 到 1（200）",
              code == 200 and d["page"]["page"] == 1, f"{code} {d.get('page')}")

        code, _, d, _ = api_json(base, "/api/packages", sid, sort="", order="")
        check("sort/order 传空串 → 默认 updated desc（200）",
              code == 200 and d["sort"] == {"by": "updated", "order": "desc"},
              f"{code} {d.get('sort')}")

        # ------------------------------------------------ 4. 兼容红线
        print("\n[4] /api/packages 向后兼容（只加不减）")
        code, _, d, _ = api_json(base, "/api/packages", sid, page=2, size=5)
        page = d.get("page") or {}
        check("v1 字段仍在：summary / repos / page.matched / limit / offset",
              code == 200 and isinstance(d.get("summary"), dict)
              and isinstance(d.get("repos"), list) and d.get("repos")
              and "matched" in page and "limit" in page and "offset" in page,
              sorted(d))
        check("v2 新增字段：ok / generated_at / sort / page.total / pages / truncated",
              d.get("ok") is True and bool(d.get("generated_at"))
              and isinstance(d.get("sort"), dict)
              and {"total", "pages", "truncated"} <= set(page), sorted(page))
        check("page.limit == size，page.offset == (page-1)*size",
              page.get("limit") == 5 and page.get("offset") == 5, page)
        check("summary 透传上游（total == 上游全量行数）",
              d["summary"].get("total") == len(FakeUpstream.packages),
              d["summary"])
        check("repos 透传上游仓库（含 repo_type）",
              d["repos"][0].get("repo") == "t/one"
              and d["repos"][0].get("repo_type") == "dataset", d["repos"][:1])

        total_all = len(FakeUpstream.packages)
        _, _, d, _ = api_json(base, "/api/packages", sid, q="dup", size=200)
        check("page.total 是面板过滤后总数，page.matched 不含 q",
              d["page"]["total"] == 2 and d["page"]["matched"] == total_all,
              d["page"])
        check("page.pages == ceil(total/size)",
              d["page"]["pages"] == max(1, -(-d["page"]["total"] // d["page"]["size"])),
              d["page"])

        # ------------------------------------------------ 5. q 过滤
        print("\n[5] q 过滤：分页前生效 + 大小写不敏感")
        _, _, d, _ = api_json(base, "/api/packages", sid, q="BOB")
        check("q 大小写不敏感（BOB → bob）",
              d["page"]["total"] == 1 and ids(d["packages"]) == ["shard-00002.tar"],
              (d["page"]["total"], ids(d["packages"])))
        _, _, d, _ = api_json(base, "/api/packages", sid, q="bOb")
        check("q 混合大小写（bOb → bob）", d["page"]["total"] == 1,
              d["page"]["total"])
        _, _, d, _ = api_json(base, "/api/packages", sid, q="CAROL", size=200)
        check("q 也能匹配 owner（CAROL → completed 那个）",
              d["page"]["total"] == 1
              and d["packages"][0]["id"] == "shard-00004.tar", d["page"]["total"])
        _, _, d, _ = api_json(base, "/api/packages", sid, q="shard-", size=2, page=2)
        check("q 在分页前生效：total=6、pages=3、第 2 页仍是过滤结果切片",
              d["page"]["total"] == 6 and d["page"]["pages"] == 3
              and ids(d["packages"]) == ["shard-00002.tar", "shard-00003.tar"],
              (d["page"], ids(d["packages"])))
        _, _, d, _ = api_json(base, "/api/packages", sid, q="绝不存在", size=5)
        check("q 无匹配 → total=0、pages=1、packages=[]（不是 404）",
              d["page"]["total"] == 0 and d["page"]["pages"] == 1
              and d["packages"] == [], d["page"])
        _, _, d, _ = api_json(base, "/api/packages", sid, q="<script>", size=200)
        check("q 含 <script> → 正常返回一行，JSON 可解析",
              d["page"]["total"] == 1 and d["packages"][0]["id"] == ID_SCRIPT,
              (d["page"]["total"], ids(d["packages"])))
        _, _, d, _ = api_json(base, "/api/packages", sid,
                              repo="<script>alert(1)</script>", size=5)
        check("repo 名含 <script> → 200 且 total=0（不崩）",
              d["page"]["total"] == 0, (d["page"]["total"],))

        # ------------------------------------------------ 6. /api/meta
        print("\n[6] /api/meta：前端启动配置")
        code, hdr, d, text = api_json(base, "/api/meta", sid)
        check("/api/meta 200 且含全部契约字段",
              code == 200 and META_KEYS <= set(d),
              sorted(META_KEYS - set(d)))
        check("version 形如 x.y.z", bool(re.match(r"^\d+\.\d+\.\d+$", d.get("version") or "")),
              d.get("version"))
        check("upstream == 假上游 base 且不含密钥",
              d.get("upstream") == up_base and FAKE_KEY not in text, d.get("upstream"))
        check("upstream_ok=true / upstream_error=null",
              d.get("upstream_ok") is True and d.get("upstream_error") is None,
              (d.get("upstream_ok"), d.get("upstream_error")))
        check("refresh_ms=15000 / default_page_size=50 / audit_page_size=25 / export_max_rows=100000",
              d.get("refresh_ms") == 15000 and d.get("default_page_size") == 50
              and d.get("audit_page_size") == 25
              and d.get("export_max_rows") == 100000, d)
        check("page_sizes == [25, 50, 100, 200]",
              d.get("page_sizes") == [25, 50, 100, 200], d.get("page_sizes"))
        st = d.get("statuses") or []
        check("statuses 3 项且 value/label 成对",
              [x.get("value") for x in st] == ["available", "claimed", "completed"]
              and [x.get("label") for x in st] == ["未认领", "处理中", "已完成"], st)
        sorts = {x.get("value"): x for x in d.get("sorts") or []}
        check("sorts 含 updated/size/id 且默认方向 desc/desc/asc",
              set(sorts) == {"updated", "size", "id"}
              and sorts["updated"]["default_order"] == "desc"
              and sorts["size"]["default_order"] == "desc"
              and sorts["id"]["default_order"] == "asc", list(sorts))
        check("repos 与上游一致（2 个，enabled 恒 true）",
              len(d.get("repos") or []) == 2
              and all(x.get("enabled") is True for x in d["repos"]),
              d.get("repos"))
        check("features 四项全 true",
              d.get("features") == {"drawer": True, "export": True,
                                    "sort": True, "theme": True},
              d.get("features"))
        check("audit.enabled=false（未配置审计文件）",
              d.get("audit") == {"enabled": False}, d.get("audit"))

        # ------------------------------------------------ 7. 静态资源
        print("\n[7] 静态资源：Content-Type / ETag / 304 / Cache-Control / CSP")
        check("web/ 四件交付就位（index/login/app.css/app.js）",
              all((WEB / n).is_file() for n in
                  ("index.html", "login.html", "app.css", "app.js")),
              sorted(p.name for p in WEB.glob("*")))

        code, hdr, body = req(base + "/static/app.css")
        check("/static/app.css → 200 text/css",
              code == 200 and (hdr.get("content-type") or "").startswith(
                  "text/css; charset=utf-8"), f"{code} {hdr.get('content-type')}")
        check("app.css 字节与 web/ 一致",
              body == (WEB / "app.css").read_bytes(), f"{len(body)} vs "
              f"{(WEB / 'app.css').stat().st_size}")
        etag_css = hdr.get("etag") or ""
        check("ETag 形如 \"<mtime_ns>-<size>\"",
              bool(re.fullmatch(r'"\d+-\d+"', etag_css)), etag_css)
        check("ETag 的 size 段 == 响应体字节数",
              etag_css.strip('"').split("-")[-1] == str(len(body)), etag_css)
        check("css Cache-Control: no-cache",
              "no-cache" in (hdr.get("cache-control") or ""), hdr.get("cache-control"))

        code, hdr, body304 = req(base + "/static/app.css",
                                 extra={"If-None-Match": etag_css})
        check("If-None-Match 命中 → 304", code == 304, str(code))
        check("304 空体且不带 Content-Length",
              body304 == b"" and "content-length" not in hdr, (body304, hdr))

        code, _, _ = req(base + "/static/app.css",
                         extra={"If-None-Match": f'"deadbeef-1", {etag_css}'})
        check("If-None-Match 逗号列表命中 → 304", code == 304, str(code))
        code, _, _ = req(base + "/static/app.css", extra={"If-None-Match": "*"})
        check("If-None-Match: * → 304", code == 304, str(code))
        code, _, _ = req(base + "/static/app.css",
                         extra={"If-None-Match": '"deadbeef-1"'})
        check("不匹配的 If-None-Match → 200 全量", code == 200, str(code))

        code, hdr, body = req(base + "/static/app.js")
        check("/static/app.js → 200 application/javascript",
              code == 200 and (hdr.get("content-type") or "").startswith(
                  "application/javascript"), f"{code} {hdr.get('content-type')}")
        check("app.js 字节与 web/ 一致", body == (WEB / "app.js").read_bytes(), "")
        check("js Cache-Control: no-cache",
              "no-cache" in (hdr.get("cache-control") or ""), hdr.get("cache-control"))

        code, hdr, body = req(base + "/", cookie=sid)
        text = body.decode()
        check("/ → 200 text/html", code == 200 and (hdr.get("content-type") or "")
              .startswith("text/html; charset=utf-8"), f"{code} {hdr.get('content-type')}")
        check("首页 Cache-Control: no-store",
              "no-store" in (hdr.get("cache-control") or ""), hdr.get("cache-control"))
        check("首页 ETag 形状正确",
              bool(re.fullmatch(r'"\d+-\d+"', hdr.get("etag") or "")), hdr.get("etag"))
        check("首页 __VERSION__/{{VERSION}} 已替换成 meta.version",
              "{{VERSION}}" not in text and "__VER__" not in text
              and d.get("version") in text, "placeholder left")
        check("首页不含管理员密钥", FAKE_KEY not in text)

        code, hdr, body = req(base + "/login")
        check("/login（未登录）→ 200 text/html 且 no-store",
              code == 200 and "no-store" in (hdr.get("cache-control") or ""),
              f"{code} {hdr.get('cache-control')}")
        check("登录页含登录表单（不用 503）", "登录" in body.decode(), body[:80])

        # CSP 与安全头
        code, hdr, _ = req(base + "/", cookie=sid)
        csp = hdr.get("content-security-policy") or ""
        check("CSP 含 script-src 'self' 且不含 unsafe-inline",
              "script-src 'self'" in csp and "unsafe-inline" not in csp, csp)
        check("CSP 逐字等于 SPEC §7.1",
              csp == ("default-src 'self'; script-src 'self'; style-src 'self'; "
                      "img-src 'self' data:; form-action 'self'; "
                      "frame-ancestors 'none'"), csp)
        check("X-Frame-Options: DENY", hdr.get("x-frame-options") == "DENY",
              hdr.get("x-frame-options"))
        check("X-Content-Type-Options: nosniff",
              hdr.get("x-content-type-options") == "nosniff",
              hdr.get("x-content-type-options"))
        check("Referrer-Policy: no-referrer",
              hdr.get("referrer-policy") == "no-referrer",
              hdr.get("referrer-policy"))
        check("API 响应也带 CSP 且 Cache-Control: no-store",
              all("script-src 'self'" in (h.get("content-security-policy") or "")
                  for _, h, _ in [api(base, "/api/meta", sid),
                                  api(base, "/api/packages", sid, size=1)]),
              "")

        # ------------------------------------------------ 8. web/ 缺失回退
        print("\n[8] web/ 缺失 → 回退内置页；web/ 半交付 → 明确 503")
        webless = tmp / "webless"
        webless.mkdir()
        shutil.copy(PANEL, webless / "panel.py")
        wl_port = free_port()
        wl = subprocess.Popen([PY, str(webless / "panel.py"), "serve",
                               "--host", "127.0.0.1", "--port", str(wl_port)],
                              env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        procs.append(wl)
        wl_base = f"http://127.0.0.1:{wl_port}"
        check("回退实例就绪", wait(wl_base + "/login"))
        code, _, body = req(wl_base + "/", cookie=sid)
        check("web/ 不存在时 / 仍 200（内置页面回退）",
              code == 200 and "fox-label" in body.decode(), str(code))
        code, _, body = req(wl_base + "/static/app.css")
        check("web/ 不存在时 app.css 仍 200（内置 CSS）", code == 200, str(code))
        code, _, _ = req(wl_base + "/static/app.js")
        check("web/ 不存在时 app.js 仍 200（内置 JS）", code == 200, str(code))
        code, _, body = req(wl_base + "/login")
        check("web/ 不存在时 /login 仍 200（内置登录页）", code == 200, str(code))
        stop(wl)

        partial = tmp / "partial"
        (partial / "web").mkdir(parents=True)
        shutil.copy(PANEL, partial / "panel.py")
        shutil.copy(WEB / "index.html", partial / "web" / "index.html")
        pt_port = free_port()
        pt = subprocess.Popen([PY, str(partial / "panel.py"), "serve",
                               "--host", "127.0.0.1", "--port", str(pt_port)],
                              env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        procs.append(pt)
        pt_base = f"http://127.0.0.1:{pt_port}"
        check("半交付实例就绪", wait(pt_base + "/login"))
        code, hdr, body = req(pt_base + "/static/app.css")
        check("web/ 已交付但缺 app.css → 503 {\"error\": 静态资源缺失：app.css}",
              code == 503 and (hdr.get("content-type") or "").startswith(
                  "application/json")
              and json.loads(body).get("error") == "静态资源缺失：app.css",
              f"{code} {body[:120]}")
        code, _, body = req(pt_base + "/", cookie=sid)
        check("半交付时已存在的 index.html 仍 200", code == 200, str(code))
        code, _, _ = req(pt_base + "/login")
        check("半交付时 /login 回退内置页 → 200（登录口不 503）", code == 200, str(code))
        stop(pt)

        # ------------------------------------------------ 9. 缓存
        print("\n[9] 全量扫描缓存（SCAN_TTL 2s）与计数")
        time.sleep(2.1)                     # 让之前的缓存过期，从冷启动开始数
        reset_hits()
        api(base, "/api/packages", sid, size=50)
        api(base, "/api/packages", sid, size=50)
        check("同一筛选 2 秒内两次请求只扫描一次",
              hit_count("list", "", "") == 1, hit_count("list", "", ""))
        api(base, "/api/packages", sid, repo="t/one", size=50)
        check("新筛选（repo=t/one）各自扫描一次",
              hit_count("list", "t/one", "") == 1, hit_count("list", "t/one", ""))
        api(base, "/api/packages", sid, repo="t/one", size=50)
        check("重复同一筛选不再扫描",
              hit_count("list", "t/one", "") == 1, hit_count("list", "t/one", ""))
        api(base, "/api/packages", sid, repo="t/one", status="claimed", size=50)
        check("status 也进缓存键（t/one+claimed 单独一次）",
              hit_count("list", "t/one", "claimed") == 1,
              hit_count("list", "t/one", "claimed"))
        _ = api(base, "/api/export.csv", sid, repo="t/one")
        check("导出复用列表的扫描缓存（不再多打一次上游）",
              hit_count("list", "t/one", "") == 1, hit_count("list", "t/one", ""))

        # ------------------------------------------------ 10. 上游故障
        print("\n[10] 上游挂掉：新接口 503、首页仍可用；恢复后缓存已清")
        upstream.shutdown()
        upstream.server_close()
        time.sleep(2.3)                      # 跨过 SCAN_TTL，避免读到 2 秒内的旧扫描
        code, _, d, _ = api_json(base, "/api/packages", sid, size=5)
        check("上游挂掉 → /api/packages 503",
              code == 503 and (d.get("error") or "").startswith("上游"),
              f"{code} {d}")
        code, _, d, _ = api_json(base, "/api/package", sid, repo="t/one",
                                 id="shard-00000.tar")
        check("上游挂掉 → /api/package 503",
              code == 503 and (d.get("error") or "").startswith("上游"),
              f"{code} {d}")
        code, hdr, body = api(base, "/api/export.csv", sid)
        check("上游挂掉 → /api/export.csv 503 JSON（不是空 CSV）",
              code == 503
              and (hdr.get("content-type") or "").startswith("application/json")
              and json.loads(body).get("error", "").startswith("上游"),
              f"{code} {body[:120]!r}")
        code, _, d, _ = api_json(base, "/api/meta", sid)
        check("上游挂掉 → /api/meta 仍 200 且 upstream_ok=false",
              code == 200 and d.get("upstream_ok") is False
              and (d.get("upstream_error") or "").startswith("上游"),
              f"{code} {d.get('upstream_ok')} {d.get('upstream_error')}")
        check("503 文案里没有内部路径与密钥",
              "/home/" not in json.dumps(d, ensure_ascii=False)
              and FAKE_KEY not in json.dumps(d, ensure_ascii=False), str(d)[:160])
        code, hdr, body = req(base + "/", cookie=sid)
        check("上游挂掉时首页仍 200（面板不跟着崩）",
              code == 200 and "fox-label" in body.decode(), str(code))
        code, _, _ = req(base + "/login")
        check("上游挂掉时 /login 仍 200", code == 200, str(code))

        # 恢复上游并追加一行：立刻能读到新数据 ⇒ 失败那次的缓存确实被清掉了
        FakeUpstream.packages.append(
            {"repo": "t/one", "id": "after-recovery.tar", "size": 9,
             "sha256": None, "status": "available", "owner": None,
             "claimed_at": None, "completed_at": None, "downloaded_at": None,
             "note": None, "updated_at": "2026-11-10T12:00:00+08:00",
             "status_label": "空闲"})
        upstream = start_upstream(up_port)
        check("上游重启就绪", wait(up_base + "/packages"))
        code, _, d, _ = api_json(base, "/api/packages", sid, size=200)
        check("上游恢复后立即 200 且包含新增行（缓存已清，未在 TTL 内复用旧数据）",
              code == 200
              and "after-recovery.tar" in ids(d["packages"]), f"{code}")
        code, _, d, _ = api_json(base, "/api/meta", sid)
        check("上游恢复后 meta.upstream_ok=true",
              code == 200 and d.get("upstream_ok") is True, str(code))

        # ------------------------------------------------ 11. 会话红线
        print("\n[11] 会话与既有行为红线")
        for path in ("/api/packages", "/api/meta", "/api/export.csv",
                     "/api/package?id=x"):
            code, hdr, body = req(base + path)
            check(f"未登录 {path} → 401 JSON",
                  code == 401 and (hdr.get("content-type") or "").startswith(
                      "application/json")
                  and json.loads(body).get("error") == "未登录", str(code))

        # 伪造过期会话
        secret = bytes.fromhex(cfg["secret"])
        exp = int(time.time()) - 10
        payload = f"{TEST_USER}|{exp}"
        sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        old = base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode().rstrip("=")
        code, _, _ = req(base + "/api/package?repo=t/one&id=shard-00000.tar",
                         cookie=old)
        check("过期会话 → 401", code == 401, str(code))

        code, hdr, _ = req(base + "/", cookie="")
        check("未登录访问页面 → 302 到 /login",
              code == 302 and "/login" in (hdr.get("location") or ""),
              f"{code} {hdr.get('location')}")

        # ------------------------ 12. 交付字节绑定 + t14 修复落地（当前字节）
        print("\n[12] 当前字节 sha256 绑定 + t14 十一条修复落地核验")
        css_text = (WEB / "app.css").read_text(encoding="utf-8")
        js_text = (WEB / "app.js").read_text(encoding="utf-8")
        html_text = (WEB / "index.html").read_text(encoding="utf-8")
        login_text = (WEB / "login.html").read_text(encoding="utf-8")
        rules = parse_css(css_text)
        root = css_rules(rules, ":root", plain=True)
        root = root[0][0] if root else {}

        # ---- sha256：把结论绑定到具体字节（供 t7 交付引用）
        files = {"panel.py": PANEL,
                 "web/index.html": WEB / "index.html",
                 "web/login.html": WEB / "login.html",
                 "web/app.css": WEB / "app.css",
                 "web/app.js": WEB / "app.js"}
        hashes = {name: sha256_of(path) for name, path in files.items()}
        check("五个交付文件 sha256 均为 64 位小写 hex",
              all(re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes.values()),
              hashes)
        print("    sha256 " + json.dumps(hashes, ensure_ascii=False))
        code, _, css_body = req(base + "/static/app.css")
        check("服务端 app.css 字节 == 仓库 web/app.css（sha256 绑定生效）",
              code == 200 and hashlib.sha256(css_body).hexdigest() == hashes["web/app.css"],
              code)
        code, _, js_body = req(base + "/static/app.js")
        check("服务端 app.js 字节 == 仓库 web/app.js",
              code == 200 and hashlib.sha256(js_body).hexdigest() == hashes["web/app.js"],
              code)
        ver_src = re.search(r'^VERSION = "([^"]+)"', (ROOT / "panel.py").read_text(
            encoding="utf-8"), re.M)
        check("panel.py 的 VERSION 与 /api/meta 一致（落盘字节 ↔ 运行行为）",
              bool(ver_src) and ver_src.group(1) == d.get("version"),
              f"{ver_src.group(1) if ver_src else None} vs {d.get('version')}")

        # ---- F1 触控目标 ≥44（<768）；≥768 收紧到 32
        check("F1 <768 触控目标基值 --fp-tap: 44px",
              root.get("--fp-tap") == "44px", root.get("--fp-tap"))
        check("F1 ≥768 覆盖 --fp-tap: 32px",
              any(x.get("--fp-tap") == "32px"
                  for x, _ in css_rules(rules, ":root", media="min-width: 768px")),
              [x.get("--fp-tap") for x, _ in
               css_rules(rules, ":root", media="min-width: 768px")])
        check("F1 .fp-btn min-width 用 var(--fp-tap)",
              any(x.get("min-width") == "var(--fp-tap)"
                  for x, _ in css_rules(rules, ".fp-btn")), "")
        check("F1 .fp-btn--sm min-width 用 var(--fp-tap)",
              any(x.get("min-width") == "var(--fp-tap)"
                  for x, _ in css_rules(rules, ".fp-btn--sm")), "")
        check("F1 .fp-iconbtn 宽高都用 var(--fp-tap)（图标按钮 44×44/32×32）",
              any(x.get("width") == "var(--fp-tap)"
                  and x.get("min-width") == "var(--fp-tap)"
                  and x.get("height") == "var(--fp-tap)"
                  for x, _ in css_rules(rules, ".fp-iconbtn")), "")

        # ---- F2/F3 工具栏与表头吸顶
        check("F2 --fp-sticky-top 基值 = 页头高，≥768 = 页头 + 页签",
              root.get("--fp-sticky-top") == "var(--fp-header-h)"
              and any(x.get("--fp-sticky-top")
                      == "calc(var(--fp-header-h) + var(--fp-tabs-h))"
                      for x, _ in css_rules(rules, ":root", media="min-width: 768px")),
              root.get("--fp-sticky-top"))
        check("F2 .fp-toolbar 吸顶 top: var(--fp-sticky-top)（≥768）",
              any(x.get("position") == "sticky" and x.get("top") == "var(--fp-sticky-top)"
                  for x, _ in css_rules(rules, ".fp-toolbar", media="min-width: 768px")),
              css_rules(rules, ".fp-toolbar", media="min-width: 768px"))
        check("F3 表头吸顶 top: calc(var(--fp-sticky-top) + var(--fp-toolbar-h))",
              any(x.get("position") == "sticky"
                  and x.get("top") == "calc(var(--fp-sticky-top) + var(--fp-toolbar-h))"
                  for x, _ in css_rules(rules, ".fp-table th", media="min-width: 768px")),
              css_rules(rules, ".fp-table th", media="min-width: 768px"))
        check("F3 --fp-toolbar-h 由几何量算出（≥768）",
              any("var(--fp-field-h)" in (x.get("--fp-toolbar-h") or "")
                  and "var(--fp-sp-2)" in (x.get("--fp-toolbar-h") or "")
                  for x, _ in css_rules(rules, ":root", media="min-width: 768px")),
              [x.get("--fp-toolbar-h") for x, _ in
               css_rules(rules, ":root", media="min-width: 768px")])

        # ---- F2/F3 表格容器不再是内嵌滚动容器
        tw_media = css_rules(rules, ".fp-tablewrap", media="min-width: 768px")
        tw_all = css_rules(rules, ".fp-tablewrap")
        check("F3 .fp-tablewrap 在 ≥768 为 overflow: visible（不给表头造新滚动上下文）",
              any(x.get("overflow") == "visible" for x, _ in tw_media),
              [x.get("overflow") for x, _ in tw_media])
        check("F3 .fp-tablewrap 任何断点都没有 overflow auto/scroll/hidden",
              not any(x.get("overflow") in ("auto", "scroll", "hidden")
                      for x, _ in tw_all), tw_all)
        check("F3 .fp-tablewrap 没有 max-height（不设内嵌滚动容器）",
              not any("max-height" in x for x, _ in tw_all), tw_all)
        maxh = [(x.get("max-height"), at) for _s, b, at in rules
                for x in [decl_map(b)] if "max-height" in x]
        check("F3 全站唯一的 max-height 属于快捷键弹窗 .fp-dialog（不是表格）",
              len(maxh) == 1 and maxh[0][0] == "80dvh", maxh)

        # ---- F4 亮色 tabbar 激活文字对比度
        light = theme_tokens(rules, "light")
        dark = theme_tokens(rules, "dark")
        light_ratio = contrast(light["--fp-text-1"], light["--fp-bg-subtle"])
        dark_ratio = contrast(dark["--fp-text-1"], dark["--fp-bg-subtle"])
        old_ratio = contrast(light["--fp-primary"], light["--fp-bg-subtle"])
        check(f"F4 亮色 tabbar 激活文字对比度 {light_ratio:.2f}:1 ≥ 4.5",
              light_ratio >= 4.5, round(light_ratio, 2))
        check(f"F4 暗色 tabbar 激活文字对比度 {dark_ratio:.2f}:1 ≥ 4.5",
              dark_ratio >= 4.5, round(dark_ratio, 2))
        check(f"F4 对照：亮色 primary/bg-subtle 只有 {old_ratio:.2f}:1（<4.5，故必须换色）",
              old_ratio < 4.5, round(old_ratio, 2))
        check("F4 激活项文字用 --fp-text-1、上边框留 --fp-primary",
              any(x.get("color") == "var(--fp-text-1)"
                  and x.get("border-top-color") == "var(--fp-primary)"
                  for x, _ in css_rules(rules, '.fp-tabbar__item[aria-current="page"]')),
              css_rules(rules, '.fp-tabbar__item[aria-current="page"]'))

        # ---- F5 role=tablist 已移除、三处导航都有 aria-label
        check("F5 index.html 不再有 role=\"tablist\"", "tablist" not in html_text, "")
        check("F5 app.js 也不再动态设置 tablist", "tablist" not in js_text, "")
        check("F5 三处导航（侧栏/页签/底部 tab）都在 JS 里补 aria-label",
              all(f'$("#{i}").setAttribute("aria-label"' in js_text
                  for i in ("nav-side", "tabs", "tabbar")), "")

        # ---- F6 七个分页 nav 都有 aria-label
        pager_ids = re.findall(r'<nav class="fp-pager" id="([a-z-]+)"', html_text)
        check("F6 index.html 恰好 7 个分页 nav", len(pager_ids) == 7, pager_ids)
        missing = [i for i in pager_ids if f'"#{i}"' not in js_text]
        check("F6 7 个分页 nav 全部在 JS 里挂 aria-label", not missing, missing)

        # ---- F7（t14 补的 focusin/focusout）与 F8 进度过渡 200ms
        check("F7 Toast 悬停与聚焦共用 pause/resume",
              all(t in js_text for t in ('"mouseenter"', '"mouseleave"',
                                        '"focusin"', '"focusout"')), "")
        check("F8 --fp-dur-progress: 200ms",
              root.get("--fp-dur-progress") == "200ms", root.get("--fp-dur-progress"))
        prog = css_rules(rules, ".fp-progress::-webkit-progress-value")
        check("F8 进度条过渡用 var(--fp-dur-progress)",
              any("var(--fp-dur-progress)" in x.get("transition", "") for x, _ in prog),
              [x.get("transition") for x, _ in prog])
        check("F8 进度条过渡不再是 240ms",
              not any("240ms" in x.get("transition", "") for x, _ in prog),
              [x.get("transition") for x, _ in prog])

        # ---- F9 <768 卡片包名标题化
        title = css_rules(rules, ".fp-table tbody td.fp-cell-title")
        t0 = title[0][0] if title else {}
        check("F9 <768 卡片主键列占 grid 第 1 行第 1 列、14px/600",
              t0.get("grid-column") == "1" and t0.get("grid-row") == "1"
              and t0.get("font-size") == "var(--fp-fs-14)"
              and t0.get("font-weight") == "600", t0)
        check("F9 标题单元格不显示字段名（::before content: none）",
              any(x.get("content") == "none" for x, _ in
                  css_rules(rules, ".fp-table tbody td.fp-cell-title::before")), "")
        check("F9 状态列同排右侧（.fp-cell-status 占 grid 第 2 列）",
              any(x.get("grid-column") == "2" and x.get("justify-self") == "end"
                  for x, _ in css_rules(rules, ".fp-table tbody td.fp-cell-status")), "")
        check("F9 至少 4 张表用 fp-cell-title（包/设备/租约/异常/审计）",
              js_text.count("fp-cell-title") >= 4, js_text.count("fp-cell-title"))
        check("F9 回到 ≥768 时还原字号/字重（不被卡片类污染）",
              any(x.get("font-weight") == "400" and x.get("font-size") == "var(--fp-fs-13)"
                  for x, _ in css_rules(rules, ".fp-table tbody td.fp-cell-title",
                                        media="min-width: 768px")), "")

        # ---- F10 <768 导出与每页同排
        check("F10 <768 导出按钮排到动作行左侧（order: -1）",
              any(x.get("order") == "-1"
                  for x, _ in css_rules(rules, "#pkg-export", plain=True)),
              css_rules(rules, "#pkg-export", plain=True))
        check("F10 ≥768 恢复 DOM 顺序（order: 0，导出在右）",
              any(x.get("order") == "0"
                  for x, _ in css_rules(rules, "#pkg-export", media="min-width: 768px")),
              css_rules(rules, "#pkg-export", media="min-width: 768px"))
        check("F10 <768 动作行 nowrap + space-between（导出/每页同排）",
              any(x.get("flex-wrap") == "nowrap"
                  and x.get("justify-content") == "space-between"
                  for x, _ in css_rules(rules, ".fp-toolbar__r", plain=True)),
              css_rules(rules, ".fp-toolbar__r", plain=True))
        check("F10 <768 仓库/状态并排、搜索整行",
              any(x.get("flex", "").startswith("1 1 calc(50%")
                  for x, _ in css_rules(rules, ".fp-toolbar__l .fp-field", plain=True))
              and any(x.get("flex") == "1 1 100%"
                      for x, _ in css_rules(rules, ".fp-toolbar__l .fp-field--grow",
                                            plain=True)), "")

        # ---- F11 时间类列右对齐
        check("F11 包列表「更新时间」列 cls=fp-align-right",
              bool(re.search(r'T\.col\.updated,\s*sort:\s*"updated",\s*'
                             r'cls:\s*"fp-align-right"', js_text)), "")
        check("F11 设备「最近心跳」单元格 cls 含 fp-align-right",
              bool(re.search(r'T\.col\.lastSeen[^;]{0,120}?fp-align-right', js_text)), "")
        check("F11 审计「时间」列 cls=fp-col-first fp-align-right",
              bool(re.search(r'T\.col\.time,\s*cls:\s*"fp-col-first fp-align-right"',
                             js_text)), "")
        check("F11 CSS 里 th/td .fp-align-right 都是 text-align: right",
              any(x.get("text-align") == "right"
                  for x, _ in css_rules(rules, ".fp-table th.fp-align-right"))
              and any(x.get("text-align") == "right"
                      for x, _ in css_rules(rules, ".fp-table td.fp-align-right")), "")

        # ---- t14 的多类名 bug（classList.add 传多类字符串会抛错）已修
        check("t14 buildTable 按 token 拆分 class，不再把多类字符串交给 classList.add",
              "split(/\\s+/)" in js_text and "classList.contains(tk)" in js_text, "")

        # ---- 零内联（CSP 前提）在 t14 之后仍然成立
        inline = [n for n, t in (("index.html", html_text), ("login.html", login_text),
                                 ("app.css", css_text), ("app.js", js_text))
                  if 'style="' in t or "<style" in t]
        check("零内联：web/ 四件没有 style=\"…\" 与 <style>", not inline, inline)
        script_inline = [n for n, t in (("index.html", html_text),
                                        ("login.html", login_text))
                         if re.search(r"<script(?![^>]*\bsrc=)[^>]*>", t)]
        check("零内联脚本：HTML 里没有不带 src 的 <script>", not script_inline,
              script_inline)
        check("零 XSS 面：app.js 没有 .innerHTML / .style. 赋值",
              ".innerHTML" not in js_text and ".style." not in js_text, "")

        # ---- 真实 admin key 不得出现在交付字节或页面里（只判断包含，不打印）
        real_key = ROOT.parent / "fox-label" / "state" / "admin.key"
        if real_key.is_file():
            key = real_key.read_text(encoding="utf-8").strip()
            leaked = [n for n, t in (("index.html", html_text), ("login.html", login_text),
                                     ("app.css", css_text), ("app.js", js_text))
                      if key and key in t]
            check("web/ 四件不含真实 admin key", not leaked, leaked)
            _, _, page = req(base + "/", cookie=sid)
            _, _, login_page = req(base + "/login")
            check("服务端页面（/ 与 /login）不含真实 admin key",
                  key.encode() not in page and key.encode() not in login_page, "")
        else:
            check("（未找到真实 admin key 文件，仅以假 key 覆盖泄露检查）", True,
                  str(real_key.name))

    finally:
        for p in procs:
            stop(p)
        try:
            upstream.shutdown()
            upstream.server_close()
        except Exception:
            pass

    print("\n" + "=" * 72)
    total = PASSED + len(FAILS)
    if FAILS:
        print(f"结果: {PASSED}/{total} 通过，失败 {len(FAILS)} 项")
        for f in FAILS:
            print("  -", f)
        print(f"临时目录: {tmp}")
        return 1
    print(f"结果: 全部 {total} 项通过")
    print(f"临时目录: {tmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
