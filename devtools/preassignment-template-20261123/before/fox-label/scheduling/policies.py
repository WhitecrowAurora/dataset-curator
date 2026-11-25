"""工作区权限、用户分组、排序覆盖和分项指定。"""
import json
import time


def ensure_schema(c, dispatch):
    from .allocation import ensure_schema as ensure_allocation
    ensure_allocation(c, dispatch)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS user_groups (
      group_id TEXT PRIMARY KEY,name TEXT NOT NULL,workspace_id TEXT,repos TEXT NOT NULL DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS part_assignments (
      repo TEXT NOT NULL,pkg TEXT NOT NULL,part TEXT NOT NULL CHECK(part IN ('tag','nl')),
      target_type TEXT NOT NULL CHECK(target_type IN ('user','device')),target_id TEXT NOT NULL,
      actor TEXT NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(repo,pkg,part));
    """)
    dispatch._add_columns(c, "users", (("group_id", "TEXT"), ("repo_order", "TEXT NOT NULL DEFAULT '[]'")))
    dispatch._add_columns(c, "devices", (("repo_order", "TEXT NOT NULL DEFAULT '[]'"),
                                        ("parts_pref", "TEXT NOT NULL DEFAULT 'both'")))
    dispatch._add_columns(c, 'part_assignments', (("config", "TEXT NOT NULL DEFAULT '{}'"),))


def group(c, user):
    return c.execute("SELECT g.* FROM users u JOIN user_groups g ON g.group_id=u.group_id WHERE u.user_id=?",
                     (user,)).fetchone()


def repo_order(c, device, ws):
    allowed = list(ws["repos"]) if ws is not None else [r["repo"] for r in c.execute(
        "SELECT repo FROM repos WHERE enabled=1 ORDER BY repo")]
    from .allocation import permissions
    manual, assigned = permissions(c, device, ws)
    allowed = [r for r in allowed if r not in manual or bool(ws and ws.get('allocation_mode') == 'manual' and r in assigned)]
    user = c.execute("SELECT repo_order FROM users WHERE user_id=?", (device["user_id"],)).fetchone()
    g = group(c, device["user_id"])
    for raw in (device.get("repo_order"), user["repo_order"] if user else None, g["repos"] if g else None):
        order = json.loads(raw or "[]")
        if order:
            return [r for r in order if r in allowed] + [r for r in allowed if r not in order]
    return allowed


def view(c, dispatch):
    groups = [{**dict(r), "repos": json.loads(r["repos"])} for r in c.execute("SELECT * FROM user_groups ORDER BY group_id")]
    users = [dict(r) for r in c.execute("SELECT user_id,group_id,workspace_id,repo_order,status FROM users ORDER BY user_id")]
    devices = []
    for r in c.execute("SELECT * FROM devices ORDER BY device_id"):
        d = dict(r)
        item = {k: d[k] for k in ("device_id", "user_id", "workspace_id", "repo_order", "status", "parts", "parts_pref")}
        item["repo_order"] = json.loads(item["repo_order"])
        item["effective_repos"] = repo_order(c, d, dispatch.workspace_of(c, d))
        item["leases"] = [{"lease_id": l["lease_id"], "kind": l["kind"], "repo": l["repo"], "package": l["pkg"],
                           "parts": json.loads(l["parts_json"])} for l in c.execute(
            "SELECT * FROM leases WHERE device_id=? AND status='open'", (d["device_id"],))]
        devices.append(item)
    for u in users:
        u["repo_order"] = json.loads(u["repo_order"])
    from .lifetime import SECONDS
    return {"ok": True, "groups": groups, "users": users, "devices": devices,
            "assignments": [dict(r) for r in c.execute("SELECT * FROM part_assignments ORDER BY repo,pkg,part")],
            "offline_grace": SECONDS}


def ordering(value):
    if (not isinstance(value, list) or len(value) > 256 or any(not isinstance(x, str) or not x for x in value)
            or len(set(value)) != len(value)):
        raise ValueError("仓库优先级格式不正确")
    return value


def update(c, dispatch, body, actor):
    if not isinstance(body, dict):
        raise ValueError('请求体必须是 JSON 对象')
    action = body.get("action")
    changes = {}
    with dispatch._Tx(c):
        if action == 'allocation':
            from .allocation import update as allocate
            changes = allocate(c, dispatch, body)
        elif action == "settings":
            value = body.get("offline_grace")
            if value != 48 * 3600:
                raise ValueError("在线租约统一为成功上报后 48 小时，旧离线宽限设置已替换")
            c.execute("INSERT INTO dispatch_settings VALUES('offline_grace',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (str(value),))
        elif action == "group":
            gid = dispatch._sid(body.get("group_id"), "group_id")
            ws = body.get("workspace_id") or None
            order = ordering(body.get("repos", []))
            if ws:
                workspace = dispatch.ws_get(c, ws)
                if not workspace or any(r not in workspace["repos"] for r in order):
                    raise ValueError("分组仓库超出工作区")
            elif any(not c.execute("SELECT 1 FROM repos WHERE repo=?", (r,)).fetchone() for r in order):
                raise ValueError("仓库未登记")
            name = dispatch._opt_str(body.get("name", gid), "name", 128)
            c.execute("INSERT INTO user_groups VALUES(?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET"
                      " name=excluded.name,workspace_id=excluded.workspace_id,repos=excluded.repos",
                      (gid, name, ws, json.dumps(order)))
        elif action == "delete_group":
            gid = dispatch._sid(body.get("group_id"), "group_id")
            c.execute("UPDATE users SET group_id=NULL WHERE group_id=?", (gid,))
            c.execute("DELETE FROM user_groups WHERE group_id=?", (gid,))
        elif action in ("user", "device"):
            table, column = ("users", "user_id") if action == "user" else ("devices", "device_id")
            ids = body.get("ids") or [body.get(column)]
            if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
                raise ValueError("请选择用户或设备")
            order = ordering(body.get("repos", []))
            gid = body.get("group_id") or None
            if action == "user" and gid and not c.execute("SELECT 1 FROM user_groups WHERE group_id=?", (gid,)).fetchone():
                raise ValueError("分组不存在")
            for ident in ids:
                row = c.execute(f"SELECT * FROM {table} WHERE {column}=?", (dispatch._sid(ident, column),)).fetchone()
                if not row:
                    raise ValueError("用户或设备不存在")
                if action == "user":
                    c.execute("UPDATE users SET group_id=? WHERE user_id=?", (gid, ident))
                    device = dict(user_id=ident, device_id="", workspace_id=None)
                else:
                    device = dict(row)
                workspace = dispatch.workspace_of(c, device)
                permitted = workspace['repos'] if workspace is not None else [r['repo'] for r in c.execute('SELECT repo FROM repos')]
                if any(r not in permitted for r in order):
                    raise ValueError("仓库优先级超出工作区")
                c.execute(f"UPDATE {table} SET repo_order=? WHERE {column}=?", (json.dumps(order), ident))
        elif action == "assign":
            from assignments.store import selection
            items = selection(body)
            for repo, pkg in items:
                row = c.execute("SELECT 1 FROM packages WHERE repo=? AND id=?", (repo, pkg)).fetchone()
                if not row:
                    raise ValueError("分片不存在")
                for part in ("tag", "nl"):
                    target = body.get(part)
                    if target is None:
                        continue
                    if not isinstance(target, dict) or target.get("type") not in ("auto", "user", "device"):
                        raise ValueError("分项分配格式不正确")
                    from .leases import held
                    if part in held(c).get((repo, pkg), ()):
                        raise dispatch.LeaseError("分项已在执行或预锁，请先回收预约")
                    if target["type"] == "auto":
                        c.execute("DELETE FROM part_assignments WHERE repo=? AND pkg=? AND part=?", (repo, pkg, part))
                        continue
                    ident = dispatch._sid(target.get("id"), "目标")
                    if target["type"] == "device":
                        d = dispatch._row(c, "SELECT * FROM devices WHERE device_id=?", (ident,))
                        if (not d or d["status"] in ("archived", "blocked") or ident.startswith('offline-')
                                or part not in (dispatch._wanted_parts(d) or dispatch.PARTS)):
                            raise ValueError("目标设备不能处理该分项")
                        candidates = [d]
                        user_id = d['user_id']
                    else:
                        user_id = ident
                        candidates = [dict(r) for r in c.execute(
                            "SELECT * FROM devices WHERE user_id=? AND status NOT IN ('blocked','archived')", (ident,))
                            if not r['device_id'].startswith('offline-')
                            and part in (dispatch._wanted_parts(dict(r)) or dispatch.PARTS)]
                    u = c.execute("SELECT status FROM users WHERE user_id=?", (user_id,)).fetchone()
                    if not u or u["status"] != "active":
                        raise ValueError("目标用户不存在或停用")
                    from assignments.preassignment import user_allowed
                    if target['type'] == 'user' and user_allowed(c, dispatch, user_id, repo):
                        candidates.append(dict(user_id=user_id, device_id='', workspace_id=None, parts=part))
                    if not candidates:
                        raise ValueError('目标用户没有可处理该分项的设备')
                    from .allocation import allowed
                    if not any(allowed(c, dispatch, candidate, repo) for candidate in candidates):
                        raise ValueError('目标未获仓库分配')
                    whole = c.execute('SELECT user_id FROM package_targets WHERE repo=? AND pkg=?', (repo, pkg)).fetchone()
                    queue = c.execute('SELECT device_id FROM package_queue WHERE repo=? AND pkg=?', (repo, pkg)).fetchone()
                    viable = [candidate for candidate in candidates if allowed(c, dispatch, candidate, repo)
                              and (not whole or whole['user_id'] == candidate['user_id'])
                              and (not queue or queue['device_id'] == candidate['device_id'])]
                    if not viable:
                        raise ValueError('分项目标与已有分片指定冲突')
                    c.execute("INSERT INTO part_assignments(repo,pkg,part,target_type,target_id,actor,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(repo,pkg,part)"
                              " DO UPDATE SET target_type=excluded.target_type,target_id=excluded.target_id,"
                              " actor=excluded.actor,updated_at=excluded.updated_at,config='{}'",
                              (repo, pkg, part, target["type"], ident, actor, time.time()))
        else:
            raise ValueError("未知调度配置")
        if action in ('group', 'delete_group', 'user', 'device'):
            from .transitions import reconcile
            changes = reconcile(c, dispatch)
    return {**view(c, dispatch), **changes}
