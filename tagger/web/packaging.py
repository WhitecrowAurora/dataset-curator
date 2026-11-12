"""数据打包：分包 / 拼接 / 封包 三条数据物流链路的 web 层。

单独建模块（不进 jobs.py）的理由与 dedup.py 相同：

1. 三条链路都是磁盘 IO 的活，不占 GPU——打标跑着的时候照样可以分包/拼接/
   重封，所以不进任务簿的互斥，也不走 WS 事件流，前端每秒轮询快照。
2. 生命周期不同：没有模型、没有 tag/pack 两阶段，就是"编排领域原语 →
   写产出"一条线。CLI（cmd_split / cmd_merge / cmd_pack）是"原语 + print"，
   这里是"原语 + 回调"，业务规则（尤其 image_id 分配的安全线）两边一致。

同一时刻只允许一个数据任务：分包和封包都在写大文件、拼接在读全量产物，
互相抢磁盘毫无收益。与打标/去重/回传互不排斥。

素材与成员产物全程只读：分包只写 dist 包和新表，拼接只写总表和抽检清单，
封包只在产物目录追加 pack-*。没有任何删除/移动。
"""

from __future__ import annotations

import dataclasses
from itertools import islice
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..storage.discovery import InputScanStopped

LOG_CAP = 200          # 页面日志环形缓冲
RATE_WINDOW = 12.0     # 速率按最近这么多秒的进度估
SUMMARY_CAP = 50       # 摘要里列表字段的上限（缺失 id 之类可能上百万条）

KINDS = ("split", "merge", "pack")


class DataJobStopped(Exception):
    """should_stop 触发的主动停止；DataJob.run 把它翻成 stopped 状态。"""


# ---------------------------------------------------------------- 参数

def _sibling_stem(input_dir: str) -> str:
    """文件输入取 stem，目录输入取名字——与 -tags / -dedup 的命名约定一致。"""
    p = Path(input_dir)
    return p.stem if p.is_file() else p.name


@dataclass(slots=True)
class SplitParams:
    """分包：素材 → dist-*.zip + 分包表（image_id 在这里一次性分配）。"""

    input_dir: str
    out_dir: str = ""            # 默认旁边 <名字>-dist/
    table: str = ""              # 默认旁边 <名字>-shards.json
    mode: str = "count"          # count | bytes
    per_shard: int = 20000
    max_bytes: int = 0
    layout: str = "flat"
    seq_width: int | None = None  # 首批默认 6；续批继承上一批
    start_seq: int = 1
    previous_table: str = ""
    recursive: bool = True
    exclude: str = ""            # 去重清单（dedup-exclude.txt），可空
    limit: int = 0
    verify_pack: bool = True     # 写完回读 manifest 校验
    # 画师对照表（空 = 跟配置）。分包阶段会用它查画师，而画师名是 id 分配的
    # 排序键之一——换表就会换一批 image_id，所以它属于"必须与下游对齐"的口径，
    # 不能只在打标页配。
    artist_table: str = ""

    # 嵌套压缩包：勾上就走流式（边遍历边封包）。代价是 image_id 按"发现
    # 顺序"编，而不是"按画师排序后编"——顺序稳定，但同画师的图不再连号。
    nested: bool = False
    work_dir: str = ""           # 解压临时目录，留空 = 系统临时目录
    passwords: str = ""          # 密码表，每行一个密码，# 开头是注释
    workers: int = 0             # 0 = 自动（核数 - cpu_reserve）
    cpu_reserve: int = 2         # 留几个核给自己用
    mem_reserve_gb: int = 4      # 留多少内存，不够时自动降并发

    # 入包时顺手缩放分辨率。none = 原样入包（一个字节都不重编码）
    resize_mode: str = "none"    # none | longest | shortest | pixels
    resize_value: int = 768      # longest/shortest 是边长；pixels 是百万像素
    jpeg_quality: int = 92

    # 预扫描表：留空 = <输出目录>/scan.jsonl。表存在就复用，换 per_shard
    # 重切不用再解压 4 TB。
    scan_table: str = ""
    verify_n: int = 20           # 抽样核对条数，0 = 跳过

    def resolved_out_dir(self) -> Path:
        return Path(self.out_dir) if self.out_dir else (
            Path(self.input_dir).resolve().parent / f"{_sibling_stem(self.input_dir)}-dist")

    def resolved_table(self) -> Path:
        return Path(self.table) if self.table else (
            Path(self.input_dir).resolve().parent / f"{_sibling_stem(self.input_dir)}-shards.json")

    def validate(self) -> None:
        from ..distribute import resolve_sequence
        resolve_sequence(start=self.start_seq, seq_width=self.seq_width,
                         previous_table=self.previous_table)
        if not self.input_dir:
            raise ValueError("必须给素材目录")
        if not Path(self.input_dir).exists():
            raise ValueError(f"素材路径不存在: {self.input_dir}")
        if self.mode not in ("count", "bytes"):
            raise ValueError("切分方式只能是 count / bytes")
        if self.mode == "count" and self.per_shard < 1:
            raise ValueError("每包张数至少 1")
        if self.mode == "bytes" and self.max_bytes < 1:
            raise ValueError("按字节切分时要给每包大小上限")
        if self.layout not in ("flat", "mirror"):
            raise ValueError("布局只能是 flat / mirror")
        if self.limit < 0:
            raise ValueError("limit 不能是负数")
        # 分包表是"哪个序号给了哪张图"的唯一凭证，写覆盖等于把已发出的
        # image_id 静默指给另一批图（成员产物在 merge 时全部变成内容冲突）。
        if self.resolved_table().is_file():
            raise ValueError(
                f"分包表已存在: {self.resolved_table()}。分第二批素材请换一个表文件名；"
                f"确定要丢弃旧记录就先删掉它。")
        if self.exclude and not Path(self.exclude).is_file():
            raise ValueError(f"去重清单不存在: {self.exclude}")
        if self.passwords and not Path(self.passwords).is_file():
            raise ValueError(f"密码表不存在: {self.passwords}")
        if self.workers < 0:
            raise ValueError("并发数不能是负数")
        if self.cpu_reserve < 0:
            raise ValueError("CPU 保留核数不能是负数")
        if self.mem_reserve_gb < 0:
            raise ValueError("内存保留量不能是负数")
        if self.resize_mode not in ("none", "longest", "shortest", "pixels"):
            raise ValueError("缩放方式只能是 none / longest / shortest / pixels")
        if self.resize_mode != "none" and self.resize_value < 1:
            raise ValueError("缩放目标必须大于 0")
        if self.resize_mode == "pixels" and self.resize_value > 200:
            raise ValueError("像素预算按百万像素算，填 1~200 之间（比如 2 表示 2MP）")
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("JPEG 质量要在 1~100 之间")
        if self.nested and self.work_dir:
            # 早点建、早点炸：跑几小时才发现临时盘没有写权限，代价太大
            try:
                Path(self.work_dir).mkdir(parents=True, exist_ok=True)
                probe = Path(self.work_dir) / ".tagger-write-test"
                probe.write_bytes(b"1")
                probe.unlink()
            except OSError as exc:
                raise ValueError(f"临时解压目录不可写: {self.work_dir}（{exc}）") from None


