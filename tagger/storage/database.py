"""SQLite 连接的共同设置；数据库只用于本机磁盘。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=60, check_same_thread=False)
    try:
        db.execute("PRAGMA busy_timeout=60000")
        deadline = time.monotonic() + 60
        while True:
            try:
                # WAL 是数据库的持久设置。仅首次开启；同时首次连接时
                # SQLite 的 journal 切换可能不走 busy_timeout，需要重试。
                if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if (getattr(exc, "sqlite_errorcode", 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA cache_size=-8192")
        db.execute("PRAGMA temp_store=FILE")
    except BaseException:
        db.close()
        raise
    return db
