#!/usr/bin/env python3
"""worker.py（v3 客户端）验收测试。

全程真 HTTP、真 curl 下载、真子进程、真心跳线程，不 mock 内部函数。
服务端把 TTL 压到十几秒，所以整套几十秒内跑完。
"""

from __future__ import annotations

import atexit
import hashlib
import http.server
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
WORKER = ROOT / "worker.py"
PY = sys.executable
FAKE = ROOT / "tests" / "fake_task.py"

FAILS: list[str] = []
PASSED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}" + (f"  {detail[:400]}" if detail else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_ready(base: str, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.1)
    return False


class HFHandler(http.server.BaseHTTPRequestHandler):
    """假 HF：只按文件名返回字节，模拟 /datasets/<repo>/resolve/main/<pkg>。"""

    files: Path

    def do_GET(self):  # noqa: N802
        name = self.path.rsplit("/", 1)[-1].split("?")[0]
        p = self.files / name
        if not p.exists():
            self.send_error(404)
            return
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def start_hf(files: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    HFHandler.files = files
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HFHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="foxworker-"))
    state = tmp / "state"
    files = tmp / "hffiles"
    files.mkdir()
    cfg_path = tmp / "worker.json"
    out_dir = tmp / "out"

    # ---- 造 6 个分片（真字节，sha256 用真实值） ----
    repo = "test/dispatch"
    tree = []
    for i in range(6):
        name = f"shard-00000{i}.tar"
        payload = (f"payload-{i}-".encode() * 900) + os.urandom(64)
        (files / name).write_bytes(payload)
        tree.append({"type": "file", "path": name, "size": len(payload),
                     "lfs": {"oid": hashlib.sha256(payload).hexdigest()}})
    tf = tmp / "tree.json"
    tf.write_text(json.dumps(tree), encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "FOXLABEL_V3_BEAT": "1",
        "FOXLABEL_V3_ACTIVE_TTL": "20",
        "FOXLABEL_V3_PRELOCK_TTL": "10",
        "FOXLABEL_V3_QUOTA_MIN": "0",
        "FOXLABEL_V3_QUOTA_PCT": "0",
    })
    r = subprocess.run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
                        "--repo", repo, "--from-tree", str(tf)],
                       capture_output=True, text=True, env=env)
    check("建库成功", r.returncode == 0, r.stderr)
    admin_key = (state / "admin.key").read_text(encoding="utf-8").strip()

    port = free_port()
    base = f"http://127.0.0.1:{port}"

    def start_server():
        p = subprocess.Popen([PY, str(SERVER), "--data-dir", str(state), "serve",
                              "--host", "127.0.0.1", "--port", str(port),
                              "--server-id", "fox-worker-test"],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        atexit.register(lambda: p.poll() is None and p.kill())
        return p

    srv = start_server()
    check("服务就绪", wait_ready(base), "服务端未启动")
    hf, hf_base = start_hf(files)

    def worker(*args, extra_env=None, timeout=90):
        e = dict(os.environ)
        e.update({"HF_ENDPOINT": hf_base, "FOXLABEL_WORKER_CONFIG": str(cfg_path),
                  "PYTHONUNBUFFERED": "1"})
        if extra_env:
            e.update(extra_env)
        return subprocess.run([PY, str(WORKER), *args], capture_output=True,
                              text=True, env=e, timeout=timeout)

    # ---------------------------------------------------------- 1. login
    print("\n[1] login：首次握手换取 secret 与 device_id")
    r = worker("login", "--server", base, "--user-id", "alice")
    check("login 成功", r.returncode == 0, r.stderr)
    check("签发 secret", "密钥   : 已签发" in r.stdout, r.stdout)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    check("配置落盘含 secret/device_id", bool(cfg.get("secret")) and bool(cfg.get("device_id")),
          str(cfg.keys()))
    check("配置文件权限 600", oct(cfg_path.stat().st_mode)[-3:] == "600",
          oct(cfg_path.stat().st_mode)[-3:])
    dev = cfg["device_id"]
    check("拿到派工", "派工   :" in r.stdout, r.stdout)

    print("\n[2] 重复 login：身份稳定、不重复签发")
    r2 = worker("login", "--server", base, "--user-id", "alice")
    cfg2 = json.loads(cfg_path.read_text(encoding="utf-8"))
    check("device_id 不变", cfg2["device_id"] == dev, cfg2["device_id"])
    check("不重复签发 secret", "已签发" not in r2.stdout, r2.stdout)

    print("\n[3] show：显示身份与派工")
    r3 = worker("show")
    check("show 成功", r3.returncode == 0 and "在线状态: 正常" in r3.stdout, r3.stdout + r3.stderr)

    # ---------------------------------------------------------- 2. 一次完整作业
    print("\n[4] run --once：下载 → 跑活 → 交付")
    task = f"{PY} {FAKE}"
    r4 = worker("run", "--once", "--out", str(out_dir), "--interval", "1", "--cmd", task,
                extra_env={"FOXLABEL_DURATION": "1.2"})
    check("run --once 成功", r4.returncode == 0, (r4.stdout + r4.stderr)[-600:])
    check("下载了分片", any(out_dir.glob("shard-*.tar")), str(list(out_dir.iterdir())))
    check("sha256 校验通过", "sha256 校验通过" in r4.stdout, r4.stdout[-400:])
    check("交付成功", "已交付" in r4.stdout, r4.stdout[-300:])

    c = sqlite3.connect(state / "db.sqlite3")
    n_done = c.execute("SELECT COUNT(*) FROM packages WHERE status='completed'").fetchone()[0]
    check("服务端记 1 个已完成", n_done == 1, str(n_done))
    prog = c.execute("SELECT progress_json FROM leases WHERE status='done'"
                     " ORDER BY issued_at DESC LIMIT 1").fetchone()
    check("心跳上报了进度", prog and json.loads(prog[0] or "{}").get("done", 0) > 0,
          str(prog))
    c.close()
    check("本地记下已产出的包", bool(json.loads(cfg_path.read_text()).get("have")), "have 为空")

    # ---------------------------------------------------------- 3. 命令失败
    print("\n[5] 任务命令失败：上报 fail，包退回池子")
    r5 = worker("run", "--once", "--out", str(out_dir), "--interval", "1", "--cmd", task,
                extra_env={"FOXLABEL_DURATION": "0.3", "FOXLABEL_FAIL": "7"})
    check("失败退出码 6", r5.returncode == 6, f"rc={r5.returncode} {r5.stdout[-300:]}")
    c = sqlite3.connect(state / "db.sqlite3")
    failed = c.execute("SELECT COUNT(*) FROM leases WHERE status='failed'").fetchone()[0]
    c.close()
    check("服务端记 1 个 failed 租约", failed >= 1, str(failed))

    # ---------------------------------------------------------- 4. 心跳续约
    print("\n[6] 心跳续约：跑活期间租约被持续延长")

    # 用后台线程同时观察 leases 表
    seen = {"renewed": False}
    stop_watch = threading.Event()

    def watch():
        c2 = sqlite3.connect(state / "db.sqlite3")
        while not stop_watch.is_set():
            row = c2.execute("SELECT last_beat_at, issued_at FROM leases"
                             " WHERE status='open' AND kind='active'").fetchone()
            if row and row[0] and row[1] and row[0] > row[1] + 0.5:
                seen["renewed"] = True
                break
            time.sleep(0.2)
        c2.close()

    th = threading.Thread(target=watch, daemon=True)
    th.start()
    r6 = worker("run", "--once", "--out", str(out_dir), "--interval", "1", "--cmd", task,
                extra_env={"FOXLABEL_DURATION": "3.5"})
    stop_watch.set()
    th.join(timeout=3)
    check("run 成功", r6.returncode == 0, (r6.stdout + r6.stderr)[-400:])
    check("租约被心跳续约（last_beat_at 推进）", seen["renewed"], str(seen))

    # ---------------------------------------------------------- 5. 租约被收回
    print("\n[7] 租约被管理员收回：本地停手、不提交、退出码 5")
    proc = subprocess.Popen([PY, str(WORKER), "run", "--once", "--out", str(out_dir),
                             "--interval", "1", "--cmd", task],
                            env={**os.environ, "HF_ENDPOINT": hf_base,
                                 "FOXLABEL_WORKER_CONFIG": str(cfg_path),
                                 "PYTHONUNBUFFERED": "1", "FOXLABEL_DURATION": "9"},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2.5)
    r = subprocess.run([PY, str(SERVER), "--data-dir", str(state), "unassign", "--device", dev],
                       capture_output=True, text=True, env=env)
    check("unassign 执行成功", r.returncode == 0, r.stderr)
    out, _ = proc.communicate(timeout=30)
    check("租约收回后退出码 5", proc.returncode == 5, f"rc={proc.returncode}\n{out[-400:]}")
    check("提示产物保留", "产物保留" in out, out[-300:])
    c = sqlite3.connect(state / "db.sqlite3")
    n_completed = c.execute("SELECT COUNT(*) FROM packages WHERE status='completed'").fetchone()[0]
    c.close()
    check("被收回的包没有被误标完成", n_completed == 2, str(n_completed))

    # ---------------------------------------------------------- 6. 离线容忍
    print("\n[8] 服务端短暂不可用：心跳失败但本地不停工")
    p2 = subprocess.Popen([PY, str(WORKER), "run", "--once", "--out", str(out_dir),
                           "--interval", "1", "--cmd", task],
                          env={**os.environ, "HF_ENDPOINT": hf_base,
                               "FOXLABEL_WORKER_CONFIG": str(cfg_path),
                               "PYTHONUNBUFFERED": "1", "FOXLABEL_DURATION": "6"},
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.8)
    srv.kill()
    srv.wait()
    time.sleep(2.0)
    srv = start_server()          # 同一端口同一库，租约仍在有效期内
    wait_ready(base)
    out2, _ = p2.communicate(timeout=40)
    check("服务端恢复后作业继续并交付", p2.returncode == 0, f"rc={p2.returncode}\n{out2[-500:]}")
    check("出现过心跳失败告警", "心跳失败" in out2, out2[-400:])

    # ---------------------------------------------------------- 7. 亲和性
    print("\n[9] 亲和性：本地已有产物的包优先派回")
    have = json.loads(cfg_path.read_text(encoding="utf-8")).get("have") or []
    c = sqlite3.connect(state / "db.sqlite3")
    local_row = c.execute("SELECT local_pkgs FROM devices WHERE device_id=?", (dev,)).fetchone()
    c.close()
    local = json.loads((local_row or ("[]",))[0] or "[]")
    check("握手把本地产出报给了服务端（devices.local_pkgs）",
          bool(local) and set(local) <= set(have), f"have={have} local={local}")
    check("have 里是 repo/package 形式", all("/" in x for x in have), str(have))

    # ---------------------------------------------------------- 8. 收尾
    print("\n[10] 池子空 / 未登录 的错误处理")
    r = worker("run", "--once", "--out", str(out_dir))
    check("池子空时正常退出", r.returncode == 0, f"rc={r.returncode} {r.stdout[-200:]}")
    env2 = dict(os.environ)
    env2["FOXLABEL_WORKER_CONFIG"] = str(tmp / "missing.json")
    r = subprocess.run([PY, str(WORKER), "run"], capture_output=True, text=True, env=env2)
    check("未登录时退出码 2", r.returncode == 2, f"rc={r.returncode} {r.stderr[-200:]}")
    fresh = tmp / "fresh.json"
    r = subprocess.run([PY, str(WORKER), "login", "--server", base, "--user-id", "alice"],
                       capture_output=True, text=True,
                       env={**os.environ, "FOXLABEL_WORKER_CONFIG": str(fresh)})
    check("配置丢失后登录被拒（401）", r.returncode == 3, f"rc={r.returncode} {r.stderr[-200:]}")
    check("401 时给出 reset-secret 指引", "reset-secret" in r.stderr, r.stderr[-300:])
    rr = subprocess.run([PY, str(SERVER), "--data-dir", str(state), "reset-secret",
                         "--user-id", "alice"], capture_output=True, text=True, env=env)
    check("管理员 reset-secret 可用", rr.returncode == 0 and "已为 alice 重签" in rr.stdout,
          rr.stderr[-200:])
    row = [l.split("=", 1)[1].strip() for l in rr.stdout.splitlines()
           if l.startswith("secret=")]
    check("reset-secret 输出机器可读的 secret", len(row) == 1, rr.stdout)
    new_secret = row[0] if row else ""
    fresh.unlink(missing_ok=True)
    r = subprocess.run([PY, str(WORKER), "login", "--server", base, "--user-id", "alice",
                        "--secret", new_secret], capture_output=True, text=True,
                       env={**os.environ, "FOXLABEL_WORKER_CONFIG": str(fresh)})
    check("用管理员给的新 secret 可以重新登录", r.returncode == 0, r.stderr[-300:])
    ncfg = json.loads(fresh.read_text(encoding="utf-8"))
    check("新配置里的 secret 就是新签发的", ncfg.get("secret") == new_secret, "不一致")

    srv.terminate()
    hf.shutdown()
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
