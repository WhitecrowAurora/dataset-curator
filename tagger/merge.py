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

import hashlib
import json
import os
import random
import tempfile
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from .storage.collections import SpillList, SpillMap

TAG_SUFFIX = "-tag.json"
NL_SUFFIX = "-nl.json"


def _content_digest(value: str) -> str:
    """内容的短哈希，与 `conflicts.Candidate.digest` 同一算法。

    决策文件里存的就是这个值，两处必须一致：一处改了另一处就对不上，
    表现成"批过的冲突合并时又报出来"。
    """
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class Record:
    """一个成员产出的中间态。两个文件都齐全才算完整。"""

    image_id: str
    caption: str = ""
    tag: str = ""
    # 非空 = 这一条是"确定无产物"的剔除记录（输入无效 / 重试仍失败）。
    # 它来自产物 jsonl 里的 excluded 行，见 artifacts.excluded_row。
    # 有它就不该算进 missing_ids，否则几张坏图就能把整批合并判成失败。
    excluded: dict = field(default_factory=dict)
    src_files: list[str] = field(default_factory=list)
    # 产物里自带的路径（HF 模式才有）。为空时由 build_rows 按配置拼。
    image_path: str = ""
    # kind -> provenance（tag / nl 各一份）。两份的字段不完全一样：
    # tag_backend 只在 tag 文件里，nl 文件没有。分开存才不会串。
    prov: dict[str, dict] = field(default_factory=dict)
    # 产物顶层的溯源字段（文档 V0.2 §8）。存在 payload 顶层而不是 provenance
    # 里，是为了让下游一句 SQL 就能筛语言、模型版本、来源 revision。
    # 用 dict 而不是五个字段：以后文档加字段不用再改 Record 的构造点。
    meta: dict[str, object] = field(default_factory=dict)
    # 元数据按 kind 分开存。meta 是"最后写入者覆盖"的并集，
    # 逐键 update 会让「被拒绝版本独有的键」残留：实测正文取了 B、
    # artist 却还是 A 的。分开存、生成时再合成，两侧才不会互相掏空。
    meta_by_kind: dict[str, dict] = field(default_factory=dict)


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
    # 冲突已按「冲突与决策」页的批准结果定下来（取的是人选的那份）。
    # 它不该算失败——人已经拍过板了，再报错等于按钮白点。
    resolved: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    # 产物里有、分发表里没有的 id。这**不算错**（可能是补跑后新加的素材），
    # 但说明分配记录与实际对不上，值得单列——之前混进 unreadable，让
    # "读不动的坏文件"和"多出来的好数据"搅在同一个计数里。
    extra_ids: list[str] = field(default_factory=list)
    by_artist: dict[str, dict[str, int]] = field(default_factory=dict)
    # 各溯源维度的取值分布（prompt_sha / model_id / tag_backend）。
    # 出现两种以上取值，说明这批数据是不同配置混出来的。
    provenance: dict[str, dict[str, int]] = field(default_factory=dict)

    # ---- 整份 / 整包级别的缺失。这一层是"看着 98% 其实缺了三百万条"的解药。
    # 空回收目录 = 某个成员一份都没交（目录不存在也记在这里）。
    empty_roots: list[str] = field(default_factory=list)
    # 按分发表逐包统计的覆盖。没有分发表时为空。
    shard_coverage: list[ShardCoverage] = field(default_factory=list)
    # 落在所有分包区间之外的 id：说明分发表与实际产物对不上。
    unassigned_ids: list[str] = field(default_factory=list)
    # 已声明剔除（输入无效 / 重试仍失败）的 id。它们的结论写在产物 jsonl 的
    # excluded 行里，所以不计入 missing_ids、也不该让整批判失败。
    excluded: object = field(default_factory=SpillList)

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
        if self.excluded:
            # 单独一行且不进 failed：它们是"确认无产物"，混进缺失里会让人
            # 以为是漏做，进而去重跑那些本来就打不出来的图。
            lines.append(f"已剔除     : {len(self.excluded)}"
                         "（输入无效 / 重试仍失败，不算缺失）")
        if self.duplicates:
            lines.append(f"内容冲突   : {len(self.duplicates)} 个 id（同一 id 两份不同内容）")
        if self.idempotent_repeats:
            lines.append(f"重复提交   : {len(self.idempotent_repeats)} 次（内容一致，已取一份）")
        if self.resolved:
            lines.append(f"已裁决     : {len(self.resolved)} 条（按「冲突与决策」批准的那份取）")
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