@dataclass(slots=True)
class MergeParams:
    """拼接：回收成员产物，对账后写总表 + 抽检清单。"""

    roots: list[str] = field(default_factory=list)   # 回收目录，可多个
    shard_dir: str = ""          # 分发包目录（dist-*.zip），逐条对账用
    table: str = ""              # 分包总表，整包缺失检测用
    tables: list[str] = field(default_factory=list)  # 多批分包表
    out: str = ""                # 总表输出，默认 work/merged.jsonl
    fmt: str = "jsonl"           # jsonl | json
    require: str = "both"        # nl | tag | both
    dataset: str = ""            # 数据集名，拼进 image_path
    sample_n: int = 20
    sample_out: str = ""         # 抽检清单，默认 work/sample.md
    keep_empty: bool = False

    def resolved_out(self) -> Path:
        return Path(self.out) if self.out else Path("work/merged.jsonl")

    def resolved_sample_out(self) -> Path:
        return Path(self.sample_out) if self.sample_out else Path("work/sample.md")

    def validate(self) -> None:
        if not self.roots or not any(r.strip() for r in self.roots):
            raise ValueError("至少给一个回收目录")
        if self.fmt not in ("jsonl", "json"):
            raise ValueError("总表格式只能是 jsonl / json")
        if self.require not in ("nl", "tag", "both"):
            raise ValueError("产物要求只能是 nl / tag / both")
        if self.sample_n < 0:
            raise ValueError("抽检张数不能是负数")
        # roots 的存在性不查——"目录没传上来"正是对账要抓的情况，
        # 拼接对不存在的目录照样能跑，结果里会单列"整份没交"。


@dataclass(slots=True)
class PackParams:
    """封包：已打标的目录 → parquet / webdataset / zip 成品（不碰打标）。"""

    input_dir: str               # 图片来源（重新读像素要用）
    out_dir: str = ""            # tags/ 所在的产物目录，默认 <名字>-tags
    formats: list[str] = field(default_factory=lambda: ["parquet"])
    jpeg_quality: int = 95
    jpeg_subsampling: int = 0
    reencode: str = "always"
    compress: str = "stored"
    max_records: int = 5000
    max_bytes: int = 0
    layout: str = "flat"
    seq_width: int = 6
    start_seq: int = 1
    # image_id 口径，必须与打标那次一致，否则产物一个都对不上：
    # False = 本机「新建任务」打的标（id 是相对路径，兼容历史产物）；
    # True = 成员按分包表打标回来的产物（画师-序号全局 id，与 split 分配一致）。
    assign_ids: bool = False
    artist: bool = True
    artist_depth: int = 0
    artist_inject: bool = True
    # 画师对照表（空 = 跟配置）。封包时查表 = 让画师跟着图一起封进去。
    artist_table: str = ""
    include_zips: bool = True
    include_loose: bool = True
    recursive: bool = True
    limit: int = 0
    check_space: bool = True

    def resolved_out_dir(self) -> Path:
        return Path(self.out_dir) if self.out_dir else (
            Path(self.input_dir).resolve().parent / f"{_sibling_stem(self.input_dir)}-tags")

    def validate(self) -> None:
        if not self.input_dir:
            raise ValueError("必须给图片目录")
        if not Path(self.input_dir).exists():
            raise ValueError(f"图片路径不存在: {self.input_dir}")
        if not self.formats or any(f not in ("parquet", "webdataset", "zip")
                                   for f in self.formats):
            raise ValueError("格式只能是 parquet / webdataset / zip")
        if self.reencode not in ("always", "non_jpeg", "never"):
            raise ValueError("重编码只能是 always / non_jpeg / never")
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("JPEG 质量在 90~100 之间（1~100 合法）")
        if self.jpeg_subsampling not in (0, 1, 2):
            raise ValueError("色度采样只能是 0 / 1 / 2")
        if self.compress not in ("stored", "deflate"):
            raise ValueError("zip 压缩只能是 stored / deflate")
        if self.max_records < 1:
            raise ValueError("每包条数至少 1")
        if self.max_bytes < 0:
            raise ValueError("每包大小上限不能是负数")
        if self.layout not in ("flat", "mirror"):
            raise ValueError("布局只能是 flat / mirror")
        if self.limit < 0:
            raise ValueError("limit 不能是负数")


