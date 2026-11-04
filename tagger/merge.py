"""成员成果回收与总表合并。

流程位置
--------
    总机分发 → 各成员本地打标 → 各自交回 → **本模块** → 总表 + 抽检清单

合并这件事看起来只是拼字段，实际上 90% 的复杂度在**对账**：
谁交了、谁没交、谁的哪个画师整片空着、哪条记录重复覆盖了另一条。
这些都不会抛异常，只会让最终数据集悄悄少一块。

三种失败模式及其识别方式：

1. 整条没交      image_id 在分发表里有、在回收目录里找不到
2. 交了但内容空  -nl.json 存在但 caption 是 ""、或 json 是 {} 
3. 重复覆盖      同一个 image_id 出现在两个人的目录里（多半是重复领取）

第 3 种最危险：合并时后写的覆盖先写的，两条都"存在"，但只剩一条的数据。
所以遇到重复一律报错而不是取其中一个。

为什么不用「占位符填充」的方案
------------------------------
有个直觉是：总表里先把 caption/tag 留成占位符，成员填完发回。
但占位符有个致命问题——**填失败和没填长得一样**。模型返回空、脚本中途崩、
文件写了一半，占位符还在原位，看起来像"已处理"，实则空着。

用「独立文件存在性」就没有这个歧义：没有 -nl.json 就是没有产出，
一眼可见，也天然可统计。所以本模块只认文件，不认占位符。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

TAG_SUFFIX = "-tag.json"
NL_SUFFIX = "-nl.json"


@dataclass(slots=True)
class Record:
    """一个成员产出的中间态。两个文件都齐全才算完整。"""

    image_id: str
    caption: str = ""
    tag: str = ""
    src_files: list[str] = field(default_factory=list)
    # 产物里自带的路径（HF 模式才有）。为空时由 build_rows 按配置拼。
    image_path: str = ""
    # kind -> provenance（tag / nl 各一份）。两份的字段不完全一样：
    # tag_backend 只在 tag 文件里，nl 文件没有。分开存才不会串。
    prov: dict[str, dict] = field(default_factory=dict)


@dataclass(slots=True)
class ShardCoverage:
    """分发表里的一个包，落在回收结果上的覆盖情况。

    为什么要有这一层：image_id 级别的缺失能说明"少了几条"，但说明不了
    "少的是谁那一整包"。10 个人各领一包时，最坏的情况不是每人少几张，
    而是**某个人一张都没交**——此时 9/10 的完整率看着还行，实际缺了
    300 万条，且分布上完全偏向那个人负责的画师。
    """

    name: str
    first_id: str = ""
    last_id: str = ""
    expected: int = 0
    got: int = 0
    complete: int = 0

    @property
    def whole_missing(self) -> bool:
        """整包一条都没回来。expected=0 的空包不算。"""
        return self.expected > 0 and self.got == 0

    @property
    def short(self) -> int:
        return max(0, self.expected - self.got)


@dataclass(slots=True)
class MergeStats:
    """合并报告。字段刻意留在最外层，便于直接打进日志。"""

    expected: int = 0
    found_ids: int = 0
    complete: int = 0
    missing_ids: list[str] = field(default_factory=list)
    missing_nl: list[str] = field(default_factory=list)
    missing_tag: list[str] = field(default_factory=list)
    empty_content: list[str] = field(default_factory=list)
    # 内容不同的重复 = 真冲突，必须人工裁决
    duplicates: dict[str, list[str]] = field(default_factory=dict)
    # 内容相同的重复 = 幂等重跑。重跑没清目录、补跑目录叠加都会产生，
    # 不是错误，取一份即可，但值得计数——数量大说明流程在反复做无用功。
    idempotent_repeats: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    # 产物里有、分发表里没有的 id。这**不算错**（可能是补跑后新加的素材），
    # 但说明分配记录与实际对不上，值得单列——之前混进 unreadable，让
    # "读不动的坏文件"和"多出来的好数据"搅在同一个计数里。
    extra_ids: list[str] = field(default_factory=list)
    by_artist: dict[str, dict[str, int]] = field(default_factory=dict)
    # 各溯源维度的取值分布（prompt_ver / model_id / tag_backend）。
    # 出现两种以上取值，说明这批数据是不同配置混出来的。
    provenance: dict[str, dict[str, int]] = field(default_factory=dict)

    # ---- 整份 / 整包级别的缺失。这一层是"看着 98% 其实缺了三百万条"的解药。
    # 空回收目录 = 某个成员一份都没交（目录不存在也记在这里）。
    empty_roots: list[str] = field(default_factory=list)
    # 按分发表逐包统计的覆盖。没有分发表时为空。
    shard_coverage: list[ShardCoverage] = field(default_factory=list)
    # 落在所有分包区间之外的 id：说明分发表与实际产物对不上。
    unassigned_ids: list[str] = field(default_factory=list)

    @property
    def whole_missing_shards(self) -> list[ShardCoverage]:
        return [c for c in self.shard_coverage if c.whole_missing]

    @property
    def short_shards(self) -> list[ShardCoverage]:
        """有缺失但没整包丢的包。整包丢的已经单列，不重复计入。"""
        return [c for c in self.shard_coverage if c.short and not c.whole_missing]

    @property
    def failed(self) -> bool:
        # 幂等重复不算失败：那是同一份内容的第二份拷贝，
        # 拦下来只会让"重跑一下"这种正常操作变得无法收尾。
        return bool(self.missing_ids or self.duplicates or self.unreadable
                    or self.empty_roots or self.whole_missing_shards
                    or self.unassigned_ids)

    def summary_lines(self) -> list[str]:
        lines = [
            f"应产出     : {self.expected}",
            f"实际找到   : {self.found_ids}",
            f"完整       : {self.complete}",
        ]
        if self.missing_ids:
            lines.append(f"整条缺失   : {len(self.missing_ids)}")
        if self.missing_nl:
            lines.append(f"缺 NL      : {len(self.missing_nl)}")
        if self.missing_tag:
            lines.append(f"缺 tag     : {len(self.missing_tag)}")
        if self.empty_content:
            lines.append(f"内容为空   : {len(self.empty_content)}")
        if self.duplicates:
            lines.append(f"内容冲突   : {len(self.duplicates)} 个 id（同一 id 两份不同内容）")
        if self.idempotent_repeats:
            lines.append(f"重复提交   : {len(self.idempotent_repeats)} 次（内容一致，已取一份）")
        if self.unreadable:
            lines.append(f"读不动     : {len(self.unreadable)}")
        if self.extra_ids:
            lines.append(f"不在分发表 : {len(self.extra_ids)}（多出的产出，不算错）")
        if self.empty_roots:
            n = len(self.empty_roots)
            lines.append(f"整份没交   : {n} 个回收目录（一份产出都没有）")
        if self.shard_coverage:
            whole = self.whole_missing_shards
            short = self.short_shards
            if whole:
                lines.append(f"整包没交   : {len(whole)} 个分包（分发表里有、回收 0 条）")
            if short:
                lack = sum(c.short for c in short)
                lines.append(f"分包不足   : {len(short)} 个包共缺 {lack} 条")
            if self.unassigned_ids:
                lines.append(f"不在分发表 : {len(self.unassigned_ids)} 个 id（分配记录与产物不符）")
        return lines


# ------------------------------------------------------------------ 收集


def _iter_json_files(root: Path) -> Iterator[Path]:
    """递归找所有中间态文件。保留目录结构时它们会在多层子目录里。"""
    for p in sorted(root.rglob("*.json")):
        if p.name.endswith(TAG_SUFFIX) or p.name.endswith(NL_SUFFIX):
            yield p


def _id_from_filename(name: str) -> str:
    for suffix in (TAG_SUFFIX, NL_SUFFIX):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return ""


def _read_one(path: Path) -> tuple[str, str, str, str, dict]:
    """读一个中间态文件，返回 (image_id, kind, 内容, image_path, provenance)。

    内容为空不是这里判断的事——那是 empty_content 的活。这里只负责
    把「文件坏了」和「文件是空的」分开，因为两者处理方式不同：
    前者要重跑，后者要检查上游模型。
    """
    kind = "tag" if path.name.endswith(TAG_SUFFIX) else "nl"
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"读取失败: {exc}") from exc

    fallback_id = _id_from_filename(path.name)
    if not raw:
        return fallback_id, kind, "", "", {}

    try:
        obj = json.loads(raw)
    except ValueError:
        # 不是 json 时按纯文本处理。成员手工改过、或工具直接写了裸文本。
        return fallback_id, kind, raw, "", {}

    if isinstance(obj, str):
        return fallback_id, kind, obj.strip(), "", {}
    if not isinstance(obj, dict):
        return fallback_id, kind, "", "", {}

    return _tuple_from_payload(obj, fallback_id, kind)


def _tuple_from_payload(obj: object, fallback_id: str,
                        kind: str) -> tuple[str, str, str, str, dict]:
    """把一份 payload 折成 (image_id, kind, 内容, image_path, provenance)。

    json 文件与 parquet 行共用这一段：两种形态存的是**同一份 payload**，
    解析口径必须只有一处，否则"换成 parquet 之后对账结论变了"这种问题
    会查很久。
    """
    if not isinstance(obj, dict):
        return fallback_id, kind, "", "", {}
    image_id = str(obj.get("image_id") or fallback_id)
    value = obj.get("tag") if kind == "tag" else (obj.get("caption")
                                                  or obj.get("nl") or "")
    prov = obj.get("provenance")
    return (image_id, kind, str(value or "").strip(),
            str(obj.get("image_path") or ""),
            prov if isinstance(prov, dict) else {})


def _iter_parquet_records(root: Path) -> Iterator[tuple[str, str, str, str, dict]]:
    """读 parquet 形态的产物（artifacts-*.parquet）。

    tag / nl 两列存的是与 json 文件完全相同的 payload 字符串，见 artifacts.py。
    """
    from .artifacts import (is_artifact_parquet, iter_parquet_artifacts,
                            parse_payload)

    for path in sorted(root.rglob("*.parquet")):
        if not is_artifact_parquet(path):
            continue
        try:
            # 行号进 source：同一个分片里的两行如果同一个 id，也要能定位到行，
            # 只说"这个文件里重复了"等于没说。
            for idx, row in enumerate(iter_parquet_artifacts(path)):
                fallback = str(row.get("image_id") or "")
                for kind in ("tag", "nl"):
                    obj = parse_payload(kind, str(row.get(kind) or ""))
                    if obj is None:
                        continue
                    image_id, k, value, image_path, prov = _tuple_from_payload(
                        obj, fallback, kind)
                    yield image_id, k, value, image_path, prov, f"{path}#{idx}"
        except Exception as exc:      # noqa: BLE001 - 坏片要报，不该中断整批
            raise ValueError(f"{path}: {exc}") from exc


def _iter_member_records(
    root: Path, stats: MergeStats,
) -> Iterator[tuple[str, str, str, str, dict]]:
    """把 json 形态与 parquet 形态统一成一个流。

    同一个目录里两种形态并存时（改了 --artifact-format 重跑），两边的记录
    都会进 records；内容不同就是重复冲突，这正是要报出来的东西。
    """
    for path in _iter_json_files(root):
        try:
            image_id, kind, value, image_path, prov = _read_one(path)
        except ValueError as exc:
            stats.unreadable.append(f"{path}: {exc}")
            continue
        if not image_id:
            stats.unreadable.append(f"{path}: 文件名里取不到 image_id")
            continue
        yield image_id, kind, value, image_path, prov, str(path)

    try:
        yield from _iter_parquet_records(root)
    except ValueError as exc:
        stats.unreadable.append(str(exc))


def collect(
    roots: Iterable[str | Path],
    *,
    stats: MergeStats | None = None,
    on_progress: "Callable[[int], None] | None" = None,
    should_stop: "Callable[[], bool] | None" = None,
) -> dict[str, Record]:
    """扫描若干回收目录，按 image_id 归并。

    同一个 id 出现在多个文件里视为重复——不静默覆盖，全部记进 stats 让人处理。
    on_progress 每读一个产物文件回调一次（已扫条数）；should_stop 返回 True
    时提前收手，返回已扫到的部分——调用方据此放弃后续写盘（半份对账比
    假的完整对账诚实）。
    """
    stats = stats or MergeStats()
    records: dict[str, Record] = {}
    # 记录每个 (id, kind) 第一次出现在哪个文件。
    # 用完整路径做键而不是文件名——不同人的目录下文件名必然相同
    # (`xxx-tag.json`)，按文件名去重会把真正的重复当成"同一份"跳过，
    # 而重复恰恰是本模块最该抓的东西。
    filled: dict[str, tuple[str, str]] = {}   # "id:kind" -> (首次路径, 内容)
    duplicates: dict[str, list[str]] = {}
    idempotent: list[str] = []
    # 每个回收目录自己出了多少条。整份没交（成员一份没交 / 目录根本没传上来）
    # 只有在这个粒度上才看得见：全局缺失列表会把"某人整包没有"和
    # "十个包各少几张"混成同一个数字。
    per_root: dict[str, int] = {}
    scanned = 0

    for root in roots:
        base = Path(root)
        if not base.exists():
            stats.unreadable.append(f"{base}: 目录不存在")
            per_root[str(base)] = 0
            continue
        per_root.setdefault(str(base), 0)
        for image_id, kind, value, image_path, prov, source in _iter_member_records(base, stats):
            if should_stop is not None and should_stop():
                stats.found_ids = len(records)
                return records
            scanned += 1
            if on_progress is not None:
                on_progress(scanned)
            per_root[str(base)] += 1

            rec = records.get(image_id)
            if rec is None:
                rec = Record(image_id=image_id)
                records[image_id] = rec

            key = f"{image_id}:{kind}"
            prev = filled.get(key)
            if prev is not None:
                prev_path, prev_value = prev
                if prev_value == value:
                    # 内容一模一样：重跑没清目录、补跑目录叠加都会这样。
                    # 取一份即可，不算错——拦下来只会让"重跑一下"没法收尾。
                    idempotent.append(f"{image_id}:{kind}")
                else:
                    # 内容不同：可能是不同 prompt / 不同模型 / 一份被截断。
                    # 这种情况绝不静默取其一，必须人工看看到底该留哪份。
                    dup = duplicates.setdefault(image_id, [prev_path])
                    dup.append(source)
                continue

            filled[key] = (source, value)
            rec.src_files.append(source)
            if image_path and not rec.image_path:
                rec.image_path = image_path
            if prov and kind not in rec.prov:
                rec.prov[kind] = prov
            if kind == "tag":
                rec.tag = value
            else:
                rec.caption = value

    stats.duplicates = duplicates
    stats.idempotent_repeats = idempotent
    stats.found_ids = len(records)
    # 一份产出都没有的目录。目录不存在也算——"没传上来"和"传了个空壳"
    # 对结果没有区别，都需要人去看一眼。
    stats.empty_roots = sorted(b for b, n in per_root.items() if n == 0)
    return records


# 只在 tag 产物里有的维度。混进 NL 产物去查会永远得到"(缺失)"，
# 而且会把"这个成员只交了 NL"误报成"溯源缺失"。
TAG_ONLY_DIMS = frozenset({"tag_backend", "tag_precision"})

PROVENANCE_DIMS = ("prompt_ver", "model_id", "tag_backend", "tag_precision",
                   "variant")


def collect_provenance(
    records: dict[str, Record],
    *,
    stats: MergeStats | None = None,
) -> dict[str, dict[str, int]]:
    """统计溯源维度的取值分布。

    为什么必须查：一个成员的产物可能来自不同配置——prompt 换过版本、
    服务端换过模型、tag 换了后端。这些差异在正文里看不出来，
    但会让数据分布悄悄分叉，事后无法归因。

    尤其 `tag_backend`：CPU tagger 给的是真概率（0.31、0.77），
    VLM 出 tag 时恒为 1.0。下游若按 `>0.5` 过滤，VLM 那批会全留、
    tagger 那批会被砍掉一半——这是口径不一致，不是数据质量问题。
    """
    stats = stats or MergeStats()
    out: dict[str, dict[str, int]] = {}

    for dim in PROVENANCE_DIMS:
        counter: dict[str, int] = {}
        for rec in records.values():
            # tag_backend 只存在于 tag 文件。这里必须查"有没有"，不能直接
            # 去 rec.prov["tag"]：老产物、手改的产物、只回传了 nl 的产物都
            # 没有这个键，直取会让整个 merge 崩在这儿——而缺溯源只是告警
            # 级别的问题，不该阻断合并。
            kinds = ("tag",) if dim in TAG_ONLY_DIMS else tuple(rec.prov)
            available = [k for k in kinds if k in rec.prov]
            if not available:
                counter["(缺失)"] = counter.get("(缺失)", 0) + 1
                continue
            values = {str(rec.prov[k].get(dim) or "(缺失)") for k in available}
            for v in values:
                counter[v] = counter.get(v, 0) + 1
        out[dim] = counter

    stats.provenance = out
    return out


def provenance_warnings(prov: dict[str, dict[str, int]]) -> list[str]:
    """把"不一致"翻译成人能读的告警。一致时不返回任何东西。"""
    msgs: list[str] = []
    for dim, counter in prov.items():
        if len(counter) <= 1:
            continue
        parts = "、".join(f"{k} {v} 条"
                          for k, v in sorted(counter.items(), key=lambda kv: -kv[1]))
        msgs.append(f"{dim} 不一致：{parts}")
    return msgs


def load_shard_plans(path: str | Path) -> list[dict]:
    """读分包总表（`tagger split` 写的 shards.json）。

    分表只有区间（first_id / last_id / count），没有逐条 id。这是刻意的：
    3000 万条逐条落表要 2GB，而区间已经够回答"这个包交了没有"。
    逐条对账仍然靠分发包里的 manifest。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("shards") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"{path} 不是分包总表：找不到 shards 数组")
    plans: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        plans.append({
            "name": str(row.get("name") or f"shard-{row.get('shard', len(plans))}"),
            "first_id": str(row.get("first_id") or ""),
            "last_id": str(row.get("last_id") or ""),
            "count": int(row.get("count") or 0),
        })
    return plans


