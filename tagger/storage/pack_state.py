"""封包断点及输入更新后的分片失效；旧分片保留在输出目录的 .delete。

为什么每次写都要拿文件锁并先对齐
--------------------------------
同一个产物目录经常有多个进程在封包（一张卡一个 worker，或者主进程和命令行
同时收尾）。断点是一份追加日志，读-改-写不在同一把锁里就会这样：

    A: 读到 done={k1,k2}（内存快照）
    B: 发现 k2 的标签被改过 → 追加 retire(k2) → 把 k2 所在旧分片搬进 .delete
    A: 拿内存里那份旧快照继续 mark(k2) → 追加一行"k2 完成"

这一行会把**已经退役**的 k2 重新立成"已完成"。它的图已经被搬走了，封包时
按断点跳过——成品静默缺图，日志上一句警告都没有。

所以 `_sync()`（只读新增段，别全量重读，那是 O(n²)）+ `self.lock` 是所有
写路径的前提。
"""
from __future__ import annotations

import json
import os
import re
import tarfile
import time
import uuid
import zipfile
from pathlib import Path

from .locking import FileLock


def artifact_signature(path: Path) -> str:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return ""
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _pack_ids(path: Path) -> set[str]:
    """旧断点未记录分片归属，仅在标签更新时读取旧包的元数据。"""
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq
        return set(pq.read_table(path, columns=["image_id"])["image_id"].to_pylist())
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            rows = (json.loads(archive.read(name)) for name in archive.namelist()
                    if name.endswith(".json") and name != "index.json")
            return {str(row["image_id"]) for row in rows if row.get("image_id")}
    with tarfile.open(path) as archive:
        result = set()
        for member in archive:
            if member.isfile() and member.name.endswith(".json"):
                with archive.extractfile(member) as stream:
                    row = json.load(stream)
                if row.get("image_id"):
                    result.add(str(row["image_id"]))
        return result


