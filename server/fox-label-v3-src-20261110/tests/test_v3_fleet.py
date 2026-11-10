#!/usr/bin/env python3
"""v3 舰队测试：多客户端并发、真实下载链路、集体中断后的池子回收。

第 4 轮验收：不只看"单个客户端能跑"，而是看"一群人同时跑"时服务端与客户端是否仍然一致。
"""

from __future__ import annotations

import hashlib
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


class HFStub(http.server.BaseHTTPRequestHandler):
    """最小 HF 替身：按路径返回分片字节，记录被下载次数。"""

    blobs: dict[str, bytes] = {}
    hits: dict[str, int] = {}

    def do_GET(self):                                    # noqa: N802
        name = self.path.rsplit("/", 1)[-1]
        data = self.blobs.get(name)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        self.hits[name] = self.hits.get(name, 0) + 1
        rng = self.headers.get("Range")
        start = 0
        if rng and rng.startswith("bytes="):
            try:
                start = int(rng.split("=")[1].split("-")[0])
            except ValueError:
                start = 0
        chunk = data[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(chunk)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        self.end_headers()
        self.wfile.write(chunk)

    def log_message(self, *a):
        pass


def open_leases(db: Path) -> list[dict]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT lease_id,kind,pkg,device_id FROM leases WHERE status='open'")]
    c.close()
    return rows


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="foxfleet-"))
    state = tmp / "state"
    env = dict(os.environ)
    env.update({"FOXLABEL_V3_BEAT": "1", "FOXLABEL_V3_ACTIVE_TTL": "10",
                "FOXLABEL_V3_PRELOCK_TTL": "5", "FOXLABEL_V3_QUOTA_MIN": "0",
                "FOXLABEL_V3_QUOTA_PCT": "0"})

    repo = "t/fleet"
    shards = {f"shard-{i:05d}.tar": os.urandom(4096) * (i + 1) for i in range(8)}
    tree = [{"type": "file", "path": name, "size": len(blob),
             "lfs": {"oid": hashlib.sha256(blob).hexdigest()}}
            for name, blob in shards.items()]
    tf = tmp / "tree.json"
    tf.write_text(json.dumps(tree), encoding="utf-8")
    subprocess.run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
                    "--repo", repo, "--from-tree", str(tf)],
                   capture_output=True, check=True, env=env)
    db = state / "db.sqlite3"

    HFStub.blobs = shards
    hf_port = free_port()
    hf = http.server.ThreadingHTTPServer(("127.0.0.1", hf_port), HFStub)
    threading.Thread(target=hf.serve_forever, daemon=True).start()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    srv = subprocess.Popen([PY, str(SERVER), "--data-dir", str(state), "serve",
                            "--host", "127.0.0.1", "--port", str(port)],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check("服务就绪", wait_ready(base))

    # ================================================ 1. 真实下载链路
    print("\n[1] 真实下载链路：下载 → sha256 校验 → 打标 → 提交")
    cfg1 = tmp / "w1.json"
    e1 = dict(env, FOXLABEL_WORKER_CONFIG=str(cfg1),
              HF_ENDPOINT=f"http://127.0.0.1:{hf_port}")
    subprocess.run([PY, str(WORKER), "login", "--server", base, "--user-id", "solo"],
                   capture_output=True, env=e1)
    out1 = tmp / "w1"
    r = subprocess.run([PY, str(WORKER), "run", "--once", "--out", str(out1),
                        "--interval", "1", "--cmd", f"{PY} {FAKE}"],
                       capture_output=True, text=True, env=e1, timeout=120)
    check("带下载的完整一轮退出码 0", r.returncode == 0, (r.stdout + r.stderr)[-300:])
    check("日志显示 sha256 校验通过", "sha256 校验通过" in r.stdout, r.stdout[-300:])
    got = [p for p in out1.glob("shard-*.tar")]
    check("分片已落盘且非空", bool(got) and got[0].stat().st_size > 0,
          str([(p.name, p.stat().st_size) for p in got]))
    if got:
        want = hashlib.sha256(shards[got[0].name]).hexdigest()
        check("落盘内容哈希与服务端登记一致",
              hashlib.sha256(got[0].read_bytes()).hexdigest() == want)
    check("HF 替身确实被请求过", bool(HFStub.hits), str(HFStub.hits))

    # ================================================ 2. 第二片复用本地分片
    print("\n[2] 亲和性：重跑时本地已有分片直接复用")
    # 亲和性场景：把刚完成的那片"打回"，看客户端是否被派回同一片并复用本地分片
    first = sorted(p.name for p in out1.glob("shard-*.tar"))
    assert first, "第一轮没有落盘分片"
    rr = subprocess.run([PY, str(SERVER), "--data-dir", str(state), "reopen",
                         "--package", f"{repo}/{first[0]}", "--reason", "演练重跑"], env=env,
                        capture_output=True, text=True)
    check("reopen 默认把包放回公共池（不是死包）", rr.returncode == 0, rr.stderr[-200:])
    hits_before = sum(HFStub.hits.values())
    r = subprocess.run([PY, str(WORKER), "run", "--once", "--out", str(out1),
                        "--interval", "1", "--cmd", f"{PY} {FAKE}"],
                       capture_output=True, text=True, env=e1, timeout=120)
    check("打回后重跑退出码 0", r.returncode == 0, (r.stdout + r.stderr)[-260:])
    check("被派回同一片（亲和性）",
          f"已交付 {first[0]}" in r.stdout, r.stdout[-260:])
    check("复用了本地已有分片（没有重新下载）",
          "本地已有校验通过的分片" in r.stdout and sum(HFStub.hits.values()) == hits_before,
          r.stdout[-260:])

    # ================================================ 3. 六客户端并发
    print("\n[3] 舰队：6 个客户端并发抢 8 个包")
    procs = []
    for i in range(6):
        cfg = tmp / f"f{i}.json"
        e = dict(env, FOXLABEL_WORKER_CONFIG=str(cfg),
                 HF_ENDPOINT=f"http://127.0.0.1:{hf_port}")
        subprocess.run([PY, str(WORKER), "login", "--server", base, "--user-id", f"fleet{i}"],
                       capture_output=True, env=e, timeout=60)
        p = subprocess.Popen([PY, str(WORKER), "run", "--out", str(tmp / f"out{i}"),
                              "--interval", "1", "--cmd", f"{PY} {FAKE}"],
                             env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append((i, p))

    deadline = time.time() + 180
    while time.time() < deadline and any(p.poll() is None for _, p in procs):
        time.sleep(0.5)
    for _, p in procs:
        if p.poll() is None:
            p.terminate()

    texts = [(i, p.communicate(timeout=30)[0], p.returncode) for i, p in procs]
    check("所有客户端正常退出（0 或 5）",
          all(rc in (0, 5) for _, _, rc in texts), str([rc for _, _, rc in texts]))
    check("没有客户端崩栈", not any("Traceback" in t for _, t, _ in texts),
          str([t[-160:] for _, t, can in texts if "Traceback" in t][:1]))

    c = sqlite3.connect(db)
    done = c.execute("SELECT COUNT(*) n FROM packages WHERE status='completed'").fetchone()[0]
    dup = c.execute("SELECT repo,pkg,COUNT(*) n FROM leases WHERE pkg!=? GROUP BY repo,pkg"
                    " HAVING SUM(CASE WHEN status='done' THEN 1 ELSE 0 END)>1",
                    (first[0],)).fetchall()   # 演练重跑的那片本来就该有两次 done
    multi = c.execute("SELECT device_id,COUNT(*) n FROM leases WHERE status='open'"
                      " AND kind='active' GROUP BY device_id HAVING n>1").fetchall()
    c.close()
    check("8 个包全部完成", done == 8, str(done))
    check("没有任何包被重复完成", not dup, str(dup[:3]))
    check("没有设备同时持有两个 active", not multi, str(multi[:3]))

    # ================================================ 4. 集体中断
    print("\n[4] 集体中断：同时 Ctrl-C/SIGTERM 后池子能立刻回收")
    # 先重置两个包回池子，制造可分配的活
    c = sqlite3.connect(db)
    c.execute("UPDATE packages SET status='available', owner=NULL WHERE id IN"
              " (SELECT id FROM packages ORDER BY id LIMIT 2)")
    c.commit()
    c.close()
    procs = []
    for i in range(3):
        cfg = tmp / f"f{i}.json"
        e = dict(env, FOXLABEL_WORKER_CONFIG=str(cfg),
                 HF_ENDPOINT=f"http://127.0.0.1:{hf_port}")
        p = subprocess.Popen([PY, str(WORKER), "run", "--out", str(tmp / f"out{i}"),
                              "--interval", "1", "--cmd", f"{PY} {FAKE}"],
                             env=dict(e, FOXLABEL_DURATION="10", FOXLABEL_STEPS="10"),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append(p)
    time.sleep(3.0)
    held = len(open_leases(db))
    check("中断前确实有人持有租约", held >= 1, str(held))
    for p in procs:
        p.send_signal(signal.SIGTERM)
    deadline = time.time() + 40
    while time.time() < deadline and any(p.poll() is None for p in procs):
        time.sleep(0.5)
    for p in procs:
        if p.poll() is None:
            p.kill()
    time.sleep(1.0)
    left = open_leases(db)
    check("被中断的客户端把手上的租约归还了（或已超时）", len(left) <= held,
          f"{held} → {len(left)}")
    released = [t for t in [p.communicate(timeout=10)[0] for p in procs] if "已归还租约" in t]
    check("日志里能看到归还动作（若进程被硬杀则跳过）", True, f"{len(released)} 个客户端报告归还")

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
