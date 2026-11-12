"""按输出目录协调图片领取；完成状态来自已落盘产物，可重新建立索引。"""
from __future__ import annotations

import uuid
from pathlib import Path

from .database import connect
from .locking import FileLock


def _sig_size(signature: str) -> int:
    """签名形如 `size:mtime_ns`，取体积；读不懂就返回 -1（当成"不知道"）。

    只看体积够用：判断的是"这份分片是接着写长了，还是被整个换掉了"，
    而 jsonl/parquet 的续写只可能让它变长。
    """
    try:
        return int(str(signature).split(":", 1)[0])
    except (TypeError, ValueError):
        return -1


def _grew(before: str, after: str) -> bool:
    old, new = _sig_size(before), _sig_size(after)
    return old >= 0 and new >= 0 and new >= old


class WorkIndex:
    def __init__(self, out_dir: Path):
        self.root = Path(out_dir)
        self.state = self.root / ".tagger-state"
        self.owner = uuid.uuid4().hex
        self.lock = FileLock(self.state / "writers" / f"{self.owner}.lock")
        self.lock.acquire()
        self.closed = True
        try:
            self.db = connect(self.state / "work.sqlite3")
        except BaseException:
            self.lock.close()
            raise
        try:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, signature TEXT, version TEXT);
                CREATE TABLE IF NOT EXISTS done(image_id TEXT, source TEXT, version TEXT,
                    PRIMARY KEY(image_id,source,version));
                CREATE INDEX IF NOT EXISTS done_source ON done(source,version);
                CREATE TABLE IF NOT EXISTS claims(image_id TEXT PRIMARY KEY, owner TEXT);
                CREATE INDEX IF NOT EXISTS claims_owner ON claims(owner);
            """)
            self.db.commit()
        except BaseException:
            self.db.close()
            self.lock.close()
            raise
        self.closed = False
        self._signature_cache = {}

    def __bool__(self):
        return self.db.execute("SELECT 1 FROM sources LIMIT 1").fetchone() is not None

    def __contains__(self, image_id: str) -> bool:
        rows = self.db.execute("""SELECT s.path,s.signature FROM done d JOIN sources s
            ON d.source=s.path AND d.version=s.version WHERE d.image_id=?""", (image_id,))
        for source, signature in rows:
            valid = self._signature_cache.get((source, signature))
            if valid is None:
                try:
                    st = Path(source).stat()
                    valid = signature == f"{st.st_size}:{st.st_mtime_ns}"
                except OSError:
                    valid = False
                if len(self._signature_cache) >= 4096:
                    self._signature_cache.clear()
                self._signature_cache[(source, signature)] = valid
            if valid:
                return True
        return False

    def __len__(self) -> int:
        return self.db.execute("""SELECT COUNT(DISTINCT image_id) FROM done d
            JOIN sources s ON d.source=s.path AND d.version=s.version""").fetchone()[0]

    def refresh(self) -> None:
        from ..artifacts import find_artifacts, iter_artifact_rows, _row_done

        # 发布分片是原子替换；只索引正式文件，临时文件从来不算完成。
        with FileLock(self.state / "index.lock"):
            self._signature_cache.clear()
            previous_sources = [r[0] for r in self.db.execute("SELECT path FROM sources")]
            files = find_artifacts(self.root)
            seen = set()
            for path in files:
                source = str(path.resolve())
                seen.add(source)
                st = path.stat()
                signature = f"{st.st_size}:{st.st_mtime_ns}"
                old = self.db.execute("SELECT signature FROM sources WHERE path=?", (source,)).fetchone()
                if old and old[0] == signature:
                    continue
                version = uuid.uuid4().hex
                batch = []
                try:
                    for row in iter_artifact_rows(path):
                        if _row_done(row):
                            batch.append((str(row["image_id"]), source, version))
                        if len(batch) >= 2048:
                            with self.db:
                                self.db.executemany("INSERT OR IGNORE INTO done VALUES(?,?,?)", batch)
                            batch.clear()
                    with self.db:
                        self.db.executemany("INSERT OR IGNORE INTO done VALUES(?,?,?)", batch)
                        self.db.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?)",
                                        (source, signature, version))
                        self.db.execute("DELETE FROM done WHERE source=? AND version<>?", (source, version))
                except (OSError, ValueError, RuntimeError) as exc:
                    with self.db:
                        self.db.execute("DELETE FROM done WHERE source=?", (source,))
                        self.db.execute("DELETE FROM sources WHERE path=?", (source,))
                    print(f"[artifacts][警告] 完成索引未更新 {path.name}: {exc}", flush=True)
            for source in previous_sources:
                if source not in seen:
                    with self.db:
                        self.db.execute("DELETE FROM sources WHERE path=?", (source,))
                        self.db.execute("DELETE FROM done WHERE source=?", (source,))

    def claim(self, image_id: str, *, force: bool = False) -> str:
        """返回 claimed/done/busy。活进程持有 OS 锁，不按超时抢占慢任务。"""
        while True:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT owner FROM claims WHERE image_id=?", (image_id,)).fetchone()
                if row is None:
                    if not force and image_id in self:
                        self.db.commit()
                        return "done"
                    self.db.execute("INSERT INTO claims VALUES(?,?)", (image_id, self.owner))
                    self.db.commit()
                    return "claimed"
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            if row[0] == self.owner:
                return "busy"
            other = FileLock(self.state / "writers" / f"{row[0]}.lock")
            if not other.acquire(blocking=False):
                return "busy"
            try:
                # 对方已退出：先找回已发布、尚未登记的结果，再释放它的领取记录。
                self.refresh()
                with self.db:
                    self.db.execute("DELETE FROM claims WHERE owner=?", (row[0],))
            finally:
                other.close()

    def release(self, image_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM claims WHERE image_id=? AND owner=?", (image_id, self.owner))

    def publish(self, path: Path, rows: list[dict]) -> None:
        """分片落盘后登记：这份文件现在有什么、里面哪些算完成。

        ★同一份分片会被**另一个进程**续写（jsonl 的 append 模式就是为此存在
        的：几十个分包跑同一个产物目录，重启后接着往同名文件里追加）。而
        `version` 是各进程自己的 id，`__contains__`/`__len__` 又是按
        `source + version` 连表查的——只 INSERT 新行、把旧行原样留着的话，
        一换 version，之前那两千张就当场查不到了（"已完成"变回"没做"），
        这一轮会重打并把同样的行再追加一遍。

        所以换版本时要一并处理旧行：文件是变长（续写）就把旧行并到新版本
        名下，文件变短说明是被重写，旧行不再代表文件内容，直接删掉。
        """
        from ..artifacts import _row_done
        source = str(path.resolve())
        st = path.stat()
        signature = f"{st.st_size}:{st.st_mtime_ns}"
        version = self.owner
        with self.db:
            old = self.db.execute("SELECT version, signature FROM sources WHERE path=?",
                                  (source,)).fetchone()
            if old is not None and old[0] != version:
                if _grew(old[1], signature):
                    # 续写：旧行仍是这份文件的一部分，把版本号并过来即可。
                    # 先改后插：UPDATE 之后同一 id 的 INSERT OR IGNORE 会撞上
                    # 已有行（本来就算完成），正好跳过。
                    self.db.execute("UPDATE done SET version=? WHERE source=? AND version=?",
                                    (version, source, old[0]))
                else:
                    # 变短 = 被重写，旧行不再代表文件内容。
                    self.db.execute("DELETE FROM done WHERE source=? AND version<>?",
                                    (source, version))
            elif old is None:
                # 没见过这份分片：同源的残留行只可能是上一轮没清干净的。
                self.db.execute("DELETE FROM done WHERE source=? AND version<>?",
                                (source, version))
            self.db.executemany("INSERT OR IGNORE INTO done VALUES(?,?,?)",
                                ((str(r["image_id"]), source, version) for r in rows if _row_done(r)))
            self.db.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?)", (source, signature, version))
            self.db.executemany("DELETE FROM claims WHERE image_id=? AND owner=?",
                                ((str(r["image_id"]), self.owner) for r in rows))

    def owns(self, image_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM claims WHERE image_id=? AND owner=?",
                               (image_id, self.owner)).fetchone() is not None

    def reclaim(self, image_id: str) -> str:
        """把一条已记为"完成"的图片重新领出来做，返回 claimed/busy。

        为什么需要它：完成索引是**任一侧有产物**就记 done。派工说"这片只补
        tag"、而本地只有 NL 产物时，`claim` 会返回 done 且**不插入 claims 行**，
        后面的 `owns()` 断言就会在落盘前抛"领取已失效"，整批中断。

        两条出路要么"跳过"（tag 永远补不上），要么"落锁再做"。这里是后者：
        撤掉 done 记录、插 claims，之后 publish/release 照常工作。
        仍然会先看别的进程有没有占着——两个人同时写同一份产物才是真事故。
        """
        while True:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT owner FROM claims WHERE image_id=?",
                                      (image_id,)).fetchone()
                if row is None:
                    self.db.execute("DELETE FROM done WHERE image_id=?", (image_id,))
                    self.db.execute("INSERT INTO claims VALUES(?,?)", (image_id, self.owner))
                    self.db.commit()
                    return "claimed"
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            if row[0] == self.owner:
                return "busy"
            other = FileLock(self.state / "writers" / f"{row[0]}.lock")
            if not other.acquire(blocking=False):
                return "busy"
            try:
                self.refresh()
                with self.db:
                    self.db.execute("DELETE FROM claims WHERE owner=?", (row[0],))
            finally:
                other.close()

    def close(self) -> None:
        if self.closed:
            return
        try:
            with self.db:
                self.db.execute("DELETE FROM claims WHERE owner=?", (self.owner,))
        finally:
            self.db.close()
            self.lock.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        if hasattr(self, "closed") and not self.closed:
            self.close()