def _seq_of(image_id: str) -> int | None:
    """`artist-000123` → 123。取不到数字返回 None。

    image_id 的序号是全局递增分配的（见 distribute.assign_ids），
    所以"落在某个区间内"可以用来判断一条产出属于哪个包，
    不需要把逐条 id 都载入内存。
    """
    _, dash, tail = image_id.rpartition("-")
    if dash and tail.isdigit():
        return int(tail)
    return None


def _is_complete(rec: Record, require: str) -> bool:
    want_nl = require in ("nl", "both")
    want_tag = require in ("tag", "both")
    return (not want_nl or bool(rec.caption)) and (not want_tag or bool(rec.tag))


def coverage_by_shard(
    records: dict[str, Record],
    plans: Sequence[dict],
    *,
    require: str = "both",
    stats: MergeStats | None = None,
) -> list[ShardCoverage]:
    """把产出按分发表区间归包，算每包覆盖。

    这是"整包没交"的检测点：某个区间一条都没命中，就说明那个包
    （通常就是某个人）整个没回来。它比逐条缺失更值得先看——逐条缺失
    可能只是尾部没跑完，整包缺失基本意味着分发或回传环节断了。
    """
    stats = stats or MergeStats()
    rows: list[ShardCoverage] = []
    prepared: list[tuple[ShardCoverage, int | None, int | None]] = []

    for plan in plans:
        first = str(plan.get("first_id") or "")
        last = str(plan.get("last_id") or "")
        lo, hi = _seq_of(first), _seq_of(last)
        expected = int(plan.get("count") or 0)
        if not expected and lo is not None and hi is not None:
            expected = max(0, hi - lo + 1)
        cov = ShardCoverage(name=str(plan.get("name") or ""), first_id=first,
                            last_id=last, expected=expected)
        rows.append(cov)
        prepared.append((cov, lo, hi))

    unassigned: list[str] = []
    for image_id, rec in records.items():
        seq = _seq_of(image_id)
        target: ShardCoverage | None = None
        if seq is not None:
            for cov, lo, hi in prepared:
                if lo is None or hi is None:
                    continue
                if lo <= seq <= hi:
                    target = cov
                    break
        if target is None:
            unassigned.append(image_id)
            continue
        target.got += 1
        if _is_complete(rec, require):
            target.complete += 1

    stats.shard_coverage = rows
    stats.unassigned_ids = sorted(unassigned)
    return rows


