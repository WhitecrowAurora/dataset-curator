"""成员产物的另一种落盘形态：parquet 分片。

两种形态并存，各有各的场合
--------------------------
JSON 形态（默认）：一张图两个文件 `<id>-tag.json` / `<id>-nl.json`。
    好处是**完成信号没有歧义**——文件在且非空就是做完了，不需要读内容；
    单条重跑、单条排查都方便。
    坏处是 3000 万张 = 6000 万个小文件。到了那个量级：
      · merge / verify 一次 rglob + open 就是千万次 syscall；
      · NTFS / 网络盘在千万级 inode 下明显退化；
      · 每个文件至少占一个簇（4KB），1KB 的 json 实际吃掉 4 倍空间。

parquet 形态（`--artifact-format parquet`）：N 行攒成一个分片。
    文件数降 4 个数量级，列式压缩后体积也能减半。
    代价是"完成信号"变成"要读一遍 image_id 列建集合"——只在启动时付一次。

两边读同一份 JSON payload
-------------------------
parquet 里的 tag / nl 两列存的是**和 json 文件完全相同的 JSON 字符串**，
不是拆开的列。理由：merge 的对账逻辑（内容冲突、空内容、溯源）全部建立在
payload 之上，换一套列就等于把那段逻辑复制两份，而两份一定会分叉。
代价是查询时要 json.loads 一次，但那本来就是 merge 要做的。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

ARTIFACT_PREFIX = "artifacts-"
ARTIFACT_SUFFIX = ".parquet"
COLUMNS = ("image_id", "tag", "nl", "ts")


def _pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "parquet 产物需要 pyarrow：pip install pyarrow"
            "（或改用默认的 --artifact-format json）"
        ) from exc
    return pa, pq


@dataclass
class ParquetSink:
    """按行攒、按片落。行数到了就写一个分片，写完立刻原子替换。"""

    out_dir: Path
    rows_per_file: int = 2000
    _rows: list[dict] = field(default_factory=list)
    _seq: int = 0

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # 序号取现存最大号 + 1，不能用文件个数：中间一片被人工删掉/挪走后，
        # 个数会回退，新一轮分片与现存片撞号，flush 里的 replace 会把
        # 那片**还有效的产物**整个覆盖掉。
        seqs: list[int] = []
        for p in self.out_dir.glob(f"{ARTIFACT_PREFIX}*{ARTIFACT_SUFFIX}"):
            tail = p.stem.rsplit("-", 1)[-1]
            if tail.isdigit():
                seqs.append(int(tail))
        self._seq = (max(seqs) + 1) if seqs else 0

    @property
    def pending(self) -> int:
        return len(self._rows)

    def add(self, image_id: str, tag: dict | None, nl: dict | None) -> None:
        self._rows.append({
            "image_id": str(image_id),
            "tag": json.dumps(tag, ensure_ascii=False) if tag is not None else "",
            "nl": json.dumps(nl, ensure_ascii=False) if nl is not None else "",
            "ts": time.time(),
        })
        if len(self._rows) >= max(1, self.rows_per_file):
            self.flush()

    def flush(self) -> Path | None:
        if not self._rows:
            return None
        pa, pq = _pyarrow()
        table = pa.table({
            "image_id": pa.array([r["image_id"] for r in self._rows], type=pa.string()),
            "tag": pa.array([r["tag"] for r in self._rows], type=pa.string()),
            "nl": pa.array([r["nl"] for r in self._rows], type=pa.string()),
            "ts": pa.array([r["ts"] for r in self._rows], type=pa.float64()),
        })
        final = self.out_dir / f"{ARTIFACT_PREFIX}{self._seq:05d}{ARTIFACT_SUFFIX}"
        tmp = final.with_suffix(".parquet.tmp")
        pq.write_table(table, tmp, compression="zstd")
        # 原子替换：崩在写一半时不会留下半截分片被 merge 当成"正常产物"。
        tmp.replace(final)
        self._seq += 1
        self._rows.clear()
        return final

    def close(self) -> None:
        self.flush()


def is_artifact_parquet(path: Path) -> bool:
    return path.name.startswith(ARTIFACT_PREFIX) and path.suffix == ARTIFACT_SUFFIX


def iter_parquet_artifacts(path: str | Path) -> Iterator[dict]:
    """逐行读出一个分片。列不全时按缺失处理，不报错。"""
    _, pq = _pyarrow()
    pf = pq.ParquetFile(str(path))
    names = [c for c in COLUMNS if c in pf.schema_arrow.names]
    if "image_id" not in names:
        raise ValueError(f"{path} 不是产物分片：没有 image_id 列")
    for batch in pf.iter_batches(batch_size=2048, columns=names):
        for row in batch.to_pylist():
            yield row


def load_done_ids(out_dir: str | Path) -> set[str]:
    """扫已有分片，收集已经产出过的 image_id。

    只读 image_id 一列：3000 万行的 id 大约 600MB 内存，而带上正文会翻十倍。
    这个集合就是 parquet 模式下的"文件是否存在"。
    """
    root = Path(out_dir)
    done: set[str] = set()
    if not root.is_dir():
        return done
    _, pq = _pyarrow()
    for path in sorted(root.glob(f"{ARTIFACT_PREFIX}*{ARTIFACT_SUFFIX}")):
        pf = pq.ParquetFile(str(path))
        if "image_id" not in pf.schema_arrow.names:
            continue
        for batch in pf.iter_batches(batch_size=8192, columns=["image_id"]):
            done.update(str(v) for v in batch.column(0).to_pylist())
    return done


def parse_payload(kind: str, raw: str) -> dict | None:
    """把 parquet 行里的 JSON 字符串还原成 payload。空串 = 该侧没产出。"""
    text = (raw or "").strip()
    if not text:
        return None
    key = "tag" if kind == "tag" else "caption"
    try:
        obj = json.loads(text)
    except ValueError:
        # 不是 JSON 就按裸文本收下：成员手改过、或别的工具直接写了纯文本。
        # 直接返回 None 会把这些内容当成"没产出"，而它们其实是有内容的。
        return {key: text}
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        return {key: obj}
    return None
