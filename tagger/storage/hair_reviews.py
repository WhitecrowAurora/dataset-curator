"""单图发色复核及人工选择的事务化存储。"""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from .database import connect
from .review_feedback import append_feedback, ensure_feedback


class HairReviews:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "reviews.sqlite3"
        with closing(connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY, value TEXT NOT NULL)")
            ensure_feedback(db)
            for field in ("status", "job_id", "parent_id"):
                db.execute(f"CREATE INDEX IF NOT EXISTS reviews_{field} ON reviews(json_extract(value, '$.{field}'))")

    def put(self, record: dict):
        with closing(connect(self.path)) as db, db:
            db.execute("INSERT INTO reviews VALUES(?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                       (record["id"], json.dumps(record, ensure_ascii=False)))

    def mark_interrupted(self):
        with closing(connect(self.path)) as db, db:
            db.execute("""UPDATE reviews SET value=json_set(value, '$.status', 'failed',
                       '$.reason', '上次复核被服务重启中断，请重新提交')
                       WHERE json_extract(value, '$.status') IN ('pending', 'running')""")

    def get(self, review_id: str) -> dict:
        with closing(connect(self.path)) as db:
            row = db.execute("SELECT value FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise KeyError(review_id)
        return json.loads(row[0])

    def list(self, offset=0, limit=20, status="all", job_id="") -> dict:
        clauses, params = [], []
        if status == "todo":
            clauses.append("json_extract(value, '$.status') IN ('review','failed') OR json_extract(value, '$.writeback_error') IS NOT NULL")
        elif status != "all":
            clauses.append("json_extract(value, '$.status')=?")
            params.append(status)
        if job_id:
            clauses.append("json_extract(value, '$.job_id')=? OR EXISTS (SELECT 1 FROM json_each(reviews.value, '$.job_ids') WHERE json_each.value=?)")
            params.extend((job_id, job_id))
        where = " WHERE " + " AND ".join(f"({c})" for c in clauses) if clauses else ""
        with closing(connect(self.path)) as db:
            total = db.execute("SELECT COUNT(*) FROM reviews" + where, params).fetchone()[0]
            active = db.execute("SELECT COUNT(*) FROM reviews WHERE json_extract(value, '$.status') IN ('pending','running')").fetchone()[0]
            rows = db.execute("SELECT id, value FROM reviews" + where + " ORDER BY rowid DESC LIMIT ? OFFSET ?", (*params, limit, offset))
            items = [json.loads(value) for _, value in rows]
            roles = {}
            if items:
                keys = [r["id"] for r in items]
                query = f"""SELECT json_extract(value, '$.parent_id'), COUNT(*),
                            SUM(json_extract(value, '$.status') IN ('accepted','decided','skipped'))
                            FROM reviews WHERE json_extract(value, '$.parent_id') IN ({','.join('?' for _ in keys)})
                            GROUP BY json_extract(value, '$.parent_id')"""
                roles = {key: (count, settled) for key, count, settled in db.execute(query, keys)}
            for record in items:
                record["role_count"], record["settled_roles"] = roles.get(record["id"], (0, 0))
        return {"items": items, "total": total, "active": active}

    def job_ids(self, job_id):
        with closing(connect(self.path)) as db:
            rows = db.execute("""SELECT id FROM reviews WHERE json_extract(value, '$.job_id')=?
                               OR EXISTS (SELECT 1 FROM json_each(reviews.value, '$.job_ids') WHERE json_each.value=?)
                               ORDER BY rowid""", (job_id, job_id))
            return [row[0] for row in rows]

    def attach(self, review_id, job_id, *, path=""):
        """续做只修改关联信息，保留人工决定及历史任务关联。"""
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM reviews WHERE id=?", (review_id,)).fetchone()
            if row is None:
                raise KeyError(review_id)
            record = json.loads(row[0])
            record["job_ids"] = list(dict.fromkeys(filter(None, [*record.get("job_ids", []), record.get("job_id"), job_id])))
            record["job_id"] = job_id
            record["request"]["job_id"] = job_id
            if path:
                record["request"]["artifact_path"] = str(path)
            db.execute("UPDATE reviews SET value=? WHERE id=?", (json.dumps(record, ensure_ascii=False), review_id))
        return record

    def children(self, review_id):
        with closing(connect(self.path)) as db:
            rows = db.execute("SELECT value FROM reviews WHERE json_extract(value, '$.parent_id')=? ORDER BY rowid", (review_id,))
            return [json.loads(row[0]) for row in rows]

    def set_writeback(self, review_id, *, shard=None, path=None, error=None):
        """只更新回写位置和错误，保留并发人工裁决。"""
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM reviews WHERE id=?", (review_id,)).fetchone()
            if row is None:
                raise KeyError(review_id)
            record = json.loads(row[0])
            for key, value in (("artifact_shard", shard), ("artifact_path", path)):
                if value:
                    record["request"][key] = str(value)
                    locations = record["request"].setdefault(key + "s", [])
                    if str(value) not in locations:
                        locations.append(str(value))
            if error:
                record["writeback_error"] = error
            else:
                record.pop("writeback_error", None)
            db.execute("UPDATE reviews SET value=? WHERE id=?", (json.dumps(record, ensure_ascii=False), review_id))

    def decide(self, review_id: str, colors: list[str], no_hair=False, *, multiple_people=False, confirmed_roles=()) -> dict:
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM reviews WHERE id=?", (review_id,)).fetchone()
            if row is None:
                raise KeyError(review_id)
            record = json.loads(row[0])
            if record["status"] not in ("review", "decided", "accepted", "skipped", "failed"):
                raise ValueError("只有待人工复核的记录可以裁决")
            before = json.loads(row[0])
            record.update(status="decided" if colors or no_hair else "review", selected=colors, no_hair=no_hair,
                          multiple_people=multiple_people, confirmed_roles=list(confirmed_roles))
            record["reason"] = "人工确认无可见头发" if no_hair else "人工确认发色" if colors else "已撤销决定，等待人工确认"
            db.execute("UPDATE reviews SET value=? WHERE id=?", (json.dumps(record, ensure_ascii=False), review_id))
            image_key = record.get("image_sha256") or review_id
            if record.get("parent_id"):
                parent = db.execute("SELECT value FROM reviews WHERE id=?", (record["parent_id"],)).fetchone()
                if parent:
                    image_key = json.loads(parent[0]).get("image_sha256") or record["parent_id"]
            append_feedback(db, subject=f"hair:{review_id}", image_key=f"image:{image_key}", kind="hair",
                            action="confirm" if colors or no_hair else "undo",
                            value={"origin": "manual", "before": before, "after": record})
        return record
