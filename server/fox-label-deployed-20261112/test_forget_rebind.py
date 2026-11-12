"""本地回归：设备改绑放宽 + 强行解绑。

不起 HTTP，直接拿 dispatch 的三个入口打内存库，验证：
1. 首次 handshake 会派工，设备名下有 open 租约时不许改绑（给明确错误）；
2. forget_device 作废租约并删掉 devices 行；
3. 同一 device_id 之后可以用**另一个用户**正常握手；
4. 没有在跑租约时，改绑直接放行并留 v3_device_rebind 事件。
"""
import os
import sqlite3
import sys

import dispatch as v3

EVENTS: list[tuple] = []


def on_event(c, kind, user, device, detail=None):
    EVENTS.append((kind, user, device, detail))


def fresh():
    import server          # 只为了拿基础表结构（repos/packages/events）
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(server.SCHEMA_TABLES)
    c.executescript(server.SCHEMA_INDEXES)
    c.executescript(v3.SCHEMA_V3)
    c.execute("INSERT INTO repos(repo, repo_type, enabled, created_at)"
              " VALUES('r/x','dataset',1,'now')")
    for i in range(6):
        c.execute("INSERT INTO packages(repo,id,size,status,updated_at)"
                  " VALUES('r/x',?,?,'available','now')", (f"shard-{i:04d}.tar", 1024))
    c.commit()          # 不留隐式事务，dispatch 用显式 BEGIN IMMEDIATE
    return c


def handshake(c, user, dev, secret=None):
    return v3.handshake(c, v3.params(), {"user_id": user, "device_id": dev,
                                         "secret": secret, "client_ver": "t"},
                        on_event=on_event)


