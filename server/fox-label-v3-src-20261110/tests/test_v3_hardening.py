#!/usr/bin/env python3
"""v3 加固验收：secret 哈希、存储故障降级、多进程并发、设备归档。

对应上一轮列出的四个遗留问题，每条都有可证伪的断言。
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
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
        print(f"  FAIL  {name}" + (f"  {str(detail)[:260]}" if detail else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def post(base: str, path: str, body: dict, timeout: float = 20) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}
    except Exception as e:
        return -1, {"_err": str(e)[:80]}


def wait_ready(base: str, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.05)
    return False


def make_repo(state: Path, tmp: Path, name: str, n: int, env: dict) -> None:
    tree = [{"type": "file", "path": f"shard-{i:05d}.tar", "size": 100_000 + i,
             "lfs": {"oid": hashlib.sha256(f"{name}{i}".encode()).hexdigest()}}
            for i in range(n)]
    tf = tmp / f"t-{name.replace('/', '_')}.json"
    tf.write_text(json.dumps(tree), encoding="utf-8")
    r = subprocess.run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
                        "--repo", name, "--from-tree", str(tf)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr


def spawn(state: Path, port: int, env: dict) -> subprocess.Popen:
    p = subprocess.Popen([PY, str(SERVER), "--data-dir", str(state), "serve",
                          "--host", "127.0.0.1", "--port", str(port)],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_ready(f"http://127.0.0.1:{port}")
    return p


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="foxhard-"))
    env = dict(os.environ)
    env.update({"FOXLABEL_V3_BEAT": "1", "FOXLABEL_V3_ACTIVE_TTL": "60",
                "FOXLABEL_V3_PRELOCK_TTL": "60", "FOXLABEL_V3_QUOTA_MIN": "0",
                "FOXLABEL_V3_QUOTA_PCT": "0"})

    # ================================================ 1. secret 哈希化
    print("\n[1] secret 只存哈希，库里拿不到可用凭据")
    st = tmp / "s1"
    make_repo(st, tmp, "t/sec", 4, env)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    srv = spawn(st, port, env)
    db = st / "db.sqlite3"

    code, hs = post(base, "/api/v1/handshake", {"user_id": "alice"})
    secret = hs.get("secret") or ""
    check("首次握手下发 secret", code == 200 and len(secret) > 10, str(hs)[:120])

    c = sqlite3.connect(db)
    stored = c.execute("SELECT secret FROM users WHERE user_id='alice'").fetchone()[0]
    c.close()
    check("库中不是明文", stored != secret, stored[:20])
    check("库中是 sha256", stored == hashlib.sha256(secret.encode()).hexdigest(), stored[:20])
    raw_bytes = db.read_bytes()
    check("数据库文件里搜不到明文 secret", secret.encode() not in raw_bytes)

    code, _ = post(base, "/api/v1/handshake",
                   {"user_id": "alice", "device_id": hs["device_id"], "secret": secret})
    check("用明文 secret 仍能通过（客户端不感知哈希）", code == 200, str(code))

    # 老库迁移：把 secret 改回明文，重启后被就地哈希化
    c = sqlite3.connect(db)
    c.execute("UPDATE users SET secret='legacy-plain' WHERE user_id='alice'")
    c.commit()
    c.close()
    srv.terminate()
    srv.wait(timeout=10)
    srv = spawn(st, port, env)
    c = sqlite3.connect(db)
    stored = c.execute("SELECT secret FROM users WHERE user_id='alice'").fetchone()[0]
    c.close()
    check("老明文 secret 已被迁移成哈希",
          stored == hashlib.sha256(b"legacy-plain").hexdigest(), stored[:20])
    code, _ = post(base, "/api/v1/handshake",
                   {"user_id": "alice", "device_id": hs["device_id"], "secret": "legacy-plain"})
    check("迁移后老 secret 仍可登录", code == 200, str(code))
    srv.terminate()

    # ================================================ 2. 存储故障 → 503
    print("\n[2] 存储被独占时返回 503（不是 500、也不挂死）")
    env_b = dict(env, FOXLABEL_DB_TIMEOUT_MS="200")
    st2 = tmp / "s2"
    make_repo(st2, tmp, "t/busy", 3, env_b)
    port2 = free_port()
    base2 = f"http://127.0.0.1:{port2}"
    srv2 = spawn(st2, port2, env_b)

    lock = sqlite3.connect(st2 / "db.sqlite3", timeout=5, isolation_level=None)
    lock.execute("BEGIN EXCLUSIVE")
    t0 = time.time()
    code, body = post(base2, "/api/v1/handshake", {"user_id": "blocked"})
    took = time.time() - t0
    check(f"写锁被占用时返回 503（{took*1000:.0f}ms）", code == 503, str((code, body)))
    check("503 有明确错误信息", "暂时不可用" in (body.get("error") or ""), str(body))
    check("不会卡住（<3s）", took < 3, f"{took:.2f}s")
    lock.execute("ROLLBACK")
    lock.close()
    code, ok_body = post(base2, "/api/v1/handshake", {"user_id": "blocked"})
    check("释放后可正常服务", code == 200 and ok_body.get("device_id"), str(code))
    srv2.terminate()

    # ================================================ 3. 多进程并发派工
    print("\n[3] 两个服务进程共享同一库并发派工，不得重复占用")
    st3 = tmp / "s3"
    make_repo(st3, tmp, "t/mp", 8, env)
    p_a, p_b = free_port(), free_port()
    base_a, base_b = f"http://127.0.0.1:{p_a}", f"http://127.0.0.1:{p_b}"
    sa = spawn(st3, p_a, env)
    sb = spawn(st3, p_b, env)

    def grab(i: int):
        target = base_a if i % 2 == 0 else base_b
        code_, res = post(target, "/api/v1/handshake", {"user_id": f"mp{i:02d}"})
        return code_, [f"{l['repo']}/{l['package']}" for l in (res.get("leases") or [])]

    with cf.ThreadPoolExecutor(max_workers=30) as ex:
        out = list(ex.map(grab, range(30)))
    codes = [c_ for c_, _ in out]
    assigned = [pkg for _, pkgs in out for pkg in pkgs]
    check("30 并发跨两进程无 5xx", all(c_ < 500 for c_ in codes), str(set(codes)))
    check("派出的包没有重复",
          len(assigned) == len(set(assigned)),
          f"{len(assigned)} 个租约 / {len(set(assigned))} 个不同包")
    c = sqlite3.connect(f"file:{st3 / 'db.sqlite3'}?mode=ro", uri=True)
    dup = c.execute("SELECT repo,pkg,COUNT(*) n FROM leases WHERE status='open'"
                    " GROUP BY repo,pkg HAVING n>1").fetchall()
    multi = c.execute("SELECT device_id,COUNT(*) n FROM leases WHERE status='open'"
                      " AND kind='active' GROUP BY device_id HAVING n>1").fetchall()
    c.close()
    check("库里无重复占用", not dup, str(dup[:3]))
    check("库里无单设备多 active", not multi, str(multi[:3]))
    sa.terminate()
    sb.terminate()

    # ================================================ 4. 设备归档
    print("\n[4] 僵尸设备归档")
    st4 = tmp / "s4"
    make_repo(st4, tmp, "t/prune", 6, env)
    port4 = free_port()
    base4 = f"http://127.0.0.1:{port4}"
    srv4 = spawn(st4, port4, env)
    _, h_old = post(base4, "/api/v1/handshake", {"user_id": "old"})
    _, h_hold = post(base4, "/api/v1/handshake", {"user_id": "holder"})

    # old 必须先把手上的租约交干净，否则"持有租约的设备不归档"会保护它
    subprocess.run([PY, str(SERVER), "--data-dir", str(st4), "unassign",
                    "--device", h_old["device_id"]], capture_output=True, text=True, env=env)

    c = sqlite3.connect(st4 / "db.sqlite3", timeout=10)
    c.execute("UPDATE devices SET last_seen=? WHERE device_id=?",
              (time.time() - 90 * 86400, h_old["device_id"]))
    c.execute("UPDATE devices SET last_seen=? WHERE device_id=?",
              (time.time() - 90 * 86400, h_hold["device_id"]))   # 但有 open 租约
    c.commit()
    c.close()

    r = subprocess.run([PY, str(SERVER), "--data-dir", str(st4), "devices",
                        "--prune", "--days", "30"], capture_output=True, text=True, env=env)
    check("prune 命令可用", r.returncode == 0 and "已归档 1 台" in r.stdout,
          (r.stdout + r.stderr)[:200])

    c = sqlite3.connect(st4 / "db.sqlite3")
    stat_old = c.execute("SELECT status FROM devices WHERE device_id=?",
                         (h_old["device_id"],)).fetchone()[0]
    stat_hold = c.execute("SELECT status FROM devices WHERE device_id=?",
                          (h_hold["device_id"],)).fetchone()[0]
    c.close()
    check("长期失联设备被归档", stat_old == "archived", stat_old)
    check("持有租约的设备不被归档", stat_hold != "archived", stat_hold)

    code, back = post(base4, "/api/v1/handshake",
                      {"user_id": "old", "device_id": h_old["device_id"],
                       "secret": h_old["secret"]})
    check("归档设备重新握手可恢复", code == 200, str(code))
    c = sqlite3.connect(st4 / "db.sqlite3")
    stat = c.execute("SELECT status FROM devices WHERE device_id=?",
                     (h_old["device_id"],)).fetchone()[0]
    c.close()
    check("恢复后状态回到 online", stat == "online", stat)
    srv4.terminate()

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