# 产物顶层里要一并带到最终表的字段（文档 V0.2 §8）。
# 不放 provenance：那一份是"模型参数对账"用的，混进业务字段会让续跑校验
# 和人工读日志都变模糊。
_META_FIELDS = ("caption_language", "annotation_model", "annotation_version",
                "source_revision", "tags_original",
                # 画师三键：产物顶层就带着（tag/nl 各一份）。不挑出来的话，
                # 合并产物里只剩 caption 里的 @前缀 可解析——那是文本，不是字段。
                "artist", "artist_tag", "artist_source", "artists")
# 这个字段是列表，不能走 str()：`str(["a"])` 会写成 "['a']"，下游 json.loads
# 拿到的是字符串而不是字符串列表。
_LIST_FIELDS = ("tags_original", "artists")


def _annotation_meta_of(obj: object) -> dict:
    """从 payload 顶层挑出溯源字段；空值不写，避免整列都是空串。

    名字里带 `annotation` 是为了和 conflicts.py 里那个"按文件名取时间戳"的
    `_meta_of` 区分开——两个都叫 `_meta_of` 时，import 一次就会串。
    """
    if not isinstance(obj, dict):
        return {}
    out: dict = {}
    for key in _META_FIELDS:
        val = obj.get(key)
        if val in (None, "", []):
            continue
        out[key] = list(val) if key in _LIST_FIELDS else str(val)
    return out


