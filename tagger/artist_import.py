"""把千奇百怪的画师输入整流成一种 schema。

为什么需要这一层
----------------
画师名在链路里有四个来源：素材目录名、danbooru 的 `post id → artists` 表、
封包产物里的 artist 列、以及人手工整理的一张表。它们的分隔符、命名习惯、
有没有 id 全都不一样。如果每个消费点各写一遍解析，迟早出现
"打标侧把 `a & b` 当两个画师、封包侧当一个"这种事，而且不会报错。

所以这里只做一件窄事：任意输入 → 一张 parquet，
    id:int64 / artists:list<str> / sources:list<str>
并把口径问题在入口一次性解决：

1. **分隔符**：`& , ; | 、` 统一切开。`/` 不当分隔符——
   `hatsune_miku_(cosplay)` 这类名字里带斜杠，按它切会把一个画师劈成两个。
2. **清洗**：复用 `clean_artist_name`，与注入侧同一口径（空格→下划线、去 @#）。
3. **别名**：查 `AliasTable`，只认人工填过的；没填的保留原名，**不音译**。
4. **冲突**：同一个 id 两行给了不同画师集合，默认直接失败。

为什么冲突要失败而不是后者覆盖：覆盖后整张表看起来完全正常，只有那批图
挂上了别人的画师。这种错进了 30M 规模的数据集，抽样是发现不了的。

产物与 `work/shard_artists.py` 同构（列 + 文件级 metadata 的 `shard`/`rows`），
所以 `ArtistTable` 的配对校验能直接吃——整流完不用再写第二套加载逻辑。
"""

from __future__ import annotations

import csv
import io
import json
import re
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .artist import AliasTable, ArtistTable, clean_artist_name
from .config import ConfigError

# 多画师分隔符。逗号/分号/竖线/顿号是各来源的常见写法，`&` 是 danbooru 原生。
_SPLIT = re.compile(r"\s*[&,;|、]\s*")

# 视为「这一列是 id」的名字，用于带表头的表格。
_ID_KEYS = ("id", "image_id", "post_id", "postid", "vid")
_NAME_KEYS = ("artists", "artist", "author", "creator", "names", "name")

# 打包成分片时认得的分片后缀（与 distribute / hf_source 一致）。
_SHARD_GLOBS = ("shard-*.tar", "shard-*.tar.gz", "shard-*.tgz", "shard-*.zip")


@dataclass(slots=True)
class ImportReport:
    """整流结果。数字都按「实际写进表里的」算，不按源文件行数。"""

    rows: int = 0
    with_artist: int = 0
    names: int = 0
    skipped: int = 0
    conflicts: list[str] = field(default_factory=list)
    output: str = ""
    shards: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts


def parse_artists(text: object, *, aliases: dict[str, str] | None = None,
                  max_len: int = 64) -> list[str]:
    """一段文本 → 去重保序的画师名列表。

    别名只映射**整段**还是**每一段**？这里按切分后的每一段查：
    danbooru 里 `english_name` 本身就是单个人名，整段查会在 `a & b` 上永远
    不命中，等于白搭。
    """
    if text is None:
        return []
    raw = text if isinstance(text, str) else " ".join(str(x) for x in text)
    out: list[str] = []
    seen: set[str] = set()
    for part in _SPLIT.split(raw):
        part = part.strip()
        if not part:
            continue
        mapped = (aliases or {}).get(part)
        name = clean_artist_name(mapped or part, max_len)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


# ------------------------------------------------------------------ 读入


