"""明细独立于事件流；按租约和 image_id 去重，客户端重启后可安全重发。"""
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS skip_items (
 lease_id TEXT NOT NULL, image_id TEXT NOT NULL, kind TEXT NOT NULL,
 reason TEXT NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(lease_id,image_id)
);
"""


class SkipError(ValueError):
    pass


def ensure_schema(c):
    c.executescript(SCHEMA)


def receive(c, dispatch, body):
    if not isinstance(body, dict):
        raise SkipError("明细请求必须是对象")
    user = dispatch._sid(body.get("user_id"), "user_id")
    device_id = dispatch._sid(body.get("device_id"), "device_id")
    lease_id = dispatch._sid(body.get("lease_id"), "lease_id")
    with dispatch._Tx(c):
        device = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if not device or device["user_id"] != user:
            raise dispatch.AuthError("设备与用户不匹配")
        dispatch._require_device(c, user, device_id, body.get("secret"))
        account = c.execute("SELECT status FROM users WHERE user_id=?", (user,)).fetchone()
        if not account or account["status"] != "active" or device["status"] in ("archived", "blocked"):
            raise dispatch.AuthError("用户或设备已停用")
        lease = c.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
        if (not lease or lease["user_id"] != user or lease["device_id"] != device_id
                or lease["kind"] not in ("active","offline") or lease["status"] != "open"):
            raise dispatch.LeaseError("当前身份没有这个开放的 active 租约")
        if lease["kind"]=="offline":
            from assignments.offline import authenticate
            authenticate(c,dispatch,lease,body.get("secret"))
        if body.get("package") != lease["pkg"]:
            raise SkipError("明细分片与租约不匹配")
        batch_no, items = body.get("batch_no"), body.get("items")
        if type(batch_no) is not int or batch_no < 1:
            raise SkipError("批次号必须为正整数")
        if not isinstance(items, list) or len(items) > 200:
            raise SkipError("每批明细应为数组，最多 200 条")
        now, cleaned = time.time(), []
        for item in items:
            if not isinstance(item, dict):
                raise SkipError("明细条目必须是对象")
            image_id, kind, reason = item.get("image_id"), item.get("kind"), item.get("reason", "")
            if not isinstance(image_id, str) or not image_id.strip() or len(image_id) > 512:
                raise SkipError("image_id 不能为空或超过 512 字符")
            if kind not in ("skipped", "excused", "excluded") or not isinstance(reason, str) or len(reason) > 2000:
                raise SkipError("明细类型或原因格式不正确")
            cleaned.append((lease_id, image_id.strip(), kind, reason, now))
        c.executemany("INSERT INTO skip_items VALUES(?,?,?,?,?) ON CONFLICT(lease_id,image_id)"
                      " DO UPDATE SET kind=excluded.kind,reason=excluded.reason,updated_at=excluded.updated_at"
                      " WHERE skip_items.kind<>excluded.kind OR skip_items.reason<>excluded.reason", cleaned)
        total = c.execute("SELECT COUNT(*) FROM skip_items WHERE lease_id=?", (lease_id,)).fetchone()[0]
    return {"ok": True, "accepted": len(items), "batch_no": batch_no, "total": total}


def view(c, repo, package, offset=0):
    if not repo or not package:
        raise SkipError("请指定仓库和分片")
    try:
        offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise SkipError("分页位置必须是非负整数") from exc
    if offset < 0:
        raise SkipError("分页位置必须是非负整数")
    query = (" FROM skip_items s JOIN leases l ON l.lease_id=s.lease_id"
             " WHERE l.repo=? AND l.pkg=? AND l.status IN ('open','done')")
    params = (repo, package)
    counts = {r["kind"]: r["n"] for r in c.execute("SELECT s.kind,COUNT(*) n" + query + " GROUP BY s.kind", params)}
    total = sum(counts.values())
    rows = c.execute("SELECT s.image_id,s.kind,s.reason,s.lease_id,l.device_id,l.status lease_status"
                     + query + " ORDER BY l.issued_at,s.image_id LIMIT 100 OFFSET ?", (*params, offset))
    items = [dict(row) for row in rows]
    next_offset = offset + len(items)
    return {"ok": True, "total": total, "counts": counts, "items": items,
            "next_offset": next_offset if next_offset < total else None}
