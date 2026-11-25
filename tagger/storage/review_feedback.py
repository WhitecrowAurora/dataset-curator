"""与人工决定同事务保存追加事件，撤销和改选不覆盖历史。"""
from __future__ import annotations

import json
import time
from contextlib import closing
from pathlib import Path

from .database import connect


def ensure_feedback(db):
    db.execute("""CREATE TABLE IF NOT EXISTS feedback_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,subject TEXT NOT NULL,image_key TEXT NOT NULL,
        kind TEXT NOT NULL,action TEXT NOT NULL,at REAL NOT NULL,value TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS feedback_subject ON feedback_events(subject,id)")


def append_feedback(db, *, subject, image_key, kind, action, value):
    db.execute("INSERT INTO feedback_events(subject,image_key,kind,action,at,value) VALUES(?,?,?,?,?,?)",
               (subject, image_key, kind, action, time.time(), json.dumps(value, ensure_ascii=False)))


def iter_events(path: Path, *, latest=True, limit=5000):
    if not path.is_file():
        return
    with closing(connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='feedback_events'").fetchone():
            return
        where = " WHERE id IN (SELECT MAX(id) FROM feedback_events GROUP BY subject)" if latest else ""
        rows = db.execute("SELECT id,subject,image_key,kind,action,at,value FROM feedback_events" + where + " ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            yield {"id": row[0], "subject": row[1], "image_key": row[2], "kind": row[3],
                   "action": row[4], "at": row[5], "value": json.loads(row[6])}


def events(path: Path, *, latest=True, limit=5000):
    return list(iter_events(path, latest=latest, limit=limit))


def summary(path: Path):
    if not path.is_file():
        return {"events": 0, "latest_id": 0}
    with closing(connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='feedback_events'").fetchone():
            return {"events": 0, "latest_id": 0}
        row = db.execute("SELECT COUNT(*),COALESCE(MAX(id),0) FROM feedback_events").fetchone()
        return {"events": row[0], "latest_id": row[1]}
