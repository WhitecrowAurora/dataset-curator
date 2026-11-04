"""打标产物的整理与合并：一堆 json → 一个大单体 json。

要解决的具体问题
----------------
打标产物是**一张图两个文件**（`<id>-tag.json` / `<id>-nl.json`），好处是完成
信号没有歧义、单条可重跑，坏处是训练侧要的是"一条记录含全部字段"。所以中间
必然有一步合并，而这一步有三个坑：

1. **两种文件按 image_id 配对**，不是简单拼接。直接 cat 会得到两倍条数，
   一半只有 tag、一半只有 caption，训练侧读到空字段不会报错，只会静默降质。
2. **同名字段会冲突**：`artist` / `provenance` / `ts` 两边都有，值未必相同。
   静默取一个等于编造数据，所以这里计数并在报告里给出来。
3. **"大单体 json"可能真的很大**：所以写出是流式的（逐条 append + 逗号控制），
   不把整表先物化成一个 list——物化 300 万条就够把内存打满。

输入支持三种形态（扫描时按内容判断，不全靠扩展名）
--------------------------------------------------
- JSON 数组：`[{...}, {...}]`
- JSONL：每行一个对象
- 单个对象：`{...}`（本项目的逐图产物就是这种）

输出两种口径（`format`）
------------------------
- `full`：保留所有字段的并集，便于人工核对与二次加工。
- `train`：只留 `image_id / image_path / caption / tag` 四个字段，
  与 `merge.build_rows` 的总表口径一致，训练侧直接吃。
"""

from __future__ import annotations

import json
import os
import time
import shutil
import tempfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

# 逐图产物的两种后缀（与 client.TAG_SUFFIX / NL_SUFFIX 保持同一口径）。
TAG_SUFFIX = "-tag.json"
NL_SUFFIX = "-nl.json"

# 训练侧要的四个字段。顺序只影响可读性，不影响语义。
TRAIN_FIELDS = ("image_id", "image_path", "caption", "tag")

# 单次内存合并允许读入的条数上限。超过这个量，dict 索引本身就能吃掉几 GB，
# 与其 OOM 在半路，不如提前说清楚"这活该分桶做"。
MAX_RECORDS = 2_000_000

# 估算"这批值不值得分桶"用的平均值。故意取偏大：估高了只是多分几个桶
# （多几个空文件），估低了才会在桶里堆太多记录。
AVG_RECORD_BYTES = 700
# 估算条数超过这个值就自动走分桶，不必等用户自己想起来调。
AUTO_BATCH_RECORDS = 300_000
# 桶数上限：再多就是大量空文件和小 IO，收益已经没有了。
MAX_BUCKETS = 64


class TagManageError(RuntimeError):
    """输入不认识 / 写不出去 / 数据量过大。信息里要带下一步怎么做。"""


@dataclass
class MergeReport:
    """合并结果。页面直接照着渲染，不做二次加工。"""

    inputs: int = 0                 # 实际读进来的文件数
    bad_files: list[str] = field(default_factory=list)
    records_read: int = 0           # 读到的原始记录数（含重复）
    records_out: int = 0            # 去重合并后的条数
    duplicates: int = 0             # 同一 key 重复出现的次数
    conflicts: int = 0              # 同名字段值不一致的次数
    skipped: int = 0                # 没有 key / 被 require 过滤掉的
    fields: list[str] = field(default_factory=list)
    out_path: str = ""
    # 本次用的图片路径前缀（空 = 保留产物原值）。写进报告是为了让"这批数据
    # 指向哪个目录"有据可查——过两周谁也记不住当时填了什么。
    image_root: str = ""
    # 走了分桶模式吗。分桶后输出按桶序排列，不再全局按 image_id 排序，
    # 这一点要在报告里说清，不能让它成为隐藏差异。
    batched: bool = False
    buckets: int = 0
    bytes: int = 0
    elapsed_s: float = 0.0

    def as_dict(self) -> dict:
        return {
            "inputs": self.inputs,
            "bad_files": list(self.bad_files),
            "records_read": self.records_read,
            "records_out": self.records_out,
            "duplicates": self.duplicates,
            "conflicts": self.conflicts,
            "skipped": self.skipped,
            "fields": list(self.fields),
            "out_path": self.out_path,
            "image_root": self.image_root,
            "batched": self.batched,
            "buckets": self.buckets,
            "bytes": self.bytes,
            "elapsed_s": round(self.elapsed_s, 2),
        }


