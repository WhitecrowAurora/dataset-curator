"""只预留分包，真正执行仍走原来的 active/prelock 租约。"""
import time
from scheduling.transitions import atomic


def ensure_schema(c):
    c.execute("CREATE TABLE IF NOT EXISTS package_queue (repo TEXT NOT NULL,pkg TEXT NOT NULL,"
              "user_id TEXT NOT NULL,device_id TEXT NOT NULL,actor TEXT NOT NULL,created_at REAL NOT NULL,"
              "PRIMARY KEY(repo,pkg))")
    c.execute("CREATE INDEX IF NOT EXISTS idx_package_queue_device ON package_queue(device_id)")


def clear_device(c, device_id):
    c.execute("DELETE FROM package_queue WHERE device_id=?", (device_id,))
    c.execute("DELETE FROM part_assignments WHERE target_type='device' AND target_id=?", (device_id,))
    c.execute("UPDATE devices SET queue_only=0 WHERE device_id=?", (device_id,))


def trim_workspace(c, ws_id, repos):
    query = ("DELETE FROM package_queue WHERE device_id IN (SELECT d.device_id FROM devices d"
             " JOIN users u ON u.user_id=d.user_id WHERE COALESCE(d.workspace_id,u.workspace_id)=?)"
             " AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.repo=package_queue.repo"
             " AND l.pkg=package_queue.pkg AND l.status='open')")
    if repos:
        query += " AND repo NOT IN (" + ",".join("?" for _ in repos) + ")"
    c.execute(query, [ws_id, *repos])


def queue_mode(c, dispatch, body):
    with dispatch._Tx(c):
        device = identity(c, dispatch, body)
        if type(body.get("queue_only")) is not bool:
            raise ValueError("请选择仅处理所选分包或自动领取")
        c.execute("UPDATE devices SET queue_only=? WHERE device_id=?",
                  (int(body["queue_only"]), device["device_id"]))
        if body.get("clear") is True:
            c.execute("DELETE FROM part_assignments WHERE target_type='device' AND target_id=? AND NOT EXISTS"
                      " (SELECT 1 FROM lease_parts l WHERE l.repo=part_assignments.repo AND l.pkg=part_assignments.pkg"
                      " AND l.part=part_assignments.part AND l.open=1)", (device['device_id'],))
            c.execute("DELETE FROM package_queue WHERE device_id=? AND NOT EXISTS"
                      " (SELECT 1 FROM leases l WHERE l.repo=package_queue.repo AND l.pkg=package_queue.pkg AND l.status='open')",
                      (device["device_id"],))
    return {"ok": True}


@atomic
def identity(c, dispatch, body, *, update_parts=True):
    user = dispatch._sid(body.get("user_id"), "user_id")
    did = dispatch._sid(body.get("device_id"), "device_id")
    device = dispatch._row(c, "SELECT * FROM devices WHERE device_id=?", (did,))
    if not device or device["user_id"] != user:
        raise dispatch.AuthError("设备与用户不匹配")
    device = dispatch._require_device(c, user, did, body.get("secret"))
    account = dispatch._row(c, "SELECT status FROM users WHERE user_id=?", (user,))
    if not account or account["status"] != "active" or device["status"] in ("blocked", "archived"):
        raise dispatch.AuthError("用户或设备已停用")
    if update_parts and "parts" in body:
        if not isinstance(body["parts"],str) or body["parts"] not in dispatch.PARTS_CHOICES:
            raise ValueError("设备打标分项不正确")
        if device['parts'] != body['parts']:
            device["parts"]=body["parts"]
            c.execute("UPDATE devices SET parts=? WHERE device_id=?",(device["parts"],did))
            from scheduling.transitions import reconcile
            reconcile(c, dispatch)
    return device


def packages(c, dispatch, device=None, ws_id=None, offset=0):
    from scheduling.packages import page
    return page(c, dispatch, device, ws_id, offset)


def selection(body):
    items = body.get("items")
    if not isinstance(items, list) or not 0 < len(items) <= 500:
        raise ValueError("请选择 1～500 个分包")
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("分包格式不正确")
        repo, pkg = item.get("repo"), item.get("id")
        if (not isinstance(repo, str) or not isinstance(pkg, str) or not repo or not pkg
                or len(repo) > 256 or len(pkg) > 512 or "\\" in pkg
                or any(p in ("", ".", "..") for p in pkg.split("/"))
                or any(ord(ch) < 32 for ch in repo + pkg)):
            raise ValueError("分包路径不正确")
        cleaned.append((repo, pkg))
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("不能重复选择分包")
    return cleaned


def enqueue(c, dispatch, body, actor=None, on_event=None):
    from scheduling.reservations import enqueue as reserve
    return reserve(c, dispatch, body, actor, on_event)