def _read_one(path: Path) -> tuple[str, str, str, str, dict, dict]:
    """读一个中间态文件，返回 (image_id, kind, 内容, image_path, provenance, meta)。

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
        return fallback_id, kind, "", "", {}, {}

    try:
        obj = json.loads(raw)
    except ValueError:
        # 不是 json 时按纯文本处理。成员手工改过、或工具直接写了裸文本。
        return fallback_id, kind, raw, "", {}, {}

    if isinstance(obj, str):
        return fallback_id, kind, obj.strip(), "", {}, {}
    if not isinstance(obj, dict):
        return fallback_id, kind, "", "", {}, {}

    return _tuple_from_payload(obj, fallback_id, kind)


def _tuple_from_payload(obj: object, fallback_id: str,
                        kind: str) -> tuple[str, str, str, str, dict, dict]:
    """把一份 payload 折成 (image_id, kind, 内容, image_path, provenance, meta)。

    json 文件与 parquet 行共用这一段：两种形态存的是**同一份 payload**，
    解析口径必须只有一处，否则"换成 parquet 之后对账结论变了"这种问题
    会查很久。
    """
    if not isinstance(obj, dict):
        return fallback_id, kind, "", "", {}, {}
    image_id = str(obj.get("image_id") or fallback_id)
    value = obj.get("tag") if kind == "tag" else (obj.get("caption")
                                                  or obj.get("nl") or "")
    prov = obj.get("provenance")
    return (image_id, kind, str(value or "").strip(),
            str(obj.get("image_path") or ""),
            prov if isinstance(prov, dict) else {},
            _annotation_meta_of(obj))


def _iter_shard_records(root: Path) -> Iterator[tuple[str, str, str, str, dict, dict]]:
    """读分片形态的产物（artifacts-*.parquet / artifacts-*.jsonl）。

    tag / nl 两个字段存的是与 json 文件完全相同的 payload 字符串，见
    artifacts.py；两种分片的行结构一致，所以这里只写一份解析。
    """
    from .artifacts import (find_artifacts, is_excluded_row, iter_artifact_rows,
                            parse_payload)

    for path in find_artifacts(root):
        try:
            # 行号进 source：同一个分片里的两行如果同一个 id，也要能定位到行，
            # 只说"这个文件里重复了"等于没说。
            for idx, row in enumerate(iter_artifact_rows(path)):
                fallback = str(row.get("image_id") or "")
                if is_excluded_row(row):
                    # 剔除行没有 payload，走不了下面的解析；单独折成 kind="x"，
                    # 让 collect 把它记成 Record.excluded 而不是当成"没产出"。
                    yield (fallback, "excluded", "", "", {},
                           {"excluded": dict(row.get("excluded") or {})},
                           f"{path}#{idx}")
                    continue
                for kind in ("tag", "nl"):
                    obj = parse_payload(kind, str(row.get(kind) or ""))
                    if obj is None:
                        continue
                    image_id, k, value, image_path, prov, meta = _tuple_from_payload(
                        obj, fallback, kind)
                    yield image_id, k, value, image_path, prov, meta, f"{path}#{idx}"
        except Exception as exc:      # noqa: BLE001 - 坏片要报，不该中断整批
            raise ValueError(f"{path}: {exc}") from exc


def _iter_member_records(
    root: Path, stats: MergeStats,
) -> Iterator[tuple[str, str, str, str, dict, dict]]:
    """把逐图 json 与分片（parquet / jsonl）统一成一个流。

    同一个目录里几种形态并存时（改了 --artifact-format 重跑），各边的记录
    都会进 records；内容不同就是重复冲突，这正是要报出来的东西。
    """
    for path in _iter_json_files(root):
        try:
            got = _read_one(path)
        except ValueError as exc:
            stats.unreadable.append(f"{path}: {exc}")
            continue
        # 解包放在 try 外面：`_read_one` 的返回结构一变就要当场炸。放在 try 里
        # 会被上面那个 `except ValueError` 当成"文件读不了"，整批候选静默归零
        # —— 只在统计里多一个 unreadable 数字，看起来像数据没交。
        image_id, kind, value, image_path, prov, meta = got
        if not image_id:
            stats.unreadable.append(f"{path}: 文件名里取不到 image_id")
            continue
        yield image_id, kind, value, image_path, prov, meta, str(path)

    try:
        yield from _iter_shard_records(root)
    except ValueError as exc:
        stats.unreadable.append(str(exc))


def collect(
    roots: Iterable[str | Path],
    *,
    stats: MergeStats | None = None,
    on_progress: "Callable[[int], None] | None" = None,
    should_stop: "Callable[[], bool] | None" = None,
    decisions: "dict[str, str] | None" = None,
) -> dict[str, Record]:
    """扫描若干回收目录，按 image_id 归并。

    同一个 id 出现在多个文件里视为重复——不静默覆盖，全部记进 stats 让人处理。

    `decisions` 是"冲突与决策"页批准过的结果：{"image_id|kind": 选中内容的哈希}。
    命中时按它取那一份、不再记冲突——否则人点了按钮、合并照样报错，那按钮
    就白点了。哈希而不是下标：重扫一次候选顺序会变，按下标套会把决定安到
    另一份内容上。

    on_progress 每读一个产物文件回调一次（已扫条数）；should_stop 返回 True
    时提前收手，返回已扫到的部分——调用方据此放弃后续写盘（半份对账比
    假的完整对账诚实）。
    """
    stats = stats or MergeStats()
    records = SpillMap(encode=asdict, decode=lambda v: Record(**v))
    picks = decisions or {}
    # 记录每个 (id, kind) 第一次出现在哪个文件。
    # 用完整路径做键而不是文件名——不同人的目录下文件名必然相同
    # (`xxx-tag.json`)，按文件名去重会把真正的重复当成"同一份"跳过，
    # 而重复恰恰是本模块最该抓的东西。
    filled = SpillMap()
    duplicates = SpillMap()
    idempotent = SpillList()
    # 每个回收目录自己出了多少条。整份没交（成员一份没交 / 目录根本没传上来）
    # 只有在这个粒度上才看得见：全局缺失列表会把"某人整包没有"和
    # "十个包各少几张"混成同一个数字。
    per_root: dict[str, int] = {}
    scanned = 0
    stopped = False

    for root in roots:
        base = Path(root)
        if not base.exists():
            stats.unreadable.append(f"{base}: 目录不存在")
            per_root[str(base)] = 0
            continue
        per_root.setdefault(str(base), 0)
        for (image_id, kind, value, image_path, prov, meta,
             source) in _iter_member_records(base, stats):
            if should_stop is not None and should_stop():
                stopped = True
                break
            scanned += 1
            if on_progress is not None:
                on_progress(scanned)
            per_root[str(base)] += 1

            rec = records.get(image_id)
            if rec is None:
                rec = Record(image_id=image_id)
                records[image_id] = rec

            if kind == "excluded":
                # 剔除记录：只记标记，不参与 tag/nl 的内容比较——它本来就没有
                # 内容。同一个 id 若真的另有成功产出（重试救回来了），那条会走
                # 正常分支填上 tag/nl，两边共存不冲突。
                meta = dict(meta or {})
                info = dict(meta.get("excluded") or {})
                if info and not rec.excluded:
                    rec.excluded = info
                rec.src_files.append(source)
                if not info:
                    stats.unreadable.append(f"{source}: 剔除记录缺原因，已忽略")
                continue

            key = (image_id, kind)
            prev = filled.get(key)
            if prev is not None:
                prev_path, prev_value, _ = prev
                if prev_value == value:
                    # 内容一模一样：重跑没清目录、补跑目录叠加都会这样。
                    # 取一份即可，不算错——拦下来只会让"重跑一下"没法收尾。
                    idempotent.append(f"{image_id}:{kind}")
                    continue
                paths = duplicates.get(key, [prev_path])
                paths.append(source)
                duplicates[key] = paths
                pick = picks.get(f"{image_id}|{kind}", "")
                if not pick or _content_digest(value) != pick:
                    continue

            # 内容和来源一起替换，缺失的溯源也不能沿用被拒绝版本。
            filled[key] = (source, value, image_path)
            if prov:
                rec.prov[kind] = prov
            else:
                rec.prov.pop(kind, None)
            if meta:
                # 溯源字段跟着内容走：tag/nl 两份都带同一份值，先读到哪份用哪份。
                # 被拒的版本不覆盖（与 prov 同一个处置），否则"批了 A 却记着 B 的
                # 模型版本"。
                # 元数据跟着**被采纳的那一版**走：按 kind 整体替换。
                rec.meta_by_kind[kind] = dict(meta)
                rec.meta.update(meta)
            if kind == "tag":
                rec.tag = value
            else:
                rec.caption = value
            records[image_id] = rec
        if stopped:
            break

    # 扫完才按侧裁决：被批准的版本可能最后才出现，tag 的批准不能消除 nl 冲突。
    stats.duplicates = SpillMap()
    stats.resolved = SpillList()
    for (image_id, kind), paths in duplicates.items():
        decision_key = f"{image_id}|{kind}"
        if picks.get(decision_key) == _content_digest(filled[(image_id, kind)][1]):
            stats.resolved.append(decision_key)
        else:
            combined = stats.duplicates.get(image_id, [])
            combined.extend(paths)
            stats.duplicates[image_id] = combined
    for (image_id, _kind), (source, _value, image_path) in filled.items():
        rec = records[image_id]
        if source not in rec.src_files:
            rec.src_files.append(source)
        # 保留原来的两侧优先顺序，但路径必须来自该侧最终选中的版本。
        if image_path and not rec.image_path:
            rec.image_path = image_path
        records[image_id] = rec
    filled.close()
    duplicates.close()
    stats.idempotent_repeats = idempotent
    stats.found_ids = len(records)
    # 一份产出都没有的目录。目录不存在也算——"没传上来"和"传了个空壳"
    # 对结果没有区别，都需要人去看一眼。
    stats.empty_roots = sorted(b for b, n in per_root.items() if n == 0)
    return records


# 只在 tag 产物里有的维度。混进 NL 产物去查会永远得到"(缺失)"，
# 而且会把"这个成员只交了 NL"误报成"溯源缺失"。
TAG_ONLY_DIMS = frozenset({"tag_backend", "tag_precision", "tag_preprocess"})

PROVENANCE_DIMS = ("prompt_sha", "model_id", "tag_backend", "tag_precision",
                   "tag_preprocess", "variant")


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

    out = {dim: {} for dim in PROVENANCE_DIMS}
    for rec in records.values():
        for dim, counter in out.items():
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


def load_shard_plans(path: str | Path | Sequence[str | Path]) -> list[dict]:
    """读分包总表（`tagger split` 写的 shards.json）。

    分表只有区间（first_id / last_id / count），没有逐条 id。这是刻意的：
    3000 万条逐条落表要 2GB，而区间已经够回答"这个包交了没有"。
    逐条对账仍然靠分发包里的 manifest。
    """
    if not isinstance(path, (str, Path)):
        paths = list(dict.fromkeys(Path(p).resolve() for p in path))
        if len(paths) == 1:
            return load_shard_plans(paths[0])
        plans = []
        for p in paths:
            for row in load_shard_plans(p):
                plans.append({**row, "name": f"{p}::{row['name']}"})
        intervals = [(_seq_of(p["first_id"]), _seq_of(p["last_id"]))
                     for p in plans if p["count"]]
        if any(lo is None or hi is None or lo > hi for lo, hi in intervals):
            raise ValueError("多批分包表包含无效序号区间")
        intervals.sort()
        if any(intervals[i - 1][1] >= intervals[i][0] for i in range(1, len(intervals))):
            raise ValueError("多批分包表序号重叠：请使用上一批分包表续号后再合并")
        return plans
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
    tail = image_id.rpartition("-")[2]
    if tail.isascii() and tail.isdigit():
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

    unassigned = SpillList()
    intervals = sorted((lo, hi, index) for index, (_, lo, hi) in enumerate(prepared)
                       if lo is not None and hi is not None)
    starts = [lo for lo, _, _ in intervals]
    disjoint = all(intervals[i - 1][1] < intervals[i][0] for i in range(1, len(intervals)))
    for image_id, rec in records.items():
        seq = _seq_of(image_id)
        target: ShardCoverage | None = None
        if seq is not None:
            if disjoint:
                index = bisect_right(starts, seq) - 1
                if index >= 0 and seq <= intervals[index][1]:
                    target = prepared[intervals[index][2]][0]
            else:
                # 旧总表允许重叠时保留“首个匹配包”的既有语义。
                for cov, lo, hi in prepared:
                    if lo is not None and hi is not None and lo <= seq <= hi:
                        target = cov
                        break
        if target is None:
            unassigned.append(image_id)
            continue
        target.got += 1
        if _is_complete(rec, require):
            target.complete += 1

    stats.shard_coverage = rows
    unassigned.sort()
    stats.unassigned_ids = unassigned
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
        want = SpillMap()
        for image_id in expected_ids:
            want[image_id] = True
        # 缺失 = 应产出但没有正常内容、而且**不是**已声明剔除的那些。
        # 不扣掉剔除项的话，几张输入无效的坏图就能让整批合并判失败——
        # 而它们的结论早就写在 excluded 行里了，那是"了结"不是"缺交"。
        missing: list[str] = []
        for i in want:
            if i in records:
                continue
            missing.append(i)
        stats.missing_ids = SpillList(missing)
        stats.missing_ids.sort()
        stats.extra_ids = SpillList(i for i in records if i not in want)
        stats.extra_ids.sort()
        want.close()
    else:
        # 没有逐条清单时，分包总表的 count 合计才是"应产出"；
        # 拿 len(records) 当应产出等于自己跟自己比，永远是 100%。
        table_total = sum(int(p.get("count") or 0) for p in (shard_plans or []))
        stats.expected = table_total or len(records)

    want_nl = require in ("nl", "both")
    want_tag = require in ("tag", "both")

    stats.missing_nl = SpillList()
    stats.missing_tag = SpillList()
    stats.empty_content = SpillList()
    stats.complete = 0

    stats.excluded = SpillList()
    for image_id, rec in records.items():
        if rec.excluded and not rec.tag and not rec.caption:
            # 已声明剔除：没有内容是对的，不是"内容为空"。计一份单独的数，
            # 让管理员看得见"这批里有多少张是确认无产物的"，而不是让它们
            # 混进 empty_content 里去污染"模型是不是坏了"的判断。
            stats.excluded.append(image_id)
            continue
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

    ids = expected_ids if expected_ids is not None else records
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

# 最终表的两套字段名。
#   legacy : caption / tag        —— 现在所有下游读的就是这两个
#   v02    : nl_final / tags_final —— 标注规范 §8 的验收字段名
#   both   : 两套并列（默认）
# ★默认 both 是有代价的：caption 是长文本，3000 万条复制一份约十几 GB。
#   要省体积就用 `--final-names v02`（或 legacy）重跑一次 merge——merge 只读
#   产物不碰模型，重出一份命名不同的表是分钟级的事，不需要重打标。
FINAL_NAMES = ("legacy", "v02", "both")

_CAPTION_KEYS = ("caption", "nl_final")
_TAG_KEYS = ("tag", "tags_final")


def final_field(row: dict, kind: str) -> str:
    """按别名取正文。kind 给 caption / nl_final 取 NL，给 tag / tags_final 取 tag。

    读侧一律走这里：盘上可能是任一套列名（旧表只有 caption，新表可能只有
    nl_final），比较/过滤时按固定键名取值会静默把所有行判成空。
    """
    keys = _CAPTION_KEYS if kind in ("caption", "nl_final", "nl") else _TAG_KEYS
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    return ""


def apply_final_names(row: dict, mode: str) -> dict:
    """把一行改成目标命名。mode 见 FINAL_NAMES。"""
    if mode == "legacy":
        return row
    out = dict(row)
    if mode == "v02":
        out.pop("caption", None)
        out.pop("tag", None)
    out["nl_final"] = row.get("caption", "")
    out["tags_final"] = row.get("tag", "")
    return out


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

    ordered = records.sorted_items() if isinstance(records, SpillMap) else (
        (key, records[key]) for key in sorted(records))
    for image_id, rec in ordered:
        # tag 侧优先：它是"图 → 标签"的确定性产物；nl 只补 tag 缺的键。
        meta: dict = dict(rec.meta_by_kind.get("nl") or {})
        meta.update(rec.meta_by_kind.get("tag") or {})
        if not meta:
            meta = rec.meta or {}
        # 画师声明做两套形态，供校验器与下游直接比对：
        #   artists      —— 逐字原名列表（不含 @）
        #   artist_tags  —— @形态列表，与 tags_final 里的画师条目同口径
        # 老产物只有单个 artist / artist_tag，这里统一兜底成列表，
        # 免得校验器再去猜「这一行声明了几个画师」。
        _artists = [str(a) for a in (meta.get("artists") or []) if str(a).strip()]
        if not _artists and str(meta.get("artist") or "").strip():
            _artists = [str(meta["artist"])]
        yield {
            "image_id": image_id,
            # HF 模式下产物自带 image_path（hf://datasets/<repo>/<shard>#<key>），
            # 那条才是能回指到图的；配置拼出来的路径只适用于"图在本地目录"的老流程。
            "image_path": rec.image_path or f"{prefix}/{image_id}{image_ext}",
            "caption": rec.caption,
            "tag": rec.tag,
            # 文档 V0.2 §8：五列必须逐行存在。缺的写空值而不是省略键——jsonl
            # 省略了还能读，转成 parquet 就会因为各行的键集合不同直接失败。
            "caption_language": str(meta.get("caption_language") or ""),
            "tags_original": list(meta.get("tags_original") or []),
            "annotation_model": str(meta.get("annotation_model") or ""),
            "annotation_version": str(meta.get("annotation_version") or ""),
            "source_revision": str(meta.get("source_revision") or ""),
            # 画师：合并产物里也留独立字段。空值写空串而不是省略键，
            # 理由同上——各行键集合不同会让 parquet 转换直接失败。
            "artist": str(meta.get("artist") or ""),
            "artist_tag": str(meta.get("artist_tag") or ""),
            "artist_source": str(meta.get("artist_source") or ""),
            "artists": _artists,
            "artist_tags": [a if a.startswith("@") else f"@{a}" for a in _artists],
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
    final_names: str = "both",
) -> tuple[Path, int]:
    """写出总表。默认 JSONL，因为 3000 万条做成单个 JSON 数组没有工具吃得下。

    skip_empty 按 require 过滤，而不是"有任一字段就留"：both 模式下
    caption 空、tag 有值的记录若写进总表，训练侧读到空 caption 会当成
    "这张图没有描述"——比不写更糟，因为它看起来是条合法数据。

    final_names 见 FINAL_NAMES：默认同时给 caption/tag 与 nl_final/tags_final
    两套列名，谁都不用改代码。
    """
    if final_names not in FINAL_NAMES:
        raise ValueError(f"final_names 只能是 {'/'.join(FINAL_NAMES)}，"
                         f"收到 {final_names!r}")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    def _keep(row: dict) -> bool:
        if not skip_empty:
            return True
        caption = final_field(row, "caption")
        tag = final_field(row, "tag")
        if require == "both":
            return bool(caption and tag)
        if require == "nl":
            return bool(caption)
        return bool(tag)

    rows = (r for r in build_rows(records, dataset_name=dataset_name,
                                  image_root=image_root, image_ext=image_ext) if _keep(r))
    written = 0
    tmp: Path | None = None
    try:
        # 同目录临时文件：写盘失败保留上一版总表，全部写完才原子替换。
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                          dir=out.parent, prefix=f".{out.name}.",
                                          suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            if fmt == "json":
                f.write("[\n")
            for row in rows:
                if fmt == "json" and written:
                    f.write(",\n")
                f.write(json.dumps(apply_final_names(row, final_names),
                                   ensure_ascii=False))
                if fmt != "json":
                    f.write("\n")
                written += 1
            if fmt == "json":
                f.write("\n]\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(out)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
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
    def eligible():
        ordered = records.sorted_items() if isinstance(records, SpillMap) else (
            (key, records[key]) for key in sorted(records))
        for _, rec in ordered:
            if not only_complete or (rec.caption and rec.tag):
                yield rec

    # 对排序后的位置抽样，保留旧版 random.sample 的相同 seed 结果；
    # 两次流式遍历只保留样本，不复制整张总表。
    total = sum(1 for _ in eligible())
    if total <= n:
        return list(eligible())
    positions = set(random.Random(seed).sample(range(total), n))
    return [rec for index, rec in enumerate(eligible()) if index in positions]


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
