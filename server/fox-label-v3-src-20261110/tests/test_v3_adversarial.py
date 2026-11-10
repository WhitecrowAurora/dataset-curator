#!/usr/bin/env python3
"""v3 对抗性测试：状态机误用、数值污染、时钟跳变、数据被改坏、协议畸形、模糊输入。

与 test_v3_harsh 的分工
  harsh      覆盖"正常但极端"的规模与并发
  adversarial 覆盖"客户端不老实 / 数据被改坏 / 数值越界 / 时钟异常"，以及随机模糊输入。

判定原则：任何输入都必须得到一个**明确的 4xx/5xx 之外的答案**，
服务端不得崩溃、不得写入非法数据、不得让包永久卡死。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
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
FIVE_XX: list[str] = []


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


def raw(base: str, path: str, data: bytes, timeout: float = 15,
        method: str = "POST", ctype: str = "application/json") -> tuple[int, bytes]:
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        if e.code >= 500:
            FIVE_XX.append(f"{path} -> {e.code}")
        return e.code, body
    except Exception as e:
        FIVE_XX.append(f"{path} -> {type(e).__name__}")
        return -1, str(e).encode()[:120]


def post(base: str, path: str, body: dict) -> tuple[int, dict]:
    code, raw_body = raw(base, path, json.dumps(body).encode())
    try:
        return code, json.loads(raw_body.decode() or "{}")
    except ValueError:
        return code, {"_raw": raw_body[:120].decode("utf-8", "replace")}


def strict_json(text: str) -> dict:
    """拒绝 NaN / Infinity 的严格解析：非有限数会让严格客户端直接崩。"""
    def bad(x):
        raise ValueError(f"非有限数 {x}")
    return json.loads(text, parse_constant=bad)


def admin_get(base: str, key: str, path: str) -> tuple[int, str]:
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""


def wait_ready(base: str, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.05)
    return False


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="foxadv-"))
    state = tmp / "state"
    env = dict(os.environ)
    env.update({
        "FOXLABEL_V3_BEAT": "1", "FOXLABEL_V3_ACTIVE_TTL": "60",
        "FOXLABEL_V3_PRELOCK_TTL": "60", "FOXLABEL_V3_QUOTA_MIN": "2",
        "FOXLABEL_V3_QUOTA_PCT": "0", "FOXLABEL_V3_LEASE_MAX_H": "168",
        "FOXLABEL_V3_STALL_WARN": "3600", "FOXLABEL_V3_STALL_KILL": "7200",
    })
    tree = [{"type": "file", "path": f"shard-{i:05d}.tar", "size": 100_000 + i * 1000,
             "lfs": {"oid": hashlib.sha256(str(i).encode()).hexdigest()}} for i in range(20)]
    tf = tmp / "tree.json"
    tf.write_text(json.dumps(tree), encoding="utf-8")
    subprocess.run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
                    "--repo", "t/adv", "--from-tree", str(tf)],
                   capture_output=True, check=True, env=env)
    admin_key = (state / "admin.key").read_text().strip()
    db = state / "db.sqlite3"

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    srv = subprocess.Popen([PY, str(SERVER), "--data-dir", str(state), "serve",
                            "--host", "127.0.0.1", "--port", str(port)],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check("服务就绪", wait_ready(base))

    # ==================================================== A 状态机误用
    print("\n[A] 状态机误用：客户端搞错租约类型/重复动作")
    _, h = post(base, "/api/v1/handshake", {"user_id": "mis"})
    dev, sec = h["device_id"], h["secret"]
    act = [l for l in h["leases"] if l["kind"] == "active"][0]
    pre = [l for l in h["leases"] if l["kind"] == "prelock"][0]

    c = sqlite3.connect(db)
    before = c.execute("SELECT status FROM packages WHERE id=?", (pre["package"],)).fetchone()[0]
    c.close()
    code, r = post(base, "/api/v1/done", {"device_id": dev, "lease_id": pre["lease_id"]})
    c = sqlite3.connect(db)
    after = c.execute("SELECT status FROM packages WHERE id=?", (pre["package"],)).fetchone()[0]
    c.close()
    check("对预锁调 done → 409", code == 409, str((code, r)))
    check("预锁对应的包状态不变（没被误标完成）", before == after, f"{before} → {after}")

    code, _ = post(base, "/api/v1/done",
                   {"device_id": dev, "lease_id": act["lease_id"], "stats": "ok"})
    check("正常提交 active 成功", code == 200, str(code))
    code, _ = post(base, "/api/v1/fail", {"device_id": dev, "lease_id": act["lease_id"]})
    check("已完成的租约再 fail → 409", code == 409, str(code))
    code, r = post(base, "/api/v1/heartbeat",
                   {"user_id": "mis", "device_id": dev, "secret": sec,
                    "leases": [{"lease_id": act["lease_id"]}]})
    check("已结束租约出现在 rejected", act["lease_id"] in (r.get("rejected") or []), str(r))

    # 跨设备续约：别人的租约不能被续
    _, h2 = post(base, "/api/v1/handshake", {"user_id": "other"})
    victim = [l for l in h2["leases"] if l["kind"] == "active"][0]
    code, r = post(base, "/api/v1/heartbeat",
                   {"user_id": "mis", "device_id": dev, "secret": sec,
                    "leases": [{"lease_id": victim["lease_id"]}]})
    check("跨设备续约被拒（rejected）", victim["lease_id"] in (r.get("rejected") or []), str(r))

    code, r = post(base, "/api/v1/heartbeat",
                   {"user_id": "mis", "device_id": dev, "secret": sec,
                    "leases": [{"lease_id": pre["lease_id"]},
                               {"lease_id": pre["lease_id"]}]})
    check("同一租约在心跳里重复出现不报错", code == 200, str(code))

    # ==================================================== B 数值污染
    print("\n[B] 数值污染：NaN / Infinity / 负数 / 超大 / 嵌套")
    _, hb = post(base, "/api/v1/handshake", {"user_id": "num"})
    devb, secb = hb["device_id"], hb["secret"]
    lk = [l for l in hb["leases"] if l["kind"] == "active"][0]["lease_id"]
    payloads = [
        b'{"user_id":"num","device_id":"%s","secret":"%s","leases":[{"lease_id":"%s",'
        b'"progress":{"done":NaN,"total":0}}]}',
        b'{"user_id":"num","device_id":"%s","secret":"%s","leases":[{"lease_id":"%s",'
        b'"progress":{"done":Infinity}}]}',
        b'{"user_id":"num","device_id":"%s","secret":"%s","leases":[{"lease_id":"%s",'
        b'"progress":{"done":-999,"total":1e309}}]}',
        b'{"user_id":"num","device_id":"%s","secret":"%s","leases":[{"lease_id":"%s",'
        b'"progress":{"done":999999999999999999999}}]}',
        b'{"user_id":"num","device_id":"%s","secret":"%s","leases":[{"lease_id":"%s",'
        b'"progress":{"done":{"a":[1,2]}}}]}',
    ]
    codes = []
    for body in payloads:
        code, _ = raw(base, "/api/v1/heartbeat",
                      body % (devb.encode(), secb.encode(), lk.encode()))
        codes.append(code)
    check("畸形数值一律不 5xx", all(c < 500 for c in codes), str(codes))

    code, wk = admin_get(base, admin_key, "/api/v1/work")
    try:
        strict_json(wk)
        strict = True
    except ValueError as e:
        strict = False
    check("看板返回体是严格 JSON（无 NaN/Infinity）", strict, str(e) if not strict else "")
    check("看板不 5xx", code == 200, str(code))

    c = sqlite3.connect(db)
    spd, tier = c.execute("SELECT speed, tier FROM devices WHERE device_id=?",
                          (devb,)).fetchone()
    prog = c.execute("SELECT progress_json FROM leases WHERE lease_id=?", (lk,)).fetchone()[0]
    c.close()
    check("速率与档位未被污染", isinstance(spd, (int, float)) and spd >= 0,
          f"speed={spd!r} tier={tier}")
    check("落库进度是合法 JSON", json.loads(prog or "{}") is not None, str(prog)[:80])

    # ==================================================== C 时钟跳变
    print("\n[C] 时钟跳变：expiry 被改到未来一年 / issued_at 被改到过去")
    _, hc = post(base, "/api/v1/handshake", {"user_id": "clock"})
    devc, secc = hc["device_id"], hc["secret"]
    lc = [l for l in hc["leases"] if l["kind"] == "active"][0]["lease_id"]
    c = sqlite3.connect(db)
    c.execute("UPDATE leases SET expires_at=? WHERE lease_id=?",
              (time.time() + 365 * 86400, lc))
    c.commit()
    c.close()
    code, r = post(base, "/api/v1/heartbeat",
                   {"user_id": "clock", "device_id": devc, "secret": secc,
                    "leases": [{"lease_id": lc}]})
    remaining = [l["expires_in"] for l in (r.get("leases") or []) if l["lease_id"] == lc]
    check("被篡改的 expiry 会在续约时被夹回正常 TTL",
          bool(remaining) and remaining[0] <= 62, f"expires_in={remaining}")

    c = sqlite3.connect(db)
    c.execute("UPDATE leases SET issued_at=? WHERE lease_id=?", (time.time() - 200 * 3600, lc))
    c.commit()
    c.close()
    code, r = post(base, "/api/v1/heartbeat",
                   {"user_id": "clock", "device_id": devc, "secret": secc,
                    "leases": [{"lease_id": lc}]})
    check("超过绝对寿命（168h）的租约被回收",
          lc in (r.get("rejected") or []), str(r)[:220])

    # ==================================================== D 数据被改坏
    print("\n[D] 数据被改坏：孤儿租约、设备被删")
    _, hd = post(base, "/api/v1/handshake", {"user_id": "orphan"})
    devd, secd = hd["device_id"], hd["secret"]
    ld = [l for l in hd["leases"] if l["kind"] == "active"][0]
    c = sqlite3.connect(db)
    c.execute("DELETE FROM packages WHERE repo=? AND id=?", ("t/adv", ld["package"]))
    c.commit()
    c.close()
    code, r = post(base, "/api/v1/heartbeat",
                   {"user_id": "orphan", "device_id": devd, "secret": secd,
                    "leases": [{"lease_id": ld["lease_id"]}]})
    check("包被删后租约被当作孤儿回收", ld["lease_id"] in (r.get("rejected") or []), str(r))
    check("孤儿回收不影响服务可用性", code == 200, str(code))

    _, he = post(base, "/api/v1/handshake", {"user_id": "ghostdev"})
    dev_e = he["device_id"]
    c = sqlite3.connect(db)
    c.execute("DELETE FROM devices WHERE device_id=?", (dev_e,))
    c.commit()
    c.close()
    code, r = post(base, "/api/v1/heartbeat",
                   {"user_id": "orphan", "device_id": devd, "secret": secd, "leases": []})
    check("设备被删后其租约也被清掉", code == 200, str(code))
    c = sqlite3.connect(db)
    n = c.execute("SELECT COUNT(*) n FROM leases WHERE device_id=? AND status='open'",
                  (dev_e,)).fetchone()[0]
    c.close()
    check("该设备已无 open 租约", n == 0, str(n))

    # ==================================================== E 协议畸形
    print("\n[E] 协议畸形：方法错配 / chunked / Content-Type / 慢速半包")
    code, _ = raw(base, "/api/v1/handshake", b"", method="GET")
    check("GET 打 POST 端点 → 4xx", 400 <= code < 500, str(code))
    code, _ = raw(base, "/api/v1/handshake",
                  json.dumps({"user_id": "ct"}).encode(), ctype="text/plain")
    check("Content-Type 不符仍按 JSON 处理（不 5xx）", code in (200, 400, 401), str(code))

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps({"user_id": "chunky"}).encode()
    conn.request("POST", "/api/v1/handshake", body=iter([body]),
                 headers={"Content-Type": "application/json"},
                 encode_chunked=True)
    try:
        resp = conn.getresponse()
        code = resp.status
        resp.read()
    except Exception as e:
        code = -1
    conn.close()
    check("chunked 请求不导致 5xx", code != -1 and code < 500, str(code))

    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"POST /api/v1/handshake HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{")
    time.sleep(0.5)
    try:
        urllib.request.urlopen(base + "/health", timeout=3)
        alive = True
    except Exception:
        alive = False
    s.close()
    check("慢速半包连接不影响其他请求", alive, "health 不可用")

    # ==================================================== F 模糊输入
    print("\n[F] 模糊输入：200 个随机 body 打四个端点")
    rnd = random.Random(20261110)
    atoms = [None, True, False, 0, -1, 10 ** 18, -10 ** 18, 3.5, float("inf"), "", "x",
             "ｕ" * 70, "\x00", "\u200b", "../etc/passwd", "'; DROP TABLE users;--",
             "🎉", [], {}, [1, "a"], {"k": {"n": [1]}}]
    keys = ["user_id", "device_id", "secret", "client_ver", "local_pkgs", "leases",
            "lease_id", "progress", "reason", "stats", "extra"]

    def rand_val(depth=0):
        r = rnd.random()
        if depth < 2 and r < 0.2:
            return {rnd.choice(keys): rand_val(depth + 1) for _ in range(rnd.randint(0, 3))}
        if depth < 2 and r < 0.3:
            return [rand_val(depth + 1) for _ in range(rnd.randint(0, 3))]
        return rnd.choice(atoms)

    bad = []
    for i in range(200):
        body = {rnd.choice(keys): rand_val() for _ in range(rnd.randint(0, 5))}
        path = rnd.choice(["/api/v1/handshake", "/api/v1/heartbeat",
                           "/api/v1/done", "/api/v1/fail"])
        try:
            payload = json.dumps(body, allow_nan=True).encode()
        except (TypeError, ValueError):
            payload = b"{}"
        code, _ = raw(base, path, payload, timeout=10)
        if code < 0 or code >= 500:
            bad.append((path, code, str(body)[:60]))
    check("200 个随机 body 零 5xx / 零连接错误", not bad, str(bad[:3]))

    # ==================================================== G 公平与配额
    print("\n[G] 公平与配额边界")
    _, hf = post(base, "/api/v1/handshake", {"user_id": "slow"})
    devf = hf["device_id"]
    c = sqlite3.connect(db)
    c.execute("UPDATE devices SET tier='slow', speed=0.001 WHERE device_id=?", (devf,))
    c.commit()
    c.close()
    code, r = post(base, "/api/v1/heartbeat",
                   {"user_id": "slow", "device_id": devf, "secret": hf["secret"], "leases": []})
    kinds = [l["kind"] for l in r.get("leases") or []]
    check("慢设备仍能拿到 active（保底）", "active" in kinds, str(kinds))
    check("慢设备不发预锁（避免占库存）", "prelock" not in kinds, str(kinds))

    check("全程零 5xx", not FIVE_XX, str(FIVE_XX[:5]))
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