PARAM_CLASSES = {"split": SplitParams, "merge": MergeParams, "pack": PackParams}


def make_params(kind: str, fields: dict) -> "SplitParams | MergeParams | PackParams":
    """端点传来的 dict → 参数对象。未知字段直接拒绝（表单与后端不同步要炸得响）。"""
    cls = PARAM_CLASSES[kind]
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(fields) - names)
    if unknown:
        raise ValueError(f"未知参数：{', '.join(unknown)}")
    try:
        return cls(**fields)
    except TypeError as exc:
        raise ValueError(f"参数不完整：{exc}") from exc


# ---------------------------------------------------------------- 三个 runner
# 签名统一：(params, *, on_progress(done,total,label), should_stop, on_log) -> dict。
# 都是纯编排：领域原语在 distribute.py / merge.py / pipeline.py 里。

def run_split(params: SplitParams, *, on_progress, should_stop, on_log) -> dict:
    params.validate()            # 表已存在等安全线在这里拦，不等到写盘
    if params.nested:
        # 两阶段：进度可算、扫描表可复用、解压能并行。
        # 代价是**多一遍扫描时间**（嵌套包仍要解出来才看得到目录，只是不读字节、
        # 不缩放、不写包），外加一份 scan.jsonl 落在 out_dir（几百万条约几百 MB）。
        return run_split_nested(params, on_progress=on_progress,
                                should_stop=should_stop, on_log=on_log)
    from ..artist import AliasTable, extract_artist
    from ..config import DEFAULT_TIERS, load_ops, load_tier_table
    from ..dedup import load_exclude
    from ..distribute import (DistItem, assign_ids, plan_shards, read_shard_manifest,
                              resolve_sequence, write_shard, write_table)
    from ..pack import discover_inputs

    table = load_tier_table(DEFAULT_TIERS)
    ops = load_ops(table)
    rule = ops.artist_rule()
    rule.enabled = True          # 分发必须知道画师，否则 image_id 里没有归属
    # 表单给了表就用它：画师是 id 的排序键，换表等于换一批 image_id。
    rule.table = (params.artist_table or "").strip() or rule.table

    out_dir = params.resolved_out_dir()
    start, width, shard_start = resolve_sequence(
        start=params.start_seq, seq_width=params.seq_width,
        previous_table=params.previous_table)
    on_log(f"扫描输入 {params.input_dir} …")
    try:
        items = discover_inputs(params.input_dir, recursive=params.recursive,
                                limit=params.limit or None, should_stop=should_stop)
    except InputScanStopped as exc:
        raise DataJobStopped() from exc
    if not items:
        raise ValueError(f"{params.input_dir} 下没有发现图片")
    excluded = 0
    if params.exclude:
        exclude = load_exclude(params.exclude)
        before = len(items)
        items = [it for it in items if it.key not in exclude]
        excluded = before - len(items)
        on_log(f"去重清单：排除 {excluded} 张（{params.exclude}）")
        if not items:
            raise ValueError("排除后没有剩余图片")

    aliases = AliasTable(rule.aliases_path).as_dict() if rule.aliases_path else {}
    from ..artist import ArtistTable

    reader = ArtistTable(rule.table)
    if reader.active and reader.path is not None and reader.path.is_dir():
        # 分包时还没有"分片"这个概念（在分包结束后才知道谁在哪片），
        # 所以分片画师表在这里查不了。说清楚比静默退回目录名强。
        on_log("画师表是分片目录：分包阶段没有分片名可配对，本次退回目录名。"
               "要用画师表请在封包/打标阶段使用，或给全表 parquet。")

    dist_items: list[DistItem] = []
    no_artist = 0
    for it in items:
        if should_stop():
            raise DataJobStopped()
        artist, origin, names = "", "", ()
        if reader.active:
            got = reader.lookup(it.logical_path)
            if got:
                names = tuple(got)
                artist, origin = got[0], "table"
        if not artist:
            a = extract_artist(it.logical_path, rule, aliases)
            artist, origin = a.tag or "", ("path" if a.tag else "")
        if not artist:
            no_artist += 1
        dist_items.append(DistItem(
            source=it.source, member=it.member, rel_path=it.rel_path,
            artist=artist, artist_source=origin, artists=names,
            size=it.size, ext=""))

    assign_ids(dist_items, seq_width=width, start=start)
    plans = plan_shards(dist_items, mode=params.mode,
                        per_shard=params.per_shard, max_bytes=params.max_bytes,
                        start_index=shard_start)
    total_bytes = sum(p.bytes for p in plans)
    on_log(f"共 {len(dist_items)} 张，切成 {len(plans)} 个包"
           + (f"，排除 {excluded} 张" if excluded else "")
           + (f"，{no_artist} 张取不到画师名" if no_artist else ""))
    on_progress(0, len(plans), "写分发包")

    done = 0
    for plan in plans:
        if should_stop():
            raise DataJobStopped()
        write_shard(plan, str(out_dir), layout=params.layout,
                    compress="stored")
        done += 1
        on_progress(done, len(plans), plan.name)

    table_path = write_table(plans, str(params.resolved_table()),
                             next_seq=start + len(dist_items), seq_width=width,
                             start_seq=start)
    on_log(f"分包表写入 {table_path}（{sum(p.count for p in plans)} 张）")

    verify = None
    if params.verify_pack:
        bad = 0
        for plan in plans:
            try:
                rows = read_shard_manifest(out_dir / plan.name)
            except (KeyError, OSError):
                bad += 1
                continue
            if (len(rows) != plan.count
                    or rows[0]["image_id"] != plan.first_id
                    or rows[-1]["image_id"] != plan.last_id):
                bad += 1
        verify = {"checked": len(plans), "bad": bad}
        on_log("回读校验：" + ("全部一致" if not bad else f"{bad} 个包有问题"))

    return {
        "discovered": len(items) + excluded,
        "excluded": excluded,
        "no_artist": no_artist,
        "packages": [{"name": p.name, "count": p.count, "bytes": p.bytes,
                      "first_id": p.first_id, "last_id": p.last_id}
                     for p in plans[:SUMMARY_CAP]],
        "package_count": len(plans),
        "total": sum(p.count for p in plans),
        "bytes": total_bytes,
        "out_dir": str(out_dir),
        "table_path": str(table_path),
        "next_seq": start + len(dist_items),
        "seq_width": width,
        "verify": verify,
    }


