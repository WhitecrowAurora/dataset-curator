"""从输入到封包的完整链路：发现 → 画师 → 全局命名 → 打标 → 封包。

这一层存在的理由
----------------
`pack.py` 会封包但不管打标；`local_tag.py` 会打标但只会扫目录、产物是一堆
json。WebUI 要的是"指一个目录或一个 4TB 的 zip，出一批 parquet / tar"，
中间还夹着三件必须一起决定的事：

1. **image_id 必须是全局序号**（`画师-000123`）。打标产物的文件名、归档里
   的成员名、总表的 image_path 都靠它对齐；用相对路径当 id 在 zip 成员上
   会带 `::`（Windows 文件名非法），多包同名路径还会互相覆盖。
2. **画师名从路径取**，不交给模型猜。flat 布局下它进 id，mirror 布局下
   原目录结构保留、画师仍写进 meta。
3. **两阶段而不是一遍过**：先打标落 `<out>/tags/<id>-tag.json`，再读产物
   封包。这样"打标→封包"各自可续做，且封包参数（质量/采样/格式）改了
   不需要重跑模型——只重封一遍。

断点续做的两个独立记忆
----------------------
- 打标：`local_tag` 的配置指纹（已有产物 + 同配置才跳过）。
- 封包：`PackState`。它的 key 带封包参数指纹（`.packstate-<fp>.jsonl`），
  所以改质量/采样/格式会重新封一套包，而不会误以为"已经做过"。

封包只在打标阶段完整跑完时进行。中途停止的任务不封包——半批数据封出来的
包会混进下游，而"再跑一次补齐"比"事后挑出半包"便宜得多。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence

from .artist import ArtistRule, extract_artist, prepend_tag
from .local_tag import TagItem, artifact_path, read_artifact
from .naming import archive_name, make_image_id, normalize_ext, sanitize
from .pack import (InputItem, PackOptions, PackState, Packer, discover_inputs,
                   sha256_of_file)

# 封包一次预取多少条。太小等于没并行，太大把转码后的图堆在内存里。
_PACK_WINDOW_FACTOR = 4

# ---- 产出体积估算（"盘够不够写"的唯一依据）
#
# 为什么不能再用固定阈值（比如"剩余 < 30G 就停"）：12G 的盘跑 3 张测试图
# 会被无谓拦下，而 200G 的盘跑 8TB 素材会一路写到爆盘才停。判断标准应该是
# "这一批还要写多少"，不是"盘上还剩多少"。
#
# 倍数=转码后体积 ÷ 原文件体积。取值来自 configs/tiers.yaml 的实测
# （1024² 有细节的图，原图 q92/4:2:0 = 540KB）：
#   q95/4:4:4 → 2.64×     q95/4:2:0 → 1.27×
# 4:2:2 没有实测，取两者之间。这只是**先验**：封包跑起来之后会改用实测
# 比例，先验只负责前几十张的判断。
_TRANSCODE_GROWTH = {0: 2.6, 1: 1.8, 2: 1.3}
# 质量每 +1 档，体积约 +6%（q90→q95 的 4:2:0 实测 481→688KB）
_QUALITY_STEP = 1.06
_QUALITY_PIVOT = 95
# 每条样本的 json/tag 成员量级（含 key、画师、tags 数组）
_PACK_META_BYTES = 1600
# 没拿到原始体积时的兜底（1024² JPEG 的典型值）
_DEFAULT_IMAGE_BYTES = 300 * 1024
# 冗余：估算值再留 20%。估算不是承诺，留一截才敢说"写不下就提前停"。
DISK_HEADROOM = 0.2


# ------------------------------------------------------------------ 读取


class MemberReader:
    """按需读取散图 / zip 成员，复用 zip 句柄。

    为什么需要池：`ZipFile(path).read(member)` 每次都要解析整个中央目录。
    一个 4TB 级、十万成员的包，逐条读就是十万次目录解析——O(n²) 的固定
    开销，而且它不会报错，只是越跑越慢。

    为什么加锁：打标线程和 HTTP 缩略图线程会同时读同一个包。`ZipFile`
    的 read 不是线程安全的（共用文件位置）。锁的粒度是"一次读一个成员"，
    单张几十毫秒，不会成为瓶颈。
    """

    def __init__(self, max_open: int = 4):
        self._max_open = max(1, max_open)
        self._open: OrderedDict[str, zipfile.ZipFile] = OrderedDict()
        self._lock = threading.Lock()

    def read(self, item: TagItem) -> bytes:
        if not item.member:
            return item.path.read_bytes()
        source = item.source or str(item.path)
        with self._lock:
            zf = self._open.get(source)
            if zf is None:
                while len(self._open) >= self._max_open:
                    _, old = self._open.popitem(last=False)
                    try:
                        old.close()
                    except OSError:
                        pass
                zf = zipfile.ZipFile(source, "r")
                self._open[source] = zf
            else:
                self._open.move_to_end(source)
            return zf.read(item.member)

    def close(self) -> None:
        with self._lock:
            for zf in self._open.values():
                try:
                    zf.close()
                except OSError:
                    pass
            self._open.clear()

    def __enter__(self) -> "MemberReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ------------------------------------------------------------------ 计划


@dataclass(slots=True)
class Plan:
    """输入发现 + 命名分配的结果。"""

    input_path: str
    items: list[TagItem] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    total_bytes: int = 0
    artist_count: int = 0
    artist_covered: int = 0
    no_artist: int = 0
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    layout: str = "flat"
    id_prefix: str = ""

    def sample(self, n: int = 8) -> list[dict]:
        return [
            {"rel": it.rel, "name": it.out_name or it.image_id,
             "image_id": it.image_id, "artist": it.artist}
            for it in self.items[:n]
        ]


def _path_segments(name: str) -> list[str]:
    return [p for p in name.replace("\\", "/").split("/") if p and p not in (".", "..")]


def _legacy_id(item: InputItem) -> str:
    """只在"不封包、只打标"的旧路径上用的 id。

    散图沿用历史行为（相对路径去扩展名），这样老产物还能命中、断点续跑
    不失效。zip 成员没有独立路径，用 `<包路径>/<成员路径>` 去扩展名：
    既唯一（含包名，避免多包同名覆盖），又不含 Windows 非法字符。
    """
    if item.kind == "zip_member":
        # 包里没有"相对路径"这回事，用 `<包名>/<成员路径>` 造一个唯一 id。
        # 包名去掉 .zip：id 会变成 tags/ 下的目录名，带扩展名只是噪声
        # （且与 mirror 布局的命名口径不一致）。
        zip_rel = Path(item.rel_path.split("::", 1)[0]).with_suffix("").as_posix()
        base = "/".join(_path_segments(f"{zip_rel}/{item.member}"))
        stem = base.rsplit(".", 1)[0] if "." in base.rsplit("/", 1)[-1] else base
        return "/".join(sanitize(seg) or "_" for seg in stem.split("/"))
    rel = item.rel_path.replace("\\", "/")
    return rel[: -len(Path(rel).suffix)] if Path(rel).suffix else rel


def _mirror_name(item: InputItem, image_id: str, *, used: set[str]) -> str:
    """mirror 布局的输出名：原样保留素材里的目录结构。

    zip 成员按**包内路径**铺开，不再加包名前缀。素材包先前的封包流程本身
    就保证了目录不重名（能进包说明已经是干净结构），多套一层包名只会让
    训练侧看到 `素材包/alice/1.jpg` 这种凭空多出来的层级。

    真撞上同名（两个包里有同一个 `artist/0001.jpg`）时仍会追加 `-<id>`：
    那说明上游确实存在覆盖，此时保命比保结构重要。
    """
    logical = item.member if item.kind == "zip_member" else item.rel_path
    parts = _path_segments(logical)
    if not parts:
        parts = [item.flat_name or image_id]
    name = archive_name(image_id, ext=normalize_ext(parts[-1]),
                        layout="mirror", rel_path="/".join(parts))
    # 同名冲突时把 id 塞进文件名：确定、可读、不会再撞。
    if name.lower() in used:
        p = Path(name)
        name = str(p.with_name(f"{p.stem}-{image_id}{p.suffix}")).replace("\\", "/")
    used.add(name.lower())
    return name


def plan_inputs(
    input_path: str | Path,
    *,
    recursive: bool = True,
    include_zips: bool = True,
    include_loose: bool = True,
    limit: int = 0,
    assign_ids: bool = True,
    layout: str = "flat",
    seq_width: int = 6,
    artist_rule: ArtistRule | None = None,
    aliases: dict[str, str] | None = None,
) -> Plan:
    """发现输入并决定每张图的 id / 归档名。

    assign_ids=True（封包路径）：按 (画师, 原始路径) 排序后全局编号。
    排序键与 `distribute.assign_ids` 一致，同一批素材两次跑出的 id 相同。
    assign_ids=False（只打标）：沿用相对路径当 id，兼容历史产物。
    """
    from .config import ConfigError

    layout = (layout or "flat").lower()
    if layout not in ("flat", "mirror"):
        raise ConfigError(f"layout 只能是 flat / mirror，收到 {layout!r}")

    found = discover_inputs(
        input_path,
        recursive=recursive,
        include_zips=include_zips,
        include_loose=include_loose,
        limit=limit or None,
    )
    plan = Plan(input_path=str(input_path), layout=layout)
    if not found:
        return plan

    rule = artist_rule or ArtistRule(enabled=False)
    aliases = aliases or {}

    # 第一遍：取画师、算排序键。zip 成员的画师看包内路径（见 InputItem.logical_path）。
    records: list[tuple[InputItem, str]] = []
    for it in found:
        artist = extract_artist(it.logical_path, rule, aliases).tag if rule.enabled else ""
        records.append((it, artist))

    if assign_ids:
        records.sort(key=lambda pair: (pair[1], pair[0].rel_path))

    used_names: set[str] = set()
    seq_overflow = False
    for n, (it, artist) in enumerate(records, start=1):
        if assign_ids:
            if n > 10 ** seq_width - 1 and not seq_overflow:
                seq_overflow = True
            image_id = make_image_id(artist, n, width=seq_width)
        else:
            image_id = _legacy_id(it)

        if layout == "flat" and assign_ids:
            out_name = f"{image_id}{normalize_ext(it.logical_path or it.rel_path)}"
        elif layout == "mirror":
            out_name = _mirror_name(it, image_id, used=used_names)
        else:
            # 只打标：归档名用平铺名，封包阶段用不到它
            out_name = it.flat_name

        item = TagItem(
            image_id=image_id,
            path=Path(it.source),
            rel=it.rel_path,
            source=it.source,
            member=it.member,
            out_name=out_name,
            artist=artist,
            size=it.size,
        )
        plan.items.append(item)
        plan.total_bytes += it.size

    if seq_overflow:
        raise ConfigError(
            f"序号溢出：本次要编到 {len(plan.items)}，seq_width={seq_width} 只能"
            f"表示到 {10 ** seq_width - 1}。把序号宽度调大（每加 1 位多一个数量级）。")

    # 来源统计：至少让人看到"图是从哪几个包来的"
    by_source: dict[str, dict] = {}
    for (it, _), item in zip(records, plan.items):
        row = by_source.setdefault(it.source, {
            "source": it.source, "name": Path(it.source).name,
            "kind": it.kind, "count": 0,
        })
        row["count"] += 1
    plan.sources = sorted(by_source.values(), key=lambda r: -r["count"])

    if rule.enabled:
        seen: set[str] = set()
        for it in plan.items:
            if it.artist:
                plan.artist_covered += 1
                seen.add(it.artist)
            else:
                plan.no_artist += 1
        plan.artist_count = len(seen)
        if plan.no_artist:
            plan.warnings.append(
                f"{plan.no_artist} 张取不到画师名（路径层级不足或第一层不是画师目录），"
                f"这些图的 id 只有序号")

    plan.id_prefix = make_image_id(plan.items[0].artist, 1, width=seq_width) \
        if plan.items and assign_ids else (plan.items[0].image_id if plan.items else "")
    return plan


# ------------------------------------------------------------------ 封包


def pack_fingerprint(options: PackOptions, layout: str,
                     naming: Mapping[str, object] | None = None) -> str:
    """封包参数指纹。参数一变就换一套记忆文件，避免"改了质量却跳过重封"。

    naming 里放会改变 id 空间的口径（seq_width / 画师开关 / 层级）。它不改
    输出内容的质量，但一改就整套 id 换了名字；如果指纹不带它，旧记忆会把
    "旧 id 已封过"当成"这批也封过"，同一张图就会以两个 id 进到两个分片。
    """
    raw = json.dumps({
        "formats": sorted(options.formats),
        "compress": options.compress,
        "jpeg_quality": options.jpeg_quality,
        "jpeg_subsampling": options.jpeg_subsampling,
        "reencode": options.reencode_mode,
        "max_records": options.max_records,
        "max_bytes": options.max_bytes,
        "layout": layout,
        "naming": dict(naming or {}),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _image_size(data: bytes) -> tuple[int, int]:
    """只读图片头拿宽高（不整幅解码）。坏图返回 (0, 0)，让封包阶段自己报错。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return int(im.width), int(im.height)
    except Exception:                 # noqa: BLE001 - 坏图交给 prepare 去失败
        return 0, 0


