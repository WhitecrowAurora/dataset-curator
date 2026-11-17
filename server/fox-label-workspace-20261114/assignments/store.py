"""只预留分包，真正执行仍走原来的 active/prelock 租约。"""
import time


def ensure_schema(c):
    c.execute("CREATE TABLE IF NOT EXISTS package_queue (repo TEXT NOT NULL,pkg TEXT NOT NULL,"
              "user_id TEXT NOT NULL,device_id TEXT NOT NULL,actor TEXT NOT NULL,created_at REAL NOT NULL,"
              "PRIMARY KEY(repo,pkg))")
    c.execute("CREATE INDEX IF NOT EXISTS idx_package_queue_device ON package_queue(device_id)")


def clear_device(c, device_id):
    c.execute("DELETE FROM package_queue WHERE device_id=?", (device_id,))
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
            c.execute("DELETE FROM package_queue WHERE device_id=? AND NOT EXISTS"
                      " (SELECT 1 FROM leases l WHERE l.repo=package_queue.repo AND l.pkg=package_queue.pkg AND l.status='open')",
                      (device["device_id"],))
    return {"ok": True}


def identity(c, dispatch, body):
    user = dispatch._sid(body.get("user_id"), "user_id")
    did = dispatch._sid(body.get("device_id"), "device_id")
    device = dispatch._row(c, "SELECT * FROM devices WHERE device_id=?", (did,))
    if not device or device["user_id"] != user:
        raise dispatch.AuthError("设备与用户不匹配")
    device = dispatch._require_device(c, user, did, body.get("secret"))
    account = dispatch._row(c, "SELECT status FROM users WHERE user_id=?", (user,))
    if not account or account["status"] != "active" or device["status"] in ("blocked", "archived"):
        raise dispatch.AuthError("用户或设备已停用")
    if "parts" in body:
        if not isinstance(body["parts"],str) or body["parts"] not in dispatch.PARTS_CHOICES:
            raise ValueError("设备打标分项不正确")
        device["parts"]=body["parts"]
        c.execute("UPDATE devices SET parts=? WHERE device_id=?",(device["parts"],did))
    return device


