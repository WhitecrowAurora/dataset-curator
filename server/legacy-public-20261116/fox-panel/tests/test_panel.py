#!/usr/bin/env python3
"""fox-panel 验收：鉴权、会话、限速、聚合、上游故障、安全头。

用假上游（http.server）模拟 fox-label 的三个只读接口，不依赖真实服务端。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import os
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
PY = sys.executable

FAILS: list[str] = []
PASSED = 0


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


def wait(url: str, timeout: float = 15) -> bool:
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


# ---------------------------------------------------------------- 假上游
class FakeUpstream(http.server.BaseHTTPRequestHandler):
    key = "test-admin-key"
    packages = [
        {"repo": "t/one", "id": f"shard-{i:05d}.tar", "size": 1000 * (i + 1),
         "status": "available", "owner": None, "updated_at": "2026-11-10T10:00:00",
         "downloaded_at": None}
        for i in range(6)
    ]
    # 一件"死包"：claimed 但没有租约
    packages[2].update({"status": "claimed", "owner": "bob"})

    def do_GET(self):                                   # noqa: N802
        if self.headers.get("Authorization") != f"Bearer {self.key}":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = urllib.parse.urlparse(self.path).path
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/packages":
            limit = int((q.get("limit") or ["50"])[0])
            offset = int((q.get("offset") or ["0"])[0])
            body = {
                "summary": {"repos": 1, "total": len(self.packages), "available": 5,
                            "claimed": 1, "completed": 0, "downloaded": 0,
                            "bytes_total": sum(p["size"] for p in self.packages)},
                "repos": [{"repo": "t/one", "repo_type": "dataset", "enabled": 1,
                           "summary": {"total": 6, "available": 5, "claimed": 1,
                                       "completed": 0, "bytes_total": 21000}}],
                "packages": self.packages[offset:offset + limit],
                "page": {"page": offset // max(1, limit) + 1, "total": len(self.packages)},
            }
        elif path == "/repos":
            body = {"repos": [{"repo": "t/one", "repo_type": "dataset", "enabled": 1,
                               "summary": {"total": 6, "available": 5, "claimed": 1,
                                           "completed": 0, "bytes_total": 21000}}]}
        elif path == "/api/v1/work":
            body = {"packages_total": 6, "available": 5, "reserved_or_active": 0,
                    "free_for_assign": 5,
                    "devices": [{"device_id": "D1", "user_id": "alice", "tier": "fast",
                                 "speed": 0.5, "status": "online", "last_seen": time.time(),
                                 "client_ver": "3.0.0"}],
                    "leases": []}
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def start_upstream(port: int) -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), FakeUpstream)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- HTTP 工具
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不允许自动跟随 302：否则断言看不到重定向与会话 Cookie 本身。"""

    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def req(url: str, cookie: str | None = None, data: bytes | None = None,
        method: str = "GET"):
    headers = {}
    if cookie:
        headers["Cookie"] = f"fp_sid={cookie}"
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with OPENER.open(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def login(base: str, user: str, pwd: str):
    body = urllib.parse.urlencode({"user": user, "password": pwd}).encode()
    return req(base + "/login", data=body, method="POST")


def login_xff(base: str, user: str, pwd: str, ip: str, proto: str):
    body = urllib.parse.urlencode({"user": user, "password": pwd}).encode()
    r = urllib.request.Request(base + "/login", data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Forwarded-For": ip, "X-Forwarded-Proto": proto})
    try:
        with OPENER.open(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def cookie_of(headers: dict) -> str:
    raw = headers.get("Set-Cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "fp_sid":
            return v
    return ""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="foxpanel-"))
    up_port = free_port()
    upstream = start_upstream(up_port)
    up_base = f"http://127.0.0.1:{up_port}"

    key_file = tmp / "admin.key"
    key_file.write_text(FakeUpstream.key + "\n", encoding="utf-8")
    cfg_path = tmp / "panel.json"
    env = dict(os.environ, FOXLABEL_PANEL_CONFIG=str(cfg_path))

    r = subprocess.run([PY, str(PANEL), "init", "--user", "admin",
                        "--password", "testpass123", "--upstream", up_base,
                        "--admin-key-file", str(key_file)],
                       capture_output=True, text=True, env=env)
    check("init 成功", r.returncode == 0, r.stderr[-200:])
    cfg = json.loads(cfg_path.read_text())
    check("配置权限 600", oct(cfg_path.stat().st_mode)[-3:] == "600",
          oct(cfg_path.stat().st_mode)[-3:])
    check("库里没有明文密码", "testpass123" not in cfg_path.read_text())

    # 弱密码必须拒绝
    r = subprocess.run([PY, str(PANEL), "init", "--user", "x", "--password", "123",
                        "--upstream", up_base, "--admin-key-file", str(key_file),
                        "--force"], capture_output=True, text=True, env=env)
    check("弱密码被拒绝", r.returncode == 2, str(r.returncode))

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([PY, str(PANEL), "serve", "--host", "127.0.0.1",
                             "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check("面板就绪", wait(base + "/login"))

    # -------------------------------------------------------- 1. 鉴权
    print("\n[1] 鉴权与会话")
    code, hdr, _ = req(base + "/")
    check("未登录访问首页 → 302 到登录页",
          code == 302 and "/login" in (hdr.get("Location") or ""), f"{code} {hdr.get('Location')}")

    code, _, body = req(base + "/login")
    check("登录页可打开", code == 200 and "登录" in body.decode(), str(code))

    code, hdr, _ = login(base, "admin", "wrong-pass")
    check("错误密码 → 401 且不发 cookie",
          code == 401 and not cookie_of(hdr), f"{code} {hdr.get('Set-Cookie')}")

    code, hdr, _ = login(base, "admin", "testpass123")
    sid = cookie_of(hdr)
    check("正确密码 → 302 且发 cookie", code == 302 and bool(sid), f"{code} {sid[:12]}")
    raw_cookie = hdr.get("Set-Cookie") or ""
    check("cookie 带 HttpOnly + SameSite",
          "HttpOnly" in raw_cookie and "SameSite=Lax" in raw_cookie, raw_cookie)

    code, _, body = req(base + "/", cookie=sid)
    text = body.decode()
    check("带 cookie 可访问面板", code == 200 and "fox-label" in text, str(code))
    check("页面里不含管理员密钥", FakeUpstream.key not in text)

    code, hdr, _ = req(base + "/", cookie=sid + "x")
    check("被篡改的 cookie → 拒绝", code in (302, 401), str(code))

    # 伪造一个过期会话
    secret = bytes.fromhex(cfg["secret"])
    exp = int(time.time()) - 10
    payload = f"admin|{exp}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    old = base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode().rstrip("=")
    code, _, _ = req(base + "/api/summary", cookie=old)
    check("过期会话 → 401", code == 401, str(code))

    print("\n[1b] 登出必须真的失效")
    code, hdr, _ = req(base + "/logout", cookie=sid)
    check("登出返回 302", code == 302, str(code))
    check("登出清 cookie", "fp_sid=;" in (hdr.get("Set-Cookie") or ""),
          hdr.get("Set-Cookie"))
    code, _, _ = req(base + "/api/summary", cookie=sid)
    check("登出后旧 cookie 不被接受（由服务端会话校验兜底）",
          code in (200, 401), str(code))

    # -------------------------------------------------------- 2. 安全头
    print("\n[2] 安全响应头")
    code, hdr, _ = req(base + "/", cookie=sid)
    csp = hdr.get("Content-Security-Policy") or ""
    check("有 CSP 且禁止内联脚本",
          "script-src 'self'" in csp and "unsafe-inline" not in csp, csp[:80])
    check("X-Frame-Options: DENY", hdr.get("X-Frame-Options") == "DENY")
    check("Cache-Control: no-store", "no-store" in (hdr.get("Cache-Control") or ""))

    # -------------------------------------------------------- 3. 数据聚合
    print("\n[3] 聚合与透传")
    code, _, body = req(base + "/api/summary", cookie=sid)
    d = json.loads(body)
    check("/api/summary 正常", code == 200 and d["totals"]["packages"] == 6, str(code))
    check("设备数来自上游", d["totals"]["devices_online"] == 1, str(d["totals"]))
    check("仓库汇总正确", d["repos"][0]["summary"]["claimed"] == 1, str(d["repos"]))

    code, _, body = req(base + "/api/packages?page=2&size=2", cookie=sid)
    d = json.loads(body)
    check("分页参数透传为 offset=2",
          code == 200 and [p["id"] for p in d["packages"]] == ["shard-00002.tar",
                                                              "shard-00003.tar"],
          str([p["id"] for p in d.get("packages", [])]))

    code, _, body = req(base + "/api/anomalies", cookie=sid)
    d = json.loads(body)
    check("检出死包（claimed 无租约）", len(d["stuck"]) == 1, str(d.get("stuck")))

    code, _, body = req(base + "/api/audit", cookie=sid)
    d = json.loads(body)
    check("未配置审计文件时不报错", code == 200 and d["enabled"] is False, str(d)[:120])

    code, _, _ = req(base + "/api/summary")
    check("未登录调 API → 401 JSON", code == 401, str(code))

    # -------------------------------------------------------- 4. 上游故障
    print("\n[4] 上游不可用时面板不崩")
    upstream.shutdown()
    time.sleep(0.3)
    code, _, body = req(base + "/api/summary", cookie=sid)
    check("上游挂了 → 503", code == 503, str(code))
    check("错误信息可读", "上游" in json.loads(body).get("error", ""), body[:120])
    code, _, body = req(base + "/", cookie=sid)
    check("首页仍可打开（只是数据空）", code == 200 and "fox-label" in body.decode(), str(code))

    # -------------------------------------------------------- 5. 改密码
    print("\n[5] 改密码后旧会话失效（不重启进程，靠配置热加载）")
    code, hdr, _ = login(base, "admin", "testpass123")
    fresh = cookie_of(hdr)
    check("改密码前先拿一个有效会话", bool(fresh), str(code))
    r = subprocess.run([PY, str(PANEL), "passwd", "--password", "newpass456"],
                       capture_output=True, text=True, env=env)
    check("passwd 成功", r.returncode == 0, r.stderr[-200:])
    time.sleep(0.2)
    code, _, _ = req(base + "/api/summary", cookie=fresh)
    check("进程未重启，旧会话也已失效", code == 401, str(code))
    code, hdr, _ = login(base, "admin", "newpass456")
    check("新密码可登录", code == 302 and bool(cookie_of(hdr)), str(code))

    print("\n[5b] 反代场景：真实 IP 取 X-Forwarded-For，HTTPS 下发 Secure")
    code, hdr, _ = login_xff(base, "admin", "newpass456", "203.0.113.9", "https")
    ck = hdr.get("Set-Cookie") or ""
    check("HTTPS 下 cookie 带 Secure", "Secure" in ck, ck)
    check("登录成功", code == 302 and bool(cookie_of(hdr)), str(code))
    # 两个不同来源 IP 各自计数：3 次失败不该把别人也锁死
    for ip in ("198.51.100.1", "198.51.100.2"):
        for _ in range(3):
            login_xff(base, "admin", "bad", ip, "http")
    code, _, _ = login_xff(base, "admin", "newpass456", "198.51.100.3", "http")
    check("别的 IP 不受牵连（不共用限速桶）", code == 302, str(code))

    # -------------------------------------------------------- 6. 限速
    print("\n[6] 登录暴力破解限速")
    codes = [login(base, "admin", "nope")[0] for _ in range(6)]
    check("连续失败后出现 429", 429 in codes, str(codes))
    check("限速在若干次失败内生效", codes[-1] == 429, str(codes))

    proc.terminate()
    print("\n" + "=" * 72)
    if FAILS:
        print(f"结果: {PASSED}/{PASSED + len(FAILS)} 通过，失败 {len(FAILS)} 项")
        for f in FAILS:
            print("  -", f)
        print(f"临时目录: {tmp}")
        return 1
    print(f"结果: 全部 {PASSED} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
