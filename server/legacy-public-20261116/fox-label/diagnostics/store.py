"""完成结果保存在独立表中，与租约关闭使用同一事务。"""
import json
import math


def ensure_schema(c):
    c.execute("CREATE TABLE IF NOT EXISTS execution_results (lease_id TEXT PRIMARY KEY,"
              " fail_kind TEXT NOT NULL DEFAULT '', metrics TEXT NOT NULL DEFAULT '{}')")


def record(c, lease_id, body, ok):
    kind = body.get("fail_kind", "")
    if not isinstance(kind, str):
        kind = ""
    metrics = body.get("metrics")
    fields = ("download_s", "task_s", "images", "retries", "prefetch_hit")
    cleaned = {key: value for key, value in (metrics.items() if isinstance(metrics, dict) else ())
               if key in fields and type(value) in (int, float) and math.isfinite(value) and value >= 0}
    c.execute("INSERT INTO execution_results VALUES(?,?,?) ON CONFLICT(lease_id)"
              " DO UPDATE SET fail_kind=excluded.fail_kind,metrics=excluded.metrics",
              (lease_id, "" if ok else kind.strip()[:64], json.dumps(cleaned, allow_nan=False)))


def view(c, repo, package):
    rows = c.execute("SELECT l.lease_id,l.device_id,l.status,l.issued_at,e.fail_kind,e.metrics"
                     " FROM execution_results e JOIN leases l ON l.lease_id=e.lease_id"
                     " WHERE l.repo=? AND l.pkg=? ORDER BY l.issued_at DESC,l.lease_id DESC LIMIT 100",
                     (repo, package))
    return {"ok": True, "items": [{**dict(row), "metrics": json.loads(row["metrics"])} for row in rows]}