def kind_of(path: Path) -> str:
    """按文件名判断这是哪种产物：tag / nl / parquet / other。

    只认后缀不读内容：扫描一个几万文件的目录时不值得为分类把每个文件读一遍。
    parquet 只认 `artifacts-*.parquet`——那是本项目的产物分片；别的 parquet
    没有 `image_id` 列，读进来只会报错。
    """
    name = path.name
    if name.endswith(TAG_SUFFIX):
        return "tag"
    if name.endswith(NL_SUFFIX):
        return "nl"
    if name.startswith("artifacts-") and name.endswith(".parquet"):
        return "parquet"
    return "other"


def scan(root: str | Path, *, recursive: bool = True) -> list[dict]:
    """列出目录下的 json，带大小与类型。

    排序按路径：用户勾选时的顺序应当稳定，不能随文件系统返回顺序抖动。
    """
    base = Path(root)
    if not base.exists():
        raise TagManageError(f"目录不存在：{base}")
    if base.is_file():
        pats = [base]
    else:
        walk = base.rglob if recursive else base.glob
        # parquet 产物必须一起收：`--artifact-format parquet` 的用户根本没有
        # 任何 json 文件，只扫 *.json 会让这个页面看起来"什么都找不到"。
        # 但只收 `artifacts-*.parquet`：数据集本身的 parquet 分片没有 image_id
        # 列，收进来只会把列表塞满一堆读不了的文件。
        pats = sorted([p for p in walk("*") if p.is_file()
                       and (p.suffix.lower() == ".json"
                            or kind_of(p) == "parquet")])
    out: list[dict] = []
    for p in pats:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        out.append({
            "path": str(p),
            "name": p.name,
            "dir": str(p.parent),
            "size": size,
            # 空文件是"下载没写完/被截断"的典型形态，标出来比让它静默进合并好
            "empty": size == 0,
            "kind": kind_of(p),
        })
    return out


def _load_parquet(path: Path) -> list[dict]:
    """读产物分片，展开成逐条 payload。

    parquet 的 tag / nl 两列存的就是**和 json 文件完全相同的 JSON 字符串**
    （见 `artifacts.py`），所以这里原样解析成两条记录、再交给同一套按 key
    合并的逻辑——换成拆开的列等于把配对规则复制一份，两份一定会分叉。
    """
    from . import artifacts

    rows: list[dict] = []
    for record in artifacts.iter_parquet_artifacts(path):
        for name in ("tag", "nl"):
            blob = record.get(name)
            if not blob:
                continue
            try:
                obj = json.loads(blob)
            except ValueError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def load_records(path: str | Path) -> list[dict]:
    """读一个文件，返回记录列表。JSON 三种形态 + 产物 parquet 都认。

    解析失败抛 ValueError 而不是静默跳过：一个坏文件被跳过，用户看到的是
    "合并成功、条数少了"，这比报错难查得多。
    """
    p = Path(path)
    if p.suffix.lower() == ".parquet":
        try:
            return _load_parquet(p)
        except RuntimeError as exc:      # pyarrow 缺失，artifacts 会给出安装提示
            raise ValueError(str(exc)) from exc
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        obj = json.loads(text)
    except ValueError:
        # 不是合法 JSON，再按 JSONL 逐行试；仍然失败的行走坏文件处理
        rows: list[dict] = []
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{p.name} 第 {i} 行不是 JSON：{exc}") from exc
            if isinstance(item, dict):
                rows.append(item)
        return rows
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        # 有些导出会把数组包在一层壳里（{"records": [...]}），认这个常见形态
        for name in ("records", "data", "items", "annotations"):
            inner = obj.get(name)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        return [obj]
    return []


