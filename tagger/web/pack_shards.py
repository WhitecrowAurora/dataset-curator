"""离线包分片清单 + 各片已经产出多少。

为什么需要：打标页的「已下载的离线包」只给了一个下拉框，选中就等于整包按
目录跑。可离线包动辄几十个 tar，用户常常只想挑几片打。这个模块把「里面有
哪些片、哪片已经打过」一次算出来，供那个分片面板展示和勾选。

进度从哪来
----------
产物是按**包名**定名的（`MultiShardSink` 写出
`artifacts-<包名>-<进程编号>-<序号>.jsonl`），所以把文件名反解回去就是分片
归属，不必为了统计再去读分片内容。

反过来两个口子要提前说清，不能假装知道：
* 产物形态是 parquet 时按行数切、不按包切（`ParquetSink`），文件名里没有包名
  → 无法归属，返回 `attributable=False`，前端据此说明「统计不到」。
* 总行数只有 parquet 拿得到（footer 的 num_rows，O(1)）。tar/zip 得顺序读
  一遍才知道，几十 GB 的包不值得为一行数字去扫，所以 `total_rows=None`，
  只有「已产出行数」这一个进度证据。
"""

from __future__ import annotations

import threading
from pathlib import Path

from ..artifacts import ARTIFACT_PREFIX, JSONL_SUFFIX, safe_stem

# 与 client.classify_local_inputs 同一口径，别在两处各写一份后缀表。
WEBDS_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")

# 行数是按文件 stat 缓存的：分片面板会被反复打开，而产物动辄几百 MB。
# 键里带 size + mtime，产物被追加后自然失效，不需要手工清。
_LINE_CACHE: dict[tuple[str, int, int], int] = {}
_CACHE_LOCK = threading.Lock()


def _kind(name: str) -> str | None:
    low = name.lower()
    if low.endswith(".parquet"):
        return "parquet"
    if low.endswith(WEBDS_SUFFIXES):
        return "webdataset"
    return None


def _parquet_rows(path: Path) -> int | None:
    """parquet 的总行数。读不动就当未知，不挡整个列表。"""
    try:
        import pyarrow.parquet as pq

        return int(pq.ParquetFile(str(path)).metadata.num_rows)
    except Exception:  # noqa: BLE001 - 缺 pyarrow / 坏 footer 都退化成未知
        return None


def _count_lines(path: Path) -> int:
    try:
        st = path.stat()
    except OSError:
        return 0
    key = (str(path), st.st_size, int(st.st_mtime))
    with _CACHE_LOCK:
        hit = _LINE_CACHE.get(key)
    if hit is not None:
        return hit
    n = 0
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                n += chunk.count(b"\n")
    except OSError:
        return 0
    with _CACHE_LOCK:
        _LINE_CACHE[key] = n
    return n


def _owner_stem(body: str, stems: list[str]) -> str | None:
    """产物文件名中间那段 → 是哪个包。

    body 形如 `shard-000005` / `shard-000005-<32位进程编号>-00000`。
    分片名可能互为前缀（`shard-1` 与 `shard-10`），所以取最长匹配。
    """
    best: str | None = None
    for stem in stems:
        if body == stem or body.startswith(stem + "-"):
            if best is None or len(stem) > len(best):
                best = stem
    return best


def scan(dir_path: str, out_dir: str = "") -> dict:
    """列一个离线包里的分片，并算出每片已产出多少行。"""
    root = Path(dir_path).expanduser()
    if not root.is_dir():
        raise ValueError(f"目录不存在：{root}")

    found: list[dict] = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        kind = _kind(path.name)
        if kind is None:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        found.append({
            "name": path.name,
            "path": str(path),
            "bytes": size,
            "kind": kind,
            "stem": safe_stem(path.stem, fallback="multi"),
        })

    stems = [s["stem"] for s in found]
    rows_by_stem: dict[str, int] = {}
    any_jsonl = False
    out = Path(out_dir).expanduser() if out_dir else None
    if out is not None and out.is_dir():
        for art in out.glob(f"{ARTIFACT_PREFIX}*{JSONL_SUFFIX}"):
            body = art.name[len(ARTIFACT_PREFIX):]
            if body.endswith(JSONL_SUFFIX):
                body = body[:-len(JSONL_SUFFIX)]
            any_jsonl = True
            stem = _owner_stem(body, stems)
            if stem is None:
                continue
            rows_by_stem[stem] = rows_by_stem.get(stem, 0) + _count_lines(art)

    shards: list[dict] = []
    for item in found:
        done = rows_by_stem.get(item["stem"], 0)
        total = _parquet_rows(Path(item["path"])) if item["kind"] == "parquet" else None
        if done <= 0:
            state = "none"
        elif total is not None and done >= total:
            state = "done"
        else:
            # 有产物但凑不满 → 中断过 / 正在跑 / 总数未知。一律算"打了一部分"，
            # 宁可让用户多点一次「只打未完成」也不要谎报整片完成。
            state = "partial"
        shards.append({
            "name": item["name"],
            "path": item["path"],
            "bytes": item["bytes"],
            "kind": item["kind"],
            "total_rows": total,
            "done_rows": done,
            "state": state,
        })

    return {
        "dir": str(root),
        "out": str(out) if out is not None else "",
        "out_exists": bool(out is not None and out.is_dir()),
        "attributable": any_jsonl,
        "shards": shards,
    }