def _worker_count(params: SplitParams) -> int:
    """并发数 = 核数 - 保留核数；显式给了 workers 就听它的。

    再按可用内存砍一刀：剩得比 mem_reserve_gb 还少时，宁可慢也不要 OOM——
    跑一半被系统杀掉，前面几小时全白费。
    """
    cpu = os.cpu_count() or 4
    n = params.workers if params.workers > 0 else cpu - max(0, params.cpu_reserve)
    n = max(1, min(n, 32))
    if n > 1 and params.mem_reserve_gb:
        try:
            import psutil
            free_gb = psutil.virtual_memory().available / (1024 ** 3)
            if free_gb < params.mem_reserve_gb:
                n = 1
        except Exception:                     # noqa: BLE001 - 没有 psutil 就不设这道闸
            pass
    return n


def run_split_nested(params: SplitParams, *, on_progress, should_stop, on_log) -> dict:
    """两阶段：先扫描造表并抽样核对，再按容器分给多个 worker 并行处理。

    相比单遍流式，多花一遍扫描的时间，换来三件事：

    - **进度是真的**：总数已知，百分比能算；
    - **表能复用**：换 per_shard 重切不用再解压 4 TB；
    - **解压能并行**：每个 worker 负责一组容器，容器之间互不相干。

    顺序的正确性靠两处保证：扫描按 `iter_files` 的固定遍历顺序落成 seq；
    消费阶段按 chunk 顺序取队列，所以写盘顺序就是 seq 递增。
    """
    import queue

    from ..artist import AliasTable, extract_artist
    from ..config import DEFAULT_TIERS, load_ops, load_tier_table
    from ..containers import PasswordTable, iter_files
    from ..dedup import load_exclude
    from ..distribute import (StreamingShardWriter, resolve_sequence,
                              validate_sequence, write_table)
    from ..imageops import resize_bytes
    from ..naming import IMAGE_SUFFIXES, make_image_id
    from ..scantable import (build_scan, group_by_container, load_table,
                             save_table, verify)

    rule = load_ops(load_tier_table(DEFAULT_TIERS)).artist_rule()
    rule.enabled = True

    start, width, shard_start = resolve_sequence(
        start=params.start_seq, seq_width=params.seq_width,
        previous_table=params.previous_table)

    out_dir = params.resolved_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    aliases = AliasTable(rule.aliases_path).as_dict() if rule.aliases_path else {}
    passwords = PasswordTable.load(params.passwords or None)
    scan_path = Path(params.scan_table) if params.scan_table else (out_dir / "scan.jsonl")

    # ---------------------------------------------------------- 阶段 1：扫描
    # 表存在就直接用——换 per_shard 重切同一批素材，不必再解压 4 TB。
    # 表过期由阶段 2 的核对负责抓，抓不到就整批中止，不留 ID 错位的包。
    entries: list = []
    if scan_path.is_file():
        entries = load_table(scan_path)
        on_log(f"阶段 1/3：复用扫描表 {scan_path}（{len(entries)} 张，"
               f"要重扫请先删掉它）")
    else:
        on_log(f"阶段 1/3：扫描 {params.input_dir}")
        entries = build_scan(
            params.input_dir,
            work_dir=params.work_dir or None,
            passwords=passwords,
            should_stop=should_stop,
            on_log=on_log,
            image_suffixes=IMAGE_SUFFIXES,
            on_progress=lambda done, _total, stage: on_progress(done, 0, stage),
        )
        if not entries:
            raise ValueError("没有发现可用的图片——检查扩展名、嵌套层数或密码表")
        save_table(entries, scan_path)

    excluded = 0
    if params.exclude:
        exclude = load_exclude(params.exclude)
        before = len(entries)
        entries = [e for e in entries if e.vpath not in exclude]
        excluded = before - len(entries)
        on_log(f"去重清单：排除 {excluded} 张（{params.exclude}）")
    if params.limit and len(entries) > params.limit:
        entries = entries[:params.limit]
        on_log(f"limit={params.limit}：只处理前 {len(entries)} 张")
    if not entries:
        raise ValueError("排除后没有剩余图片")

    # 过滤后 seq 会留空洞，所以按 seq 建索引，不能拿列表下标当 seq 用
    next_seq = start + max(e.seq for e in entries)
    validate_sequence(start, width, next_seq - start)
    by_seq = {e.seq: e for e in entries}
    total_bytes = sum(e.size for e in entries)
    on_log(f"扫描就绪：{len(entries)} 张 / {total_bytes / 2 ** 30:.2f} GiB，表 {scan_path}")

    # ---------------------------------------------------------- 阶段 2：核对
    verify_info = None
    if params.verify_n > 0:
        on_log(f"阶段 2/3：抽样核对 {params.verify_n} 条")
        verify_info = verify(
            entries, params.input_dir,
            sample_n=params.verify_n, per_shard=params.per_shard,
            work_dir=params.work_dir or None, passwords=passwords, on_log=on_log)
        on_log(f"核对通过：{verify_info['checked']} 条对得上，"
               f"{verify_info['hashed']} 条算过 sha256")
    else:
        on_log("阶段 2/3：已跳过核对（verify_n=0）")

    # ---------------------------------------------------------- 阶段 3：并行
    groups = sorted(group_by_container(entries).items(), key=lambda kv: kv[1][0].seq)
    workers = max(1, min(_worker_count(params), len(groups)))
    per_worker = max(1, len(entries) / workers)

    chunks: list[list] = []
    cur: list = []
    acc = 0
    for group in groups:
        if cur and acc + len(group[1]) > per_worker and len(chunks) < workers - 1:
            chunks.append(cur)
            cur, acc = [], 0
        cur.append(group)
        acc += len(group[1])
    if cur:
        chunks.append(cur)

    on_log(f"阶段 3/3：单线程写包（{len(groups)} 个容器）" if len(chunks) == 1 else
           f"阶段 3/3：{len(chunks)} 个 worker 并行写包"
           f"（保留 {params.cpu_reserve} 核 / {params.mem_reserve_gb} GB）")

    def produce(chunk: list, box: "queue.Queue") -> None:
        """一个 worker 负责一组容器：重新打开源，按 vpath 认领自己那几条。

        放进队列用带超时的 put：任务被取消时消费端已经退出，生产者不能死等
        一个再也没人取的满队列——那样 join 只能靠超时一个个熬过去。
        """
        def put(item) -> None:
            while True:
                try:
                    box.put(item, timeout=0.5)
                    return
                except queue.Full:
                    if should_stop():
                        return

        try:
            for container, wanted in chunk:
                source = container.partition("::")[0]
                # 重新打开 source 时 vpath 已经是"相对 source"的完整虚拟路径；
                # 只有散图（source 就是那张图）才对不上，退回文件名认一次。
                exact = {e.vpath: e for e in wanted}
                by_name: dict[str, list] = {}
                for e in wanted:
                    by_name.setdefault(Path(e.vpath).name, []).append(e)
                found = 0
                try:
                    for entry in iter_files(source, work_dir=params.work_dir or None,
                                            passwords=passwords):
                        if should_stop():
                            break
                        e = exact.get(entry.vpath)
                        if e is None:
                            # 同名多条是歧义：认错就等于把图挂到别人的 id 上
                            cands = by_name.get(entry.name) or []
                            e = cands[0] if len(cands) == 1 else None
                        if e is None:
                            continue
                        try:
                            with entry.open() as fh:
                                data = fh.read()
                            data, ext = resize_bytes(
                                data, mode=params.resize_mode, value=params.resize_value,
                                quality=params.jpeg_quality, name=entry.name)
                        except Exception as exc:  # noqa: BLE001 - 单张坏了不拖垮整批
                            put(("skip", e.seq, f"{type(exc).__name__}: {exc}"))
                            continue
                        put(("ok", e.seq, (data, ext)))
                        found += 1
                except Exception as exc:          # noqa: BLE001 - 整个容器读不出来
                    for e in wanted:
                        put(("skip", e.seq, f"容器打不开：{exc}"))
                    continue
                if found == 0:
                    for e in wanted:
                        put(("skip", e.seq, "重新遍历时没找到"))
        finally:
            # 无论如何都要给消费端一个结束标记，否则它会在 get() 上等死
            put(("end", 0, None))

    # 先建 writer 再起线程：构造失败时不必去收拾一堆卡在队列上的线程
    writer = StreamingShardWriter(
        out_dir, layout=params.layout, compress="stored",
        per_shard=params.per_shard, max_bytes=params.max_bytes, mode=params.mode,
        start_index=shard_start)

    boxes: list = [queue.Queue(maxsize=8) for _ in chunks]
    threads = [threading.Thread(target=produce, args=(chunk, box), daemon=True)
               for chunk, box in zip(chunks, boxes)]
    for t in threads:
        t.start()

    written = skipped = no_artist = 0
    try:
        # 严格按 chunk 顺序消费：chunk 内 seq 递增、chunk 之间连续，
        # 所以整体 seq 递增，image_id 与表一一对应。
        for box in boxes:
            while True:
                if should_stop():
                    raise DataJobStopped()
                try:
                    kind, seq, payload = box.get(timeout=1.0)
                except queue.Empty:
                    continue            # 只是为了定期回头看取消标记
                if kind == "end":
                    break
                if kind == "skip":
                    skipped += 1
                    on_log(f"跳过 seq={seq}：{payload}")
                    continue
                data, ext = payload
                item = by_seq[seq]
                # 画师必须从**包内路径**提：拿完整 vpath 按 / 切第一段会得到
                # `inner.zip::alice`，image_id 就成了 `inner.zipalice-000001`。
                _, _, member = item.vpath.rpartition("::")
                logical = member or item.vpath
                artist = extract_artist(logical, rule, aliases)
                if not artist.tag:
                    no_artist += 1
                image_id = make_image_id(artist.tag or "", start + seq - 1, width=width)
                writer.add(image_id, data, artist=artist.tag or "",
                           src=item.source, member=member,
                           rel_path=logical, ext=ext, size=len(data))
                written += 1
                if written % 200 == 0:
                    on_progress(written, len(entries), f"已写入 {written} 张")
    finally:
        # 一个共同期限：32 个线程各等 5 秒就要熬两分半，收尾不该这么贵
        deadline = time.monotonic() + 10
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        plans = writer.finish()

    if not plans:
        raise ValueError("一张也没写出来——检查密码表或源文件是否可读")

    total = sum(p.count for p in plans)
    table_path = write_table(plans, str(params.resolved_table()), next_seq=next_seq,
                             seq_width=width, start_seq=start)
    on_log(f"完成：{total} 张 / {len(plans)} 个包，分包表 {table_path}")

    if passwords.failures:
        on_log(f"有 {len(passwords.failures)} 个容器打不开（多半是缺密码）")
        for name, why in list(passwords.failures.items())[:10]:
            on_log(f"  {name} —— {why}")

    return {
        "discovered": len(entries) + excluded,
        "excluded": excluded,
        "skipped": skipped,
        "no_artist": no_artist,
        "packages": [{"name": p.name, "count": p.count, "bytes": p.bytes,
                      "first_id": p.first_id, "last_id": p.last_id}
                     for p in plans[:SUMMARY_CAP]],
        "package_count": len(plans),
        "total": total,
        "bytes": sum(p.bytes for p in plans),
        "out_dir": str(out_dir),
        "table_path": str(table_path),
        "scan_table": str(scan_path),
        "next_seq": next_seq,
        "seq_width": width,
        "verify": verify_info,
        "failed_containers": len(passwords.failures),
    }


