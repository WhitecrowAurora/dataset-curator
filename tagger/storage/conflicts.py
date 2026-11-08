"""冲突清单的磁盘分页和事务化决策；旧 JSON 决策只在首次打开时迁入。"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from .database import connect


class Decisions(Mapping):
    def __init__(self, state: Path, *, picks=False):
        self.path = state / "decisions.sqlite3"
        self.picks = picks
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS decisions(key TEXT PRIMARY KEY,pick TEXT,strategy TEXT,at REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)")
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM meta WHERE key='imported'").fetchone():
                old = state / "decisions.json"
                if old.is_file():
                    # 旧文件保持原样；错误显式上报，不用空表覆盖无法读取的旧决定。
                    data = json.loads(old.read_text(encoding="utf-8"))
                    for key, value in data.items():
                        db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?,?,?)",
                                   (key, value.get("pick", ""), value.get("strategy", "manual"), value.get("at", 0)))
                db.execute("INSERT INTO meta VALUES('imported','1')")
        self.reader = connect(self.path)

    def connection(self):
        # 调用方的 with 既提交事务也关闭连接。
        return _Connection(self.path)

    def __getitem__(self, key):
        row = self.reader.execute("SELECT pick,strategy,at FROM decisions WHERE key=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return row[0] if self.picks else dict(zip(("pick", "strategy", "at"), row))

    def __iter__(self):
        with self.connection() as db:
            for (key,) in db.execute("SELECT key FROM decisions ORDER BY key"):
                yield key

    def items(self):
        with self.connection() as db:
            for key, pick, strategy, at in db.execute("SELECT key,pick,strategy,at FROM decisions ORDER BY key"):
                yield key, pick if self.picks else {"pick": pick, "strategy": strategy, "at": at}

    def __len__(self):
        return self.reader.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]

    def __del__(self):
        reader = getattr(self, "reader", None)
        if reader is not None:
            reader.close()


class _Connection:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.db = connect(self.path)
        return self.db

    def __exit__(self, typ, value, tb):
        try:
            self.db.__exit__(typ, value, tb)
        finally:
            self.db.close()


class ConflictItems(Sequence):
    def __init__(self, path: Path, picks):
        self.path, self.picks = path, picks

    def _decode(self, raw):
        from ..conflicts import Candidate, Conflict
        obj = json.loads(raw)
        item = Conflict(obj["image_id"], obj["kind"], [Candidate(**v) for v in obj["candidates"]])
        pick = self.picks.get(item.key, "")
        item.decided = pick if any(c.digest == pick for c in item.candidates) else ""
        return item

    def __len__(self):
        with _Connection(self.path) as db:
            return db.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def __iter__(self):
        with _Connection(self.path) as db:
            for (raw,) in db.execute("SELECT value FROM items ORDER BY position"):
                yield self._decode(raw)

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step != 1:
                return [self[i] for i in range(start, stop, step)]
            with _Connection(self.path) as db:
                return [self._decode(raw) for (raw,) in db.execute(
                    "SELECT value FROM items WHERE position>=? AND position<? ORDER BY position", (start, stop))]
        if index < 0:
            index += len(self)
        with _Connection(self.path) as db:
            row = db.execute("SELECT value FROM items WHERE position=?", (index,)).fetchone()
        if row is None:
            raise IndexError(index)
        return self._decode(row[0])

    def decided_count(self):
        if isinstance(self.picks, Decisions):
            with _Connection(self.path) as db:
                db.execute("ATTACH DATABASE ? AS chosen", (str(self.picks.path),))
                return db.execute("""SELECT COUNT(*) FROM items i JOIN chosen.decisions d ON i.key=d.key
                    WHERE EXISTS(SELECT 1 FROM candidates c WHERE c.position=i.position AND c.digest=d.pick)""").fetchone()[0]
        return sum(bool(item.decided) for item in self)