# ------------------------------------------------------------------ 磁盘预算


class PackSpaceError(RuntimeError):
    """预计产出装不下。带上具体数字，用户才能决定换盘还是降质量。"""


def _item_name(item: TagItem) -> str:
    return (item.member or item.path.name or "").lower()


def _item_size(item: TagItem) -> int:
    """原始字节数。发现阶段已经 stat 过，这里只补"历史对象没带 size"的缺口。"""
    size = int(getattr(item, "size", 0) or 0)
    if size > 0:
        return size
    if item.member:
        return _DEFAULT_IMAGE_BYTES      # 池里的成员，重新开包就为拿体积不值得
    try:
        return max(0, item.path.stat().st_size)
    except OSError:
        return _DEFAULT_IMAGE_BYTES


def growth_factor(options: PackOptions) -> float:
    """转码后 / 转码前的体积倍数（先验）。

    质量每高 1 档约 +6%，采样每降一档约 -35%。数字来自 tiers.yaml 里
    1024² 实图的实测，不追求精确到每张图——它的用途是"封包前判断盘够不够"，
    差 10% 由 20% 的冗余吸收。
    """
    base = _TRANSCODE_GROWTH.get(int(options.jpeg_subsampling), 2.6)
    return base * (_QUALITY_STEP ** (int(options.jpeg_quality) - _QUALITY_PIVOT))