def run_merge(params: MergeParams, *, on_progress, should_stop, on_log) -> dict:
    params.validate()
    from ..conflicts import picks_for_merge
    from ..config import DEFAULT_TIERS, load_ops, load_tier_table
    from ..distribute import read_shard_manifest
    from ..merge import (MergeStats, check, collect, collect_provenance,
                         load_shard_plans, provenance_warnings, sample_records,
                         write_merged, write_sample_sheet)

    table = load_tier_table(DEFAULT_TIERS)
    ops = load_ops(table)
    spec = ops.output_spec()
    cfg = ops.merge or {}
    dataset_name = params.dataset or str(cfg.get("dataset_name", ""))
    image_root = str(cfg.get("image_root", "/image"))
    image_ext = str(cfg.get("image_ext", ".jpg"))
    sample_seed = int(cfg.get("sample_seed", 42))
    require = params.require or spec.mode

    # 应产出的 id 从分发包 manifest 汇总；总表负责"整包没交"检测。
    expected: list[str] | None = None
    if params.shard_dir:
        from ..storage.collections import SpillList
        ids = SpillList()
        for shard in sorted(Path(params.shard_dir).glob("dist-*.zip")):
            try:
                ids.extend(r["image_id"] for r in read_shard_manifest(shard))
            except (KeyError, OSError) as exc:
                on_log(f"警告：读不动 {shard.name}: {exc}")
        expected = ids
        on_log(f"分发包列出 {len(expected)} 个 image_id")

    plans: list[dict] | None = None
    if params.tables:
        paths = ([params.table] if params.table else []) + params.tables
        plans = load_shard_plans(paths)
        on_log(f"读取 {len(paths)} 份分包表，共 {len(plans)} 个包")
    elif params.table:
        p = Path(params.table)
        if not p.is_file():
            on_log(f"警告：分包总表不存在: {p}（整包缺失将无法检测）")
        else:
            try:
                plans = load_shard_plans(p)
            except (ValueError, OSError) as exc:
                on_log(f"警告：分包总表读不动 {p}: {exc}")
            else:
                on_log(f"分包总表 {p}：{len(plans)} 个包、"
                       f"{sum(int(x.get('count') or 0) for x in plans)} 条")

    roots = [r.strip() for r in params.roots if r.strip()]
    on_log(f"回收目录：{', '.join(roots)}")
    stats = MergeStats()
    records = collect(
        roots, stats=stats, decisions=picks_for_merge(),
        on_progress=lambda n: on_progress(n, 0, f"已扫 {n} 条产物"),
        should_stop=should_stop,
    )
    if should_stop():
        raise DataJobStopped()       # 半份对账不写盘，重跑从头再扫

    check(records, expected_ids=expected, require=require, stats=stats,
          shard_plans=plans)
    prov = collect_provenance(records, stats=stats)
    prov_warn = provenance_warnings(prov)
    for line in stats.summary_lines():
        on_log(line)

    out_path, written = write_merged(
        records, str(params.resolved_out()),
        dataset_name=dataset_name, image_root=image_root, image_ext=image_ext,
        fmt=params.fmt, require=require, skip_empty=not params.keep_empty)
    on_log(f"总表写入 {out_path}（{written} 条，格式 {params.fmt}）")

    sample_path, sample_count = "", 0
    if params.sample_n:
        samples = sample_records(records, n=params.sample_n, seed=sample_seed)
        sheet = write_sample_sheet(
            samples, str(params.resolved_sample_out()),
            dataset_name=dataset_name, image_root=image_root, image_ext=image_ext)
        sample_path, sample_count = str(sheet), len(samples)
        on_log(f"抽检清单写入 {sheet}（{len(samples)} 条，seed={sample_seed}）")

    whole = [{"name": c.name, "first_id": c.first_id, "last_id": c.last_id,
              "expected": c.expected}
             for c in stats.whole_missing_shards[:SUMMARY_CAP]]
    short = [{"name": c.name, "got": c.got, "expected": c.expected, "short": c.short}
             for c in stats.short_shards[:SUMMARY_CAP]]
    return {
        "expected": stats.expected,
        "found": stats.found_ids,
        "complete": stats.complete,
        "missing_n": len(stats.missing_ids),
        "missing_nl_n": len(stats.missing_nl),
        "missing_tag_n": len(stats.missing_tag),
        "empty_content_n": len(stats.empty_content),
        "duplicates_n": len(stats.duplicates),
        "duplicates": {k: v[:4] for k, v in
                       islice(stats.duplicates.items(), SUMMARY_CAP)},
        "idempotent_n": len(stats.idempotent_repeats),
        "unreadable_n": len(stats.unreadable),
        "unreadable": stats.unreadable[:SUMMARY_CAP],
        "extra_n": len(stats.extra_ids),
        "unassigned_n": len(stats.unassigned_ids),
        "empty_roots": stats.empty_roots[:SUMMARY_CAP],
        "whole_missing": whole,
        "short": short,
        "prov_warnings": prov_warn,
        "failed": stats.failed,
        "summary_lines": stats.summary_lines(),
        "merged_path": str(out_path),
        "written": written,
        "sample_path": sample_path,
        "sample_count": sample_count,
    }


