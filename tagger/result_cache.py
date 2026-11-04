"""打标结果缓存：同图 + 同参数 → 直接复用上次的 tag/nl 产出。

为什么值得单独做一层
--------------------
一次 VLM 推理 0.8~2.5 s，而客户端准备（解码 + 缩放 + base64）只有几十 ms。
能省掉"整次推理"的只有结果级缓存——客户端那点 CPU 缓存再怎么优化，天花板
也就 3%（见 HANDOFF 9.1 的实测：准备 82 ms vs 请求 2.5 s）。

为什么默认关闭（要显式 `--cache`）
----------------------------------
首轮 3000 万张的命中率是 0，而图哈希本身要 1~2 ms/张（30 TB 素材 ≈ 十几小时
纯 CPU）。只有"重跑 / 换输出目录 / 多机处理有重叠的素材 / 调参做 A/B"这几类
场景才回本，所以让它显式开启，而不是给所有人的首轮默默加 3% 开销。

key 的口径
----------
`运行参数指纹 + 图内容 sha256`。**任何影响产出的东西都必须进指纹**：
prompt、模型、精度、像素预算、采样参数、seed、质检策略、tag 后端。
漏一项的后果不是"少命中"，而是"换 seed 重跑拿到旧结果并写成新参数"——
那是最难发现的一类数据污染，所以宁可让指纹里多塞几个字段。

为什么用 sqlite 而不是 jsonl
----------------------------
3000 万条 × ~2 KB 是 60 GB 量级，需要按 last_used 淘汰，jsonl 做不到原地更新。
形态照抄 `dedup.HashCache`（同仓库已有先例），不引入新概念。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS results ("
    " key TEXT PRIMARY KEY,"
    " body TEXT NOT NULL,"
    " created REAL NOT NULL,"
    " used REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS results_used ON results(used)",
)


class ResultCache:
    """同进程多线程共用的结果缓存。

    线程安全靠一把锁：命中率高的场景下 get 是纯读，写只在"新结果"时发生，
    锁竞争远小于一次推理的耗时，不值得为此上连接池。
    """

    def __init__(self, path: str | Path, *, max_bytes: int = 8 << 30,
                 sync_every: int = 256, evict_every: int = 4096) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max(0, int(max_bytes))
        self._sync_every = max(1, int(sync_every))
        self._evict_every = max(1, int(evict_every))
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # WAL + NORMAL：缓存不是权威数据，崩溃丢最后几条完全可接受，
        # 但每条都 fsync 的话 3000 万次写会变成小时级。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        for sql in _SCHEMA:
            self._conn.execute(sql)
        self._conn.commit()
        self._page_size = int(
            (self._conn.execute("PRAGMA page_size").fetchone() or [4096])[0] or 4096)
        self._pending = 0
        self._writes = 0
        self.hits = 0
        self.misses = 0
        self.evicted = 0

    # ------------------------------------------------------------ 读写

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM results WHERE key=?", (key,)).fetchone()
            if row is None:
                self.misses += 1
                return None
            # 命中才刷 used：这正是 LRU 淘汰的依据，代价是一次 UPDATE。
            self._conn.execute("UPDATE results SET used=? WHERE key=?",
                               (time.time(), key))
            self._pending += 1
            self.hits += 1
            if self._pending >= self._sync_every:
                self._commit_locked()
            try:
                body = json.loads(row[0])
            except ValueError:
                return None
            return body if isinstance(body, dict) else None

    def put(self, key: str, record: dict) -> None:
        now = time.time()
        body = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO results(key, body, created, used) VALUES(?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET body=excluded.body,"
                " used=excluded.used",
                (key, body, now, now))
            self._pending += 1
            self._writes += 1
            if self._pending >= self._sync_every:
                self._commit_locked()
            if self._writes % self._evict_every == 0:
                self._evict_locked()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._conn.close()

    # ------------------------------------------------------------ 维护

    def _commit_locked(self) -> None:
        try:
            self._conn.commit()
        except sqlite3.Error:
            # 磁盘满之类的写入失败不该让整批打标挂掉：缓存丢了只是下次重算。
            pass
        self._pending = 0

    def _bytes_locked(self) -> int:
        try:
            pages = int(self._conn.execute(
                "PRAGMA page_count").fetchone()[0] or 0)
        except sqlite3.Error:
            return 0
        return pages * self._page_size

    def _evict_locked(self) -> None:
        """超过上限就按 last_used 删掉最旧的四分之一。

        只在"非常大"时才 VACUUM：VACUUM 会重写整个文件并锁表，
        对 8 GB 的库是分钟级，跑到一半卡住比留点空洞糟糕得多。
        """
        if not self.max_bytes:
            return
        size = self._bytes_locked()
        if size <= self.max_bytes:
            return
        try:
            total = int(self._conn.execute(
                "SELECT COUNT(*) FROM results").fetchone()[0] or 0)
        except sqlite3.Error:
            return
        drop = max(1000, total // 4)
        try:
            self._conn.execute(
                "DELETE FROM results WHERE key IN ("
                " SELECT key FROM results ORDER BY used ASC LIMIT ?)", (drop,))
            self._conn.commit()
        except sqlite3.Error:
            return
        self.evicted += drop
        if size > self.max_bytes * 2:
            try:
                self._conn.execute("VACUUM")
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------ 观测

    def describe(self) -> dict:
        with self._lock:
            try:
                rows = int(self._conn.execute(
                    "SELECT COUNT(*) FROM results").fetchone()[0] or 0)
            except sqlite3.Error:
                rows = 0
            size = self._bytes_locked()
        return {
            "path": str(self.path),
            "rows": rows,
            "bytes": size,
            "hits": self.hits,
            "misses": self.misses,
            "evicted": self.evicted,
        }