def estimate_item_bytes(item: TagItem, options: PackOptions, *,
                        growth: float | None = None) -> int:
    """单条目封包后要写多少字节（不含冗余）。

    reencode=never（以及 non_jpeg 下的 JPEG）是原样拷贝，不该乘转码涨幅：
    按 4:4:4 的 2.6× 去估会把"原图直搬"的批次高估一倍多，白白拦住。
    """
    grow = growth_factor(options) if growth is None else growth
    size = _item_size(item)
    mode = options.reencode_mode
    keep = mode == "never" or (
        mode == "non_jpeg" and _item_name(item).endswith((".jpg", ".jpeg")))
    if not keep:
        size = int(size * grow)
    return (size + _PACK_META_BYTES) * max(1, len(options.formats))


def estimate_pack_bytes(items: Sequence[TagItem], options: PackOptions, *,
                        growth: float | None = None) -> int:
    """估算这批条目封包后要写多少字节（**不含**冗余）。

    每个格式都各自存一份图片，所以按格式数乘。已经在 PackState 里的条目
    由调用方先剔掉（它们不会再写）。
    """
    return sum(estimate_item_bytes(item, options, growth=growth) for item in items)


def required_pack_bytes(items: Sequence[TagItem], options: PackOptions, *,
                        growth: float | None = None) -> int:
    """要预留的字节数 = 估算 × (1 + 冗余)。"""
    return int(estimate_pack_bytes(items, options, growth=growth) * (1 + DISK_HEADROOM))


