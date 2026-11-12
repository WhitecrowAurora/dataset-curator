"""成员产物的另外两种落盘形态：parquet 分片与 jsonl 分片。

三种形态并存，各有各的场合
--------------------------
JSON 形态（默认）：一张图两个文件 `<id>-tag.json` / `<id>-nl.json`。
    好处是可以逐图校验 payload 判断是否完成；
    单条重跑、单条排查都方便。
    坏处是 3000 万张 = 6000 万个小文件。到了那个量级：
      · merge / verify 一次 rglob + open 就是千万次 syscall；
      · NTFS / 网络盘在千万级 inode 下明显退化；
      · 每个文件至少占一个簇（4KB），1KB 的 json 实际吃掉 4 倍空间。

parquet 形态（`--artifact-format parquet`）：N 行攒成一个分片。
    文件数降 4 个数量级，列式压缩后体积也能减半。
    代价是启动时要逐批验证 payload，再用有效的 image_id 建完成集合。

jsonl 形态（`--artifact-format jsonl`）：同样 N 行攒成一片，但一片是一个
    纯文本文件，一行一条。
    为什么要它：parquet 要 pyarrow（嵌入式环境里是个几十 MB 的可选依赖），
    而分片本身是**不可分割的二进制**——小半截坏掉整片都读不出来，只能靠
    专门的读取器看；jsonl 用标准库就能写，用 less/grep 就能查，坏了也只坏
    那一行。代价是没有列式压缩（体积大约是 parquet 的 1.5~2 倍）和读回时
    每行一次 json.loads。
    怎么选：要交给 HF 直接流式读 / 磁盘紧 → parquet；要人不装依赖也能看、
    要跨机器搬运 → jsonl。

三者读同一份 JSON payload
--------------------------
分片里的 tag / nl 两个字段存的是**和 json 文件完全相同的 JSON 字符串**，
不是拆开的列。理由：merge 的对账逻辑（内容冲突、空内容、溯源）全部建立在
payload 之上，换一套列就等于把那段逻辑复制两份，而两份一定会分叉。
代价是查询时要 json.loads 一次，但那本来就是 merge 要做的。

行结构在两种分片之间逐字段一致（image_id / tag / nl / ts），所以读侧
（merge / report / tag_manage）只写一份解析逻辑，换形态不换结论。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

ARTIFACT_PREFIX = "artifacts-"
ARTIFACT_SUFFIX = ".parquet"
JSONL_SUFFIX = ".jsonl"
COLUMNS = ("image_id", "tag", "nl", "ts")


def _pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "parquet 产物需要 pyarrow：pip install pyarrow"
            "（或改用 --artifact-format json / jsonl）"
        ) from exc
    return pa, pq


def _next_seq(out_dir: Path, suffix: str) -> int:
    """下一个分片号。两个 sink 共用，两种形态各自独立编号。

    序号取现存最大号 + 1，不能用文件个数：中间一片被人工删掉/挪走后，
    个数会回退，新一轮分片与现存片撞号，flush 里的 replace 会把
    那片**还有效的产物**整个覆盖掉。
    """
    seqs: list[int] = []
    for p in out_dir.glob(f"{ARTIFACT_PREFIX}*{suffix}"):
        tail = p.stem.rsplit("-", 1)[-1]
        if tail.isdigit():
            seqs.append(int(tail))
    return (max(seqs) + 1) if seqs else 0


def ensure_available(fmt: str) -> None:
    """起跑前自检：parquet 要 pyarrow，缺了就现在报。

    不检的话第一条 flush 才会炸，而那时已经烧了几千张图的推理时间，报错还
    藏在 worker 的兜底里（"内部异常: RuntimeError: ..."），看起来像个 bug。
    """
    if fmt == "parquet":
        _pyarrow()


_STEM_BAD = '<>:"/\\|?*'


def safe_stem(name: str, *, fallback: str = "batch", limit: int = 60) -> str:
    """分包名 → 能当文件名的主干。

    只做"能不能落盘"的清洗：Windows 那九个非法字符和控制字符换 `_`，空白换
    `_`；**中文保留**——NTFS 支持，日志里也看得出是哪个包，转拼音反而认不出。
    首尾的点/下划线/空格要去掉：`name.` 在 Windows 上会被悄悄改成 `name`，
    那两边算出来的文件名就不一致了。
    """
    buf: list[str] = []
    for ch in str(name or ""):
        if ch in _STEM_BAD or ord(ch) < 32 or ch.isspace():
            buf.append("_")
        else:
            buf.append(ch)
    text = "".join(buf).strip("._ ")
    text = text[:limit].rstrip("._ ")
    return text or fallback


def shard_stem(shard: str) -> str:
    """从来源包路径取一个能当文件名的名字。

    HF 直读时 `TaskRow.shard` 长这样：`hf://org/repo/shard-00001.tar#member`。
    直接拿 stem 会得到 `s#m` 这种名字，所以先砍掉 `#成员`、再砍掉协议头，
    最后取路径末段。取不到就回退成 `multi`（单个包 / 拿不到来源的场景）。
    """
    text = str(shard or "").split("#", 1)[0]
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.replace("\\", "/").rstrip("/")
    name = text.rsplit("/", 1)[-1] if text else ""
    return safe_stem(Path(name).stem if name else "", fallback="multi")


def _row(image_id: str, tag: dict | None, nl: dict | None) -> dict:
    """一行的结构。两种分片共用这一处，换形态之后读侧才不用分叉。

    tag / nl 存 JSON 字符串而不是拆开的列：merge 的对账全建立在 payload 上，
    拆开等于把那段逻辑复制两份。空串表示该侧这一张没产出（例如只跑 tag）。
    """
    return {
        "image_id": str(image_id),
        "tag": json.dumps(tag, ensure_ascii=False) if tag is not None else "",
        "nl": json.dumps(nl, ensure_ascii=False) if nl is not None else "",
        "ts": time.time(),
    }


# 剔除/跳过行的标记键。有它、且 tag/nl 都为空 = "这张确定为无产物"。
#
# 为什么必须写进 jsonl 而不是只记在本机账本里：产物最终是"所有人上传到同一个
# FTP、服务器把所有分片 jsonl 合成一个整体文件"。合成之后本机那些
# skipped.jsonl / excused.jsonl 早就不知道去哪台机器了，只有跟着这行一起走，
# 合并侧才回答得出"这张不是漏做，是输入无效"。
EXCLUDED_KEY = "excluded"


def excluded_row(image_id: str, kind: str, reason: str = "") -> dict:
    """一条"确定为无产物"的记录。kind 取 skipped / excused / excluded。

    tag/nl 留空串是刻意的：下游所有"有内容才算产出"的判定都维持原样，
    不会把一张没有标注的图当成有标注。识别只靠 EXCLUDED_KEY。
    """
    return {
        "image_id": str(image_id),
        "tag": "",
        "nl": "",
        "ts": time.time(),
        EXCLUDED_KEY: {"kind": str(kind or "excluded"),
                       "reason": str(reason or "")[:200]},
    }


def is_excluded_row(row: object) -> bool:
    """这一行是不是"确定无产物"的剔除记录。"""
    return isinstance(row, dict) and bool(row.get("image_id")) and \
        isinstance(row.get(EXCLUDED_KEY), dict)


@dataclass
class ParquetSink:
    """按行攒、按片落。行数到了就写一个分片，写完立刻原子替换。"""

    out_dir: Path
    rows_per_file: int = 2000
    _rows: list[dict] = field(default_factory=list)
    _seq: int = 0

    writer: str = ""
    on_commit: object = None

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0 if self.writer else _next_seq(self.out_dir, ARTIFACT_SUFFIX)

    @property
    def pending(self) -> int:
        return len(self._rows)

    def add(self, image_id: str, tag: dict | None, nl: dict | None,
            shard: str = "") -> None:
        # shard 是给 MultiShardSink 的；parquet 分片按行数切，不按包切，忽略它。
        self._rows.append(_row(image_id, tag, nl))
        if len(self._rows) >= max(1, self.rows_per_file):
            self.flush()

    def flush(self) -> Path | None:
        if not self._rows:
            return None
        pa, pq = _pyarrow()
        table = pa.table({
            "image_id": pa.array([r["image_id"] for r in self._rows], type=pa.string()),
            "tag": pa.array([r["tag"] for r in self._rows], type=pa.string()),
            "nl": pa.array([r["nl"] for r in self._rows], type=pa.string()),
            "ts": pa.array([r["ts"] for r in self._rows], type=pa.float64()),
        })
        prefix = f"{self.writer}-" if self.writer else ""
        final = self.out_dir / f"{ARTIFACT_PREFIX}{prefix}{self._seq:05d}{ARTIFACT_SUFFIX}"
        tmp = final.with_suffix(".parquet.tmp")
        pq.write_table(table, tmp, compression="zstd")
        with tmp.open("r+b") as fh:
            os.fsync(fh.fileno())
        # 原子替换：崩在写一半时不会留下半截分片被 merge 当成"正常产物"。
        tmp.replace(final)
        from .storage.atomic import sync_directory
        sync_directory(final.parent)
        if self.on_commit is not None:
            self.on_commit(final, self._rows)
        self._seq += 1
        self._rows.clear()
        return final

    def close(self) -> None:
        self.flush()


@dataclass
class JsonlSink:
    """按行攒、按片落，一片一个 `.jsonl`。

    接口与 ParquetSink 逐字一致（add / flush / close / rows_per_file）：调用方
    只按"要不要 sink"分支，不按形态分支——多一个分支就多一条没人跑到过的路。

    两种落名方式：

    * `stem=""`（默认）：编号分片 `artifacts-00000.jsonl`，满了换下一号。
      给不关心来源的调用方（脚本、测试、`tagger merge` 之类）用。
    * `stem="dist-01"`：定名 `artifacts-dist-01.jsonl`，**一个包一个文件**。
      打工地常见几十个分包丢进同一个输入目录、写同一个产物目录，按包定名
      回传时才能一人发自己的，也才一眼看得出哪个包打歪了。此时 `rows_per_file`
      仍然按期 flush（内存不涨），但**不换文件**；文件名里的序号也就不存在了。

    `append=True`（定名模式下）表示同名文件已存在就接着往后写：暂停后继续不会
    多出一个 `-00001`。不安全的前提只有一个——`--force` 会重做已完成的图，
    那种情况必须 `append=False`（会退成 `artifacts-<stem>-2.jsonl`，绝不覆盖）。
    """

    out_dir: Path
    rows_per_file: int = 2000
    stem: str = ""
    append: bool = True
    _rows: list[dict] = field(default_factory=list)
    _seq: int = 0
    _path: Path | None = None
    _append_offset: int | None = field(default=None, init=False)
    writer: str = ""
    on_commit: object = None

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # ★定名只看 stem，不能"有 writer 就退成编号分片"：MultiShardSink 恰好
        #   同时传 stem 和 writer（见 _sink_for），旧写法会让"一包一文件"退化，
        #   且 _path 恒为 None，files() 的 glob 与实际落名对不上。
        if self.stem:
            self._path = self._named_path(safe_stem(self.stem))
        else:
            self._seq = 0 if self.writer else _next_seq(self.out_dir, JSONL_SUFFIX)

    def _named_path(self, stem: str) -> Path:
        final = self.out_dir / f"{ARTIFACT_PREFIX}{stem}{JSONL_SUFFIX}"
        if not final.exists() or self.append:
            return final
        n = 2
        while True:
            cand = self.out_dir / f"{ARTIFACT_PREFIX}{stem}-{n}{JSONL_SUFFIX}"
            if not cand.exists():
                return cand
            n += 1

    @property
    def pending(self) -> int:
        return len(self._rows)

    @property
    def path(self) -> Path | None:
        return self._path

    def add(self, image_id: str, tag: dict | None, nl: dict | None,
            shard: str = "") -> None:
        # shard 只有"一包一文件"的 MultiShardSink 用；单文件 sink 忽略它。
        # 参数留着是为了让调用点不必分成两套写法。
        self._rows.append(_row(image_id, tag, nl))
        if len(self._rows) >= max(1, self.rows_per_file):
            self.flush()

    def append_row(self, row: dict) -> None:
        """直接塞一整行（剔除记录用）。走同一个缓冲区，顺序和落盘方式都不变。"""
        self._rows.append(dict(row))
        if len(self._rows) >= max(1, self.rows_per_file):
            self.flush()

    def flush(self) -> Path | None:
        if not self._rows:
            return None
        rows = self._rows
        if self._path is not None:
            data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
            with self._path.open("a+b", buffering=0) as fh:
                if self._append_offset is None:
                    _repair_jsonl_tail(fh)
                    self._append_offset = fh.tell()
                else:
                    # 上次写盘或回滚失败：先撤掉那一批，再重试，避免重复追加。
                    fh.truncate(self._append_offset)
                try:
                    remaining = memoryview(data)
                    while remaining:
                        written = fh.write(remaining)
                        if not written:
                            raise OSError("JSONL 写入未完成")
                        remaining = remaining[written:]
                    os.fsync(fh.fileno())
                except OSError:
                    try:
                        fh.truncate(self._append_offset)
                    except OSError:
                        pass  # 保留 offset 和整批缓冲，下次 flush 继续回滚。
                    raise
            self._append_offset = None
            self._rows.clear()
            return self._path

        prefix = f"{safe_stem(self.stem)}-" if self.stem else ""
        prefix = f"{prefix}{self.writer}-" if self.writer else ""
        final = self.out_dir / f"{ARTIFACT_PREFIX}{prefix}{self._seq:05d}{JSONL_SUFFIX}"
        tmp = final.with_name(final.name + ".tmp")
        # newline="\n" 是硬要求：Windows 上默认的 \r\n 会让"一行多少字节"随平台
        # 漂移，而分片是要在成员机之间搬的，跨平台读回来必须一模一样。
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False))
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        # 原子替换：崩在写一半时不会留下半截分片被 merge 当成"正常产物"。
        tmp.replace(final)
        from .storage.atomic import sync_directory
        sync_directory(final.parent)
        if self.on_commit is not None:
            self.on_commit(final, self._rows)
        self._seq += 1
        self._rows.clear()
        return final

    def close(self) -> None:
        self.flush()


def _repair_jsonl_tail(fh) -> None:
    """只检查末行：丢弃中断的残行，完整但没换行的 JSON 补上换行。"""
    end = fh.seek(0, os.SEEK_END)
    if not end:
        return
    fh.seek(end - 1)
    if fh.read(1) == b"\n":
        return
    pos, chunks = end, []
    while pos:
        start = max(0, pos - 8192)
        fh.seek(start)
        chunk = fh.read(pos - start)
        split = chunk.rfind(b"\n")
        chunks.append(chunk[split + 1:])
        pos = start if split < 0 else start + split + 1
        if split >= 0:
            break
    tail = b"".join(reversed(chunks))
    try:
        json.loads(tail.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        fh.truncate(pos)
    else:
        if fh.write(b"\n") != 1:
            raise OSError("JSONL 末行换行写入失败")
    fh.seek(0, os.SEEK_END)


class MultiShardSink:
    """一包一文件：每个来源包各一个 `artifacts-<包名>.jsonl`。

    不做成"一个 job 一个文件"是有原因的：成员机常常把几十个分发包一起丢进
    输入目录、跑到同一个产物目录。按包切，回传时才能一人发自己的，也才看得
    出哪个包没交、哪个包打歪了。切换包时顺手把上一个包刷盘——崩了只丢当前
    这一包，而不是整批。

    对外接口与单个 sink 一致（add / flush / close / rows_per_file），只是 add
    多一个 `shard` 参数说明这一行来自哪个包。
    """

    def __init__(self, out_dir: str | Path, *, rows_per_file: int = 2000,
                 append: bool = True, writer: str = "", on_commit=None):
        self.out_dir = Path(out_dir)
        self.rows_per_file = max(1, int(rows_per_file))
        self.append = append
        self.writer = writer
        self.on_commit = on_commit
        self._by_shard: dict[str, JsonlSink] = {}
        self._stem_owner: dict[str, str] = {}
        self._current = ""
        self.out_dir.mkdir(parents=True, exist_ok=True)

    @property
    def pending(self) -> int:
        return sum(s.pending for s in self._by_shard.values())

    def files(self) -> list[Path]:
        """本 sink 落过的文件。

        优先用真实路径（sink 自己知道写到哪），glob 只是兜底：早先按
        `artifacts-*-{writer}-*` 去 glob，但 `_stem_for` 从不把 writer 拼进
        文件名，那条模式永远匹配不到，等于这个接口恒返回空。
        """
        live = [s.path for s in self._by_shard.values() if s.path is not None]
        if live:
            return sorted(live)
        return sorted(self.out_dir.glob(f"{ARTIFACT_PREFIX}*{JSONL_SUFFIX}"))

    def _stem_for(self, key: str) -> str:
        stem = shard_stem(key)
        # 不同目录里同名包（两个 `dist-01.zip`）不能并进一个文件。
        owner = self._stem_owner.get(stem)
        if owner is not None and owner != key:
            base, n = stem, 2
            while f"{base}-{n}" in self._stem_owner:
                n += 1
            stem = f"{base}-{n}"
            print(f"[artifacts][提示] 包名重复：{stem} 来自另一个目录，"
                  f"改名为 {ARTIFACT_PREFIX}{stem}{JSONL_SUFFIX}", flush=True)
        self._stem_owner[stem] = key
        return stem

    def _sink_for(self, key: str) -> JsonlSink:
        sink = self._by_shard.get(key)
        if sink is None:
            sink = JsonlSink(self.out_dir, rows_per_file=self.rows_per_file,
                             stem=self._stem_for(key), append=self.append,
                             writer=self.writer, on_commit=self.on_commit)
            self._by_shard[key] = sink
        return sink

    def add(self, image_id: str, tag: dict | None, nl: dict | None,
            shard: str = "") -> None:
        key = str(shard or "")
        if key != self._current:
            # 换包：先把上一个包刷掉，别攒着——批量跑时内存和崩溃半径都小。
            prev = self._by_shard.get(self._current)
            if prev is not None:
                prev.flush()
            self._current = key
        self._sink_for(key).add(image_id, tag, nl)

    def add_excluded(self, image_id: str, kind: str, reason: str = "",
                     shard: str = "") -> None:
        """把一条剔除/跳过记录写进**这个包自己的**文件。

        走 MultiShardSink 而不是回退到单文件 sink：合并时按分片对账，剔除行
        必须和它所属分片的产出待在一起，否则"这一包少了 5 张"永远对不上。
        """
        key = str(shard or "")
        if key != self._current:
            prev = self._by_shard.get(self._current)
            if prev is not None:
                prev.flush()
            self._current = key
        self._sink_for(key).append_row(excluded_row(image_id, kind, reason))

    def flush(self) -> None:
        for sink in self._by_shard.values():
            sink.flush()

    def close(self) -> None:
        self.flush()


def is_artifact_parquet(path: Path) -> bool:
    return path.name.startswith(ARTIFACT_PREFIX) and path.suffix == ARTIFACT_SUFFIX


def is_artifact_jsonl(path: Path) -> bool:
    return path.name.startswith(ARTIFACT_PREFIX) and path.suffix == JSONL_SUFFIX


def is_artifact(path: Path) -> bool:
    """是不是本项目的产物分片（任一形态）。

    靠前缀 + 后缀双重判定：目录里还有 failures.jsonl / excused.jsonl /
    manifest.jsonl / 数据集自己的 *.parquet，它们没有 image_id 字段，误收进来
    只会变成"读到一半报错"或者"一堆读不了的文件"。
    """
    return path.name.startswith(ARTIFACT_PREFIX) and path.suffix in (
        ARTIFACT_SUFFIX, JSONL_SUFFIX)


def iter_parquet_artifacts(path: str | Path) -> Iterator[dict]:
    """逐行读出一个 parquet 分片。列不全时按缺失处理，不报错。"""
    _, pq = _pyarrow()
    pf = pq.ParquetFile(str(path))
    names = [c for c in COLUMNS if c in pf.schema_arrow.names]
    if "image_id" not in names:
        raise ValueError(f"{path} 不是产物分片：没有 image_id 列")
    for batch in pf.iter_batches(batch_size=2048, columns=names):
        for row in batch.to_pylist():
            yield row


def iter_jsonl_artifacts(path: str | Path) -> Iterator[dict]:
    """逐行读出一个 jsonl 分片。

    坏行的处理分两种：**末尾那行没有换行符**是写到一半被打断的残留（比如有人在
    写盘途中 kill 了进程、或者手工 append 时断掉），跳过就算了；**中间的行**坏掉
    则是真的坏了，报出来——静默跳过会让 merge 少几张图而看不出来。
    """
    p = Path(path)
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except ValueError as exc:
                if not line.endswith("\n"):
                    return
                raise ValueError(f"{p}:{lineno}: 不是合法 JSON：{exc}") from exc
            if isinstance(row, dict):
                yield row


def iter_artifact_rows(path: str | Path) -> Iterator[dict]:
    """按后缀分派到对应的读取器。两种分片的行结构逐字段一致。"""
    p = Path(path)
    if p.suffix == JSONL_SUFFIX:
        yield from iter_jsonl_artifacts(p)
    else:
        yield from iter_parquet_artifacts(p)


def find_artifacts(root: str | Path) -> list[Path]:
    """列出目录下全部分片，两种形态混在一起按路径排序。

    同时收两形态是刻意的：换了 --artifact-format 重跑时老片还在，它们仍然
    是**有效产物**，读侧不该因为形态不同就把它们当"没产出"。
    """
    base = Path(root)
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*") if p.is_file() and is_artifact(p))


def _payload_done(kind: str, raw: object) -> bool:
    """续做判定：兼容旧正文，但坏 JSON 和明确失败不能算完成。"""
    obj = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return False
        try:
            obj = json.loads(text)
        except ValueError:
            return text[0] not in '{["'
    if isinstance(obj, str):
        return bool(obj.strip())
    if not isinstance(obj, dict) or obj.get("ok") is False:
        return False
    value = obj.get("tag") if kind == "tag" else (obj.get("caption") or obj.get("nl"))
    if not isinstance(value, str):
        return False
    if value.strip():
        return True
    # 分类头阈值以上没有标签是合法结果；空的 VLM 回复不是。
    prov = obj.get("provenance")
    prov = prov if isinstance(prov, dict) else {}
    backend = obj.get("tag_backend") or prov.get("tag_backend")
    return (kind == "tag" and obj.get("ok") is True
            and (prov.get("mode") == "local-tag"
                 or (bool(backend) and backend != "vlm" and obj.get("tags") == [])))


def _row_done(row: object, require: str | None = None) -> bool:
    """这条记录算不算"做完了"。

    require=None 是历史口径：**任一侧**有有效产物就算做过。它服务的是普通续跑
    （"这张别重做"），那时不挑侧是对的。

    只补一侧时必须传 require（"tag" / "nl"）：派工说这一片要补 tag，而本地只有
    NL 产物，按任一侧口径会判成做完、整片跳过，tag 永远补不上——这正是分项
    账本会被"看起来跑了"骗过去的地方。
    """
    if not (isinstance(row, dict) and bool(row.get("image_id"))):
        return False
    if is_excluded_row(row):
        # 剔除行是"这张已经下过结论了"，续跑不该再捞它出来重打——那正是
        # excused.jsonl 的语义。不认它的话，每次重开任务都会把这些脏图
        # 再跑一遍、再失败一遍。
        return True
    if require in ("tag", "nl"):
        return _payload_done(require, row.get(require))
    return any(_payload_done(kind, row.get(kind)) for kind in ("tag", "nl"))


def load_done_ids(out_dir: str | Path, *, fmt: str = "all") -> set[str]:
    """扫已有分片，收集已经产出过的 image_id。

    这个集合就是分片形态下的"文件是否存在"。

    默认两种形态一起收：续跑只关心"这张做过没有"，与它当初用哪种形态写下无关。
    换了 --artifact-format 重跑时，并集才能让它不重做——重做出来的行会在 merge
    里变成"同一个 id 两份内容不同"，而那是**必须人工裁决**的冲突，比多花点
    启动时间贵得多。

    正文按批读取、验证后立即释放，只保留已完成的 ID；任一侧有效即可跳过。
    """
    if fmt not in ("all", "parquet", "jsonl"):
        raise ValueError(f"fmt 只能是 all / parquet / jsonl，收到 {fmt!r}")
    root = Path(out_dir)
    done: set[str] = set()
    if not root.is_dir():
        return done

    if fmt in ("all", "parquet"):
        files = sorted(root.glob(f"{ARTIFACT_PREFIX}*{ARTIFACT_SUFFIX}"))
        if files:
            try:
                _, pq = _pyarrow()
            except RuntimeError:
                # jsonl 用户没装 pyarrow 不该被历史 parquet 卡住；但那些片确实
                # 读不出来，得说一声——否则表现成"凭空重做一批已完成的图"。
                print(f"[artifacts][警告] 有 {len(files)} 个 parquet 分片，"
                      f"但没装 pyarrow；续跑判定只能忽略它们（其中的图会被重做）")
                files = []
            for path in files:
                pf = pq.ParquetFile(str(path))
                if "image_id" not in pf.schema_arrow.names:
                    continue
                names = [c for c in ("image_id", "tag", "nl") if c in pf.schema_arrow.names]
                for batch in pf.iter_batches(batch_size=2048, columns=names):
                    done.update(str(row["image_id"]) for row in batch.to_pylist()
                                if _row_done(row))

    if fmt in ("all", "jsonl"):
        for path in sorted(root.glob(f"{ARTIFACT_PREFIX}*{JSONL_SUFFIX}")):
            with path.open("rb") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        # 坏行/半行只影响这一条的续跑判定：宁可多打一张，
                        # 也不该让一次启动因为一个残行整个失败。
                        continue
                    if _row_done(obj):
                        done.add(str(obj["image_id"]))
    return done


def parse_payload(kind: str, raw: str) -> dict | None:
    """把分片行里的 JSON 字符串还原成 payload。空串 = 该侧没产出。"""
    text = (raw or "").strip()
    if not text:
        return None
    key = "tag" if kind == "tag" else "caption"
    try:
        obj = json.loads(text)
    except ValueError:
        # 不是 JSON 就按裸文本收下：成员手改过、或别的工具直接写了纯文本。
        # 直接返回 None 会把这些内容当成"没产出"，而它们其实是有内容的。
        return {key: text}
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        return {key: obj}
    return None