def _same(a, b) -> bool:
    """字段值是否等价。数字与字符串不混为一谈，None 与空串也不。"""
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-9
        except (TypeError, ValueError):
            return False
    return a == b


def merge_records(
    records: Iterable[dict],
    *,
    key: str = "image_id",
    on_conflict: str = "first",
    require: str = "any",
) -> tuple[dict[str, dict], dict]:
    """按 key 合并成一张表。返回 (表, 统计)。

    合并策略是**字段级并集**，因为 tag 与 nl 两份产物恰恰是"字段不重叠"的
    （一个只有 tag/tags/rating，一个只有 caption/score/flags）。真正重叠的
    只有 artist / provenance / ts 这类元数据，冲突时按 `on_conflict` 取，
    并计数——训练侧要的是内容字段，元数据取哪份都不影响可用性，但不该悄悄丢。

    `require`：any / both / tag / nl，与 merge 的过滤口径一致。
    """
    table: dict[str, dict] = {}
    stats = {"records_read": 0, "duplicates": 0, "conflicts": 0, "skipped": 0}
    for rec in records:
        stats["records_read"] += 1
        rid = str(rec.get(key) or "").strip()
        if not rid:
            stats["skipped"] += 1
            continue
        cur = table.get(rid)
        if cur is None:
            table[rid] = dict(rec)
            continue
        stats["duplicates"] += 1
        for name, value in rec.items():
            if name == key:
                continue
            if name not in cur:
                cur[name] = value
                continue
            if _same(cur[name], value):
                continue
            stats["conflicts"] += 1
            if on_conflict == "last":
                cur[name] = value
            # first：保留已有值，什么也不做

    if require != "any":
        drop = [rid for rid, row in table.items() if not _keep(row, require)]
        for rid in drop:
            table.pop(rid, None)
            stats["skipped"] += 1
    return table, stats


def _keep(row: dict, require: str) -> bool:
    has_tag = bool(str(row.get("tag") or "").strip())
    has_nl = bool(str(row.get("caption") or "").strip())
    if require == "both":
        return has_tag and has_nl
    if require == "tag":
        return has_tag
    if require == "nl":
        return has_nl
    return True


def reorder(row: dict, *, key: str = "image_id") -> dict:
    """把关键字段挪到前面，其余保持原顺序。

    合并是"先读到的文件先占位"，所以字段顺序取决于扫描顺序（`-nl.json`
    排在 `-tag.json` 前），读出来的第一条会是 caption 开头。内容没错，但
    人工核对时看不到 image_id 在哪儿，`jq` 之类的输出也不顺眼。
    """
    core = (key, ) + tuple(f for f in TRAIN_FIELDS if f != "image_id")
    out: dict = {}
    for name in core:
        if name in row:
            out[name] = row[name]
    for name, value in row.items():
        if name not in out:
            out[name] = value
    return out


def project(row: dict, fmt: str, *, key: str = "image_id") -> dict:
    """按输出口径裁字段并调顺序。

    `train` 只留四个字段：多出来的 provenance / tags 会显著改变文件体积，
    而训练侧一个也不读。
    """
    if fmt != "train":
        return reorder(row, key=key)
    out = {"image_id": row.get(key) or row.get("image_id") or ""}
    out["image_path"] = row.get("image_path") or ""
    out["caption"] = row.get("caption") or ""
    out["tag"] = row.get("tag") or ""
    return out