class PackState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._done: dict[str, dict] = {}
        self._pending: dict[str, dict] = {}
        # 退役过的键 → 被搬走的那些旧包。用来挡住"过期快照把退役条目又写回来"
        # 这种写法：重建时记的是**新包名**，而过期快照记的正是被搬走的旧包名。
        self._retired: dict[str, set[str]] = {}
        # 已经吃进内存的字节数：_sync 从这里往后读，只处理新增段。
        self._pos = 0
        self.lock = FileLock(self.path.with_suffix(self.path.suffix + ".lock"))
        with self.lock:
            self._sync()
            self._fix_tail()
            # 先落失效记录再搬旧包；中断后继续搬完，完成后不再碰同名新包。
            for row in list(self._pending.values()):
                self._move_retired(row)
                self._append({"retired": row["backup"]})

    # ------------------------------------------------------------ 读

    def _sync(self) -> None:
        """把别的进程追加的行吃进内存。**调用方必须持有 self.lock**。"""
        if not self.path.is_file():
            return
        with self.path.open("rb") as stream:
            stream.seek(self._pos)
            chunk = stream.read()
        # 只处理到最后一个换行为止：写方总是整行 fsync，半行留着等下次。
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return
        self._pos += cut + 1
        for line in chunk[:cut].split(b"\n"):
            try:
                row = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue          # 崩溃留下的残行：跳过，别把整份断点作废
            self._apply(row)

    def _apply(self, row) -> None:
        if not isinstance(row, dict):
            return
        if "retire" in row:
            packs = set(row.get("retire") or ())
            for key in row.get("keys") or ():
                self._done.pop(key, None)
                self._retired.setdefault(key, set()).update(packs)
            self._pending[str(row.get("backup") or "")] = row
        elif "retired" in row:
            self._pending.pop(str(row.get("retired") or ""), None)
        else:
            key = row.get("key")
            if key is not None:
                self._done[key] = row

    def _fix_tail(self) -> None:
        """末行没换行就补一个，并把 _pos 推到末尾。

        崩溃写出来的半行不能和后一行粘成一条坏记录；补完直接跳过它（读不出
        完整 JSON 的行本来也进不了断点）。
        """
        if not (self.path.is_file() and self.path.stat().st_size):
            return
        with self.path.open("rb+") as stream:
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                stream.write(b"\n")
        self._pos = self.path.stat().st_size

    # ------------------------------------------------------------ 写

    def _append_many(self, rows) -> None:
        """追加若干行。**调用方必须持有 self.lock**（否则会与别人的行交错）。"""
        data = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        payload = data.encode("utf-8")
        if not payload:
            return
        with self.path.open("ab") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        self._pos += len(payload)

    def _append(self, row: dict) -> None:
        self._append_many([row])

    def done(self, key: str) -> bool:
        return key in self._done

    def mark(self, key: str, **extra) -> None:
        self.mark_many([(key, extra)])

    def mark_many(self, entries) -> None:
        entries = list(entries)
        if not entries:
            return
        with self.lock:
            self._sync()          # 别的进程可能刚把这批里的某几张退休掉
            rows: dict[str, dict] = {}
            for key, extra in entries:
                if key in self._done:
                    continue
                gone = self._retired.get(key)
                if gone and gone.intersection(extra.get("packs") or ()):
                    # 记的还是那个已经被搬进 .delete 的包 —— 这是过期快照的
                    # 产物，不是"重建完成"。写下去会让 later 的封包按断点跳过
                    # 这张图，而它的图已经不在原地了（成品静默缺图）。
                    continue
                rows[key] = {"key": key, "ts": time.time(), **extra}
            if rows:
                self._append_many(rows.values())
                self._done.update(rows)
                for key in rows:
                    self._retired.pop(key, None)   # 已按新包重建，退役到此为止

    @property
    def count(self) -> int:
        return len(self._done)

    def stale(self, signatures: dict[str, str], worker: str) -> tuple[set[str], set[str]]:
        with self.lock:
            self._sync()          # 先对齐再判：别人的退役/新包也算数
        changed = set()
        for key, signature in signatures.items():
            row = self._done.get(key)
            if row is None or not signature:
                continue
            previous = row.get("signature")
            if (previous != signature if previous is not None else
                    int(signature.split(":")[1]) > int(row.get("ts", 0) * 1e9)):
                changed.add(key)
        if not changed:
            return set(), set()
        packs = {name for key in changed for name in self._done[key].get("packs", [])}
        affected = set(changed)
        legacy = {key for key in changed if not self._done[key].get("packs")}
        if legacy:
            pattern = re.compile(rf"pack-{re.escape(worker)}-\d+\.(zip|tar|parquet)")
            for path in self.path.parent.iterdir():
                if pattern.fullmatch(path.name) and path.is_file():
                    ids = _pack_ids(path)
                    if ids & legacy:
                        packs.add(path.name)
                        affected.update(ids)
        for key, row in self._done.items():
            if packs.intersection(row.get("packs", [])):
                affected.add(key)
        return affected, packs

    def retire(self, keys: set[str], packs: set[str]) -> None:
        if not keys:
            return
        with self.lock:
            self._sync()
            # 只退役"现在还记着完成"的键：别人已经退过、或者根本没写进断点的，
            # 再退一次会凭空多出一份待搬清单（旧包已经不在原地了）。
            keys = {key for key in keys if key in self._done}
            if not keys:
                return
            row = {"retire": sorted(packs), "keys": sorted(keys),
                   "backup": f"pack-rebuild-{uuid.uuid4().hex}"}
            self._append(row)
            for key in keys:
                self._done.pop(key, None)
            self._move_retired(row)
            self._append({"retired": row["backup"]})

    def _move_retired(self, row: dict) -> None:
        root = self.path.parent.resolve()
        backup = str(row["backup"])
        if Path(backup).name != backup:
            raise ValueError("封包备份目录无效")
        for name in row["retire"]:
            if Path(name).name != name:
                raise ValueError("封包记录包含无效分片路径")
            source = root / name
            target = root / ".delete" / backup / name
            if not target.exists() and source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                source.replace(target)
