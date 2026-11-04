"""分发包生成与分包表。

要解决的问题
------------
30M 张图、10 个人分担，不能让人自己去目录里挑。必须一次性切好、装上、
写上"这个包负责哪些 image_id"，发下去之后各自照着做。

两个关键决定：

1. **image_id 在分发时分配，本地不再生成。**
   见 naming.py 的说明。分包表就是这个分配的凭证：表里记着
   `第 3 包 → a-040001 ~ a-060000`，合并时按它对账。

2. **排序方式是 image_id 升序，不是文件系统顺序。**
   "图片范围"这个说法只有在全局有序时才成立。按目录遍历顺序切出来的包，
   范围是断裂的，既没法用区间描述，也没法判断中间有没有漏。

按数量还是按大小
----------------
count  每包固定条数。均衡、可预测，适合人多且大家效率差不多的场景。
bytes  每包不超过 N 字节。适合图片体积差异大的库——否则按条数切会出现
       某个包 20000 条却 40GB、另一个包同样条数只有 6GB，前者把人等死。

bytes 模式下照样先按 image_id 排序再累加，所以每个包仍然是一个**连续区间**，
只是区间长度不等。这一点很重要：区间连续才能用"首尾 id"描述一个包。
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .config import ConfigError
from .naming import make_image_id, normalize_ext, sanitize

MANIFEST_NAME = "manifest.jsonl"
TABLE_NAME = "shards.json"


@dataclass(slots=True)
class DistItem:
    """待分发的一张图。image_id 由本模块分配。"""

    source: str          # 原始 zip 路径或散图路径
    member: str          # zip 内的成员名，散图为空
    rel_path: str        # 原始相对路径
    artist: str          # 画师名（原始拼写）
    size: int
    ext: str
    image_id: str = ""

    @property
    def is_zip_member(self) -> bool:
        return bool(self.member)


@dataclass(slots=True)
class ShardPlan:
    """一个分发包的计划。写成表就是为了事后能回答"这批是谁的"。"""

    index: int
    name: str
    first_id: str = ""
    last_id: str = ""
    count: int = 0
    bytes: int = 0
    items: list[DistItem] = field(default_factory=list)

    def to_row(self) -> dict:
        """写进分包表的一行。不含 items——那是内存里的东西，不进表。"""
        return {
            "shard": self.index,
            "name": self.name,
            "first_id": self.first_id,
            "last_id": self.last_id,
            "count": self.count,
            "bytes": self.bytes,
        }


def assign_ids(
    items: Iterable[DistItem],
    *,
    seq_width: int = 6,
    start: int = 1,
) -> list[DistItem]:
    """给每张图分配全局唯一的 image_id。

    先按 (画师, 原名) 排序再编号，好处是同一画师的图在 id 上连续，
    人工抽检时容易一眼看出"这个人的图集中在这里"。
    排序键带上原名是为了结果稳定——同样的输入跑两次必须得到同样的 id，
    否则分包表和实际产物会对不上。
    """
    ordered = sorted(items, key=lambda i: (i.artist, i.rel_path))
    count = len(ordered)
    # 序号宽度是硬上限：6 位装不下第 100 万张时 f"{n:06d}" 会静默变成
    # 7 位字符串，字典序被打乱——merge 按首尾序号对账的分包区间全部失真，
    # 而且产出的 id 已经发出去了，事后改不回来。必须在分配前拦住。
    last_seq = start + count - 1
    if count and last_seq > 10 ** seq_width - 1:
        raise ConfigError(
            f"序号溢出：本次要编到 {last_seq}，seq_width={seq_width} 只能表示到 "
            f"{10 ** seq_width - 1}。请在 configs/tiers.yaml 的 distribute 段"
            f"把 seq_width 调大（每加 1 位多一个数量级），重新 split。")
    for n, it in enumerate(ordered, start=start):
        it.image_id = make_image_id(it.artist, n, width=seq_width)
        it.ext = normalize_ext(it.rel_path or it.member)
    return ordered


def plan_shards(
    items: Sequence[DistItem],
    *,
    mode: str = "count",
    per_shard: int = 20000,
    max_bytes: int = 0,
    prefix: str = "dist",
) -> list[ShardPlan]:
    """按数量或大小切分。返回的每个包都是一个连续 id 区间。

    入参 items 必须已经过 assign_ids（即已排序且带 image_id）。
    顺序是正确性的前提，所以这里再排一次而不是信任调用方。
    """
    if mode not in ("count", "bytes"):
        raise ValueError(f"分发模式只能是 count / bytes，收到 {mode!r}")
    if mode == "bytes" and max_bytes <= 0:
        raise ValueError("bytes 模式必须给 max_bytes")
    if mode == "count" and per_shard <= 0:
        raise ValueError("count 模式必须给 per_shard")

    ordered = sorted(items, key=lambda i: i.image_id)
    plans: list[ShardPlan] = []
    cur: list[DistItem] = []
    cur_bytes = 0

    def flush() -> None:
        nonlocal cur, cur_bytes
        if not cur:
            return
        idx = len(plans)
        plans.append(ShardPlan(
            index=idx,
            name=f"{prefix}-{idx:04d}.zip",
            first_id=cur[0].image_id,
            last_id=cur[-1].image_id,
            count=len(cur),
            bytes=cur_bytes,
            items=list(cur),
        ))
        cur = []
        cur_bytes = 0

    for it in ordered:
        over_count = mode == "count" and len(cur) >= per_shard
        over_bytes = mode == "bytes" and cur and cur_bytes + it.size > max_bytes
        if over_count or over_bytes:
            flush()
        cur.append(it)
        cur_bytes += it.size
    flush()
    return plans


# ------------------------------------------------------------------ 打包


def _read_member(source: str, member: str, pool: "_ZipPool | None" = None) -> bytes:
    if member:
        if pool is not None:
            return pool.read(source, member)
        with zipfile.ZipFile(source, "r") as zf:
            return zf.read(member)
    return Path(source).read_bytes()


class _ZipPool:
    """按路径复用 ZipFile 句柄。

    write_shard 逐成员取图时每个成员重开一次 zip：包内几万成员就是几万次
    打开-解析中央目录-关闭，O(n²) 的固定开销全花在重复解析上。一个包计划
    内的成员都来自同一个 zip，句柄留到整包写完再关。
    """

    def __init__(self) -> None:
        self._open: dict[str, zipfile.ZipFile] = {}

    def read(self, source: str, member: str) -> bytes:
        zf = self._open.get(source)
        if zf is None:
            zf = zipfile.ZipFile(source, "r")
            self._open[source] = zf
        return zf.read(member)

    def close(self) -> None:
        for zf in self._open.values():
            try:
                zf.close()
            except OSError:
                pass
        self._open.clear()


def _mirror_member(rel_path: str, image_id: str, ext: str) -> str:
    """mirror 布局下的成员名：保留原目录树，但去掉一切能穿出包外的路径。

    为什么不能直接拿 `rel_path` 当成员名：它来自磁盘遍历，可能是
    `D:\\素材\\a.jpg` 或 `../lb/a.jpg`。`../` 进了 zip 就是 zip-slip，
    解包时能写到包外；盘符段在 zip 里是个叫 `D:` 的目录，纯属噪音。
    所以只保留"干净的相对路径段"，其他一律丢弃。
    """
    parts = [p for p in str(rel_path or "").replace("\\", "/").split("/")
             if p and p not in (".", "..")]
    # 首段是盘符（`D:`）时丢掉——它是绝对路径残留，不是目录
    if parts and len(parts[0]) == 2 and parts[0][1] == ":":
        parts = parts[1:]
    if not parts:
        return f"{image_id}{ext}"
    return "/".join(parts)


def write_shard(
    plan: ShardPlan,
    out_dir: str | Path,
    *,
    layout: str = "flat",
    compress: str = "stored",
    verbose: bool = True,
) -> Path:
    """把一个计划落成一个 zip 分发包。

    包内布局（`layout` 决定，与产出侧同名同义）：
        flat    <image_id>.jpg      全部平铺。成员按 manifest 顺序读即可，
                                    不关心原目录，适合"只管打标"的批次
        mirror  <rel_path>          保留原始目录树（如 `artist/0001.jpg`）。
                                    成员解包后目录结构就是素材原样，适合还要
                                    按画师分目录、或与产出侧镜像对齐的批次
        manifest.jsonl              本包负责的 image_id 与来源，一行一条；
                                    其中 `file` 恒为**实际成员名**，读侧只看它

    manifest 的 `file` 是读侧唯一依据（`client._load_shard_tasks` 用
    `row["file"]` 取图），所以两种布局都能直接工作，不需要成员侧分支。

    重名保命：mirror 下不同来源可能有同一个相对路径（两个 zip 里都叫
    `a/0001.jpg`），撞名时追加 `-<image_id>`。覆盖掉一张图比目录不好看严重得多。
    """
    layout = str(layout or "flat").lower()
    if layout not in ("flat", "mirror"):
        # 拼错时不能静默按 flat 走：用户会以为镜像生效了（这正是这个参数
        # 之前一直没接上时最难发现的地方）。
        raise ValueError(f"分发布局只能是 flat / mirror，收到 {layout!r}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / plan.name
    tmp = final.with_suffix(".zip.tmp")

    comp = zipfile.ZIP_STORED if compress == "stored" else zipfile.ZIP_DEFLATED
    pool = _ZipPool()
    try:
        with zipfile.ZipFile(tmp, "w", compression=comp, allowZip64=True) as zf:
            rows: list[str] = []
            # 小写比较：zip 内大小写敏感，但 Windows 解包不敏感。宁可多改一个
            # 名字，也不要让成员在自己的机器上解出"少了一张"。
            used = {MANIFEST_NAME.lower()}
            for it in plan.items:
                data = _read_member(it.source, it.member, pool)
                if layout == "mirror":
                    name = _mirror_member(it.rel_path, it.image_id, it.ext)
                else:
                    name = f"{it.image_id}{it.ext}"
                if name.lower() in used:
                    p = Path(name)
                    name = str(p.with_name(
                        f"{p.stem}-{it.image_id}{p.suffix}")).replace("\\", "/")
                used.add(name.lower())
                zf.writestr(name, data)
                rows.append(json.dumps({
                    "image_id": it.image_id,
                    "file": name,
                    "artist": it.artist,
                    "src": it.source,
                    "member": it.member,
                    "rel_path": it.rel_path,
                    "bytes": it.size,
                }, ensure_ascii=False))
            zf.writestr(MANIFEST_NAME, "\n".join(rows) + "\n")
    finally:
        pool.close()

    tmp.replace(final)
    if verbose:
        print(f"[dist] {final.name}  {plan.count} 张  "
              f"{plan.bytes / 1073741824:.2f} GB  "
              f"{plan.first_id} ~ {plan.last_id}")
    return final


def write_table(plans: Sequence[ShardPlan], out_path: str | Path, *,
                next_seq: int | None = None) -> Path:
    """写出分包总表。总机留一份，用来回答"这批图在谁手上"。

    next_seq 记录下一批分发该从哪个序号开始——image_id 只保证同一张表内
    唯一，分两批切素材时第二批不带 --start 接着编，必然撞号。
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "shards": [p.to_row() for p in plans],
        "total_count": sum(p.count for p in plans),
        "total_bytes": sum(p.bytes for p in plans),
    }
    if next_seq is not None:
        payload["next_seq"] = int(next_seq)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    return out


def table_next_seq(path: str | Path) -> int:
    """读总表里记录的下一批起始序号。老表没有这个字段时返回 0（调用方自行兜底）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if isinstance(data, dict):
        try:
            return int(data.get("next_seq") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def load_table(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_shard_manifest(shard_path: str | Path) -> list[dict]:
    """读分发包内的 manifest。成员本地处理前先读它拿 image_id 顺序。"""
    with zipfile.ZipFile(shard_path, "r") as zf:
        if MANIFEST_NAME not in zf.namelist():
            raise KeyError(f"{Path(shard_path).name} 里没有 {MANIFEST_NAME}")
        raw = zf.read(MANIFEST_NAME).decode("utf-8")
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def artist_dir_name(artist: str) -> str:
    """画师名转目录名时的统一口径，产出与核对都要用同一个。"""
    return sanitize(artist) if artist else ""
