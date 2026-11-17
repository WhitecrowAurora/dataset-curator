"""分片验收展示；派工继续使用 packages.status。"""
import json


LABELS = {"available": "未认领", "incomplete": "未完结", "verifying": "待核验",
          "claimed": "已认领", "completed": "已完成"}


def columns():
    processed = "(p.tag_status='done' OR p.nl_status='done' OR a.repo IS NOT NULL OR e.repo IS NOT NULL)"
    pending = "v.repo IS NOT NULL"
    return (f"CASE WHEN p.status='completed' THEN 'completed' WHEN {pending} THEN 'verifying' "
            f"WHEN p.status='claimed' THEN 'claimed' WHEN {processed} THEN 'incomplete' "
            "ELSE 'available' END AS display_status, a.report_json AS verification_json")


JOIN = """ LEFT JOIN package_artifacts a ON a.repo=p.repo AND a.pkg=p.id AND a.source_sha256=p.sha256
 LEFT JOIN (SELECT DISTINCT l.repo,l.pkg FROM execution_results e JOIN leases l ON l.lease_id=e.lease_id
   WHERE l.status IN ('done','failed','released','expired') AND
   (e.fail_kind IN ('incomplete','task') OR json_extract(e.metrics,'$.images')>0)) e ON e.repo=p.repo AND e.pkg=p.id
 LEFT JOIN (SELECT h.repo,h.package AS pkg,h.source_sha256 FROM history_checks h
   LEFT JOIN managed_sessions s ON s.session_id=h.session_id
   WHERE h.status='verifying' OR (h.status='pending' AND s.released=0)
   UNION SELECT repo,pkg,source_sha256 FROM source_submissions WHERE state='pending') v
   ON v.repo=p.repo AND v.pkg=p.id AND v.source_sha256=p.sha256 """


def decorate(row):
    status = row.get("display_status", row["status"])
    row["status_label"] = LABELS.get(status, status)
    row["status_hint"] = {"incomplete": "等待分发续做", "verifying": "等待中心逐图核验"}.get(status, "")
    report = row.pop("verification_json", None)
    row["verification"] = json.loads(report) if report else None
    return row


def counts(c, repo=None):
    where, args = (" WHERE p.repo=?", (repo,)) if repo else ("", ())
    rows = c.execute("SELECT display_status,COUNT(*) n FROM (SELECT " + columns()
                     + " FROM packages p" + JOIN + where + ") GROUP BY display_status", args)
    totals = {r["display_status"]: r["n"] for r in rows}
    return {"unclaimed": totals.get("available", 0), 'processing': totals.get('claimed', 0), "incomplete": totals.get("incomplete", 0),
            "verifying": totals.get("verifying", 0)}