def _read_text(path: Path) -> str:
    """按 utf-8-sig → gbk 顺序试。Windows 上手工整理的表十有八九是 gbk/ANSI。"""
    for enc in ("utf-8-sig", "gbk"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _is_header(parts: list[str]) -> bool:
    lowered = {p.strip().lower().lstrip("\ufeff") for p in parts}
    return bool(lowered & set(_ID_KEYS + _NAME_KEYS))


def _split_row(parts: list[str]) -> tuple[str, str]:
    """一行 → (id 或空, 名字文本)。

    带 id 的两列式有两种写法（`id,names` 与 `names,id`），按"哪边是纯数字"
    判断，不靠列序——手工表两种都有人这么填。
    """
    cells = [p.strip() for p in parts if p and p.strip()]
    if not cells:
        return "", ""
    if len(cells) == 1:
        return "", cells[0]
    if cells[0].isdigit():
        return cells[0], " ".join(cells[1:])
    if cells[-1].isdigit():
        return cells[-1], " ".join(cells[:-1])
    return "", " ".join(cells)


def _iter_csv(path: Path) -> Iterator[tuple[str, str]]:
    delim = "\t" if path.suffix.lower() in (".tsv", ".tab") else ","
    reader = csv.reader(io.StringIO(_read_text(path)), delimiter=delim)
    for i, row in enumerate(reader):
        if not row or all(not c.strip() for c in row):
            continue
        if i == 0 and _is_header(row):
            continue
        yield _split_row(row)


def _iter_txt(path: Path) -> Iterator[tuple[str, str]]:
    for i, line in enumerate(_read_text(path).splitlines()):
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        # 用制表/竖线当列分隔，避免按空格切坏带空格的画师名
        if "\t" in line:
            parts = line.split("\t")
        elif "|" in line:
            parts = line.split("|")
        else:
            parts = [line]
        if i == 0 and _is_header(parts):
            continue
        yield _split_row(parts)


def _iter_jsonl(path: Path) -> Iterator[tuple[str, str]]:
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        iid = ""
        for key in _ID_KEYS:
            if obj.get(key) not in (None, ""):
                iid = str(obj[key])
                break
        names = ""
        for key in _NAME_KEYS:
            val = obj.get(key)
            if not val:
                continue
            names = val if isinstance(val, str) else " & ".join(str(x) for x in val)
            break
        yield iid, names


def _iter_parquet(path: Path) -> Iterator[tuple[str, str]]:
    """已有 parquet 也走这条路（换列名、换 schema 的迁移用）。"""
    import pyarrow.parquet as pq

    try:
        tbl = pq.read_table(path)
    except Exception as exc:                       # noqa: BLE001
        raise ConfigError(f"读不了 {path.name}：{type(exc).__name__} {exc}") from exc
    cols = {c.lower(): c for c in tbl.column_names}
    id_col = next((cols[k] for k in _ID_KEYS if k in cols), None)
    name_col = next((cols[k] for k in _NAME_KEYS if k in cols), None)
    if name_col is None:
        raise ConfigError(f"{path.name} 里找不到画师列（认 {_NAME_KEYS}）")
    ids = tbl.column(id_col).to_pylist() if id_col else [None] * tbl.num_rows
    names = tbl.column(name_col).to_pylist()
    for iid, val in zip(ids, names):
        text = "" if val is None else (
            val if isinstance(val, str) else " & ".join(str(x) for x in val))
        yield ("" if iid is None else str(iid)), text


def _iter_source(path: Path) -> Iterator[tuple[str, str]]:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv", ".tab"):
        yield from _iter_csv(path)
    elif suffix in (".jsonl", ".ndjson"):
        yield from _iter_jsonl(path)
    elif suffix == ".parquet":
        yield from _iter_parquet(path)
    else:
        yield from _iter_txt(path)


def collect(
    sources: Iterable[str | Path],
    *,
    aliases: dict[str, str] | None = None,
    max_len: int = 64,
    start: int = 1,
    on_conflict: str = "error",
) -> tuple[dict[int, list[str]], ImportReport]:
    """所有输入文件 → `id → 画师列表`。

    没有 id 的行（纯名字表）按出现顺序编号，从 `start` 开始。这样
    "第 N 行对应第 N 张图"的旧习惯还能用，但只适合已经对齐过的清单。
    """
    if on_conflict not in ("error", "first", "last"):
        raise ConfigError(f"on_conflict 只能是 error/first/last，收到 {on_conflict!r}")

    table: dict[int, list[str]] = {}
    report = ImportReport()
    seq = int(start)

    for src in sources:
        path = Path(src)
        if not path.is_file():
            raise ConfigError(f"画师输入不存在：{path}")
        for raw_id, raw_names in _iter_source(path):
            names = parse_artists(raw_names, aliases=aliases, max_len=max_len)
            if raw_id:
                pid = ArtistTable.id_of(raw_id)
                if pid is None:
                    report.skipped += 1
                    continue
            else:
                pid = seq
                seq += 1
            if not names:
                report.skipped += 1
                continue
            old = table.get(pid)
            if old is None:
                table[pid] = names
                continue
            if old == names:
                continue
            report.conflicts.append(
                f"{pid}：{old} 与 {names}（{path.name}）")
            if on_conflict == "error":
                continue
            if on_conflict == "first":
                continue
            table[pid] = names

    return table, report


def raise_on_conflicts(report: ImportReport, on_conflict: str = "error") -> None:
    """有冲突且口径是 error 就失败。

    故意拆出来而不是塞进 `collect`：`collect` 是"看一眼有什么"的只读动作，
    要在报错之前把冲突清单打给用户看；真正决定写不写盘的是调用方
    （`import_artists` / CLI 的切分分支）。
    """
    if not report.conflicts or on_conflict != "error":
        return
    raise ConfigError(
        f"画师表有 {len(report.conflicts)} 处冲突，未写出任何文件：\n  "
        + "\n  ".join(report.conflicts[:20])
        + ("\n  …" if len(report.conflicts) > 20 else "")
        + "\n确认哪边对之后用 --on-conflict first|last 重跑。")


# ------------------------------------------------------------------ 写出


def _write_table(path: Path, ids: list[int], artists: list[list[str]],
                 *, meta: dict[str, str]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:                      # pragma: no cover - 环境缺依赖
        raise ConfigError("整流画师表需要 pyarrow：pip install pyarrow") from exc

    schema = pa.schema([
        pa.field("id", pa.int64()),
        pa.field("artists", pa.list_(pa.string())),
        pa.field("sources", pa.list_(pa.string())),
    ])
    label = meta.get("source", "")
    tbl = pa.Table.from_arrays(
        [pa.array(ids, pa.int64()),
         pa.array(artists, pa.list_(pa.string())),
         pa.array([[label] if label else [] for _ in ids], pa.list_(pa.string()))],
        schema=schema).replace_schema_metadata(
            {str(k).encode(): str(v).encode() for k, v in meta.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(tbl, tmp, compression="zstd")
    tmp.replace(path)


def _meta(source: str, rows: int, *, shard: str = "", matched: int = 0) -> dict[str, str]:
    meta = {
        "source": source,
        "rows": str(rows),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "builder": "tagger/artist_import.py",
    }
    if shard:
        meta["shard"] = shard
        meta["matched"] = str(matched)
    return meta


def import_artists(
    sources: Iterable[str | Path],
    out: str | Path,
    *,
    aliases_path: str | Path | None = None,
    max_len: int = 64,
    start: int = 1,
    on_conflict: str = "error",
    source_label: str = "import",
) -> ImportReport:
    """整流成单个全表 parquet（`id → [artists]`）。"""
    aliases = AliasTable(aliases_path).as_dict() if aliases_path else {}
    table, report = collect(sources, aliases=aliases, max_len=max_len,
                            start=start, on_conflict=on_conflict)
    # 必须在写盘之前失败：写了一半再抛，用户会分不清这份表能不能用。
    raise_on_conflicts(report, on_conflict)
    ids = sorted(table)
    _write_table(Path(out), ids, [table[i] for i in ids],
                 meta=_meta(source_label, len(ids), matched=len(ids)))
    report.rows = len(ids)
    report.with_artist = len(ids)
    report.names = sum(len(v) for v in table.values())
    report.output = str(out)
    return report


def _member_ids(archive: Path) -> list[int]:
    """分片成员名 → post id。只认末段是纯数字的成员（`8349000.jpg`）。"""
    out: list[int] = []
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
    else:
        with tarfile.open(archive, "r:*") as tf:
            names = [m.name for m in tf if m.isfile()]
    for name in names:
        stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if stem.isdigit():
            out.append(int(stem))
    return out


def split_by_shards(
    table: dict[int, list[str]],
    shard_dir: str | Path,
    out_dir: str | Path,
    *,
    source_label: str = "import",
) -> list[dict]:
    """按分片**实际成员**切片，一个 tar/zip 配一个 `<stem>.artist.parquet`。

    为什么不按 id 区间推算：区间是"累加到 N 张就封口"的估算，只要有一片
    计数差一行，后面每片都会错位，而错位是静默的。以成员名为准，错不了。

    每个成员都写一行（没画师的写空列表）：这样 `rows` metadata 等于成员数，
    与 `work/shard_artists.py` 同构，"表里没这条"和"这条没画师"才能区分开。
    """
    root = Path(shard_dir)
    if not root.is_dir():
        raise ConfigError(f"分片目录不存在：{root}")
    archives: list[Path] = []
    for pattern in _SHARD_GLOBS:
        archives.extend(sorted(root.glob(pattern)))
    if not archives:
        raise ConfigError(f"{root} 下没有分片（认 {', '.join(_SHARD_GLOBS)}）")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stats: list[dict] = []
    total = matched = 0
    for archive in archives:
        try:
            ids = _member_ids(archive)
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise ConfigError(f"读不动分片 {archive.name}：{exc}") from exc
        rows = [table.get(i, []) for i in ids]
        hit = sum(1 for r in rows if r)
        path = out / f"{archive.stem}.artist.parquet"
        _write_table(path, ids, rows,
                     meta=_meta(source_label, len(ids), shard=archive.name,
                                matched=hit))
        stats.append({"shard": archive.name, "rows": len(ids), "matched": hit,
                      "path": str(path)})
        total += len(ids)
        matched += hit
    stats.append({"shard": "", "rows": total, "matched": matched,
                  "coverage": round(matched / total, 4) if total else 0.0,
                  "out_dir": str(out)})
    return stats