# 每张图的打标产物（json）量级：id + tags 数组 + provenance，实测 1.5~3 KB
_TAG_ARTIFACT_BYTES = 3 * 1024


def estimate_job_bytes(items: Sequence[TagItem],
                       options: PackOptions | None) -> int:
    """一个任务从头到尾要写多少字节：打标产物 + （可选）封包产出。

    任务开跑前用它判"这块盘装不装得下"。打标产物很小，真正吃盘的是封包；
    但两段都要算——只打标、不封包的任务也有产物。
    """
    return len(items) * _TAG_ARTIFACT_BYTES + (
        required_pack_bytes(items, options) if options is not None else 0)


def free_bytes(path: str | Path) -> int:
    """目标盘的剩余字节。读不到就不拦（宁可写到报错，也不要因为读不到盘就停工）。"""
    import shutil

    try:
        return int(shutil.disk_usage(str(path)).free)
    except OSError:
        return 1 << 62


def existing_ancestor(path: str | Path) -> Path:
    """产物目录通常还不存在，盘位要问最近一级存在的父目录。"""
    p = Path(path)
    for candidate in (p, *p.parents):
        if candidate.exists():
            return candidate
    return Path.cwd()


def space_error(need: int, free: int, where: str | Path) -> str:
    """空间不够时给人看的说法：三个数字 + 三条出路。"""
    return (f"预计要写 {_gb(need)}（含 {DISK_HEADROOM:.0%} 冗余），"
            f"但 {where} 所在盘只剩 {_gb(free)}，差 {_gb(need - free)}。"
            f"换输出盘、少选几种格式、或把 JPEG 质量/采样调低；"
            f"确认估算偏保守的话可以关掉空间检查再跑。")


