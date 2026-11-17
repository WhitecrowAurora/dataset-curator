"""分项占用；执行任务保留，离线只释放预约。"""
import json
import time


def ensure_schema(c, dispatch):
    dispatch._add_columns(c, "leases", (("parts_json", "TEXT NOT NULL DEFAULT '[\"tag\",\"nl\"]'"),))
    c.executescript("""
    CREATE TABLE IF NOT EXISTS lease_parts (
      lease_id TEXT NOT NULL, repo TEXT NOT NULL, pkg TEXT NOT NULL,
      part TEXT NOT NULL CHECK(part IN ('tag','nl')), open INTEGER NOT NULL,
      PRIMARY KEY(lease_id,part));
    CREATE UNIQUE INDEX IF NOT EXISTS idx_part_open ON lease_parts(repo,pkg,part) WHERE open=1;
    INSERT INTO lease_parts
      SELECT l.lease_id,l.repo,l.pkg,j.value,l.status='open'
      FROM leases l,json_each(l.parts_json) j WHERE NOT EXISTS
      (SELECT 1 FROM lease_parts p WHERE p.lease_id=l.lease_id AND p.part=j.value);
    DROP INDEX IF EXISTS idx_lease_one_open;
    CREATE TRIGGER IF NOT EXISTS lease_parts_insert AFTER INSERT ON leases BEGIN
      INSERT INTO lease_parts SELECT NEW.lease_id,NEW.repo,NEW.pkg,value,NEW.status='open'
      FROM json_each(NEW.parts_json);
    END;
    CREATE TRIGGER IF NOT EXISTS lease_parts_close AFTER UPDATE OF status ON leases BEGIN
      UPDATE lease_parts SET open=(NEW.status='open') WHERE lease_id=NEW.lease_id;
    END;
    CREATE TRIGGER IF NOT EXISTS lease_parts_delete AFTER DELETE ON leases BEGIN
      DELETE FROM lease_parts WHERE lease_id=OLD.lease_id;
    END;
    CREATE TRIGGER IF NOT EXISTS package_keep_claimed AFTER UPDATE OF status ON packages
    WHEN NEW.status='available' AND EXISTS (SELECT 1 FROM leases
      WHERE repo=NEW.repo AND pkg=NEW.id AND status='open' AND kind IN ('active','offline')) BEGIN
      UPDATE packages SET status='claimed',owner=(SELECT user_id FROM leases
      WHERE repo=NEW.repo AND pkg=NEW.id AND status='open' AND kind IN ('active','offline') LIMIT 1)
      WHERE repo=NEW.repo AND id=NEW.id;
    END;
    """)
    c.execute("CREATE TABLE IF NOT EXISTS dispatch_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    from delivery.reconciliation import ensure_schema as ensure_artifacts
    ensure_artifacts(c)


def held(c):
    busy = {}
    for r in c.execute("SELECT repo,pkg,part FROM lease_parts WHERE open=1"):
        busy.setdefault((r["repo"], r["pkg"]), set()).add(r["part"])
    # 历史检查需要一致的全量快照，检查期间暂时保留两侧。
    for r in c.execute("SELECT h.repo,h.package FROM history_checks h JOIN managed_sessions s"
                       " ON s.session_id=h.session_id WHERE h.status='pending' AND s.released=0"):
        busy[(r["repo"], r["package"])] = {"tag", "nl"}
    for r in c.execute("SELECT s.repo,s.pkg,s.parts_json FROM source_submissions s JOIN packages p"
                       " ON p.repo=s.repo AND p.id=s.pkg AND p.sha256=s.source_sha256 WHERE s.state='pending'"):
        busy.setdefault((r["repo"], r["pkg"]), set()).update(json.loads(r["parts_json"]))
    for r in c.execute("SELECT repo,package FROM history_checks WHERE status='verifying'"):
        busy[(r["repo"], r["package"])] = {"tag", "nl"}
    return busy


def parts(lease):
    return json.loads(lease.get("parts_json") or '["tag","nl"]')


def grace(c, default=300):
    row = c.execute("SELECT value FROM dispatch_settings WHERE key='offline_grace'").fetchone()
    return float(row["value"]) if row else default


def release_offline(c, default=300):
    now = time.time()
    rows = c.execute("SELECT l.lease_id FROM leases l JOIN devices d ON d.device_id=l.device_id"
                     " WHERE l.kind='prelock' AND l.status='open' AND"
                     " COALESCE(d.last_seen,l.last_beat_at,l.issued_at)<?", (now - grace(c, default),)).fetchall()
    for r in rows:
        c.execute("UPDATE leases SET status='released',closed_reason='offline_prelock' WHERE lease_id=?",
                  (r["lease_id"],))
    return [r["lease_id"] for r in rows]


def refresh(c, repo, pkg):
    active = c.execute("SELECT user_id FROM leases WHERE repo=? AND pkg=? AND status='open'"
                       " AND kind IN ('active','offline') LIMIT 1", (repo, pkg)).fetchone()
    row = c.execute("SELECT tag_status,nl_status FROM packages WHERE repo=? AND id=?", (repo, pkg)).fetchone()
    if row:
        complete = row["tag_status"] == row["nl_status"] == "done"
        c.execute("UPDATE packages SET status=?,owner=? WHERE repo=? AND id=?",
                  ("completed" if complete else "claimed" if active else "available",
                   active["user_id"] if active else None, repo, pkg))
