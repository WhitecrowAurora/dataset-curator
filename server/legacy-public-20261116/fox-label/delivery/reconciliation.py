"""普通回传和历史检查共用的逐图归并与续作快照。"""
import hashlib
import json
import os
from pathlib import Path

from verification.history import read_rows, reconcile
from . import uploads, conflicts


def ensure_schema(c):
    c.execute("CREATE TABLE IF NOT EXISTS package_artifacts (repo TEXT NOT NULL,pkg TEXT NOT NULL,"
              "source_sha256 TEXT NOT NULL,ids_sha256 TEXT NOT NULL,expected_json TEXT NOT NULL,"
              "rows_json TEXT NOT NULL,report_json TEXT NOT NULL,updated_at REAL NOT NULL,"
              "PRIMARY KEY(repo,pkg,source_sha256))")
    from source_catalog.store import ensure_schema as ensure_sources
    ensure_sources(c)
    conflicts.ensure_schema(c)


def snapshot(c, repo, pkg, sha):
    row = c.execute("SELECT * FROM package_artifacts WHERE repo=? AND pkg=? AND source_sha256=?",
                     (repo, pkg, sha)).fetchone()
    if not row:
        return {"ok": True, "rows": [], "report": None, "source_sha256": sha}
    rows = json.loads(row["rows_json"])
    digest = hashlib.sha256(row["rows_json"].encode()).hexdigest()
    return {"ok": True, "rows": rows, "report": json.loads(row["report_json"]),
            "source_sha256": sha, "sha256": digest,
            "resolutions": [{**{k: r[k] for k in ('image_id', 'part', 'state', 'updated_at')},
                              'value': json.loads(r['variants_json'])[r['chosen']] if r['state'] == 'resolved' else ''}
                             for r in conflicts.records(c, repo, pkg, sha) if r['state'] != 'pending']}


def merge(c, repo, pkg, sha, expected, rows, submitted_at=None):
    import time
    from source_catalog.revalidation import expected as authority, decorate
    expected, source = authority(c, repo, pkg, sha, expected)
    old = c.execute("SELECT * FROM package_artifacts WHERE repo=? AND pkg=? AND source_sha256=?",
                    (repo, pkg, sha)).fetchone()
    old_rows = json.loads(old['rows_json']) if old else []
    incoming = rows
    if source['state'] == 'ready':
        known = set(expected)
        rejected = [r for r in [*old_rows, *rows] if r.get('image_id') not in known]
        old_rows = [r for r in old_rows if r.get('image_id') in known]
        incoming = [r for r in rows if r.get('image_id') in known]
        if rejected:
            c.execute('INSERT OR REPLACE INTO source_rejections VALUES(?,?,?,?)',
                      (repo, pkg, sha, json.dumps(rejected, ensure_ascii=False)))
    else:
        rejected = []
    normalized, current = reconcile(expected, list(conflicts.prepare(c, repo, pkg, sha, old_rows, incoming, expected, submitted_at)))
    if old:
        if old["ids_sha256"] != current["ids_sha256"] and source["state"] != "ready":
            raise ValueError("源分片图片清单与已有验收不一致")
        # 老版本仅存冲突文字；保留无法恢复第二结果的冲突，允许明确重做。
        previous = json.loads(old["report_json"])
        saved_keys = {(r['image_id'], r['part']) for r in conflicts.records(c, repo, pkg, sha)}
        for message in previous.get('conflicts', []):
            for part in ('tag', 'nl'):
                suffix = ': ' + part + ' 内容冲突'
                if message.endswith(suffix):
                    iid = message[:-len(suffix)]
                    if iid in set(expected) and (iid, part) not in saved_keys:
                        value = next((r.get(part) for r in old_rows if r['image_id'] == iid), '')
                        c.execute('INSERT OR IGNORE INTO artifact_conflicts VALUES(?,?,?,?,?,?,?,NULL,?,?)',
                                  (repo, pkg, sha, iid, part, json.dumps([value]), 'pending', '', time.time()))
    known = set(expected)
    pending = [r for r in conflicts.records(c, repo, pkg, sha) if r['state'] == 'pending' and r['image_id'] in known]
    rejection = c.execute('SELECT rows_json FROM source_rejections WHERE repo=? AND pkg=? AND source_sha256=?', (repo, pkg, sha)).fetchone()
    foreign = json.loads(rejection[0]) if rejection else []
    problems = [r['image_id'] + ': ' + r['part'] + ' 内容冲突' for r in pending]
    problems += [r['image_id'] + ': ' + p + ' 内容冲突（源清单之外）' for r in foreign for p in ('tag', 'nl')]
    current['conflicts'] = list(dict.fromkeys([*current['conflicts'], *problems]))[:100]
    current['conflict_count'] = len(pending) + len(foreign) * 2
    blocked = {r['part'] for r in pending} | ({'tag', 'nl'} if foreign else set())
    current['complete_parts'] = [p for p in current['complete_parts'] if p not in blocked]
    current['ok'] = len(current['complete_parts']) == 2
    decorate(current, source)
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    c.execute("INSERT INTO package_artifacts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(repo,pkg,source_sha256)"
              " DO UPDATE SET ids_sha256=excluded.ids_sha256,expected_json=excluded.expected_json,"
              " rows_json=excluded.rows_json,report_json=excluded.report_json,updated_at=excluded.updated_at",
              (repo, pkg, sha, current["ids_sha256"], json.dumps(expected), encoded,
               json.dumps(current, ensure_ascii=False), time.time()))
    return current


def accept(store, dispatch, c, lease, device):
    with store.delivery.connect() as d:
        files = d.execute("SELECT * FROM files WHERE lease_id=?", (lease["lease_id"],)).fetchall()
    proof_file = next((r for r in files if Path(r["name"]).name == 'proof.json'), None)
    if not proof_file:
        raise ValueError("尚未回传逐图检查报告")
    proof = json.loads(uploads.local_file(store.delivery, proof_file).read_text(encoding="utf-8"))
    pkg = dispatch._row(c, "SELECT * FROM packages WHERE repo=? AND id=?", (lease["repo"], lease["pkg"]))
    if proof.get("source_sha256") != pkg["sha256"] or proof.get("source_bytes") != pkg["size"]:
        raise ValueError("回传源分片与租约不一致")
    expected = proof.get("expected_ids")
    rows = list(read_rows([uploads.local_file(store.delivery, r) for r in files if r['name'].endswith('.jsonl')]))
    if not rows:
        raise ValueError("尚未回传打标产物")
    from scheduling.leases import parts
    assigned = set(parts(lease))
    filtered = []
    for row in rows:
        filtered.append({"image_id": row["image_id"], **{k: row.get(k, "") for k in assigned},
                         "outcomes": {k: v for k, v in (row.get("outcomes") or {}).items() if k in assigned},
                         **({"outcomes": {k: {"status": "excluded", "reason": row["excluded"].get("reason", "")}
                                         for k in assigned}} if row.get("excluded", {}).get("kind") in
                            ("excluded", "excused", "artist", "skipped") and row["excluded"].get("reason") else {})})
    result = merge(c, lease["repo"], lease["pkg"], pkg["sha256"], expected, filtered, lease['issued_at'])
    return {"ok": True, "report": result}


def reports(c):
    return {(r["repo"], r["pkg"]): json.loads(r["report_json"]) for r in c.execute(
        "SELECT a.* FROM package_artifacts a JOIN packages p ON p.repo=a.repo AND p.id=a.pkg AND p.sha256=a.source_sha256")}