def run_pack(params: PackParams, *, on_progress, should_stop, on_log) -> dict:
    params.validate()
    from ..artist import AliasTable
    from ..config import DEFAULT_TIERS, load_ops, load_tier_table
    from ..pack import PackOptions
    from ..pipeline import PackSpaceError, pack_items, plan_inputs, plan_stats

    table = load_tier_table(DEFAULT_TIERS)
    ops = load_ops(table)
    cfg = ops.archive or {}
    rule = ops.artist_rule()
    rule.enabled = params.artist
    rule.depth = max(0, params.artist_depth)
    rule.table = (params.artist_table or "").strip() or rule.table
    if not params.artist_inject:
        rule.inject_caption = False

    options = PackOptions.from_dict({
        **cfg,
        "formats": list(params.formats),
        "jpeg_quality": params.jpeg_quality,
        "jpeg_subsampling": params.jpeg_subsampling,
        "reencode": params.reencode,
        "compress": params.compress,
        "output_max_records": params.max_records,
        "output_max_bytes": params.max_bytes,
    })

    aliases = AliasTable(rule.aliases_path).as_dict() if rule.aliases_path else {}
    # 画师表跟配置走（tiers.yaml 的 artist.table，可写本地路径或 hf://）。
    # 封包时查表 = 让画师跟着图走：产物里直接带逐图确认的值，下游不必再按
    # 分片名去找索引表——那一步正是"数据一重新切片就全错"的来源。
    from ..artist import ArtistTable

    try:
        plan = plan_inputs(
            params.input_dir,
            recursive=params.recursive,
            include_zips=params.include_zips,
            include_loose=params.include_loose,
            limit=params.limit or 0,
            assign_ids=params.assign_ids,
            layout=params.layout,
            seq_width=params.seq_width,
            start_seq=params.start_seq,
            artist_rule=rule,
            aliases=aliases,
            artist_table=ArtistTable(rule.table),
            should_stop=should_stop,
        )
    except InputScanStopped as exc:
        raise DataJobStopped() from exc
    if not plan.items:
        raise ValueError(f"{params.input_dir} 下没有发现图片")
    info = plan_stats(plan)
    out_dir = params.resolved_out_dir()
    on_log(f"共 {info['total']} 张（{'全局序号' if params.assign_ids else '相对路径'}编号），"
           f"来自 {len(plan.sources)} 个来源；"
           f"产物 {'/'.join(options.formats)}，q{options.jpeg_quality} → {out_dir}")
    on_progress(0, len(plan.items), "封包")

    packed = {"n": 0}

    def count_sample(item) -> None:
        packed["n"] += 1
        on_progress(packed["n"], len(plan.items), item.image_id)

    naming = {"artist": rule.enabled, "artist_depth": rule.depth,
              "seq_width": params.seq_width,
              "artist_inject": bool(rule.enabled and rule.inject_caption)}
    if params.assign_ids and params.start_seq != 1:
        naming["start_seq"] = params.start_seq
    try:
        report = pack_items(
            plan.items, out_dir, options,
            layout=params.layout,
            artist_prefix=rule.prefix,
            inject_artist=bool(rule.enabled and rule.inject_caption),
            inject_artist_tags=bool(rule.enabled and rule.inject_tags),
            naming=naming,
            on_sample=count_sample,
            on_seal=lambda row: on_log(
                f"✓ {row['name']}  {row['count']} 条  {row['bytes'] / 1048576:.1f} MB"),
            on_log=on_log,
            should_stop=should_stop,
            check_space=params.check_space,
        )
    except PackSpaceError as exc:
        # 盘不够是"换个盘再来"的输入问题：一条都不写、不留半成品。
        raise ValueError(f"磁盘不够：{exc}——换输出盘 / 少选格式 / 调低质量后再跑") from exc
    on_log(f"写入 {report.packed} 条，跳过（已封过）{report.skipped_done} 条，"
           f"缺产物 {report.skipped_missing} 条，失败 {report.failed} 条")
    if report.stopped:
        raise DataJobStopped()
    if should_stop():
        raise DataJobStopped()

    return {
        "packed": report.packed,
        "skipped_done": report.skipped_done,
        "skipped_missing": report.skipped_missing,
        "failed": report.failed,
        "sealed": [{"name": s["name"], "count": s["count"], "bytes": s["bytes"]}
                   for s in report.sealed[:SUMMARY_CAP]],
        "sealed_count": len(report.sealed),
        "bytes": sum(s["bytes"] for s in report.sealed),
        "out_dir": str(out_dir),
        "formats": list(options.formats),
        "total_discovered": info["total"],
    }