def check(
    records: dict[str, Record],
    *,
    expected_ids: Sequence[str] | None = None,
    require: str = "both",
    stats: MergeStats | None = None,
    shard_plans: Sequence[dict] | None = None,
) -> MergeStats:
    """对账。expected_ids 给了才能发现"整条没交"。

    stats 可以被调用方传入——collect 阶段记下的 duplicates / unreadable 要带过来。
    早先这里自己 new 了一个，导致重复检测的结果被吞掉：明明扫到了重复文件，
    报告上却一个都没有。这类"两处各有一份统计"的坑在合并逻辑里很容易犯。

    shard_plans 是分包总表。给了它就能做逐包覆盖：即使分发包已经不在了
    （成员本地删了、或者根本没拿到），也能凭区间判断哪一包整包没交。
    """
    stats = stats or MergeStats()
    stats.found_ids = len(records)

    if expected_ids is not None:
        stats.expected = len(expected_ids)
        want = set(expected_ids)
        stats.missing_ids = sorted(want - set(records))
        extra = sorted(set(records) - want)
        if extra:
            # 多出来的不算错，但说明分发表的分配记录不准，值得知道
            stats.extra_ids = extra
    else:
        # 没有逐条清单时，分包总表的 count 合计才是"应产出"；
        # 拿 len(records) 当应产出等于自己跟自己比，永远是 100%。
        table_total = sum(int(p.get("count") or 0) for p in (shard_plans or []))
        stats.expected = table_total or len(records)

    want_nl = require in ("nl", "both")
    want_tag = require in ("tag", "both")

    for image_id, rec in records.items():
        ok = True
        if want_nl and not rec.caption:
            stats.missing_nl.append(image_id)
            ok = False
        if want_tag and not rec.tag:
            stats.missing_tag.append(image_id)
            ok = False
        if not rec.caption and not rec.tag:
            stats.empty_content.append(image_id)
            ok = False
        if ok:
            stats.complete += 1

    stats.by_artist = _stats_by_artist(records, expected_ids, require)
    if shard_plans:
        coverage_by_shard(records, shard_plans, require=require, stats=stats)
    return stats


