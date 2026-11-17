"""分片扫描：回答"这一片打得怎么样"，不回答"能不能训练"。

为什么要有它（而不是直接复用 `merge`）：

- `merge.collect` 的产出是"按 image_id 归并后的成品记录"，它要把所有分片
  读进 `SpillMap` 再写一份合并结果——那是**合并**任务的成本模型。分片列表
  页只想看一眼"每片多少行、缺哪一侧、跳过了几张"，按那个量级去扫会慢到没人用。
- 这里的活是**单遍流式**统计：一个文件读一次，边读边累加，内存只留计数器和
  "这一片出现过哪些 image_id"（用于查重，可选关掉）。

一套扫描服务三个消费者：
  1. 分片列表（每片一行数）与批次汇总（`summary`）
  2. 上传提交后的自动校验（`verify`）
  3. 合并时的"哪些分片已经并进去了"（由成品清单 + 校验报告推出来）

★口径必须和 `merge` 一致，否则页面说"齐了"、合并却报缺：
  - 有产物 = tag / nl 至少一侧非空（`artifacts._payload_done` 的判定）
  - 剔除行（`excluded`）算"了结"，不算缺失
  - `missing` 只在给了期望 id 时才计算
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import artifacts as art

# 跳过原因的分桶。键名与 `client.note_skip` 写的 kind 对齐，多一个 `blank`
# 是给老账本用的——早期 skipped.jsonl 没有 kind 字段，那些行全是输入无效。
SKIP_KINDS = ("skipped", "artist", "excused", "excluded")

# 扫描时认识的分片后缀。`.tar`/`.zip` 是**成品包**（webdataset），里面是图片
# 而不是标注行，不该进分片统计——统计的对象始终是 artifacts-* 那种标注分片。
_ARTIFACT_SUFFIXES = (".jsonl", ".parquet")


@dataclass
class ShardReport:
    """一片的统计结果。所有计数都是"行"级别的，不含推断。"""

    name: str
    path: str = ""
    bytes: int = 0
    mtime: float = 0.0
    # 记录总数 = 有效标注 + 已剔除 + 空行，三项互斥且等号必须成立。
    # ★对外就说"记录总数"，不叫"总行数"：坏行（写盘中断半截）也是一条记录，
    #   说"行"会让人以为那是文件的行数，对不上时就去查文件了。
    rows: int = 0
    # 至少一侧有内容的行
    tagged: int = 0
    # 分项：只有 tag / 只有 nl / 两侧都有
    only_tag: int = 0
    only_nl: int = 0
    both: int = 0
    # 剔除行（按 kind 分桶）
    excluded: int = 0
    excluded_kinds: dict[str, int] = field(default_factory=dict)
    # 空行：既没内容也不是剔除行——这是**真问题**，通常是写盘中断
    empty: int = 0
    image_ids: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        return d


def _row_has(row: dict, side: str) -> bool:
    """某一侧有没有内容。

    ★直接复用 `artifacts._payload_done`，**不在这里另写一套判定**。它里面
    有几条容易抄漏的规则：`ok:false` 不算、`{"tag": ""}` 不算、而分类头
    "阈值以上没有标签"是**合法空结果**（要看 provenance 才分得清）。抄一遍
    就一定会漂，漂了的后果是页面说"齐了"、合并却报缺。
    """
    return art._payload_done(side, row.get(side))


def _iter_rows(path: Path):
    """逐行/逐批吐 dict。parquet 走迭代器，不会整表进内存。"""
    if path.suffix.lower() == ".jsonl":
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    # 坏行仍然算"行"，但要它能被看见——写盘中断是最常见的成因
                    yield {"__bad__": True}
                    continue
                if isinstance(obj, dict):
                    yield obj
        return
    if path.suffix.lower() == ".parquet":
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=8192):
            cols = batch.to_pydict()
            n = batch.num_rows
            for i in range(n):
                yield {k: cols[k][i] for k in cols}
        return
    raise ValueError(f"不认识的标注分片后缀：{path.suffix}")


def scan_shard(path: Path, *, keep_ids: bool = False) -> ShardReport:
    """扫一片。`keep_ids` 关掉可以省内存（列表页不需要 id 集合）。"""
    rep = ShardReport(name=path.name, path=str(path))
    try:
        st = path.stat()
        rep.bytes = st.st_size
        rep.mtime = st.st_mtime
    except OSError as exc:
        rep.error = f"{type(exc).__name__}: {exc}"
        return rep

    try:
        for row in _iter_rows(path):
            if row.get("__bad__"):
                rep.rows += 1
                rep.empty += 1
                continue
            rep.rows += 1
            iid = row.get("image_id")
            if iid and keep_ids:
                rep.image_ids.append(str(iid))
            if art.is_excluded_row(row):
                rep.excluded += 1
                kind = str((row.get(art.EXCLUDED_KEY) or {}).get("kind")
                           or "excluded")
                rep.excluded_kinds[kind] = rep.excluded_kinds.get(kind, 0) + 1
                continue
            has_tag = _row_has(row, "tag")
            has_nl = _row_has(row, "nl")
            if has_tag and has_nl:
                rep.both += 1
                rep.tagged += 1
            elif has_tag:
                rep.only_tag += 1
                rep.tagged += 1
            elif has_nl:
                rep.only_nl += 1
                rep.tagged += 1
            else:
                rep.empty += 1
    except Exception as exc:               # noqa: BLE001 - 半截文件也要给出结果
        rep.error = f"{type(exc).__name__}: {exc}"
    return rep


def list_shards(root: str | Path) -> list[Path]:
    """仓库里所有标注分片。只认 artifacts-*，成品包（tar/zip）不在此列。"""
    base = Path(root)
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir()
                  if p.is_file()
                  and p.suffix.lower() in _ARTIFACT_SUFFIXES
                  and p.name.startswith("artifacts-")
                  and not p.name.endswith((".chunks", ".part")))


@dataclass
class ScanResult:
    shards: list[ShardReport] = field(default_factory=list)
    scanned_at: float = 0.0

    def summary(self) -> dict:
        """批次汇总：顶部那张卡片的数。"""
        s = dict(rows=0, tagged=0, only_tag=0, only_nl=0, both=0,
                 excluded=0, empty=0, bytes=0, shards=len(self.shards),
                 kinds={})
        for r in self.shards:
            s["rows"] += r.rows
            s["tagged"] += r.tagged
            s["only_tag"] += r.only_tag
            s["only_nl"] += r.only_nl
            s["both"] += r.both
            s["excluded"] += r.excluded
            s["empty"] += r.empty
            s["bytes"] += r.bytes
            for k, v in r.excluded_kinds.items():
                s["kinds"][k] = s["kinds"].get(k, 0) + v
        return s


def accounting_gap(rows: int, tagged: int, excluded: int, empty: int) -> int:
    """`总数 - (有效 + 剔除 + 空)`。正常必须是 0。

    单独拎出来是因为它是**扫描器自身的健康检查**：非 0 意味着某类行被算了
    两次或者漏了一类——那页面上的所有百分比都不可信，比"少报几张图"严重。
    校验把它记成问题；页面把它直接显示出来（有差值就别装没事）。
    """
    return int(rows) - (int(tagged) + int(excluded) + int(empty))


def scan_dir(root: str | Path, *, keep_ids: bool = False) -> ScanResult:
    res = ScanResult()
    for p in list_shards(root):
        res.shards.append(scan_shard(p, keep_ids=keep_ids))
    res.scanned_at = time.time()
    return res


# ---------------------------------------------------------------- 校验

def verify_shard(path: Path, expected_ids: set[str] | None = None,
                 *, need: str = "") -> dict:
    """上传完成后的校验。返回一份可落盘的报告。

    三个问题分开答，混在一起就没人看得懂：
      - **完整性**：期望的 id 里，有没有既没产物也没剔除记录的（真漏做）
      - **一致性**：同一 id 在这片里出现多次，内容是否一致
      - **产物质量**：空行（写了但没内容）有多少

    `need`：要求哪几侧（"both" / "tag" / "nl"），空 = 不要求。
    """
    rep = scan_shard(path, keep_ids=True)
    out: dict = {
        "shard": rep.name, "path": str(path), "bytes": rep.bytes,
        "rows": rep.rows, "tagged": rep.tagged, "only_tag": rep.only_tag,
        "only_nl": rep.only_nl, "both": rep.both, "excluded": rep.excluded,
        "excluded_kinds": rep.excluded_kinds, "empty": rep.empty,
        "ok": True, "problems": [], "ts": time.time(),
    }
    problems: list[str] = list(out["problems"])

    if rep.error:
        problems.append(f"读取中断：{rep.error}")
    # 三个分项必须正好加总到总数。不相等说明扫描器漏算/重算了一类行，
    # 那时页面上的每个百分比都是错的——这比"少报几张图"严重得多。
    gap = accounting_gap(rep.rows, rep.tagged, rep.excluded, rep.empty)
    out["accounting_gap"] = gap
    if gap:
        problems.append(
            f"分项合计对不上：总数 {rep.rows} ≠ 有效 {rep.tagged} + "
            f"剔除 {rep.excluded} + 空 {rep.empty}")
    if rep.empty:
        problems.append(f"{rep.empty} 行既无内容也不是剔除记录（写盘中断？）")

    # 同一 id 出现多次：内容一致算幂等重传，不一致就是冲突
    seen: dict[str, int] = {}
    for iid in rep.image_ids:
        seen[iid] = seen.get(iid, 0) + 1
    dupes = {k: v for k, v in seen.items() if v > 1}
    if dupes:
        sample = ", ".join(sorted(dupes)[:3])
        problems.append(f"{len(dupes)} 个 image_id 重复出现（如 {sample}）")
    out["duplicates"] = len(dupes)

    if need in ("both", "tag", "nl"):
        lack = 0
        if need == "both":
            lack = rep.only_tag + rep.only_nl
        elif need == "tag":
            lack = rep.only_nl
        else:
            lack = rep.only_tag
        if lack:
            problems.append(f"{lack} 行缺 {need} 侧内容")
        out["missing_side"] = lack

    if expected_ids is not None:
        got = set(seen)
        missing = sorted(set(expected_ids) - got)
        extra = sorted(got - set(expected_ids))
        out["expected"] = len(expected_ids)
        out["missing"] = missing[:200]
        out["missing_count"] = len(missing)
        out["extra_count"] = len(extra)
        if missing:
            problems.append(f"缺 {len(missing)} 张（既无产物也无剔除记录）")

    # 错误级别：真缺 / 读断 / 空行 / 分项对不上 才算不通过；
    # 重复只算警告（幂等重传很常见）。
    hard = bool(rep.error or rep.empty or gap or out.get("missing_count")
                or out.get("missing_side"))
    out["ok"] = not hard
    out["warnings"] = [p for p in problems if not p.startswith((
        "缺 ", "读取中断", "行既无内容", "分项合计"))]
    out["problems"] = problems
    return out


def write_report(report: dict, out_path: Path) -> None:
    """校验报告落到成品旁边（`<name>.verify.json`），原子替换。"""
    tmp = out_path.with_name(out_path.name + ".part")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    import os

    os.replace(tmp, out_path)