RUNNERS = {"split": run_split, "merge": run_merge, "pack": run_pack}


# ---------------------------------------------------------------- 任务与管程

class DataJob:
    """一次数据任务（split/merge/pack）的全部状态。读写跨线程，过锁。"""

    def __init__(self, job_id: str, kind: str, params):
        self.id = job_id
        self.kind = kind
        self.params = params
        # pending | running | done | stopped | failed
        self.status = "pending"
        self.error = ""
        self.created = time.time()
        self.started = 0.0
        self.ended = 0.0
        self.stop_flag = threading.Event()
        self.done = 0
        self.total = 0
        self.current = ""
        self.summary: dict | None = None
        self._logs: deque[str] = deque(maxlen=LOG_CAP)
        self._window: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def _on_progress(self, done: int, total: int, label: str) -> None:
        now = time.time()
        with self._lock:
            if done:
                self.done = done
            if total:
                self.total = total
            self.current = label
            if done:
                self._window.append((now, done))
                while len(self._window) > 2 and now - self._window[0][0] > RATE_WINDOW:
                    self._window.popleft()

    def _on_log(self, msg: str) -> None:
        with self._lock:
            self._logs.append(msg)

    def snapshot(self) -> dict:
        with self._lock:
            window = list(self._window)
            logs = list(self._logs)
            done, total, current = self.done, self.total, self.current
            status, error = self.status, self.error
            summary = dict(self.summary) if self.summary else None
        rate = 0.0
        if len(window) >= 2:
            dt = window[-1][0] - window[0][0]
            dd = window[-1][1] - window[0][1]
            if dt > 0 and dd > 0:
                rate = dd / dt
        eta = 0.0
        if rate > 0 and total > done:
            eta = (total - done) / rate
        return {
            "id": self.id,
            "kind": self.kind,
            "params": dataclasses.asdict(self.params),
            "out_dir": _out_dir_of(self.params),
            "status": status,
            "error": error,
            "created": self.created,
            "started": self.started,
            "ended": self.ended,
            "done": done,
            "total": total,
            "current": current,
            "rate": round(rate, 1),
            "eta_s": round(eta, 1),
            "logs": logs[-50:],
            "summary": summary,
        }

    def run(self) -> None:
        self.status = "running"
        self.started = time.time()
        try:
            self.summary = RUNNERS[self.kind](
                self.params,
                on_progress=self._on_progress,
                should_stop=self.stop_flag.is_set,
                on_log=self._on_log,
            )
            if self.kind == "pack":
                if self.summary["failed"]:
                    raise RuntimeError(
                        f"封包失败 {self.summary['failed']} 张，已封成品保留；"
                        "修复后重跑会补做失败项")
                if not (self.summary["packed"] + self.summary["skipped_done"]):
                    raise RuntimeError(
                        f"没有可封包的打标产物（缺少 {self.summary['skipped_missing']} 张）；"
                        "请补齐打标结果并确认编号方式后重跑")
            self.status = "done"
        except DataJobStopped:
            with self._lock:
                self._logs.append("已停止：已写盘的部分保留，重跑从头开始")
            self.status = "stopped"
        except Exception as exc:          # noqa: BLE001 - 任何意外都落到状态里
            self.error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._logs.append(f"失败：{self.error}")
            self.status = "failed"
        finally:
            self.ended = time.time()


