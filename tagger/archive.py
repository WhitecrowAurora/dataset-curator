"""分包读写：输入用索引直取，输出追加后封口打包。

两个核心约束，决定了这里的所有设计：

1. 输入不能全解压。
   图片分包动辄 1-3 GB，成员机器上同时解压多个包既慢又占盘。
   用 ZIP 的中央目录当索引，只读出当前需要的那个条目；
   ZIP_STORED（不压缩）时读取几乎零开销，因为 JPEG 已经把压缩做完了，
   再 deflate 一次省不到 5%，却让每次读取都要解压流。

2. 输出必须能"原子追加"。
   任何归档格式（zip/tar/safetensors）都不支持原地追加——它们的索引在文件头，
   加一个成员就得整体重写。所以正确做法是：
       工作期  → per-worker 的 .jsonl 逐行 append（崩溃只丢最后一行）
       写满 N 条 → 打包成一个 .zip（内含 annotations.jsonl + index.json）
       打包完成 → fsync + rename 原子落盘
   下游拿到的是不可变的分包，重打标只产生新分包，不修改旧分包。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Iterator

INDEX_NAME = "index.json"
ANNOT_NAME = "annotations.jsonl"
META_NAME = "meta.jsonl"

from .naming import IMAGE_SUFFIXES  # noqa: F401 - 单一事实源在 naming；这里 re-export

COMPRESS_MODES = {
    "stored": zipfile.ZIP_STORED,
    "deflate": zipfile.ZIP_DEFLATED,
}


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_of_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ------------------------------------------------------------------ 输入分包


@dataclass(slots=True)
class Entry:
    """分包内的一个成员。offset 是它在 zip 内的绝对位置，便于外部工具直接读。"""

    name: str
    size: int
    offset: int
    compress_type: int = 0
    sha256: str = ""
    width: int = 0
    height: int = 0


class ZipShard:
    """输入分包读取器。

    用法：
        with ZipShard("shard-0003.zip") as shard:
            for name in shard.image_names():
                with shard.open(name) as f:
                    img = Image.open(f)      # 流式，不整块读进内存
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._zf = zipfile.ZipFile(self.path, "r")
        self._infos = {i.filename: i for i in self._zf.infolist()}
        self._meta: dict[str, dict] = {}
        self._load_meta()

    def _load_meta(self) -> None:
        """meta.jsonl 是可选条目：记录了每张图的宽高、sha256 等，
        有了它就能做预过滤（比如按宽高比筛）而不必打开图片本身。"""
        if META_NAME not in self._infos:
            return
        with self._zf.open(META_NAME) as f:
            for raw in f:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                name = item.get("name")
                if name:
                    self._meta[name] = item

    def image_names(self) -> list[str]:
        return [n for n in self._infos if n.lower().endswith(IMAGE_SUFFIXES)]

    def entries(self) -> Iterator[Entry]:
        for name in self.image_names():
            info = self._infos[name]
            meta = self._meta.get(name, {})
            yield Entry(
                name=name,
                size=info.file_size,
                offset=info.header_offset,
                compress_type=info.compress_type,
                sha256=meta.get("sha256", ""),
                width=int(meta.get("width") or 0),
                height=int(meta.get("height") or 0),
            )

    def open(self, name: str) -> IO[bytes]:
        """返回流式文件对象，不做全解压。"""
        return self._zf.open(name, "r")

    def read(self, name: str) -> bytes:
        return self._zf.read(name)

    def meta(self, name: str) -> dict:
        return self._meta.get(name, {})

    def to_index(self) -> dict:
        """导出这份分包的索引，供全池检索用（不必打开每个分包）。"""
        entries = []
        for e in self.entries():
            d = asdict(e)
            # compress_type 是内部枚举，对下游没用
            d.pop("compress_type", None)
            entries.append(d)
        return {
            "shard": self.path.name,
            "count": len(entries),
            "bytes": self.path.stat().st_size,
            "entries": entries,
        }

    def close(self) -> None:
        self._zf.close()

    def __enter__(self) -> "ZipShard":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def build_manifest(shard_dir: str | Path, out_path: str | Path | None = None) -> Path:
    """扫描目录下所有输入分包，生成全局清单（每包一行，含内部索引）。

    成员拿到清单后可以用 `--only-shard` 只取自己负责的包，
    也能在不解压的情况下按宽高做预过滤。
    """
    shard_dir = Path(shard_dir)
    out = Path(out_path) if out_path else shard_dir / "manifest.jsonl"
    zips = sorted(shard_dir.glob("*.zip"))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for zp in zips:
            try:
                with ZipShard(zp) as shard:
                    idx = shard.to_index()
            except (zipfile.BadZipFile, OSError) as exc:
                f.write(json.dumps({"shard": zp.name, "error": str(exc)},
                                   ensure_ascii=False) + "\n")
                continue
            f.write(json.dumps(idx, ensure_ascii=False) + "\n")
    return out


# ------------------------------------------------------------------ 输出分包


@dataclass(slots=True)
class SealedShard:
    path: Path
    count: int
    sha256: str
    bytes: int


