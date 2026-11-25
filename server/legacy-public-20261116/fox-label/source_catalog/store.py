"""清单配置、导入队列与不可混用的来源快照。"""
import hashlib
import json
import time

from .config import DEFAULTS, validate


def ensure_schema(c):
    from annotation.store import ensure_schema as ensure_artist
    ensure_artist(c)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS source_configs (repo TEXT PRIMARY KEY,config_json TEXT NOT NULL,generation TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS source_jobs (repo TEXT PRIMARY KEY,generation TEXT NOT NULL,state TEXT NOT NULL,
      package TEXT NOT NULL DEFAULT '',downloaded INTEGER NOT NULL DEFAULT 0,total INTEGER NOT NULL DEFAULT 0,
      error TEXT NOT NULL DEFAULT '',revision TEXT NOT NULL DEFAULT '',updated_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS source_lists (repo TEXT NOT NULL,pkg TEXT NOT NULL,source_sha256 TEXT NOT NULL,
      source_bytes INTEGER NOT NULL,generation TEXT NOT NULL,revision TEXT NOT NULL,origin TEXT NOT NULL,
      manifest_sha256 TEXT NOT NULL,ids_sha256 TEXT NOT NULL,ids_json TEXT NOT NULL,updated_at REAL NOT NULL,
      PRIMARY KEY(repo,pkg,source_sha256));
    CREATE TABLE IF NOT EXISTS source_requests (repo TEXT NOT NULL,pkg TEXT NOT NULL,source_sha256 TEXT NOT NULL,
      PRIMARY KEY(repo,pkg,source_sha256));
    CREATE TABLE IF NOT EXISTS source_submissions (lease_id TEXT PRIMARY KEY,repo TEXT NOT NULL,pkg TEXT NOT NULL,
      source_sha256 TEXT NOT NULL,device_id TEXT NOT NULL,parts_json TEXT NOT NULL,state TEXT NOT NULL,updated_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS source_rejections (repo TEXT NOT NULL,pkg TEXT NOT NULL,source_sha256 TEXT NOT NULL,
      rows_json TEXT NOT NULL,PRIMARY KEY(repo,pkg,source_sha256));
    CREATE TABLE IF NOT EXISTS source_manifests (repo TEXT NOT NULL,generation TEXT NOT NULL,revision TEXT NOT NULL,
      mapping INTEGER NOT NULL,PRIMARY KEY(repo,generation,revision));
    """)


def config(c, repo):
    row = c.execute("SELECT * FROM source_configs WHERE repo=?", (repo,)).fetchone()
    return {**DEFAULTS, **json.loads(row["config_json"]), "generation": row["generation"]} if row else {**DEFAULTS, "generation": ""}


def configure(c, repo, body):
    if not c.execute("SELECT 1 FROM repos WHERE repo=?", (repo,)).fetchone():
        raise ValueError("仓库未登记")
    cfg = validate(body)
    encoded = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    generation = hashlib.sha256(encoded.encode()).hexdigest()
    c.execute("INSERT INTO source_configs VALUES(?,?,?) ON CONFLICT(repo) DO UPDATE SET"
              " config_json=excluded.config_json,generation=excluded.generation", (repo, encoded, generation))
    c.execute("DELETE FROM source_jobs WHERE repo=? AND generation<>?", (repo, generation))
    return cfg


def enqueue(c, repo, retry=False):
    cfg = config(c, repo)
    if not cfg["enabled"]:
        return
    old = c.execute("SELECT state,generation FROM source_jobs WHERE repo=?", (repo,)).fetchone()
    if old and old["generation"] == cfg["generation"] and old["state"] in ("queued", "running"):
        return
    if old and old["state"] == "failed" and not retry:
        return
    c.execute("INSERT INTO source_jobs(repo,generation,state,updated_at) VALUES(?,?,'queued',?)"
              " ON CONFLICT(repo) DO UPDATE SET generation=excluded.generation,state='queued',error='',updated_at=excluded.updated_at",
              (repo, cfg["generation"], time.time()))


def snapshot(c, repo, pkg, sha, size):
    cfg = config(c, repo)
    if not cfg["enabled"]:
        return {"state": "disabled"}
    row = c.execute("SELECT * FROM source_lists WHERE repo=? AND pkg=? AND source_sha256=? AND source_bytes=? AND generation=?",
                    (repo, pkg, sha, size, cfg["generation"])).fetchone()
    if row:
        return {"state": "ready", "expected_ids": json.loads(row["ids_json"]), "ids_sha256": row["ids_sha256"],
                "revision": row["revision"], "origin": row["origin"], "manifest_sha256": row["manifest_sha256"]}
    job = c.execute("SELECT state,error FROM source_jobs WHERE repo=? AND generation=?", (repo, cfg["generation"])).fetchone()
    return {"state": "failed" if job and job["state"] == "failed" else "pending", "error": job["error"] if job else ""}


def status(c, repo, pkg, sha, size):
    result = snapshot(c, repo, pkg, sha, size)
    ids = result.pop("expected_ids", None)
    if ids is not None:
        result["images"] = len(ids)
    return result


def require(c, repo, pkg, sha, size):
    result = snapshot(c, repo, pkg, sha, size)
    if result["state"] == "pending":
        c.execute("INSERT OR IGNORE INTO source_requests VALUES(?,?,?)", (repo, pkg, sha))
        enqueue(c, repo)
    return result


def view(c):
    items = []
    for row in c.execute("SELECT repo FROM repos ORDER BY repo"):
        repo = row["repo"]
        cfg = config(c, repo)
        job = c.execute("SELECT * FROM source_jobs WHERE repo=?", (repo,)).fetchone()
        count = c.execute("SELECT COUNT(*) FROM source_lists s JOIN packages p ON p.repo=s.repo AND p.id=s.pkg"
                          " AND p.sha256=s.source_sha256 AND p.size=s.source_bytes WHERE s.repo=? AND s.generation=?",
                          (repo, cfg["generation"])).fetchone()[0]
        waiting = c.execute("SELECT COUNT(DISTINCT pkg) FROM source_submissions WHERE repo=? AND state='pending'", (repo,)).fetchone()[0]
        from annotation.store import config as artist_config
        items.append(dict(repo=repo, artist=artist_config(c, repo), config={k: cfg[k] for k in DEFAULTS}, job=dict(job) if job else None,
                          verified_packages=count, pending_submissions=waiting))
    return {"ok": True, "items": items}
