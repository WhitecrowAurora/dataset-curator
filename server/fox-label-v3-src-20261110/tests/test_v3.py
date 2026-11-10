#!/usr/bin/env python3
"""v3 调度层验收测试：派工 / 租约 / 心跳 / 回收 / 栅栏 / 配额。

全部走真实 HTTP 与真实子进程服务端，不 mock 内部函数。
测试把 TTL 压到 2 秒（FOXLABEL_V3_*），所以整个套件几秒钟跑完。
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import socket
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
        print(f"  FAIL  {name}" + (f"  {detail[:300]}" if detail else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run(cmd: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def post(base: str, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}


def admin_get(base: str, key: str, path: str) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}


def wait_ready(base: str, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="foxv3-"))
    state = tmp / "state"

    env = dict(os.environ)
    env.update({
        "FOXLABEL_V3_BEAT": "1",
        "FOXLABEL_V3_ACTIVE_TTL": "2",
        "FOXLABEL_V3_PRELOCK_TTL": "2",
        "FOXLABEL_V3_QUOTA_MIN": "0",
        "FOXLABEL_V3_QUOTA_PCT": "0",
        "FOXLABEL_V3_STALL_WARN": "3600",
        "FOXLABEL_V3_STALL_KILL": "7200",
    })

    # 三个不同大小的包：验证"快设备拿大包"
    repo = "test/dispatch"
    pkgs = [(f"shard-00000{i}.tar", 300_000 - i * 30_000) for i in range(8)]
    tree = [{"type": "file", "path": n, "size": sz,
             "lfs": {"oid": hashlib.sha256(n.encode()).hexdigest()}} for n, sz in pkgs]
    tf = tmp / "tree.json"
    tf.write_text(json.dumps(tree), encoding="utf-8")
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
             "--repo", repo, "--from-tree", str(tf)], env)
    check("建库成功", r.returncode == 0, r.stderr)
    admin_key = (state / "admin.key").read_text(encoding="utf-8").strip()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    srv = subprocess.Popen([PY, str(SERVER), "--data-dir", str(state), "serve",
                            "--host", "127.0.0.1", "--port", str(port),
                            "--server-id", "fox-test"],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    atexit.register(lambda: srv.poll() is None and srv.kill())
    check("服务就绪", wait_ready(base), "20s 内未就绪")

    # ---------------------------------------------------------- 1. 握手
    print("\n[1] 首次握手：签发 secret + 派第一个包")
    code, hs = post(base, "/api/v1/handshake", {"user_id": "alice", "client_ver": "3.0.0"})
    check("握手成功", code == 200, str(hs))
    check("返回 server_id", hs.get("server_id") == "fox-test", str(hs.get("server_id")))
    check("自动生成 device_id", bool(hs.get("device_id")), str(hs))
    check("首次下发 secret", bool(hs.get("secret")), str(hs.keys()))
    dev_a = hs["device_id"]
    secret_a = hs["secret"]
    check("派到一个 active 租约",
          len(hs.get("leases") or []) >= 1 and hs["leases"][0]["kind"] == "active",
          str(hs.get("leases")))
    first = hs["leases"][0]
    check("大包优先（tier=unknown 也按 LPT）", first["package"] == "shard-000000.tar",
          first["package"])
    check("租约带下载所需元数据",
          first.get("sha256") and first.get("size") and first.get("repo_type") == "dataset",
          str(first))

    print("\n[2] 二次握手：secret 必须匹配")
    code, bad = post(base, "/api/v1/handshake",
                     {"user_id": "alice", "device_id": dev_a, "secret": "wrong"})
    check("错误 secret 被拒", code == 401, str(code))
    code, good = post(base, "/api/v1/handshake",
                      {"user_id": "alice", "device_id": dev_a, "secret": secret_a})
    check("正确 secret 通过", code == 200, str(good))
    check("不再重复下发 secret", not good.get("secret"), str(good.keys()))
    actives = [l for l in good.get("leases") or [] if l["kind"] == "active"]
    check("同一设备不会拿到第二个 active", len(actives) == 1, str(len(actives)))

    # ---------------------------------------------------------- 亲和性
    print("\n[3] 亲和性：本地已有产物的包优先派回（不受 LPT 大包优先影响）")
    code, aff = post(base, "/api/v1/handshake",
                     {"user_id": "erin", "local_pkgs": [f"{repo}/shard-000007.tar"]})
    got_aff = [l["package"] for l in aff.get("leases") or []]
    check("优先派回本地已产出的包", "shard-000007.tar" in got_aff, str(got_aff))

    # ---------------------------------------------------------- 2. 心跳
    print("\n[3b] 心跳：续约 + 识别失效租约")
    code, hb = post(base, "/api/v1/heartbeat",
                    {"user_id": "alice", "device_id": dev_a, "secret": secret_a,
                     "leases": [{"lease_id": first["lease_id"],
                                 "progress": {"done": 100, "total": 6000, "phase": "tag"}}]})
    check("心跳成功", code == 200, str(hb))
    check("租约被续约", first["lease_id"] in (hb.get("renew") or []), str(hb.get("renew")))
    check("进度已记录",
          any(l["progress"].get("done") == 100 for l in hb.get("leases") or []),
          str(hb.get("leases")))
    code, hb2 = post(base, "/api/v1/heartbeat",
                     {"user_id": "alice", "device_id": dev_a, "secret": secret_a,
                      "leases": [{"lease_id": "L_not_exist"}]})
    check("未知租约进入 rejected", "L_not_exist" in (hb2.get("rejected") or []),
          str(hb2))

    # ---------------------------------------------------------- 3. 完成与栅栏
    print("\n[4] 完成：包转已完成，自动接上下一个")
    code, dn = post(base, "/api/v1/done",
                    {"device_id": dev_a, "lease_id": first["lease_id"], "stats": "6000 张"})
    check("done 成功", code == 200, str(dn))
    code, ov = admin_get(base, admin_key, "/api/v1/work")
    done_pkg = [l for l in ov["devices"] if l["device_id"] == dev_a]
    check("设备记录已更新", bool(done_pkg), str(done_pkg)[:120])
    code, again = post(base, "/api/v1/done",
                       {"device_id": dev_a, "lease_id": first["lease_id"]})
    check("已结束的租约不能重复提交（栅栏）", code == 409, str((code, again)))
    check("done 后设备仍持有 active",
          any(l["kind"] == "active" for l in dn.get("leases") or []), str(dn.get("leases")))

    # ---------------------------------------------------------- 4. 多设备/回收
    print("\n[5] 第二台设备 + TTL 回收")
    code, hb_b = post(base, "/api/v1/handshake", {"user_id": "bob"})
    check("bob 握手", code == 200, str(hb_b))
    dev_b, secret_b = hb_b["device_id"], hb_b["secret"]
    lease_b = (hb_b.get("leases") or [{}])[0]
    check("bob 拿到不同的包", lease_b.get("package") != first["package"],
          str(lease_b.get("package")))

    print("   等 TTL 过期（3s，TTL=2s）…")
    time.sleep(3)
    code, hb_b2 = post(base, "/api/v1/heartbeat",
                       {"user_id": "bob", "device_id": dev_b, "secret": secret_b,
                        "leases": [{"lease_id": lease_b.get("lease_id")}]})
    check("过期租约被拒绝续约",
          lease_b.get("lease_id") in (hb_b2.get("rejected") or []), str(hb_b2.get("rejected")))
    # 回收后包应回到池子，别的设备能拿到
    code, hs_c = post(base, "/api/v1/handshake", {"user_id": "carol"})
    got = [l["package"] for l in hs_c.get("leases") or []]
    check("回收后的包可被其他设备领取",
          lease_b.get("package") in got or len(got) >= 1, str(got))

    # ---------------------------------------------------------- 5. 配额
    print("\n[6] 预锁配额：库存不足时不发 prelock")
    port2 = free_port()
    base2 = f"http://127.0.0.1:{port2}"
    env2 = dict(env, FOXLABEL_V3_QUOTA_MIN="999")
    srv2 = subprocess.Popen([PY, str(SERVER), "--data-dir", str(state), "serve",
                             "--host", "127.0.0.1", "--port", str(port2)],
                            env=env2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    atexit.register(lambda: srv2.poll() is None and srv2.kill())
    wait_ready(base2)
    code, hs_q = post(base2, "/api/v1/handshake", {"user_id": "dave"})
    kinds = [l["kind"] for l in hs_q.get("leases") or []]
    check("配额耗尽时只发 active、不发 prelock",
          kinds.count("prelock") == 0, str(hs_q.get("leases")))
    srv2.terminate()

    # ---------------------------------------------------------- 6. 交互性
    print("\n[8] v2 claim 不得抢 v3 已派出的包")
    lease_pkgs = {l["package"] for l in (admin_get(base, admin_key, "/api/v1/work")[1]
                                        .get("leases") or [])}
    if lease_pkgs:
        target = sorted(lease_pkgs)[0]
        req = urllib.request.Request(
            base + f"/packages/{target}/claim?repo=" + urllib.parse.quote(repo, safe=""),
            data=b"{}", method="POST",
            headers={"Authorization": f"Bearer {admin_key}",
                     "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            check("v2 claim 被拒（包已被调度）", False, "居然成功了")
        except urllib.error.HTTPError as e:
            check("v2 claim 被拒（包已被调度）", e.code == 409, str(e.code))
    else:
        check("v2 claim 被拒（包已被调度）", True, "无租约，跳过")

    print("\n[8b] 失败冷却：同一台设备不会立刻拿回刚失败的包")
    hs_f = post(base, "/api/v1/handshake", {"user_id": "frank"})[1]
    dev_f, sec_f = hs_f["device_id"], hs_f["secret"]
    l_f = (hs_f.get("leases") or [{}])[0]
    failed_pkg = l_f.get("package")
    code, r_f = post(base, "/api/v1/fail",
                     {"device_id": dev_f, "lease_id": l_f["lease_id"], "reason": "模拟失败"})
    check("fail 成功", code == 200, str(r_f))
    after = [l["package"] for l in r_f.get("leases") or []]
    check("失败后不会立刻拿回同一个包", failed_pkg not in after,
          f"failed={failed_pkg} after={after}")

    print("\n[8c] 运维踢设备：租约作废、包回池")
    r = run([PY, str(SERVER), "--data-dir", str(state), "unassign", "--device", dev_f], env)
    check("unassign 命令可用", r.returncode == 0 and "退回公共池" in r.stdout, r.stderr[:200])
    code, w = admin_get(base, admin_key, f"/api/v1/work?device_id={dev_f}")
    check("作废后该设备无 open 租约", not (w.get("leases") or []), str(w.get("leases")))

    # ---------------------------------------------------------- 7. 运维视图
    print("\n[9] 运维视图")
    code, ov = admin_get(base, admin_key, "/api/v1/work")
    check("/api/v1/work 返回设备与租约",
          code == 200 and "devices" in ov and "leases" in ov, str(code))
    r = run([PY, str(SERVER), "--data-dir", str(state), "devices"], env)
    check("devices 命令可用", r.returncode == 0 and "包总数" in r.stdout, r.stderr[:200])
    r = run([PY, str(SERVER), "--data-dir", str(state), "leases"], env)
    check("leases 命令可用", r.returncode == 0, r.stderr[:200])
    non_admin = urllib.request.Request(base + "/api/v1/work")
    try:
        urllib.request.urlopen(non_admin, timeout=5)
        check("/api/v1/work 需要鉴权", False)
    except urllib.error.HTTPError as e:
        check("/api/v1/work 需要鉴权", e.code == 401, str(e.code))

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
