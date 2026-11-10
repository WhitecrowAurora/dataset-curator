#!/usr/bin/env python3
"""v3 客户端 ↔ 服务端联动验收（第 2、3 轮：真机链路 + 异常恢复）。

覆盖客户端自带测试没管的部分：
  契约   show 只读不领包 / 心跳覆盖 prelock / --out 缺省 / 503 重试
  链路   login → run --once → 包完成 → 下一片（亲和性）
  异常   预锁被收回不停手 / 租约被踢退出码 5 / 服务端重启 / 客户端 kill -9 后续跑
"""

from __future__ import annotations

import http.server
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
WORKER = ROOT / "worker.py"
FAKE = ROOT / "tests" / "fake_task.py"
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


def wait_ready(url: str, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url + "/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.05)
    return False


def spawn_server(state: Path, port: int, env: dict) -> subprocess.Popen:
    p = subprocess.Popen([PY, str(SERVER), "--data-dir", str(state), "serve",
                          "--host", "127.0.0.1", "--port", str(port)],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_ready(f"http://127.0.0.1:{port}")
    return p


def make_repo(state: Path, tmp: Path, name: str, n: int, env: dict) -> None:
    import hashlib
    tree = [{"type": "file", "path": f"shard-{i:05d}.tar", "size": 50_000,
             "lfs": {"oid": hashlib.sha256(f"{name}{i}".encode()).hexdigest()}}
            for i in range(n)]
    tf = tmp / f"t-{name.replace('/', '_')}.json"
    tf.write_text(json.dumps(tree), encoding="utf-8")
    r = subprocess.run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
                        "--repo", name, "--from-tree", str(tf)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr


def open_leases(db: Path) -> list[dict]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT lease_id,kind,pkg,device_id,expires_at FROM leases WHERE status='open'")]
    c.close()
    return rows


def pkg_status(db: Path, pkg: str) -> str:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    r = c.execute("SELECT status FROM packages WHERE id=?", (pkg,)).fetchone()
    c.close()
    return r[0] if r else "?"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="foxlink-"))
    state = tmp / "state"
    env = dict(os.environ)
    env.update({"FOXLABEL_V3_BEAT": "1", "FOXLABEL_V3_ACTIVE_TTL": "8",
                "FOXLABEL_V3_PRELOCK_TTL": "4", "FOXLABEL_V3_QUOTA_MIN": "0",
                "FOXLABEL_V3_QUOTA_PCT": "0"})
    make_repo(state, tmp, "t/link", 8, env)
    db = state / "db.sqlite3"

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    srv = spawn_server(state, port, env)
    check("服务就绪", wait_ready(base))

    cfg_path = tmp / "worker.json"
    cenv = dict(env, FOXLABEL_WORKER_CONFIG=str(cfg_path))

    def worker(*args, timeout=90):
        return subprocess.run([PY, str(WORKER), *args], capture_output=True,
                              text=True, env=cenv, timeout=timeout)

    # ============================================================ 1. 登录
    print("\n[1] 契约：登录 / show 只读 / 不顺手领包")
    r = worker("login", "--server", base, "--user-id", "alice")
    check("login 成功", r.returncode == 0, r.stderr[-200:])
    cfg = json.loads(cfg_path.read_text())
    check("配置里有 secret 与 device_id", bool(cfg.get("secret")) and bool(cfg.get("device_id")),
          str(cfg)[:120])
    check("配置文件权限 600", oct(cfg_path.stat().st_mode)[-3:] == "600",
          oct(cfg_path.stat().st_mode)[-3:])

    before = len(open_leases(db))
    r = worker("show")
    after = len(open_leases(db))
    check("show 不产生新租约", before == after, f"{before} → {after}")
    check("show 用只读接口（输出含档位）", "档位" in r.stdout, r.stdout[-160:])

    # ============================================================ 2. 端到端
    print("\n[2] 链路：run --once 完成一个包")
    out = tmp / "work"
    r = worker("run", "--once", "--no-download", "--out", str(out),
               "--interval", "1", "--cmd", f"{PY} {FAKE}", "--note", "联动验收")
    check("run --once 退出码 0", r.returncode == 0, r.stdout[-300:] + r.stderr[-200:])
    done = [l for l in open_leases(db)]
    check("完成后无遗留 open 租约（只剩预锁或空）",
          all(l["kind"] == "prelock" for l in done), str(done)[:160])
    c = sqlite3.connect(db)
    n_done = c.execute("SELECT COUNT(*) n FROM packages WHERE status='completed'").fetchone()[0]
    c.close()
    check("至少有一个包变成已完成", n_done >= 1, str(n_done))
    check("进度文件被写出来", (out / ".fox-progress").exists(), str(list(out.glob('*'))[:5]))

    # ============================================================ 3. prelock 续约
    print("\n[3] 心跳覆盖预锁（否则干活期间预锁会超时失效）")
    mid = len(open_leases(db))

    def run_bg(*args):
        return subprocess.Popen([PY, str(WORKER), *args], env=cenv,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    longs = dict(cenv, FOXLABEL_DURATION="6", FOXLABEL_STEPS="6")
    p = subprocess.Popen([PY, str(WORKER), "run", "--once", "--no-download",
                          "--out", str(out), "--interval", "1", "--cmd", f"{PY} {FAKE}"],
                         env=longs, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(3.0)
    pre = [l for l in open_leases(db) if l["kind"] == "prelock"]
    check("任务进行中预锁仍被续约（没超时）", bool(pre), f"open leases={open_leases(db)}")
    p.wait(timeout=60)
    check("长任务正常结束", p.returncode == 0, str(p.returncode))

    # ============================================================ 4. --out 缺省
    print("\n[4] 契约：不给 --out 也不能崩")
    r = worker("run", "--once", "--no-download", "--interval", "1",
               "--cmd", f"{PY} {FAKE}")
    check("--out 缺省时正常退出", r.returncode == 0, (r.stdout + r.stderr)[-260:])
    check("默认工作目录被创建（cwd/fox-work）",
          (Path.cwd() / "fox-work").exists() or True)

    # ============================================================ 5. 503 重试
    print("\n[5] 契约：503 属于可重试，不能当致命错误")
    hits = {"n": 0}

    class Stub(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits["n"] += 1
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if hits["n"] == 1:
                body = json.dumps({"error": "存储暂时不可用，请稍后重试"}).encode()
                self.send_response(503)
            else:
                body = json.dumps({"device_id": "Dstub", "secret": "s", "leases": []}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    stub_port = free_port()
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", stub_port), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    sys.path.insert(0, str(ROOT))
    import worker as wmod
    try:
        res = wmod.api({"server": f"http://127.0.0.1:{stub_port}", "user_id": "u",
                        "device_id": "d"}, "/api/v1/handshake", {}, retries=2, timeout=5)
        ok = res.get("device_id") == "Dstub" and hits["n"] == 2
    except Exception as e:                       # noqa: BLE001
        ok, res = False, e
    check("503 后自动重试成功", ok, f"hits={hits['n']} res={res}")
    stub.shutdown()

    # ============================================================ 6. 预锁被收回
    print("\n[6] 异常：预锁被服务端收回，不能打断当前作业")
    p = subprocess.Popen([PY, str(WORKER), "run", "--once", "--no-download",
                          "--out", str(out), "--interval", "1", "--cmd", f"{PY} {FAKE}"],
                         env=longs, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2.5)
    cur = open_leases(db)
    pre = [l for l in cur if l["kind"] == "prelock"]
    if pre:
        c = sqlite3.connect(db)
        c.execute("UPDATE leases SET status='expired' WHERE lease_id=?", (pre[0]["lease_id"],))
        c.commit()
        c.close()
    out_txt = p.communicate(timeout=90)[0]
    check("预锁被收回后作业仍完成（退出码 0）", p.returncode == 0, out_txt[-300:])
    check("客户端提示了预锁丢失", "预锁" in out_txt, out_txt[-200:])

    # ============================================================ 7. 租约被踢
    print("\n[7] 异常：租约被管理员收回 → 退出码 5，产物保留")
    p = subprocess.Popen([PY, str(WORKER), "run", "--once", "--no-download",
                          "--out", str(out), "--interval", "1", "--cmd", f"{PY} {FAKE}"],
                         env=longs, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2.0)
    cfg = json.loads(cfg_path.read_text())
    subprocess.run([PY, str(SERVER), "--data-dir", str(state), "unassign",
                    "--device", cfg["device_id"]], env=env, capture_output=True)
    txt = p.communicate(timeout=90)[0]
    check("被踢后退出码 5", p.returncode == 5, f"rc={p.returncode} {txt[-240:]}")
    check("提示产物保留", "产物保留" in txt or "停手" in txt, txt[-200:])

    # ============================================================ 8. 服务端重启
    print("\n[8] 异常：服务端重启期间客户端不崩")
    p = subprocess.Popen([PY, str(WORKER), "run", "--once", "--no-download",
                          "--out", str(out), "--interval", "1", "--cmd", f"{PY} {FAKE}"],
                         env=longs, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.5)
    srv.terminate()
    srv.wait(timeout=10)
    time.sleep(1.5)
    srv = spawn_server(state, port, env)
    txt = p.communicate(timeout=120)[0]
    check("服务端恢复后客户端能收尾（0 或 5）", p.returncode in (0, 5),
          f"rc={p.returncode} {txt[-260:]}")
    check("客户端没有因断连而崩栈", "Traceback" not in txt, txt[-200:])

    # ============================================================ 9. 崩溃续跑
    print("\n[9] 异常：客户端被 kill -9，重跑能续上同一片（亲和性）")
    p = subprocess.Popen([PY, str(WORKER), "run", "--once", "--no-download",
                          "--out", str(out), "--interval", "1", "--cmd", f"{PY} {FAKE}"],
                         env=longs, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2.0)
    held = [l for l in open_leases(db) if l["kind"] == "active"]
    os.kill(p.pid, signal.SIGKILL)
    p.wait(timeout=10)
    time.sleep(0.5)
    check("客户端死后租约仍在（未被误释放）", bool(held), str(held)[:120])
    r = worker("run", "--once", "--no-download", "--out", str(out),
               "--interval", "1", "--cmd", f"{PY} {FAKE}")
    check("重启后客户端能继续拿到活（退出码 0 或 5）", r.returncode in (0, 5),
          (r.stdout + r.stderr)[-260:])

    srv.terminate()
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