def _stats_by_artist(
    records: dict[str, Record],
    expected_ids: Sequence[str] | None,
    require: str,
) -> dict[str, dict[str, int]]:
    """按画师汇总完整率。

    这是发现"某个画师整片空着"的地方——总量看着 98%，但某个画师是 0%，
    说明那批素材可能根本没发到人手上，或者目录名没被识别成画师。
    """
    out: dict[str, dict[str, int]] = {}

    def artist_of(image_id: str) -> str:
        stem, dash, tail = image_id.rpartition("-")
        return stem if dash and tail.isdigit() else "(无画师)"

    ids = list(expected_ids) if expected_ids is not None else list(records)
    for image_id in ids:
        a = artist_of(image_id)
        row = out.setdefault(a, {"expected": 0, "got": 0, "complete": 0})
        row["expected"] += 1

    for image_id, rec in records.items():
        a = artist_of(image_id)
        row = out.setdefault(a, {"expected": 0, "got": 0, "complete": 0})
        row["got"] += 1
        has_nl = bool(rec.caption) or require == "tag"
        has_tag = bool(rec.tag) or require == "nl"
        if has_nl and has_tag:
            row["complete"] += 1

    return out


# ------------------------------------------------------------------ 输出


def build_rows(
    records: dict[str, Record],
    *,
    dataset_name: str = "",
    image_root: str = "/image",
    image_ext: str = ".jpg",
) -> Iterator[dict]:
    """按最终格式产出记录。

    image_path 由 image_root + 数据集名 + image_id 拼成。
    数据集名是给未来 UI 填空的（fanbox / pixiv / ...），现在先走配置，
    不写死在代码里——它决定训练侧从哪里找图，改一次要重刷全量。
    """
    prefix = image_root.rstrip("/")
    if dataset_name:
        prefix = f"{prefix}/{dataset_name.strip('/')}"

    for image_id in sorted(records):
        rec = records[image_id]
        yield {
            "image_id": image_id,
            # HF 模式下产物自带 image_path（hf://datasets/<repo>/<shard>#<key>），
            # 那条才是能回指到图的；配置拼出来的路径只适用于"图在本地目录"的老流程。
            "image_path": rec.image_path or f"{prefix}/{image_id}{image_ext}",
            "caption": rec.caption,
            "tag": rec.tag,
        }