def main() -> int:
    fails = []

    def check(label, cond, extra=""):
        print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
        if not cond:
            fails.append(label)

    c = fresh()
    r = handshake(c, "alice", "dev-1")
    secret = r["secret"]
    check("首次握手签发 secret", bool(secret))
    check("首次握手会派工", len(r["leases"]) >= 1, f"n={len(r['leases'])}")

    # 1) 手上有 open 租约 → 改绑被拒
    try:
        handshake(c, "bob", "dev-1")
        check("有租约时改绑被拒", False)
    except v3.AuthError as e:
        check("有租约时改绑被拒", "强制解绑" in str(e), str(e))

    # 2) 强行解绑：租约作废 + 设备行删除
    res = v3.forget_device(c, "dev-1", "test_forget", on_event=on_event)
    check("强行解绑作废了租约", res["released"] >= 1, f"released={res['released']}")
    check("设备行已删除",
          c.execute("SELECT COUNT(*) n FROM devices WHERE device_id='dev-1'").fetchone()["n"] == 0)
    check("包全部回到未认领",
          c.execute("SELECT COUNT(*) n FROM packages WHERE status<>'available'").fetchone()["n"] == 0)
    check("留了 v3_device_forget 事件",
          any(e[0] == "v3_device_forget" for e in EVENTS))

    # 3) 同一 device_id 换用户重新登记
    r2 = handshake(c, "bob", "dev-1")
    check("换用户后可以正常握手", r2["user_id"] == "bob")
    check("新用户拿到自己的 secret", bool(r2["secret"]) and r2["secret"] != secret)
    check("设备归属已变成 bob",
          c.execute("SELECT user_id FROM devices WHERE device_id='dev-1'").fetchone()["user_id"]
          == "bob")

    # 4) 设备行还在、但手上没有 open 租约 → 直接改绑放行并留事件
    c.execute("UPDATE leases SET status='released' WHERE device_id='dev-1' AND status='open'")
    c.execute("UPDATE packages SET status='available', owner=NULL WHERE status<>'available'")
    c.commit()
    r3 = handshake(c, "carol", "dev-1")
    check("无租约时可改绑", r3["user_id"] == "carol")
    check("设备归属已变成 carol",
          c.execute("SELECT user_id FROM devices WHERE device_id='dev-1'").fetchone()["user_id"]
          == "carol")
    check("留了 v3_device_rebind 事件",
          any(e[0] == "v3_device_rebind" and "from=bob" in (e[3] or "") for e in EVENTS),
          str([e for e in EVENTS if e[0] == "v3_device_rebind"]))

    # 5) 旧用户的租约/审计没被删（历史保留）
    check("历史租约仍在库里",
          c.execute("SELECT COUNT(*) n FROM leases").fetchone()["n"] >= 2)
    c.close()

    # ---- 二、secret 口径：客户端自持 + 只查重 ----
    EVENTS.clear()
    c = fresh()
    sA = "flw_aaaa..."
    r = handshake(c, "dave", "d-dev", sA)
    check("首次登记用客户端带来的 secret（服务端不签发）", r["secret"] is None)
    check("库里的 secret 就是客户端那把",
          c.execute("SELECT secret FROM users WHERE user_id='dave'").fetchone()["secret"]
          == v3.hash_secret(sA))

    # 6) 同一把 secret 不能挂第二个名字
    try:
        handshake(c, "eve", "e-dev", sA)
        check("重复 secret 被拒", False)
    except v3.AuthError as e:
        check("重复 secret 被拒", "已经被用户" in str(e), str(e))

    # 7) 同名换机器：以本机 secret 为准，直接改绑 + 留痕
    sA2 = "flw_bbbb..."
    r = handshake(c, "dave", "d-dev2", sA2)
    check("同名换 secret 直接采纳", r["user_id"] == "dave")
    check("库里的 secret 已换成新的",
          c.execute("SELECT secret FROM users WHERE user_id='dave'").fetchone()["secret"]
          == v3.hash_secret(sA2))
    check("留了 v3_secret_adopt 事件",
          any(e[0] == "v3_secret_adopt" for e in EVENTS),
          str([e for e in EVENTS if e[0] == "v3_secret_adopt"]))
    # 旧 secret 从此不再被认（数据库里只剩新的那一份）

    # 8) user forget：租约作废、设备删、用户删，之后可重新登记
    res = v3.forget_user(c, "dave", "test_user_forget", on_event=on_event)
    check("user forget 删掉了用户行",
          c.execute("SELECT COUNT(*) n FROM users WHERE user_id='dave'").fetchone()["n"] == 0)
    check("user forget 删掉了设备行",
          c.execute("SELECT COUNT(*) n FROM devices WHERE user_id='dave'").fetchone()["n"] == 0)
    check("user forget 作废了租约", res["released"] >= 1, f"released={res['released']}")
    check("包全部回到未认领",
          c.execute("SELECT COUNT(*) n FROM packages WHERE status<>'available'").fetchone()["n"] == 0)

    r = handshake(c, "dave", "d-dev3", sA)
    check("forget 之后同名可重新登记", r["user_id"] == "dave" and r["secret"] is None)
    check("重新登记后 secret 用本机那把",
          c.execute("SELECT secret FROM users WHERE user_id='dave'").fetchone()["secret"]
          == v3.hash_secret(sA))
    c.close()

    # ---- 三、查询类接口改按设备鉴权（多机同名的关键）----
    EVENTS.clear()
    c = fresh()
    s1 = "flw_machine1..."
    s2 = "flw_machine2..."
    handshake(c, "frank", "f-dev1", s1)
    handshake(c, "frank", "f-dev2", s2)      # 第二台把 users.secret 改成了 s2

    def call(fn, dev, secret, user="frank"):
        body = {"user_id": user, "device_id": dev, "secret": secret}
        try:
            return True, fn(c, body)
        except (v3.AuthError, v3.LeaseError) as e:
            return False, e

    ok, _ = call(v3.status, "f-dev1", s1)
    check("第一台机器用自己那把 secret 查 status 仍成功", ok)
    ok, _ = call(v3.ping, "f-dev1", s1)
    check("第一台机器 idle ping 不被 401", ok)
    ok, res = call(v3.status, "f-dev1", "whatever-not-matching")
    check("同设备同用户：secret 完全不参与（乱填也认）", ok, str(res)[:40])
    ok, res = call(v3.status, "no-such-dev", s1)
    check("没登记的设备仍然 401", not ok, str(res)[:60])

    # strict 口径保持老行为
    os.environ["FOXLABEL_SECRET_POLICY"] = "strict"
    try:
        ok, res = call(v3.status, "f-dev2", s1)
        check("strict 口径仍然校验 secret", not ok, str(res)[:50])
    finally:
        os.environ.pop("FOXLABEL_SECRET_POLICY", None)
    c.close()

    # ---- 四、同一台机器改名字：secret 跟着机器走，用户行迁过去 ----
    EVENTS.clear()
    c = fresh()
    g1 = "flw_ginamachine..."
    handshake(c, "gina", "g-dev", g1)
    c.execute("UPDATE leases SET status='released' WHERE device_id='g-dev' AND status='open'")
    c.execute("UPDATE packages SET status='available', owner=NULL WHERE status<>'available'")
    c.commit()
    handshake(c, "gina2", "g-dev", g1)
    check("改名后旧用户行已消失",
          c.execute("SELECT COUNT(*) n FROM users WHERE user_id='gina'").fetchone()["n"] == 0)
    check("改名后新用户名在同一把 secret 上",
          c.execute("SELECT secret FROM users WHERE user_id='gina2'").fetchone()["secret"]
          == v3.hash_secret(g1))
    check("设备归属也跟到新名字",
          c.execute("SELECT user_id FROM devices WHERE device_id='g-dev'").fetchone()["user_id"]
          == "gina2")
    check("留了 v3_user_rename 事件",
          any(e[0] == "v3_user_rename" for e in EVENTS),
          str([e for e in EVENTS if e[0] == "v3_user_rename"]))

    # 有在跑的包时不允许改名（避免把包搅混）
    g2 = "flw_gina2machine..."
    handshake(c, "gina3", "g-dev2", g2)
    try:
        handshake(c, "gina4", "g-dev2", g2)
        check("有在跑的包时改名被拒", False)
    except v3.AuthError as e:
        check("有在跑的包时改名被拒", "还有在跑的包" in str(e), str(e))
    c.close()
    print()
    print(f"{'ALL PASS' if not fails else 'FAILED: ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