def _out_dir_of(params) -> str:
    if isinstance(params, SplitParams):
        return str(params.resolved_out_dir())
    if isinstance(params, PackParams):
        return str(params.resolved_out_dir())
    if isinstance(params, MergeParams):
        return str(params.resolved_out())
    return ""


class DataJobManager:
    """进程级单例（app.py 持有）。分包/拼接/封包彼此一次只跑一个。"""

    def __init__(self) -> None:
        self._jobs: dict[str, DataJob] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, fields: dict) -> DataJob:
        if kind not in KINDS:
            raise ValueError(f"未知的数据任务类型: {kind}（只能是 split/merge/pack）")
        params = make_params(kind, dict(fields or {}))
        params.validate()            # ValueError 带中文原因，端点转 400
        with self._lock:
            running = [j for j in self._jobs.values()
                       if j.status in ("pending", "running")]
            if running:
                raise RuntimeError("已有数据任务在跑（分包/拼接/封包同一时间只跑一个）。"
                                   "等它结束或先停止。")
            job = DataJob(uuid.uuid4().hex[:12], kind, params)
            self._jobs[job.id] = job
        thread = threading.Thread(target=job.run, name=f"data-{kind}-{job.id}",
                                  daemon=True)
        thread.start()
        return job

    def all(self) -> list[DataJob]:
        return list(self._jobs.values())[-20:]

    def get(self, job_id: str) -> DataJob | None:
        return self._jobs.get(job_id)

    def stop(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status not in ("pending", "running"):
            return False
        job.stop_flag.set()
        return True


# app.py 持有的进程级单例。与 MANAGER / DEDUPS / UPLOADS 同一模式。
DATAJOBS = DataJobManager()
