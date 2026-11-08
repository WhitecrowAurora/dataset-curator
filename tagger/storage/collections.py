"""小集合保留内存，超过阈值转本机临时索引；对外保持映射/序列接口。"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import MutableMapping, Sequence
from pathlib import Path

SPILL_AT = 8192


class SpillMap(MutableMapping):
    def __init__(self, *, encode=lambda v: v, decode=lambda v: v, limit=SPILL_AT):
        self.memory = {}
        self.encode, self.decode, self.limit = encode, decode, limit
        self.db = None
        self.temp = None

    def _spill(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tagger-index-")
        self.db = sqlite3.connect(Path(self.temp.name) / "index.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA cache_size=-4096")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("CREATE TABLE entries(key TEXT PRIMARY KEY, value TEXT)")
        self.db.executemany("INSERT INTO entries VALUES(?,?)",
                            ((self._key(k), json.dumps(self.encode(v), ensure_ascii=False))
                             for k, v in self.memory.items()))
        self.memory.clear()

    @staticmethod
    def _key(key):
        return json.dumps(key, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _unkey(key):
        value = json.loads(key)
        return tuple(value) if isinstance(value, list) else value

    def __getitem__(self, key):
        if self.db is None:
            return self.memory[key]
        row = self.db.execute("SELECT value FROM entries WHERE key=?", (self._key(key),)).fetchone()
        if row is None:
            raise KeyError(key)
        return self.decode(json.loads(row[0]))

    def __setitem__(self, key, value):
        if self.db is None:
            self.memory[key] = value
            if len(self.memory) >= self.limit:
                self._spill()
        else:
            self.db.execute("INSERT INTO entries VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (self._key(key), json.dumps(self.encode(value), ensure_ascii=False)))

    def __delitem__(self, key):
        if self.db is None:
            del self.memory[key]
        elif not self.db.execute("DELETE FROM entries WHERE key=?", (self._key(key),)).rowcount:
            raise KeyError(key)

    def __iter__(self):
        if self.db is None:
            yield from self.memory
        else:
            for (key,) in self.db.execute("SELECT key FROM entries ORDER BY rowid"):
                yield self._unkey(key)

    def items(self):
        if self.db is None:
            yield from self.memory.items()
        else:
            # 更新 value 不改变 rowid；消费者可以逐项改写同一份索引。
            for key, value in self.db.execute("SELECT key,value FROM entries ORDER BY rowid"):
                yield self._unkey(key), self.decode(json.loads(value))

    def values(self):
        for _, value in self.items():
            yield value

    def sorted_items(self):
        if self.db is None:
            for key in sorted(self.memory):
                yield key, self.memory[key]
        else:
            self.db.create_function("decoded_key", 1, lambda raw: json.loads(raw))
            for key, value in self.db.execute("SELECT key,value FROM entries ORDER BY decoded_key(key)"):
                yield self._unkey(key), self.decode(json.loads(value))

    def __len__(self):
        if self.db is None:
            return len(self.memory)
        return self.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def __bool__(self):
        return bool(self.memory) if self.db is None else self.db.execute("SELECT 1 FROM entries LIMIT 1").fetchone() is not None

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.temp is not None:
            self.temp.cleanup()
            self.temp = None

    def __del__(self):
        self.close()


class SpillList(Sequence):
    def __init__(self, values=(), *, encode=lambda v: v, decode=lambda v: v):
        self.data = SpillMap(encode=encode, decode=decode)
        self.count = 0
        self.extend(values)

    def append(self, value):
        self.data[self.count] = value
        self.count += 1

    def extend(self, values):
        for value in values:
            self.append(value)

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self.data[i] for i in range(*index.indices(self.count))]
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        return self.data[index]

    def __iter__(self):
        yield from self.data.values()

    def __eq__(self, other):
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def sort(self, *, key=None):
        if self.data.db is None:
            values = sorted(self.data.values(), key=key)
            self.data.memory = dict(enumerate(values))
        else:
            # 大列表通常用于 ID/冲突列表；显式持久化排序键，SQL 使用磁盘排序。
            db = self.data.db
            db.execute("CREATE TABLE ordered(position INTEGER PRIMARY KEY, value TEXT)")
            decode = self.data.decode
            db.create_function("sort_key", 1,
                               lambda raw: json.dumps(key(decode(json.loads(raw))) if key else json.loads(raw),
                                                      ensure_ascii=False))
            def compare(a, b):
                left, right = json.loads(a), json.loads(b)
                return (left > right) - (left < right)
            db.create_collation("python_key", compare)
            db.execute("INSERT INTO ordered(value) SELECT value FROM entries ORDER BY sort_key(value) COLLATE python_key, rowid")
            db.execute("DELETE FROM entries")
            db.execute("INSERT INTO entries SELECT CAST(position-1 AS TEXT),value FROM ordered ORDER BY position")
            db.execute("DROP TABLE ordered")
            db.create_function("sort_key", 1, None)
            db.create_collation("python_key", None)

    def close(self):
        self.data.close()
