"""权威清单接入逐图归并；待核验提交暂占其分项。"""
import json
import time

from . import store as catalogs


def expected(c, repo, pkg, sha, client_ids):
    row = c.execute("SELECT size FROM packages WHERE repo=? AND id=? AND sha256=?", (repo, pkg, sha)).fetchone()
    source = catalogs.require(c, repo, pkg, sha, row["size"])
    if source["state"] == "ready":
        return source["expected_ids"], {k: v for k, v in source.items() if k != "expected_ids"}
    return client_ids, source


def decorate(report, source):
    report["source_verification"] = source
    if source["state"] in ("pending", "failed"):
        report["provisional_complete_parts"] = report["complete_parts"]
        report["complete_parts"] = []
        report["ok"] = False
    return report


def defer(c, lease, package, body):
    c.execute("INSERT OR REPLACE INTO source_submissions VALUES(?,?,?,?,?,?,?,?)",
              (lease["lease_id"], lease["repo"], lease["pkg"], package["sha256"], lease["device_id"],
               lease["parts_json"], "pending", time.time()))
    c.execute("UPDATE leases SET status='submitted',closed_reason='source_pending' WHERE lease_id=?", (lease["lease_id"],))
    c.execute("UPDATE leases SET progress_json=? WHERE lease_id=?", (json.dumps(body.get("progress") or {}), lease["lease_id"]))
    c.execute('DELETE FROM package_queue WHERE repo=? AND pkg=? AND device_id=?', (lease['repo'], lease['pkg'], lease['device_id']))


def refresh(c, dispatch, repo, package, report=None):
    from delivery.reconciliation import merge
    name, sha = package["id"], package["sha256"]
    if report is None:
        artifact = c.execute("SELECT * FROM package_artifacts WHERE repo=? AND pkg=? AND source_sha256=?", (repo, name, sha)).fetchone()
        if not artifact:
            return
        report = merge(c, repo, name, sha, json.loads(artifact["expected_json"]), json.loads(artifact["rows_json"]))
    if report.get("source_verification", {}).get("state") != "ready":
        return
    from scheduling.pending_results import finalize
    finalize(c,repo,name,sha,report)
    for part in ('tag', 'nl'):
        if part not in report['complete_parts']:
            c.execute(f"UPDATE packages SET {part}_status='',{part}_by=NULL WHERE repo=? AND id=? AND {part}_status='done'",
                      (repo, name))
        else:
            # 重新核验此前已登记的回传时保留原归属，只恢复通过的侧。
            c.execute(f"UPDATE packages SET {part}_status='done' WHERE repo=? AND id=? AND {part}_by IS NOT NULL", (repo, name))
    submissions = c.execute("SELECT * FROM source_submissions WHERE repo=? AND pkg=? AND source_sha256=? AND state='pending'",
                            (repo, name, sha)).fetchall()
    for submission in submissions:
        complete = set(report["complete_parts"]) & set(json.loads(submission["parts_json"]))
        for part in complete:
            c.execute(f"UPDATE packages SET {part}_status='done',{part}_by=?,updated_at=? WHERE repo=? AND id=?",
                      (submission["device_id"], time.strftime("%Y-%m-%dT%H:%M:%S%z"), repo, name))
            c.execute('DELETE FROM part_assignments WHERE repo=? AND pkg=? AND part=?', (repo, name, part))
        full = set(json.loads(submission["parts_json"])).issubset(complete)
        c.execute("UPDATE source_submissions SET state=?,updated_at=? WHERE lease_id=?",
                  ("verified" if full else "incomplete", time.time(), submission["lease_id"]))
        c.execute("UPDATE leases SET status=?,closed_reason=? WHERE lease_id=? AND status='submitted'",
                  ("done" if full else "failed", "source_verified" if full else "source_incomplete", submission["lease_id"]))
        lease = c.execute('SELECT kind FROM leases WHERE lease_id=?', (submission['lease_id'],)).fetchone()
        if lease and lease['kind'] == 'offline':
            from assignments.offline import refresh as refresh_offline
            refresh_offline(c, submission['device_id'])
    # 历史检查不依赖原设备持续在线，由已认证提交完成后在后台补登记。
    checks = c.execute("SELECT * FROM history_checks WHERE repo=? AND package=? AND source_sha256=? AND status='verifying'",
                       (repo, name, sha)).fetchall()
    for check in checks:
        registered = []
        for part in report["complete_parts"]:
            c.execute(f"UPDATE packages SET {part}_status='done',{part}_by=? WHERE repo=? AND id=?", (check["device_id"], repo, name))
            registered.append(part)
        saved = {**report, "registered_parts": registered}
        c.execute("UPDATE history_checks SET status=?,report=?,updated_at=? WHERE check_id=?",
                  ("registered" if len(report['complete_parts']) == 2 else "incomplete", json.dumps(saved, ensure_ascii=False), time.time(), check["check_id"]))
    from scheduling.leases import refresh as refresh_package
    refresh_package(c, repo, name)