def _gb(n: int) -> str:
    return f"{n / 1024 ** 3:.2f} GB"


@dataclass(slots=True)
class PackReport:
    packed: int = 0
    skipped_done: int = 0       # PackState 里已有（同一套参数下封过）
    skipped_missing: int = 0    # 没有打标产物（失败/未跑到）
    failed: int = 0
    input_bytes: int = 0
    sealed: list[dict] = field(default_factory=list)
    # 非空 = 中途停下的原因（用户停止 / 硬件保护熔断）。调用方据此把任务
    # 状态落成 stopped，而不是"0 条写入但一切正常"。
    stopped: str = ""

    @property
    def total(self) -> int:
        return self.packed + self.skipped_done + self.skipped_missing + self.failed


def _prepare_safe(packer: Packer, kw: dict) -> tuple[object | None, str]:
    try:
        return packer.prepare(**kw), ""
    except Exception as exc:          # noqa: BLE001 - 一张坏图不能毁掉整批
        return None, f"{type(exc).__name__}: {exc}"


def pack_items(
    items: Sequence[TagItem],
    out_dir: str | Path,
    options: PackOptions,
    *,
    layout: str = "flat",
    reader: MemberReader | None = None,
    workers: int = 0,
    worker_name: str = "w",
    model_id: str = "",
    artist_prefix: str = "@",
    inject_artist: bool = False,
    naming: Mapping[str, object] | None = None,
    on_sample: Callable[[TagItem], None] | None = None,
    on_skip: Callable[[TagItem, str], None] | None = None,
    on_seal: Callable[[dict], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    guard=None,
    check_space: bool = True,
) -> PackReport:
    """读打标产物 + 原图，封成配置好的归档。

    顺序与 items 一致（索引里的顺序就是命名顺序）。写盘是串行的，转码在线程
    池里并行——这是 Packer 设计的用法，也是唯一不会把 zip/tar 写串的方式。

    记忆只在**封口之后**才标记：写进缓冲区但还没封包的条目，崩溃后会重新
    写一遍。宁可重做一小段，也不能出现"状态说做过、包里没有"的静默丢图。

    `guard` 可选：天气/显存这类熔断由它管。**磁盘**不走 guard 的固定阈值
    （"剩 30G 就停"对小批量太严、对大批量太松），而是这里按"预计产出 +
    20% 冗余"判：开跑前判一次（装不下就直接失败，什么都不写），跑起来后用
    实测的产出/原图比例复算剩余量。`check_space=False` 可跳过这两道检查。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = PackReport()
    fp = pack_fingerprint(options, layout, naming)
    state = PackState(out_dir / f".packstate-{fp}.jsonl")

    packer = Packer(out_dir, options, worker=worker_name, model_id=model_id,
                    verbose=False)
    workers = workers if workers > 0 else min(8, max(1, os.cpu_count() or 1))
    window_size = max(2, workers * _PACK_WINDOW_FACTOR)
    log = on_log or (lambda _msg: None)

    reader = reader or MemberReader()
    since_seal: list[str] = []

    # ---- 盘位预算（开跑前）。先算一遍"还要写多少"，装不下就直接失败：
    # 什么都不写、重跑代价为零，好过跑到一半才发现。
    pending: list[TagItem] = []
    for item in items:
        if state.done(item.image_id):
            continue
        if not artifact_path(out_dir, item).is_file():
            continue                     # 没产物就不会写盘，不该占预算
        pending.append(item)

    if check_space:
        # 与预览同一口径（estimate_pack_bytes）：按每张的原图字节 × 转码涨幅
        # × 格式数 + 每条元数据，再乘冗余。按 reencode=never 时原样拷贝的
        # 条目不计涨幅——否则"原图直搬"的批次会被高估一倍。
        required = required_pack_bytes(pending, options)
        free = free_bytes(out_dir)
        if required > free:
            raise PackSpaceError(space_error(required, free, out_dir))
        log(f"盘位预算：预计产出 {_gb(required)}（含 {DISK_HEADROOM:.0%} 冗余），"
            f"剩余 {_gb(free)}")

    # 运行时预算：先验估算随写入逐条扣减；每封一个分片，用"该分片实测字节 /
    # 它的先验估算"校正剩余部分。只看剩余量，不重复计算已经写掉的字节。
    space = {"remaining_out": estimate_pack_bytes(pending, options),
             "buffer_est": 0.0, "sealed_est": 0.0, "stop": "", "checked": 0}
    check_every = max(500, window_size * 8)

    def correction() -> float:
        """实测产出 / 先验估算（只统计已封口的样本）。没有样本时按 1.0。"""
        sealed = sum(int(row["bytes"]) for row in report.sealed)
        if space["sealed_est"] > 0 and sealed > 0:
            return max(0.2, min(4.0, sealed / space["sealed_est"]))
        return 1.0

    def consume(item: TagItem, *, written: bool) -> None:
        """一条处理完：从剩余估算里扣掉它；写进去了就累积待实测的估算量。"""
        est = estimate_item_bytes(item, options)
        space["remaining_out"] = max(0, space["remaining_out"] - est)
        if written:
            space["buffer_est"] += est

    def settle_seal() -> None:
        """分片封口：把它那一批的先验估算挪到"已实测"这边。"""
        space["sealed_est"] += space["buffer_est"]
        space["buffer_est"] = 0.0

    def space_check() -> None:
        """够不够写完剩下的。只有"确实不够"才停，估算偏保守不该拦人。"""
        if not check_space or space["stop"]:
            return
        need = int(space["remaining_out"] * correction() * (1 + DISK_HEADROOM))
        free = free_bytes(out_dir)
        if need > free:
            space["stop"] = (f"磁盘预计不足：还需约 {_gb(need)}"
                             f"（含 {DISK_HEADROOM:.0%} 冗余），只剩 {_gb(free)}")
            log(space["stop"])

    def mark_sealed() -> None:
        """把"自上次封口以来写进去的 key"记成已完成。"""
        for key in since_seal:
            state.mark(key)
        since_seal.clear()
        settle_seal()

    def emit_new_packs(before: int) -> None:
        sealed = packer.sealed
        for pack in sealed[before:]:
            row = {"name": pack.name, "count": pack.count,
                   "bytes": pack.bytes, "sha256": pack.sha256}
            report.sealed.append(row)
            if on_seal is not None:
                try:
                    on_seal(row)
                except Exception:     # noqa: BLE001 - 订阅方的 bug 不该打断封包
                    pass

    def flush_window(pool: ThreadPoolExecutor, window: list[tuple[TagItem, dict]]) -> None:
        prepared = list(pool.map(lambda kw: _prepare_safe(packer, kw),
                                 [kw for _, kw in window]))
        for (item, _kw), (sample, error) in zip(window, prepared):
            if should_stop is not None and should_stop():
                return
            if space["stop"]:
                # 预算在窗口内部就爆了：剩下的条目留在未完成状态，下次跑
                # 接着来（已经写进去的照常封口）。不能继续写，写下去就真满了。
                return
            if sample is None:
                report.failed += 1
                consume(item, written=False)
                if on_skip is not None:
                    try:
                        on_skip(item, "failed")
                    except Exception:  # noqa: BLE001
                        pass
                log(f"封包失败 {item.image_id}: {error}")
                continue
            before = len(packer.sealed)
            packer.write(sample)
            since_seal.append(item.image_id)
            report.packed += 1
            consume(item, written=True)
            if on_sample is not None:
                try:
                    on_sample(item)
                except Exception:     # noqa: BLE001
                    pass
            if len(packer.sealed) > before:
                mark_sealed()
                emit_new_packs(before)
                # 分片一封口就有实测比例了：用它校正剩余量，别等写爆了才发现
                space_check()

    try:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="pack") as pool:
            window: list[tuple[TagItem, dict]] = []
            for item in items:
                if should_stop is not None and should_stop():
                    report.stopped = "用户停止"
                    break
                if space["stop"]:
                    report.stopped = space["stop"]
                    break
                if guard is not None:
                    st = guard.before_unit(wait=False)
                    if st.level == "stop":
                        report.stopped = st.describe()
                        log(f"封包中止：{st.describe()}")
                        break
                key = item.image_id
                if state.done(key):
                    report.skipped_done += 1
                    if on_skip is not None:
                        try:
                            on_skip(item, "done")
                        except Exception:  # noqa: BLE001
                            pass
                    continue
                payload = read_artifact(out_dir, item)
                if not payload or payload.get("ok") is not True:
                    report.skipped_missing += 1
                    if on_skip is not None:
                        try:
                            on_skip(item, "missing")
                        except Exception:  # noqa: BLE001
                            pass
                    continue
                try:
                    data = reader.read(item)
                except Exception as exc:  # noqa: BLE001 - 坏成员单独记，不中断
                    report.failed += 1
                    consume(item, written=False)
                    if on_skip is not None:
                        try:
                            on_skip(item, "failed")
                        except Exception:  # noqa: BLE001
                            pass
                    log(f"读图失败 {item.image_id}: {type(exc).__name__}: {exc}")
                    continue
                w, h = _image_size(data)
                artist_tag = f"{artist_prefix}{item.artist}" if item.artist else ""
                tag_text = str(payload.get("tag") or "")
                caption = str(payload.get("caption") or payload.get("nl") or "")
                if inject_artist and artist_tag:
                    # 画师名落到"样本能看到的文本"上，而不只是 json 字段：
                    # 训练侧读的是 .tag / .nl，模型要学到画师绑定就得在文本里。
                    # 关掉它 = 只保留 JSON 里的 artist/artist_tag（做 danbooru
                    # 纯词表的下游会这么选——`@name` 不在词表里）。
                    if tag_text:
                        tag_text = prepend_tag(tag_text, artist_tag)
                    if caption:
                        caption = prepend_tag(caption, artist_tag)
                meta = {
                    "image_id": item.image_id,
                    "artist": item.artist,
                    "artist_tag": artist_tag,
                    "rel_path": item.rel,
                    "member": item.member,
                    "source": Path(item.source).name if item.source else "",
                    "width": w,
                    "height": h,
                    "tags": payload.get("tags") or [],
                    "rating": payload.get("rating") or {},
                    "tag_backend": payload.get("tag_backend") or "",
                    "provenance": payload.get("provenance") or {},
                    "ts": time.time(),
                }
                window.append((item, {
                    "flat_name": item.out_name or f"{item.image_id}.jpg",
                    "image_bytes": data,
                    "meta": meta,
                    "tag_text": tag_text,
                    "nl": caption,
                }))
                report.input_bytes += len(data)
                space["checked"] += 1
                if space["checked"] % check_every == 0:
                    space_check()
                if len(window) >= window_size:
                    flush_window(pool, window)
                    window = []
            if window and not (should_stop is not None and should_stop()):
                flush_window(pool, window)
            # 最后一批里预算爆掉时循环已经走完，没有下一轮来设置停止原因。
            if not report.stopped and space["stop"]:
                report.stopped = space["stop"]
    finally:
        try:
            # 收尾一定要封口并记账：不封的话句柄留在 .tmp 上，下次启动会看到
            # 半个包；封了却不记，重跑会把同样几条再写进一个新包——两种都是
            # 静默丢数据/重复数据。封口 + mark 必须成对出现。
            before = len(packer.sealed)
            packer.close()
            if len(packer.sealed) > before:
                mark_sealed()
                emit_new_packs(before)
        except Exception as exc:      # noqa: BLE001 - 收尾失败不该盖住主错误
            log(f"封包收尾失败：{type(exc).__name__}: {exc}")
        reader.close()
    return report


def plan_stats(plan: Plan, *, options: PackOptions | None = None,
               out_dir: str | Path | None = None) -> dict:
    """给预览接口/CLI 的汇总数据。

    传了 options 就顺带算"预计产出多少、盘够不够"：这是新建页在按下开始
    之前唯一能拦住"跑到一半盘满"的地方，比事后报错便宜得多。
    """
    info = {
        "path": plan.input_path,
        "layout": plan.layout,
        "total": len(plan.items),
        "bytes": plan.total_bytes,
        "sources": plan.sources,
        "artists": {
            "count": plan.artist_count,
            "covered": plan.artist_covered,
            "missing": plan.no_artist,
        },
        "samples": plan.sample(8),
        "id_prefix": plan.id_prefix,
        "warnings": plan.warnings,
    }
    if options is not None or out_dir is not None:
        need = estimate_job_bytes(plan.items, options)
        free = free_bytes(existing_ancestor(out_dir or plan.input_path))
        info["estimate"] = {
            "bytes": need,
            "free": free,
            "ok": need <= free,
            "headroom": DISK_HEADROOM,
        }
    return info
