"""先筛选可做分项，再按指定、仓库优先级和倾向选择。"""
import json
import time

from . import leases, policies


def eligible(dispatch, device, row, held, assignments, target_user=None, queued_device=None):
    """自动派工和手动列表共用能力、指定及占用判定。"""
    if target_user and target_user != device['user_id'] or queued_device and queued_device != device['device_id']:
        return [], False
    specified = queued_device == device['device_id'] or target_user == device['user_id']
    available = []
    for part in dispatch.missing_parts(row):
        if part not in (dispatch._wanted_parts(device) or dispatch.PARTS) or part in held:
            continue
        target = assignments.get(part)
        if target:
            who = device['device_id'] if target['target_type'] == 'device' else device['user_id']
            if target['target_id'] != who:
                continue
            specified = True
        available.append(part)
    return (available if not device.get('queue_only') or specified else []), specified


def pick(c, dispatch, device, p=None, exclude=None):
    leases.release_offline(c)
    busy = leases.held(c)
    from delivery.conflicts import blocked
    for key, parts in blocked(c).items():
        busy.setdefault(key, set()).update(parts)
    ws = dispatch.workspace_of(c, device)
    order = policies.repo_order(c, device, ws)
    allowed = set(order)
    own_packages = {(r['repo'],r['pkg']) for r in c.execute(
        "SELECT repo,pkg FROM leases WHERE device_id=? AND status='open'", (device['device_id'],))}
    preference = dispatch._device_pref(device)
    assignments = {}
    for r in c.execute("SELECT * FROM part_assignments"):
        assignments[(r["repo"], r["pkg"], r["part"])] = dict(r)
    rows = c.execute("SELECT p.*,t.user_id target_user,q.device_id queued_device,q.actor queued_actor"
                     " FROM packages p JOIN repos r ON r.repo=p.repo"
                     " LEFT JOIN package_targets t ON t.repo=p.repo AND t.pkg=p.id"
                     " LEFT JOIN package_queue q ON q.repo=p.repo AND q.pkg=p.id"
                     " WHERE r.enabled=1 AND p.status IN ('available','claimed')")
    candidates = []
    for row in rows:
        row = dict(row)
        key = row["repo"], row["id"]
        if row["repo"] not in allowed or key in (exclude or ()) or key in own_packages:
            continue
        mine, specified = eligible(dispatch, device, row, busy.get(key, ()),
                                   {part: assignments.get((*key, part)) for part in dispatch.PARTS},
                                   row['target_user'], row['queued_device'])
        if not mine:
            continue
        row["dispatch_parts"] = mine
        row["specified"] = specified
        candidates.append(row)
    if not candidates:
        return None
    cutoff = time.time() - (p or {}).get("fail_cooldown", 3600)
    cooled = {(r["repo"], r["pkg"]) for r in c.execute(
        "SELECT repo,pkg FROM leases WHERE device_id=? AND status='failed' AND issued_at>?",
        (device["device_id"], cutoff))}
    candidates = [r for r in candidates if (r["repo"], r["id"]) not in cooled]
    if not candidates:
        return None
    local = set(json.loads(device.get("local_pkgs") or "[]"))
    rank = {repo: i for i, repo in enumerate(order)}
    return min(candidates, key=lambda r: (not r["specified"], rank[r["repo"]],
        bool(preference and preference not in r["dispatch_parts"]),
        f"{r['repo']}/{r['id']}" not in local, r["id"]))


def valid_prelock(c, dispatch, device, lease):
    package = dispatch._row(c, 'SELECT p.*,r.enabled FROM packages p JOIN repos r ON r.repo=p.repo'
                              ' WHERE p.repo=? AND p.id=?', (lease['repo'], lease['pkg']))
    if not package or not package['enabled'] or package['repo'] not in policies.repo_order(c, device, dispatch.workspace_of(c, device)):
        return False
    assignments = {r['part']: dict(r) for r in c.execute('SELECT * FROM part_assignments WHERE repo=? AND pkg=?',
                                                       (lease['repo'], lease['pkg']))}
    target = c.execute('SELECT user_id FROM package_targets WHERE repo=? AND pkg=?', (lease['repo'], lease['pkg'])).fetchone()
    queue = c.execute('SELECT device_id FROM package_queue WHERE repo=? AND pkg=?', (lease['repo'], lease['pkg'])).fetchone()
    from delivery.conflicts import blocked
    available, _ = eligible(dispatch, device, package, blocked(c).get((lease['repo'], lease['pkg']), ()), assignments, target['user_id'] if target else None,
                            queue['device_id'] if queue else None)
    return set(leases.parts(lease)).issubset(available)