def rel_dir_of(rel_path: str) -> str:
    """从产物的 `rel_path` 里取出**目录**部分，用来还原 mirror 布局的层级。

    返回空串表示"没有可用目录"，调用方退回扁平路径。以下三种都不算可用：
    - 空
    - 带协议（`hf://datasets/...`）或 `::`（manifest 里 `zip名::成员` 的老形态）
    - 只有文件名、没有目录

    文件名一律丢掉：mirror 下产物与图都被重命名成 `<image_id>`，只有目录是
    原始层级。
    """
    text = str(rel_path or "").replace("\\", "/").strip().strip("/")
    if not text or "://" in text or "::" in text:
        return ""
    parts = [p for p in text.split("/") if p and p not in (".", "..")]
    if len(parts) < 2:
        return ""
    return "/".join(parts[:-1])


def rewrite_image_path(row: dict, *, key: str = "image_id",
                       image_root: str = "", image_ext: str = ".jpg") -> dict:
    """把 `image_path` 改写成 `<root>[/<目录>]/<image_id><ext>`。

    为什么要这个口子：产物里的 `image_path` 只有 HF 模式才有（`hf://datasets/...`），
    本地分发的产物根本没有这个字段，而训练侧必须知道图在哪。写死一个前缀等于
    把"数据集放哪儿"焊进代码；让人填，才能 `/images/fanbox`、`/mnt/d/pony`
    随便换。

    `image_root` 为空时**原样返回**，不改任何字段——接手一批已经能用的数据时，
    默认行为必须是"什么都不动"。

    目录层级的两种来源要分开：`image_id` 自己带 `/`（散图按相对路径命名，
    见 `local_tag`）时它就是完整相对路径，再叠一次目录会变成 `sub/sub/001.jpg`；
    否则才从 `rel_path` 取目录（mirror 布局的分发包产物）。
    """
    root = (image_root or "").strip()
    if not root:
        return row
    rid = str(row.get(key) or row.get("image_id") or "").strip()
    if not rid:
        return row
    ext = (image_ext or "").strip()
    if ext and not ext.startswith("."):
        ext = "." + ext
    rid = rid.replace("\\", "/")
    has_dir = "/" in rid
    sub = "" if has_dir else rel_dir_of(str(row.get("rel_path") or ""))
    tail = f"{sub}/{rid}{ext}" if sub else f"{rid}{ext}"
    out = dict(row)
    out["image_path"] = f"{root.rstrip('/')}/{tail}"
    return out