class AnnotationShardWriter:
    """输出分包写入器：逐行追加 → 满额封口 → 打包成带索引的 zip。

    一个 worker 同时只应持有一个写入器（单写者原则）。
    多个 worker 各写各的文件，靠文件名区分，不做跨进程共享写。
    """

    def __init__(
        self,
        out_dir: str | Path,
        worker: str,
        *,
        max_records: int = 5000,
        compress: str = "stored",
        model_id: str = "",
        fsync_every: int = 200,
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.worker = worker
        self.max_records = max_records
        self._compress = COMPRESS_MODES.get(compress, zipfile.ZIP_STORED)
        self.model_id = model_id
        self.fsync_every = max(1, fsync_every)

        self._seq = self._next_seq()
        self._count = 0
        self._offsets: list[dict] = []
        self._fh: IO[str] = self._open_jsonl()
        self._since_fsync = 0

    def _next_seq(self) -> int:
        existing = sorted(self.out_dir.glob(f"ann-{self.worker}-*.zip"))
        if not existing:
            return 0
        try:
            return int(existing[-1].stem.rsplit("-", 1)[-1]) + 1
        except ValueError:
            return len(existing)

    def _jsonl_path(self) -> Path:
        return self.out_dir / f"ann-{self.worker}-{self._seq:05d}.jsonl"

    def _zip_path(self) -> Path:
        return self.out_dir / f"ann-{self.worker}-{self._seq:05d}.zip"

    def _open_jsonl(self) -> IO[str]:
        path = self._jsonl_path()
        # 续跑：如果 jsonl 已存在，接着往后写，offset 也要恢复
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    self._count += 1
                    self._offsets.append({
                        "id": obj.get("image_sha256", ""),
                        "model_id": (obj.get("provenance") or {}).get("model_id", ""),
                        "offset": 0,  # 会在 rebuild 时重算
                        "len": len(line),
                        "ok": obj.get("ok", True),
                    })
            self._rebuild_offsets(path)
        return path.open("a", encoding="utf-8", buffering=1)

    def _rebuild_offsets(self, path: Path) -> None:
        """按实际行字节重新计算偏移，保证索引与文件一致。"""
        self._offsets.clear()
        offset = 0
        self._count = 0
        with path.open("rb") as f:
            for raw in f:
                stripped = raw.strip()
                if not stripped:
                    offset += len(raw)
                    continue
                try:
                    obj = json.loads(stripped.decode("utf-8"))
                except ValueError:
                    offset += len(raw)
                    continue
                self._count += 1
                self._offsets.append({
                    "id": obj.get("image_sha256", ""),
                    "model_id": (obj.get("provenance") or {}).get("model_id", ""),
                    "offset": offset,
                    "len": len(stripped),
                    "ok": obj.get("ok", True),
                })
                offset += len(raw)

    @property
    def count(self) -> int:
        return self._count

    @property
    def full(self) -> bool:
        return self._count >= self.max_records

    def append(self, annotation_json: str, image_sha256: str, model_id: str,
               ok: bool = True) -> bool:
        """追加一条。返回 True 表示应该封口了。

        调用方负责给出序列化好的 jsonl 行——这里不关心字段内容，
        只负责字节偏移和刷盘，保持职责单一。
        """
        line = annotation_json.rstrip("\n")
        start = self._fh.tell()
        self._fh.write(line + "\n")
        self._since_fsync += 1
        if self._since_fsync >= self.fsync_every:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._since_fsync = 0
        self._offsets.append({
            "id": image_sha256,
            "model_id": model_id,
            "offset": start,
            "len": len(line.encode("utf-8")),
            "ok": ok,
        })
        self._count += 1
        return self.full

    def seal(self) -> SealedShard:
        """封口：jsonl → zip（含 index.json），原子 rename 落盘。"""
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()

        jsonl_path = self._jsonl_path()
        zip_path = self._zip_path()
        tmp_path = zip_path.with_suffix(".zip.tmp")

        index = {
            "schema": 1,
            "worker": self.worker,
            "seq": self._seq,
            "count": self._count,
            "model_id": self.model_id,
            "ts": time.time(),
            "source": jsonl_path.name,
            "entries": self._offsets,
        }

        with zipfile.ZipFile(tmp_path, "w", compression=self._compress,
                             allowZip64=True) as zf:
            # index.json 放第一个，下游可以只读它做定位
            zf.writestr(INDEX_NAME, json.dumps(index, ensure_ascii=False))
            zf.write(jsonl_path, ANNOT_NAME)

        # Windows 上 fsync 需要可写句柄，只读模式会 EBADF
        with tmp_path.open("rb+") as f:
            os.fsync(f.fileno())

        # 同目录 rename，POSIX 与 NTFS 上都是原子的
        os.replace(tmp_path, zip_path)

        digest = sha256_of_file(zip_path)
        size = zip_path.stat().st_size
        sealed = SealedShard(path=zip_path, count=self._count,
                             sha256=digest, bytes=size)

        # 保留 jsonl 便于续跑排查；确认无误后可手动清
        self._seq += 1
        self._count = 0
        self._offsets = []
        self._since_fsync = 0
        self._fh = self._open_jsonl()
        return sealed

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()

    def __enter__(self) -> "AnnotationShardWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_index(shard_path: str | Path) -> dict:
    """读取输出分包的索引，不加载 annotations.jsonl。"""
    with zipfile.ZipFile(shard_path, "r") as zf:
        if INDEX_NAME not in zf.namelist():
            raise KeyError(f"{shard_path} 里没有 {INDEX_NAME}")
        return json.loads(zf.read(INDEX_NAME).decode("utf-8"))


def read_annotation(shard_path: str | Path, offset: int, length: int) -> dict:
    """按索引里的偏移直读单条标注，不解压整个分片。

    仅对 ZIP_STORED 有真实收益；deflate 时会退化成解压整条流。
    """
    with zipfile.ZipFile(shard_path, "r") as zf:
        with zf.open(ANNOT_NAME) as f:
            f.seek(offset)
            raw = f.read(length)
    return json.loads(raw.decode("utf-8"))
