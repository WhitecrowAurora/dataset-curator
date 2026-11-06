"""预扫描表：先把"有哪些图、各多大、来自哪个容器"落盘，再按核分配处理。

为什么值得多扫一遍：

**1. 进度才是真的。** 流式处理时总数未知，只能报"已写入 N 张"；有了表，
百分比、剩余时间、总字节全都算得出来。

**2. 表能复用到下一次切包。** 换 per_shard 重切同一批素材，不用再解压 4 TB。

**3. 断了能接。** 4 TB 的活儿跑一半断掉是常态，按容器记已完成，重跑从断点继续。

代价是预扫描本身要解压一遍（zip 的目录在文件末尾，deflate 的内层包必须整个
解出来才看得到）。但两遍都能并行，总时间仍然比"单核跑一遍"短。

**表里存的是顺序，顺序就是 ID。** 所以表一旦生成，处理阶段必须严格按 seq
消费——这也是 `verify()` 存在的理由：抽样回读，确认表里的 vpath/size 和真实
内容还对得上。
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = 1


@dataclass(slots=True)
class ScanEntry:
    seq: int              # 全局序号，1 起；处理阶段按它排序消费
    vpath: str            # 相对 source 的虚拟路径：内层.zip::图.jpg
    source: str           # 最外层真实文件（顶层包，或散图本身）
    container: str        # source::内层链，同一容器的条目解压一次就够
    size: int
    artist: str = ""

    def to_line(self) -> str:
        return json.dumps({
            "seq": self.seq, "vpath": self.vpath, "source": self.source,
            "container": self.container, "size": self.size,
            "artist": self.artist,
        }, ensure_ascii=False)


def container_of(source: str, vpath: str) -> str:
    """最内层容器的定位键。

    必须带上 source：顶层压缩包的成员在 vpath 里**不带包名**（`alice/1.jpg`），
    光看 vpath 既不知道它来自哪个包，也没法重新打开核对。
    """
    if "::" in vpath:
        return f"{source}::{vpath.rsplit('::', 1)[0]}"
    return source


def build_scan(root: str | Path, *,
               work_dir: str | Path | None = None,
               passwords: Any = None,
               max_depth: int = 16,
               should_stop: Any = None,
               on_progress: Any = None,
               on_log: Any = None,
               image_suffixes: tuple[str, ...] = (),
               ) -> list[ScanEntry]:
    """扫一遍，只记元数据，不读图片字节。

    顺序就是 `iter_files` 的顺序——处理阶段必须复现同一个顺序，否则 seq 与
    实际图片错位，ID 全乱。
    """
    from .containers import iter_files

    entries: list[ScanEntry] = []
    seq = 0
    for entry in iter_files(root, work_dir=work_dir, passwords=passwords,
                            max_depth=max_depth, should_stop=should_stop,
                            on_log=on_log):
        if image_suffixes and not entry.name.lower().endswith(image_suffixes):
            continue
        seq += 1
        entries.append(ScanEntry(
            seq=seq,
            vpath=entry.vpath,
            source=entry.source or str(root),
            container=container_of(entry.source or str(root), entry.vpath),
            size=entry.size,
        ))
        if on_progress and seq % 500 == 0:
            on_progress(seq, 0, f"已扫描 {seq} 张")
    return entries


def save_table(entries: list[ScanEntry], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"schema": SCHEMA, "count": len(entries)},
                            ensure_ascii=False) + "\n")
        for e in entries:
            fh.write(e.to_line() + "\n")
    tmp.replace(out)
    return out


def load_table(path: str | Path) -> list[ScanEntry]:
    entries: list[ScanEntry] = []
    with open(path, encoding="utf-8") as fh:
        head = fh.readline().strip()
        if not head:
            raise ValueError(f"扫描表是空的: {path}")
        meta = json.loads(head)
        if meta.get("schema") != SCHEMA:
            raise ValueError(f"扫描表版本不认识: {meta.get('schema')}，期望 {SCHEMA}")
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            source = row.get("source") or ""
            entries.append(ScanEntry(
                seq=int(row["seq"]), vpath=row["vpath"], source=source,
                container=row.get("container") or container_of(source, row["vpath"]),
                size=int(row.get("size") or 0), artist=row.get("artist") or "",
            ))
    return entries


def group_by_container(entries: list[ScanEntry]) -> dict[str, list[ScanEntry]]:
    """按最内层容器分组——一个容器只解压一次，组内顺序保持。"""
    groups: dict[str, list[ScanEntry]] = {}
    for e in entries:
        groups.setdefault(e.container, []).append(e)
    return groups


def _pick_samples(entries: list[ScanEntry], *, sample_n: int,
                  per_shard: int) -> list[ScanEntry]:
    """抽样：包边界优先，其余随机。

    边界是最容易错的地方——第 per_shard 张和第 per_shard+1 张分别落在两个包
    的头尾，一旦遍历顺序和表不一致，最先错的就是它们。纯随机抽样反而容易
    漏掉。
    """
    picked: dict[int, ScanEntry] = {}
    if per_shard > 0:
        for i in range(0, len(entries), per_shard):
            picked[i] = entries[i]                       # 每包第一张
            if i + per_shard - 1 < len(entries):
                picked[i + per_shard - 1] = entries[i + per_shard - 1]   # 每包最后一张
    if entries:
        picked[0] = entries[0]
        picked[len(entries) - 1] = entries[-1]

    pool = [(i, e) for i, e in enumerate(entries) if i not in picked]
    rng = random.Random(0xC0FFEE)      # 固定种子：同样的表抽查同样的位置
    need = max(0, sample_n - len(picked))
    if pool and need:
        for i, e in rng.sample(pool, min(need, len(pool))):
            picked[i] = e
    return [picked[i] for i in sorted(picked)]


def verify(entries: list[ScanEntry], root: str | Path, *,
           sample_n: int = 20, per_shard: int = 0,
           work_dir: str | Path | None = None,
           passwords: Any = None,
           check_bytes: int = 3,
           on_log: Any = None) -> dict:
    """抽样回读，确认表里的记录和真实内容对得上。

    抽中的条目按容器分组，每个容器只重新解压一次；先比 vpath 序列和大小，
    再挑几条真的读出字节算 sha256。任何一处对不上就抛异常——**宁可不跑，
    也不能产出一批 ID 错位的分发包**，那种错误要到成员交回来做 merge 时
    才会暴露，代价是整批重做。
    """
    from .containers import iter_files

    if not entries:
        raise ValueError("扫描表是空的，没什么可核对")

    samples = _pick_samples(entries, sample_n=sample_n, per_shard=per_shard)
    by_container = group_by_container(samples)

    checked = 0
    mismatched: list[str] = []
    hashed = 0
    hash_targets = {id(e) for e in samples[:check_bytes]}

    for container, wanted in by_container.items():
        # container = "<source>" 或 "<source>::<内层链>"
        source = container.partition("::")[0]
        real = Path(source)
        if not real.exists():
            checked += len(wanted)
            mismatched.append(f"{container}: 源文件不在了（{source}）")
            continue

        wants = {w.vpath: w for w in wanted}
        # 散图的 source 就是那张图本身：重扫只剩文件名，表里却带目录前缀
        # （`loose/c.jpg`）。同名候选唯一时才敢认，否则宁可报错。
        name_count: dict[str, int] = {}
        for w in wanted:
            name = Path(w.vpath).name
            name_count[name] = name_count.get(name, 0) + 1
        by_name = {Path(w.vpath).name: w for w in wanted if name_count[Path(w.vpath).name] == 1}

        seen_ids: set[int] = set()
        # 必须在遍历当中读字节：嵌套成员是靠临时文件撑着的，推进到下一个容器
        # 之后那个文件就删了，事后再 open() 只会拿到"文件不存在"。
        for entry in iter_files(real, work_dir=work_dir, passwords=passwords):
            want = wants.get(entry.vpath) or by_name.get(entry.name)
            if want is None or id(want) in seen_ids:
                continue
            seen_ids.add(id(want))
            checked += 1
            if entry.size != want.size:
                mismatched.append(
                    f"{want.vpath}: 大小对不上（表 {want.size}，实际 {entry.size}）")
                continue
            if id(want) in hash_targets:
                try:
                    with entry.open() as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()[:16]
                    hashed += 1
                    if on_log:
                        on_log(f"抽样校验 {want.vpath} sha256={digest}")
                except Exception as exc:            # noqa: BLE001
                    mismatched.append(f"{want.vpath}: 读不出来（{exc}）")

        for want in wanted:
            if id(want) not in seen_ids:
                checked += 1
                mismatched.append(f"{want.vpath}: 重新扫描时找不到这一条")

    if mismatched:
        detail = "\n".join(f"  - {m}" for m in mismatched[:10])
        raise ValueError(
            f"扫描表核对失败：{len(mismatched)}/{checked} 条对不上。"
            f"表与实际内容已经不一致，继续跑只会产出 ID 错位的包。\n{detail}")

    return {"checked": checked, "hashed": hashed, "containers": len(by_container)}