def write_merged(
    records: dict[str, Record],
    out_path: str | Path,
    *,
    dataset_name: str = "",
    image_root: str = "/image",
    image_ext: str = ".jpg",
    fmt: str = "jsonl",
    require: str = "both",
    skip_empty: bool = True,
) -> tuple[Path, int]:
    """写出总表。默认 JSONL，因为 3000 万条做成单个 JSON 数组没有工具吃得下。

    skip_empty 按 require 过滤，而不是"有任一字段就留"：both 模式下
    caption 空、tag 有值的记录若写进总表，训练侧读到空 caption 会当成
    "这张图没有描述"——比不写更糟，因为它看起来是条合法数据。
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    def _keep(row: dict) -> bool:
        if not skip_empty:
            return True
        if require == "both":
            return bool(row["caption"] and row["tag"])
        if require == "nl":
            return bool(row["caption"])
        return bool(row["tag"])

    if fmt == "json":
        # 单个 JSON 数组要整表物化——3000 万行没有工具吃得下，这条路径
        # 只适合小批试跑；正式全量请用默认的 jsonl（边生成边写，不物化）。
        rows = [r for r in build_rows(records, dataset_name=dataset_name,
                                      image_root=image_root, image_ext=image_ext)
                if _keep(r)]
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        return out, len(rows)

    written = 0
    with out.open("w", encoding="utf-8") as f:
        for row in build_rows(records, dataset_name=dataset_name,
                              image_root=image_root, image_ext=image_ext):
            if not _keep(row):
                continue
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    return out, written


# ------------------------------------------------------------------ 抽检


def sample_records(
    records: dict[str, Record],
    *,
    n: int = 20,
    seed: int = 42,
    only_complete: bool = True,
) -> list[Record]:
    """随机抽样供人工比对。

    固定 seed 是刻意的：同一批数据抽出来的样本必须可复现，
    否则"A 说抽检过了"和"B 说没看到那条"会各说各话。
    """
    pool = [
        rec for rec in records.values()
        if not only_complete or (rec.caption and rec.tag)
    ]
    pool.sort(key=lambda r: r.image_id)
    if len(pool) <= n:
        return pool
    rng = random.Random(seed)
    picked = rng.sample(pool, n)
    return sorted(picked, key=lambda r: r.image_id)


def write_sample_sheet(
    samples: Sequence[Record],
    out_path: str | Path,
    *,
    dataset_name: str = "",
    image_root: str = "/image",
    image_ext: str = ".jpg",
) -> Path:
    """写出抽检清单。

    用 markdown 而不是 json：这份东西是给人看的，要能直接打开、
    对着图一行行比对，不该让人去读 json 再脑补。
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    prefix = image_root.rstrip("/")
    if dataset_name:
        prefix = f"{prefix}/{dataset_name.strip('/')}"

    lines = [
        f"# 抽检清单（{len(samples)} 条）",
        "",
        "逐条打开图片，核对 caption 描述与 tag 是否与画面相符。",
        "重点看：主体是否正确、有没有描述画面里不存在的东西、tag 是否张冠李戴。",
        "",
    ]
    for i, rec in enumerate(samples, 1):
        lines.append(f"## {i}. `{rec.image_id}`")
        lines.append("")
        lines.append(f"- 图片：`{prefix}/{rec.image_id}{image_ext}`")
        lines.append(f"- tag：{rec.tag or '（空）'}")
        lines.append(f"- caption：{rec.caption or '（空）'}")
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