def write_json(
    rows: Iterable[dict],
    out_path: str | Path,
    *,
    fmt: str = "json",
    indent: int = 2,
    on_progress: Callable[[str, int, int], None] | None = None,
    total: int = 0,
    report_every: int = 2000,
) -> int:
    """流式写单体 json 或 jsonl，返回条数。

    单体数组也是流式写的：先写 `[`，每行前加逗号，最后写 `]`。这样内存里
    只需要一条记录——把整表 list 物化再 json.dumps，300 万条会直接 OOM。

    `on_progress` 每 `report_every` 条回调一次：几十万条要写十几秒，页面
    若只等到最后才知道进度，用户会以为卡死了。
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    written = 0
    try:
        with tmp.open("w", encoding="utf-8") as f:
            if fmt == "jsonl":
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    written += 1
                    if on_progress and written % report_every == 0:
                        on_progress("write", written, total)
            else:
                f.write("[\n")
                first = True
                for row in rows:
                    if not first:
                        f.write(",\n")
                    body = json.dumps(row, ensure_ascii=False, indent=indent)
                    # 缩进进一级：让整体还是"数组里若干对象"的常见观感
                    f.write("  " + body.replace("\n", "\n  "))
                    first = False
                    written += 1
                    if on_progress and written % report_every == 0:
                        on_progress("write", written, total)
                f.write("\n]\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(out)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if on_progress:
        on_progress("write", written, total or written)
    return written


def _finish(report: MergeReport, out_path: str | Path, started: float) -> MergeReport:
    """补齐尺寸/耗时，并把"全读不了"翻成一个能照着做的错误。"""
    report.out_path = str(Path(out_path))
    try:
        report.bytes = Path(out_path).stat().st_size
    except OSError:
        report.bytes = 0
    report.elapsed_s = time.time() - started
    if not report.inputs and report.bad_files:
        raise TagManageError(
            "没有一个文件能读：" + "; ".join(report.bad_files[:3]))
    return report


def _merge_batched(
    inputs: list[Path],
    out_path: str | Path,
    *,
    report: MergeReport,
    started: float,
    fmt: str,
    as_jsonl: bool,
    key: str,
    on_conflict: str,
    require: str,
    indent: int,
    image_root: str,
    image_ext: str,
    batch_size: int,
    on_progress: Callable[[str, int, int], None] | None,
) -> MergeReport:
    """按 key 分桶的两遍法：内存上限从"全部记录"降到"一个桶"。

    为什么不能"按文件分批、每批各自合并、再拼起来"：同一个 image_id 的
    tag 与 nl 极可能落在**不同文件**，分完再拼就再也配不上对了，结果是
    双倍条数、一半没 caption。

    所以第一遍按 key 的 crc32 分桶（同 key 必然同桶，与顺序无关），
    第二遍逐桶合并。桶数按"估算总量 / batch_size"定，估算用平均每条 700
    字节——估低了只是桶内多点记录，估高了只是多几个空桶，两个方向都不出错。

    **代价**：输出按桶顺序排列，不再按 image_id 全局排序。对"给训练侧吃的
    大文件"没有影响；报告里会标出来，不让它成为隐藏差异。
    """
    total_bytes = 0
    for p in inputs:
        try:
            total_bytes += p.stat().st_size
        except OSError:
            pass
    est_rows = max(1, total_bytes // AVG_RECORD_BYTES)
    buckets = max(2, min(MAX_BUCKETS, -(-est_rows // max(1, batch_size))))
    work = Path(tempfile.mkdtemp(prefix="tagmerge-"))
    names: set[str] = set()
    try:
        handles: list = []
        try:
            handles = [(work / f"b{i:04d}.jsonl").open("w", encoding="utf-8")
                       for i in range(buckets)]
            for i, p in enumerate(inputs, 1):
                try:
                    rows = load_records(p)
                except (OSError, ValueError) as exc:
                    report.bad_files.append(f"{p.name}（{exc}）")
                    continue
                report.inputs += 1
                for rec in rows:
                    rid = str(rec.get(key) or "").strip()
                    if not rid:
                        report.skipped += 1
                        report.records_read += 1
                        continue
                    report.records_read += 1
                    # crc32 而不是内置 hash()：后者带进程级随机化，同一批输入
                    # 跑两次会分到不同桶，输出顺序就不可复现了。
                    idx = zlib.crc32(rid.encode("utf-8")) % buckets
                    handles[idx].write(json.dumps(rec, ensure_ascii=False) + "\n")
                if on_progress is not None:
                    on_progress("read", i, len(inputs))
        finally:
            for h in handles:
                try:
                    h.close()
                except OSError:
                    pass

        def rows_out() -> Iterator[dict]:
            for i in range(buckets):
                path = work / f"b{i:04d}.jsonl"
                if not path.exists():
                    continue
                bucket: list[dict] = []
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            bucket.append(json.loads(line))
                        except ValueError:
                            continue
                table, stats = merge_records(bucket, key=key,
                                             on_conflict=on_conflict,
                                             require=require)
                report.duplicates += stats["duplicates"]
                report.conflicts += stats["conflicts"]
                report.skipped += stats["skipped"]
                for rid in sorted(table):
                    row = rewrite_image_path(project(table[rid], fmt, key=key),
                                             key=key, image_root=image_root,
                                             image_ext=image_ext)
                    names.update(row)
                    yield row
                try:
                    path.unlink()
                except OSError:
                    pass

        report.records_out = write_json(
            rows_out(), out_path, fmt="jsonl" if as_jsonl else "json",
            indent=indent, on_progress=on_progress)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    report.fields = sorted(names)
    report.image_root = (image_root or "").strip()
    report.batched = True
    report.buckets = buckets
    return _finish(report, out_path, started)


def merge_files(
    paths: Iterable[str | Path],
    out_path: str | Path,
    *,
    fmt: str = "full",
    as_jsonl: bool = False,
    key: str = "image_id",
    on_conflict: str = "first",
    require: str = "any",
    indent: int = 2,
    image_root: str = "",
    image_ext: str = ".jpg",
    batch_size: int = 0,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> MergeReport:
    """把多个 json 合并进一个文件。这是页面按钮背后唯一的入口。

    顺序有意义：`on_conflict="first"` 时先读到的胜出，所以扫描结果是按路径
    排过序的——同样的输入重复跑，输出必须一致。

    `batch_size`：0 = 自动（估算量大时自动分桶）；>0 = 强制分桶，每桶约这么多
    条。分桶见 `_merge_batched`。小批量走内存合并，快且输出全局有序。
    """
    started = time.time()
    report = MergeReport()
    inputs: list[Path] = []
    total_bytes = 0
    for raw in paths:
        p = Path(raw)
        if not p.is_file():
            report.bad_files.append(f"{p}（不存在）")
            continue
        inputs.append(p)
        try:
            total_bytes += p.stat().st_size
        except OSError:
            pass

    est_rows = total_bytes // AVG_RECORD_BYTES
    want_batch = batch_size > 0 or est_rows > AUTO_BATCH_RECORDS
    if want_batch and len(inputs) > 1:
        return _merge_batched(
            inputs, out_path, report=report, started=started, fmt=fmt,
            as_jsonl=as_jsonl, key=key, on_conflict=on_conflict,
            require=require, indent=indent, image_root=image_root,
            image_ext=image_ext,
            batch_size=batch_size if batch_size > 0 else AUTO_BATCH_RECORDS // 4,
            on_progress=on_progress)

    all_rows: list[dict] = []
    for p in inputs:
        try:
            rows = load_records(p)
        except (OSError, ValueError) as exc:
            report.bad_files.append(f"{p.name}（{exc}）")
            continue
        report.inputs += 1
        all_rows.extend(rows)

    if len(all_rows) > MAX_RECORDS:
        raise TagManageError(
            f"本次要合并 {len(all_rows)} 条，超过 {MAX_RECORDS} 条的内存上限。"
            f"把「分批大小」填成 200000 之类，走分桶模式（内存上限降到一个桶）。")

    table, stats = merge_records(all_rows, key=key, on_conflict=on_conflict,
                                 require=require)
    rows_iter = (rewrite_image_path(project(table[rid], fmt, key=key), key=key,
                                    image_root=image_root, image_ext=image_ext)
                 for rid in sorted(table))
    report.records_out = write_json(rows_iter, out_path,
                                    fmt="jsonl" if as_jsonl else "json",
                                    indent=indent, on_progress=on_progress,
                                    total=len(table))
    report.records_read = stats["records_read"]
    report.duplicates = stats["duplicates"]
    report.conflicts = stats["conflicts"]
    report.skipped = stats["skipped"]
    names: set[str] = set()
    for row in table.values():
        names.update(row)
    report.fields = sorted(names)
    report.image_root = (image_root or "").strip()
    return _finish(report, out_path, started)


REMOTE_PREFIXES = ("hf://", "http://", "https://", "s3://")


def check_paths(
    paths: Iterable[str | Path],
    *,
    key: str = "image_id",
    image_root: str = "",
    image_ext: str = ".jpg",
    local_root: str = "",
    sample: int = 10,
    workers: int = 8,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> dict:
    """合并后逐条检查 `image_path` 指向的图在不在本地。

    这个检查值钱的地方在于**路径填错是静默失败**：`/images/fanbox` 写成
    `/image/fanbox` 后合并照样成功，直到训练侧读第一批数据才发现，那时已经
    多花了几小时排队。

    `local_root` 是给"容器路径 / 网络路径"留的口子：`image_path` 写的是
    `/images/fanbox/x.jpg`（训练机上的路径），而本机实际在 `D:\\ds\\fanbox`，
    填上后者就能对着本地盘核对，不必为了自检去改产物。

    远端路径（hf:// / http）不查本地：那不是缺图，是"本来就不在这台机器上"。

    `on_progress(stage, done, total)`：`read` 是读入第几个文件，`check` 是核对
    到第几条。几十万条在网络盘上要跑几分钟，不给进度的话前台只能干等。
    """
    tick = on_progress or (lambda *_a: None)
    files = [str(p) for p in paths]
    rows: list[dict] = []
    for n, raw in enumerate(files, 1):
        try:
            rows.extend(load_records(raw))
        except (OSError, ValueError):
            pass
        tick("read", n, len(files))
    table, _ = merge_records(rows, key=key)
    # 必须先按最终口径改写再查：查的应该是"合并产物里写出去的那个路径"，
    # 填了 image_root 时它与产物原值正好不同——拿原值查等于白查。
    for rid in list(table):
        table[rid] = rewrite_image_path(
            project(table[rid], "full", key=key), key=key,
            image_root=image_root, image_ext=image_ext)
    ordered = sorted(table)

    root = (image_root or "").strip().rstrip("/")
    local = (local_root or "").strip().rstrip("/")

    def target_of(row: dict) -> tuple[str, str]:
        """返回 (判定类别, 用于展示的路径)。类别：remote / empty / local。"""
        shown = str(row.get("image_path") or "").strip()
        if not shown:
            return "empty", ""
        if shown.lower().startswith(REMOTE_PREFIXES):
            return "remote", shown
        if local:
            rest = shown[len(root):].lstrip("/") if root and shown.startswith(root) else shown.lstrip("/")
            return "local", f"{local}/{rest}"
        return "local", shown

    def probe(row: dict) -> tuple[str, str, str]:
        kind, path = target_of(row)
        if kind != "local":
            return kind, path, ""
        try:
            exists = Path(path).is_file()
        except OSError:
            exists = False
        return ("ok" if exists else "missing"), path, str(row.get(key) or "")

    out = {"total": len(ordered), "ok": 0, "missing": 0,
           "remote": 0, "empty": 0, "samples": []}
    total = len(ordered)
    tick("check", 0, total)
    # IO 密集：线程池比串行快得多（网络盘尤其明显），GIL 在这里不是瓶颈
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, min(32, workers))) as pool:
        for status, path, rid in pool.map(
                probe, (table[r] for r in ordered), chunksize=256):
            done += 1
            out[status] = out.get(status, 0) + 1
            if status == "missing" and len(out["samples"]) < max(0, sample):
                out["samples"].append({"image_id": rid, "image_path": path})
            # 每 64 条刷一次：每张都回调一次等于把锁竞争当主要开销
            if done % 64 == 0 or done == total:
                tick("check", done, total)
    return out


def preview(paths: Iterable[str | Path], *, limit: int = 5,
            key: str = "image_id", image_root: str = "",
            image_ext: str = ".jpg") -> list[dict]:
    """合并预览：取前几条已经过合并的记录。

    只读前若干个文件就够——预览的目的是让用户确认"字段长这样"，不是统计。
    必须走和 `merge_files` 同一套改写，否则预览里的 image_path 和最终产物
    不是一回事，等于预览了个别的。
    """
    rows: list[dict] = []
    cap = max(1, limit) * 20
    for raw in paths:
        try:
            picked = load_records(raw)[:cap]
        except (OSError, ValueError):
            continue
        rows.extend(picked)
        # 每个文件都取一点，而不是"读满就停"：tag 与 nl 是两个文件，
        # 只读前几个文件的话预览里全是半条记录，看不到合并后的真实形态。
        if len(rows) >= cap * 4:
            break
    table, _ = merge_records(rows, key=key)
    return [rewrite_image_path(table[rid], key=key, image_root=image_root,
                               image_ext=image_ext)
            for rid in sorted(table)[:max(1, limit)]]
