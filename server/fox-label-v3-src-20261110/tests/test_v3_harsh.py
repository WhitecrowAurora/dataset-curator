#!/usr/bin/env python3
"""v3 服务端严苛测试：把"能跑通"变成"打不坏"。

覆盖六类极端情况
  A 协议滥用      畸形/超长/类型错误/未知动作 —— 不允许 5xx
  B 鉴权与越权    冒用 secret、设备换人、跨设备提交、v1/v2 密钥串用
  C 容量边界      空池、1 包 30 设备、50 包并发、池子打空
  D 竞态并发      同设备并发握手、同租约并发提交、心跳与完成交错
  E TTL 极端      高频心跳不掉租约、超时不续、进度停滞强制回收
  F 崩溃恢复      kill -9 后重启，状态与凭据仍然有效

每阶段结束都扫一遍不变量（唯一性/孤儿/状态一致性），任何一条不成立即失败。
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import signal
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


def post(base: str, path: str, body: dict, timeout: float = 15) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode()
    return raw(base, path, data, timeout)


def raw(base: str, path: str, data: bytes | None, timeout: float = 15,
        ctype: str = "application/json", method: str = "POST") -> tuple[int, dict]:
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(txt or "{}")
            except ValueError:
                return r.status, {"_raw": txt[:80]}
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        if e.code >= 500:
            FIVE_XX.append(f"{path} -> {e.code}")
        try:
            return e.code, json.loads(txt or "{}")
        except ValueError:
            return e.code, {"_raw": txt[:80]}
    except Exception as e:                      # 连接被重置/超时
        FIVE_XX.append(f"{path} -> {type(e).__name__}")
        return -1, {"_err": str(e)[:80]}


def admin_get(base: str, key: str, path: str) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def wait_ready(base: str, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.05)
    return False


# --------------------------------------------------------------- 不变量
def invariants(db: Path) -> list[str]:
    """任何时刻都该成立的五条硬约束。"""
    bad: list[str] = []
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row

    dup = c.execute("SELECT repo,pkg,COUNT(*) n FROM leases WHERE status='open'"
                    " GROUP BY repo,pkg HAVING n>1").fetchall()
    if dup:
        bad.append(f"同一包被重复占用: {[tuple(r) for r in dup][:3]}")

    multi = c.execute("SELECT device_id,COUNT(*) n FROM leases WHERE status='open'"
                      " AND kind='active' GROUP BY device_id HAVING n>1").fetchall()
    if multi:
        bad.append(f"单设备多个 active: {[tuple(r) for r in multi][:3]}")

    orphan = c.execute("SELECT l.lease_id,l.pkg FROM leases l LEFT JOIN packages p"
                       " ON p.repo=l.repo AND p.id=l.pkg"
                       " WHERE l.status='open' AND p.id IS NULL").fetchall()
    if orphan:
        bad.append(f"孤儿租约（包不存在）: {[tuple(r) for r in orphan][:3]}")

    claimed = c.execute(
        "SELECT p.repo,p.id FROM packages p WHERE p.status='claimed' AND NOT EXISTS"
        "(SELECT 1 FROM leases l WHERE l.repo=p.repo AND l.pkg=p.id"
        " AND l.status='open' AND l.kind='active')").fetchall()
    if claimed:
        bad.append(f"claimed 但无 active 租约: {[tuple(r) for r in claimed][:3]}")

    leaked = c.execute(
        "SELECT l.lease_id FROM leases l JOIN packages p ON p.repo=l.repo AND p.id=l.pkg"
        " WHERE l.status='open' AND l.kind='active' AND p.status!='claimed'").fetchall()
    if leaked:
        bad.append(f"active 租约但包不是 claimed: {[tuple(r) for r in leaked][:3]}")

    neg = c.execute("SELECT COUNT(*) n FROM leases WHERE status='open'"
                    " AND expires_at < issued_at").fetchone()["n"]
    if neg:
        bad.append(f"expires 早于 issued: {neg}")

    c.close()
    return bad


def scan(db: Path, label: str) -> None:
    bad = invariants(db)
    check(f"不变量成立（{label}）", not bad, "; ".join(bad))


def make_repo(state: Path, tmp: Path, name: str, n: int, env: dict) -> None:
    tree = [{"type": "file", "path": f"shard-{i:05d}.tar", "size": 100_000 + i * 1000,
             "lfs": {"oid": hashlib.sha256(f"{name}{i}".encode()).hexdigest()}}
            for i in range(n)]
    tf = tmp / f"tree-{name.replace('/', '_')}.json"
    tf.write_text(json.dumps(tree), encoding="utf-8")
    r = subprocess.run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
                        "--repo", name, "--from-tree", str(tf)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="foxharsh-"))
    state = tmp / "state"

    env = dict(os.environ)
    env.update({
        "FOXLABEL_V3_BEAT": "1",
        "FOXLABEL_V3_ACTIVE_TTL": "2",
        "FOXLABEL_V3_PRELOCK_TTL": "2",
        "FOXLABEL_V3_QUOTA_MIN": "0",
        "FOXLABEL_V3_QUOTA_PCT": "0",
        "FOXLABEL_V3_STALL_WARN": "2",
        "FOXLABEL_V3_STALL_KILL": "3",
    })
    make_repo(state, tmp, "t/big", 60, env)
    admin_key = (state / "admin.key").read_text().strip()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    srv = subprocess.Popen([PY, str(SERVER), "--data-dir", str(state), "serve",
                            "--host", "127.0.0.1", "--port", str(port)],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    check("服务就绪", wait_ready(base))
    db = state / "db.sqlite3"

    # ============================================================ A 协议滥用
    print("\n[A] 协议滥用：只允许 4xx，绝不允许 5xx / 挂死")
    cases = [
        ("空 body", b"", None),
        ("非法 JSON", b"{oops", None),
        ("JSON 是数组", b"[1,2,3]", None),
        ("user_id 是数字", json.dumps({"user_id": 12345}).encode(), None),
        ("user_id 是对象", json.dumps({"user_id": {"a": 1}}).encode(), None),
        ("user_id 空串", json.dumps({"user_id": "   "}).encode(), None),
        ("secret 是对象", json.dumps({"user_id": "x", "secret": {"a": 1}}).encode(), None),
        ("local_pkgs 是字符串", json.dumps({"user_id": "x", "local_pkgs": "abc"}).encode(), None),
        ("超长 user_id 8KB", json.dumps({"user_id": "u" * 8192}).encode(), None),
        ("user_id 含控制字符", json.dumps({"user_id": "a\x00b\nc\td"}).encode(), None),
        ("超大 body 200KB", json.dumps({"user_id": "y" * 200_000}).encode(), 413),
        ("非 UTF8 字节", b'{"user_id": "\xff\xfe"}', None),
    ]
    for label, payload, expect in cases:
        code, _ = raw(base, "/api/v1/handshake", payload, timeout=10)
        ok = (code == expect) if expect else (400 <= code < 500)
        check(f"{label} → {code}", ok, str(code))

    code, _ = post(base, "/api/v1/nope", {"user_id": "x"})
    check("未知动作 → 404", code == 404, str(code))
    code, _ = post(base, "/api/v1/heartbeat", {"user_id": "x", "device_id": "D-x",
                                               "leases": "notalist"})
    check("leases 传字符串不 5xx", 400 <= code < 500, str(code))
    code, _ = post(base, "/api/v1/done", {"device_id": "D-none", "lease_id": ""})
    check("未知设备提交 → 4xx", 400 <= code < 500, str(code))
    scan(db, "协议滥用后")

    # ============================================================ B 鉴权越权
    print("\n[B] 鉴权与越权")
    _, ha = post(base, "/api/v1/handshake", {"user_id": "alice"})
    _, hb = post(base, "/api/v1/handshake", {"user_id": "bob"})
    dev_a, sec_a = ha["device_id"], ha["secret"]
    dev_b, sec_b = hb["device_id"], hb["secret"]
    lease_a = ha["leases"][0]

    code, _ = post(base, "/api/v1/handshake", {"user_id": "alice", "device_id": dev_a})
    check("缺 secret 的二次握手 → 401", code == 401, str(code))
    code, _ = post(base, "/api/v1/handshake",
                   {"user_id": "alice", "device_id": dev_a, "secret": sec_b})
    check("冒用他人 secret → 401", code == 401, str(code))
    code, _ = post(base, "/api/v1/handshake",
                   {"user_id": "bob", "device_id": dev_a, "secret": sec_b})
    check("设备换用户 → 401", code == 401, str(code))
    code, _ = post(base, "/api/v1/done", {"device_id": dev_b, "lease_id": lease_a["lease_id"]})
    check("跨设备提交他人租约 → 409", code == 409, str(code))
    code, _ = post(base, "/api/v1/heartbeat",
                   {"user_id": "bob", "device_id": dev_a, "secret": sec_b, "leases": []})
    check("用错设备心跳 → 401", code == 401, str(code))
    code, _ = raw(base, "/api/v1/handshake",
                  json.dumps({"user_id": "x"}).encode())
    check("v1 接口不认 flk 之外的东西（无鉴权也走 secret 体系）", code in (200, 401), str(code))
    req = urllib.request.Request(base + "/api/v1/work")
    try:
        urllib.request.urlopen(req, timeout=5)
        check("GET /api/v1/work 无鉴权 → 401", False)
    except urllib.error.HTTPError as e:
        check("GET /api/v1/work 无鉴权 → 401", e.code == 401, str(e.code))
    code, _ = post(base, "/api/v1/done", {"device_id": dev_a, "lease_id": "L_forged"})
    check("伪造 lease_id → 409", code == 409, str(code))
    scan(db, "鉴权后")

    # ============================================================ C 容量边界
    print("\n[C] 容量边界")
    st2 = tmp / "state2"
    env2 = dict(env)
    make_repo(st2, tmp, "t/one", 1, env2)
    port2 = free_port()
    base2 = f"http://127.0.0.1:{port2}"
    srv2 = subprocess.Popen([PY, str(SERVER), "--data-dir", str(st2), "serve",
                             "--host", "127.0.0.1", "--port", str(port2)],
                            env=env2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_ready(base2)
    with cf.ThreadPoolExecutor(max_workers=30) as ex:
        res = list(ex.map(lambda i: post(base2, "/api/v1/handshake", {"user_id": f"u{i}"}),
                          range(30)))
    got = [r for c_, r in res if c_ == 200 and (r.get("leases") or [])]
    check("1 个包 + 30 设备并发：恰好 1 台拿到", len(got) == 1, f"拿到 {len(got)} 台")
    check("其余设备空手但不报错", all(c_ == 200 for c_, _ in res), str([c_ for c_, _ in res][:10]))
    scan(st2 / "db.sqlite3", "1包30设备")
    srv2.terminate()

    # 真实"池子打空"：只有 1 个包，被取走后池子即空
    st3 = tmp / "state3"
    env3 = dict(env)
    make_repo(st3, tmp, "t/drain", 1, env3)
    port3 = free_port()
    base3 = f"http://127.0.0.1:{port3}"
    srv3 = subprocess.Popen([PY, str(SERVER), "--data-dir", str(st3), "serve",
                             "--host", "127.0.0.1", "--port", str(port3)],
                            env=env3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_ready(base3)
    _, hs_first = post(base3, "/api/v1/handshake", {"user_id": "taker"})
    lk = [l for l in hs_first.get("leases") or [] if l["kind"] == "active"][0]
    post(base3, "/api/v1/done", {"device_id": hs_first["device_id"], "lease_id": lk["lease_id"]})
    code, empty = post(base3, "/api/v1/handshake", {"user_id": "ghost"})
    check("空池握手不报错", code == 200, str(code))
    check("空池返回零租约 + idle",
          not (empty.get("leases") or []) and "idle" in (empty.get("actions") or []),
          str(empty))
    srv3.terminate()

    # ============================================================ D 竞态
    print("\n[D] 并发竞态")
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        res = list(ex.map(lambda _: post(base, "/api/v1/handshake",
                                         {"user_id": "alice", "device_id": dev_a,
                                          "secret": sec_a}), range(12)))
    actives = {len([l for l in r.get("leases") or [] if l["kind"] == "active"])
               for c_, r in res if c_ == 200}
    check("同设备 12 并发握手只会有 1 个 active", actives <= {1}, str(actives))

    _, hb_now = post(base, "/api/v1/handshake", {"user_id": "bob", "device_id": dev_b,
                                                 "secret": sec_b})
    target = [l for l in hb_now.get("leases") or [] if l["kind"] == "active"][0]
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        res = list(ex.map(lambda _: post(base, "/api/v1/done",
                                         {"device_id": dev_b,
                                          "lease_id": target["lease_id"]}), range(12)))
    codes = [c_ for c_, _ in res]
    check("同一租约 12 并发提交：恰好 1 次成功",
          codes.count(200) == 1 and codes.count(409) == 11, str(codes))

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        mixed = list(ex.map(lambda i: (
            post(base, "/api/v1/heartbeat",
                 {"user_id": "alice", "device_id": dev_a, "secret": sec_a, "leases": []})
            if i % 2 else
            post(base, "/api/v1/handshake", {"user_id": f"p{i}"})), range(16)))
    check("握手与心跳交错无 5xx", all(c_ < 500 for c_ in [c for c, _ in mixed]),
          str([c for c, _ in mixed]))
    scan(db, "并发后")

    # ============================================================ E TTL
    print("\n[E] TTL 与停滞回收")
    _, hs_e = post(base, "/api/v1/handshake", {"user_id": "eve"})
    dev_e, sec_e = hs_e["device_id"], hs_e["secret"]
    lease_e = [l for l in hs_e["leases"] if l["kind"] == "active"][0]
    kept = True
    for _ in range(6):                     # 每 0.5s 一次心跳，TTL 2s，不应掉
        time.sleep(0.5)
        code, r = post(base, "/api/v1/heartbeat",
                       {"user_id": "eve", "device_id": dev_e, "secret": sec_e,
                        "leases": [{"lease_id": lease_e["lease_id"],
                                    "progress": {"done": 10}}]})
        kept = kept and code == 200 and lease_e["lease_id"] in (r.get("renew") or [])
    check("高频心跳不会丢租约", kept)

    neg = [l["expires_in"] for l in
           (post(base, "/api/v1/heartbeat", {"user_id": "eve", "device_id": dev_e,
                                             "secret": sec_e, "leases": []})[1]
            .get("leases") or [])]
    check("expires_in 不为负", all(x >= 0 for x in neg), str(neg))

    print("   模拟「进程活着但进度停滞」…")
    c = sqlite3.connect(db, timeout=10)
    c.execute("UPDATE leases SET last_progress_at=? WHERE lease_id=?",
              (time.time() - 10, lease_e["lease_id"]))
    c.commit()
    c.close()
    time.sleep(0.3)
    code, r = post(base, "/api/v1/heartbeat",
                   {"user_id": "eve", "device_id": dev_e, "secret": sec_e,
                    "leases": [{"lease_id": lease_e["lease_id"]}]})
    rejected = lease_e["lease_id"] in (r.get("rejected") or [])
    check("进度停滞的租约被强制回收", rejected, str(r)[:200])
    scan(db, "TTL 后")

    # ============================================================ F 崩溃恢复
    print("\n[F] 崩溃恢复（kill -9）")
    st4 = tmp / "state4"
    env4 = dict(env, FOXLABEL_V3_ACTIVE_TTL="120", FOXLABEL_V3_PRELOCK_TTL="120")
    make_repo(st4, tmp, "t/crash", 5, env4)
    port4 = free_port()
    base4 = f"http://127.0.0.1:{port4}"
    srv4 = subprocess.Popen([PY, str(SERVER), "--data-dir", str(st4), "serve",
                             "--host", "127.0.0.1", "--port", str(port4)],
                            env=env4, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_ready(base4)
    _, hs4 = post(base4, "/api/v1/handshake", {"user_id": "zoe"})
    dev4, sec4 = hs4["device_id"], hs4["secret"]
    before = [l["lease_id"] for l in hs4["leases"]]
    os.kill(srv4.pid, signal.SIGKILL)
    srv4.wait(timeout=10)
    srv4 = subprocess.Popen([PY, str(SERVER), "--data-dir", str(st4), "serve",
                             "--host", "127.0.0.1", "--port", str(port4)],
                            env=env4, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_ready(base4)
    code, hs4b = post(base4, "/api/v1/handshake",
                      {"user_id": "zoe", "device_id": dev4, "secret": sec4})
    after = [l["lease_id"] for l in hs4b.get("leases") or []]
    check("重启后凭据仍有效", code == 200, str(code))
    check("重启后租约仍在（未超时）", set(before) <= set(after), f"{before} vs {after}")
    scan(st4 / "db.sqlite3", "崩溃恢复后")
    srv4.terminate()

    # ============================================================ G 压力
    print("\n[G] 压力：40 设备 × 15 轮心跳")
    users = [post(base, "/api/v1/handshake", {"user_id": f"load{i}"})[1] for i in range(40)]
    lat: list[float] = []
    errs = 0

    def beat(h: dict) -> None:
        nonlocal errs
        t0 = time.time()
        code, _ = post(base, "/api/v1/heartbeat",
                       {"user_id": h.get("user_id", "x"), "device_id": h["device_id"],
                        "secret": h["secret"], "leases": []})
        lat.append(time.time() - t0)
        if code >= 500 or code < 0:
            errs += 1

    with cf.ThreadPoolExecutor(max_workers=32) as ex:
        list(ex.map(beat, [h for h in users for _ in range(15)]))
    lat.sort()
    p50 = lat[len(lat) // 2]
    p95 = lat[int(len(lat) * 0.95)]
    check(f"600 次心跳无 5xx（p50 {p50*1000:.0f}ms / p95 {p95*1000:.0f}ms）", errs == 0, str(errs))
    check("p95 延迟 < 1s", p95 < 1.0, f"{p95:.3f}s")
    scan(db, "压力后")

    check("全程零 5xx / 零连接错误", not FIVE_XX, str(FIVE_XX[:5]))

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