def packages(c, dispatch, device=None, ws_id=None, offset=0):
    try:offset = int(offset)
    except (ValueError,TypeError) as exc:raise ValueError("分页位置必须是非负整数") from exc
    if offset < 0:
        raise ValueError("分页位置不能为负数")
    ws = dispatch.workspace_of(c, device) if device else dispatch.ws_get(c, ws_id)
    allowed = list(ws["repos"]) if ws is not None else [r[0] for r in c.execute("SELECT repo FROM repos WHERE enabled=1")]
    if not allowed:
        return {"ok": True, "workspace": dispatch.workspace_brief(ws), "items": [], "total": 0,
                "next_offset": None, "allow_manual_claim": bool(ws and ws.get("allow_manual_claim")),
                "queue_only": bool(device and device.get("queue_only")),
                "empty_reason": "等待管理员配置中"}
    marks = ",".join("?" for _ in allowed)
    query = (" FROM packages p JOIN repos r ON r.repo=p.repo"
             " LEFT JOIN package_queue q ON q.repo=p.repo AND q.pkg=p.id"
             " LEFT JOIN package_targets t ON t.repo=p.repo AND t.pkg=p.id"
             " LEFT JOIN leases l ON l.repo=p.repo AND l.pkg=p.id AND l.status='open'"
             f" WHERE p.repo IN ({marks}) AND r.enabled=1 AND p.status<>'completed'")
    params = list(allowed)
    if device:
        query += " AND (t.user_id IS NULL OR t.user_id=?) AND (q.device_id IS NULL OR q.device_id=?)"
        query += " AND (l.device_id IS NULL OR l.device_id=?)"
        params += [device["user_id"], device["device_id"], device["device_id"]]
    total = c.execute("SELECT COUNT(*)" + query, params).fetchone()[0]
    rows = c.execute("SELECT p.repo,p.id,p.size,p.tag_status,p.nl_status,p.status,"
                     "q.device_id assigned_device,q.actor,l.device_id working_device,l.lease_id,"
                     "r.repo_type" + query + " ORDER BY p.repo,p.id LIMIT 100 OFFSET ?", [*params, offset])
    items = []
    for row in rows:
        item = dict(row)
        item["missing_parts"] = dispatch.missing_parts(item)
        item["claimable"] = not item["assigned_device"] and not item["lease_id"]
        if device and dispatch._wanted_parts(device):
            item["claimable"] &= bool(set(item["missing_parts"]) & set(dispatch._wanted_parts(device)))
        items.append(item)
    end = offset + len(items)
    return {"ok": True, "workspace": dispatch.workspace_brief(ws), "items": items, "total": total,
            "next_offset": end if end < total else None,
            "allow_manual_claim": ws is None or bool(ws.get("allow_manual_claim")),
            "queue_only": bool(device and device.get("queue_only")),
            "empty_reason": "" if total else "暂无待处理分包"}


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
    items = selection(body)
    manual = actor is None
    with dispatch._Tx(c):
        if manual:
            devices = [identity(c, dispatch, body)]
        else:
            ids = body.get("devices")
            if (not isinstance(ids, list) or not ids or len(ids) > 100
                    or any(not isinstance(did,str) or not did for did in ids) or len(set(ids)) != len(ids)):
                raise ValueError("请选择设备")
            devices = [dispatch._row(c, "SELECT * FROM devices WHERE device_id=?", (did,)) for did in ids]
            if any(not d or d["status"] in ("blocked", "archived") or d["device_id"].startswith("offline-") for d in devices):
                raise dispatch.LeaseError("目标设备不存在或已停用")
            for d in devices:
                account = dispatch._row(c, "SELECT status FROM users WHERE user_id=?", (d["user_id"],))
                if not account or account["status"] != "active":
                    raise dispatch.LeaseError("目标用户已停用")
                ws = dispatch.workspace_of(c, d)
                if not ws or ws["ws_id"] != body.get("ws_id"):
                    raise dispatch.LeaseError("目标设备不属于当前工作区")
        results, loads = [], {d["device_id"]: c.execute("SELECT COUNT(*) FROM package_queue WHERE device_id=?",
                                                     (d["device_id"],)).fetchone()[0] for d in devices}
        reserved = already = 0
        for repo, pkg in items:
            package = dispatch._row(c, "SELECT p.*,r.enabled FROM packages p JOIN repos r ON r.repo=p.repo"
                                   " WHERE p.repo=? AND p.id=?", (repo, pkg))
            if not package or not package["enabled"] or package["status"] not in ("available","claimed"):
                raise dispatch.LeaseError(f"分包不能领取：{repo}/{pkg}")
            held = dispatch._row(c, "SELECT device_id FROM leases WHERE repo=? AND pkg=? AND status='open'", (repo,pkg))
            existing = dispatch._row(c, "SELECT * FROM package_queue WHERE repo=? AND pkg=?", (repo,pkg))
            target = dispatch._row(c, "SELECT user_id FROM package_targets WHERE repo=? AND pkg=?", (repo,pkg))
            candidates = []
            for d in devices:
                ws = dispatch.workspace_of(c, d)
                if ws is not None and repo not in ws["repos"]:
                    continue
                if manual and ws is not None and not ws.get("allow_manual_claim"):
                    raise dispatch.LeaseError("管理员未允许客户端手动领取")
                if target and target["user_id"] != d["user_id"]:
                    continue
                if existing and existing["device_id"] != d["device_id"]:
                    continue
                if held and held["device_id"] != d["device_id"]:
                    continue
                if dispatch._wanted_parts(d) and dispatch._part_rank(package,dispatch._wanted_parts(d),dispatch._device_pref(d)) >= 2:
                    continue
                candidates.append(d)
            if not candidates:
                raise dispatch.LeaseError(f"没有可接收这个分包的选中设备：{pkg}")
            d = min(candidates,key=lambda d: loads[d["device_id"]])
            if existing or held:already += 1
            else:reserved += 1
            loads[d["device_id"]] += 1
            if not held:
                c.execute("INSERT OR IGNORE INTO package_queue VALUES(?,?,?,?,?,?)",
                          (repo,pkg,d["user_id"],d["device_id"],actor or "client",time.time()))
            c.execute("UPDATE devices SET queue_only=1 WHERE device_id=?", (d["device_id"],))
            results.append({"repo":repo,"id":pkg,"device_id":d["device_id"]})
            if on_event and not existing:on_event(c,repo,pkg,d["user_id"],d["device_id"])
    return {"ok": True,"assigned":results,"reserved":reserved,"already_reserved":already}
