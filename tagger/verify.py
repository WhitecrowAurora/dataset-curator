"""产物完整性复核。

为什么需要单独一层
------------------
打标最怕的不是"标得不准"，而是"标丢了没人知道"。后者更隐蔽：

- 模型偶尔返回空 content，JSON 变成 `{}`，写进归档后看着是条正常记录；
- 请求超时被吞掉，那一张图就静默消失了，总数少几条根本看不出来；
- 归档时某个成员没写进去，tar 里只有图没有 nl。

这些情况都不会抛异常，流水线会"成功结束"。等到训练时发现某个画师少了一半
数据，回头已经分不清是没打还是打丢了。

所以复核的口径是**对账**，不是抽样检查：输入有多少张，输出就得有多少条，
每条都得有非空内容。宁可现在报错，也不要让缺口流到下游。

复核三个层次：
    1. 数量对账   输入 N 张 vs 输出 M 条，缺失的列出名字
    2. 内容非空   nl / tag 按模式检查，空的就是错
    3. 结构完整   json 能解析、带样本标识、token 落在预算内
"""

from __future__ import annotations

import json
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# 一个样本可能由这些成员组成。图片扩展名不固定，只按「已知文本成员」识别。
TEXT_MEMBERS = (".json", ".nl", ".tag", ".caption", ".txt")

# 归档自带的索引文件，不是样本成员。
# 不排除掉的话，zip 里的 index.json 会被当成一个叫 "index" 的样本：
# 它有合法 json、没有 nl/tag，于是每次复核都会凭空多出一条"缺内容"的错误。
META_MEMBERS = ("index.json", "manifest.jsonl", ".packstate.jsonl")


def _is_meta_member(full: str) -> bool:
    base = full.rsplit("/", 1)[-1]
    return base in META_MEMBERS or base.startswith(".")


@dataclass(slots=True)
class Issue:
    kind: str        # missing | empty | bad_json | no_nl | no_tag | duplicate | out_of_budget
    detail: str
    level: str = "error"   # error | warn


@dataclass(slots=True)
class VerifyReport:
    """复核结果。字段刻意保持扁平，便于直接打进日志或 json 报告。"""

    scanned: int = 0                 # 读到的样本数
    ok: int = 0
    issues: list[Issue] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)      # 输入有、输出没有
    extra: list[str] = field(default_factory=list)        # 输出有、输入没有
    counts: dict[str, int] = field(default_factory=dict)  # 按 kind 汇总
    shards: int = 0

    @property
    def failed(self) -> bool:
        return bool([i for i in self.issues if i.level == "error"])

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)
        self.counts[issue.kind] = self.counts.get(issue.kind, 0) + 1

    def summary_lines(self) -> list[str]:
        lines = [
            f"扫描分片   : {self.shards} 个",
            f"读到的样本 : {self.scanned}",
            f"合格       : {self.ok}",
        ]
        if self.missing:
            lines.append(f"缺失       : {len(self.missing)}（输入有、输出没有）")
        if self.extra:
            lines.append(f"多出       : {len(self.extra)}（输出有、输入没有）")
        for kind, n in sorted(self.counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {kind:<14} {n}")
        return lines


# ------------------------------------------------------------------ 读取


def _iter_tar(path: Path) -> Iterator[tuple[str, dict[str, bytes]]]:
    """流式产出一条样本的全部成员。

    用 r| (流式) 而不是 r：分片动辄几 GB，seek 模式会把索引全读进内存。
    WebDataset 的成员本来就是按样本顺序写的，顺序读完全够用。
    """
    stem: str | None = None
    buf: dict[str, bytes] = {}

    with tarfile.open(path, "r|") as tf:
        for member in tf:
            if not member.isfile():
                continue
            # 用完整成员名而不是 basename：mirror 布局下 tar 内有目录层级，
            # 取 basename 会让不同画师下的同名文件被并成一条。
            full = member.name.replace("\\", "/")
            if _is_meta_member(full):
                continue
            dot = full.rfind(".")
            if dot <= 0:
                continue
            cur, suffix = full[:dot], full[dot:]
            if suffix not in TEXT_MEMBERS:
                # 图片成员：划分样本边界。即使上一条**一个文本成员都没有**
                # 也要吐出去——"只有图没有 json"正是要报的错，
                # 在这里把它悄悄 skip 掉，复核就永远看不见这类样本。
                if stem is not None and cur != stem:
                    yield stem, buf
                    stem, buf = cur, {}
                elif stem is None:
                    stem, buf = cur, {}
                continue
            if cur != stem:
                if stem is not None:
                    yield stem, buf
                stem, buf = cur, {}
            handle = tf.extractfile(member)
            if handle is not None:
                buf[suffix] = handle.read()
    if stem is not None:
        yield stem, buf


def _iter_zip(path: Path) -> Iterator[tuple[str, dict[str, bytes]]]:
    stem: str | None = None
    buf: dict[str, bytes] = {}
    with zipfile.ZipFile(path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            full = info.filename.replace("\\", "/")
            if _is_meta_member(full):
                continue
            dot = full.rfind(".")
            if dot <= 0:
                continue
            cur, suffix = full[:dot], full[dot:]
            if suffix not in TEXT_MEMBERS:
                if stem is not None and cur != stem:
                    yield stem, buf
                    stem, buf = cur, {}
                elif stem is None:
                    stem, buf = cur, {}
                continue
            if cur != stem:
                if stem is not None:
                    yield stem, buf
                stem, buf = cur, {}
            buf[suffix] = zf.read(info)
    if stem is not None:
        yield stem, buf


def _payload_text(raw: str, field: str) -> str:
    """列值可能是纯文本，也可能是整段 JSON 载荷。

    两种产物形态都要能复核：
    - `tagger pack` 的 `tag` / `caption` 列是纯文本；
    - VLM 客户端写的成员产物（`artifacts.ParquetSink`）把整个 payload
      json.dumps 进 `tag` / `nl` 列，正文在 `{"tag": ...}` / `{"caption": ...}` 里。

    不区分的话，直接拿 JSON 那段当正文：只有 NL 的批次会被判成"缺 NL"——
    数据好着，是复核口径读错了。
    """
    text = (raw or "").strip()
    if not text.startswith("{"):
        return text
    try:
        obj = json.loads(text)
    except ValueError:
        return text
    if not isinstance(obj, dict):
        return text
    return str(obj.get(field) or "").strip()


def _iter_parquet(path: Path) -> Iterator[tuple[str, dict[str, bytes]]]:
    """parquet 分片（Packer 的默认格式）。复核必须能读它，否则主格式没人验。

    按 row group 迭代而不是 read_table：一个分片几百万行时整表读入会
    直接把进程顶死，而复核只需要逐条看一眼。图片列不读——复核看的是文本。
    """
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            "这个目录里有 parquet 分片，但没装 pyarrow，复核读不了。"
            "装一下：pip install pyarrow"
        ) from exc

    pf = pq.ParquetFile(path)
    names = set(pf.schema_arrow.names)
    cols = [c for c in ("image_id", "caption", "nl", "tag") if c in names]
    if "image_id" not in cols:
        return
    for batch in pf.iter_batches(batch_size=512, columns=cols):
        for row in batch.to_pylist():
            image_id = str(row.get("image_id") or "").strip()
            if not image_id:
                continue
            # 还原成与 tar/zip 一致的成员视图，下游不必区分三种格式。
            members: dict[str, bytes] = {
                ".json": json.dumps({"name": f"{image_id}.jpg", "key": image_id},
                                    ensure_ascii=False).encode("utf-8"),
            }
            caption = _payload_text(str(row.get("caption") or ""), "caption")
            if not caption:
                caption = _payload_text(str(row.get("nl") or ""), "caption")
            tag = _payload_text(str(row.get("tag") or ""), "tag")
            if caption:
                members[".nl"] = caption.encode("utf-8")
            if tag:
                members[".tag"] = tag.encode("utf-8")
            yield image_id, members


SHARD_SUFFIXES = (".tar", ".zip", ".parquet")


def list_shards(out_dir: str | Path) -> list[Path]:
    """归档目录下所有可复核的分片。三种格式一视同仁。"""
    root = Path(out_dir)
    found: list[Path] = []
    for suffix in SHARD_SUFFIXES:
        found.extend(root.glob(f"*{suffix}"))
    return sorted(found)


def iter_samples(out_dir: str | Path) -> Iterator[tuple[str, dict[str, bytes]]]:
    """遍历归档目录下所有分片的样本。"""
    for path in list_shards(out_dir):
        if path.suffix == ".tar":
            yield from _iter_tar(path)
        elif path.suffix == ".zip":
            yield from _iter_zip(path)
        else:
            yield from _iter_parquet(path)


# ------------------------------------------------------------------ 检查


def verify_sample(
    stem: str,
    members: dict[str, bytes],
    *,
    mode: str = "both",
    nl_min_tokens: int = 0,
    nl_max_tokens: int = 0,
) -> list[Issue]:
    """检查一条样本。返回它的问题列表，空列表表示通过。"""
    issues: list[Issue] = []

    raw = members.get(".json")
    if raw is None:
        issues.append(Issue("bad_json", f"{stem}: 缺 .json 成员"))
        return issues

    try:
        meta = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        issues.append(Issue("bad_json", f"{stem}: .json 无法解析"))
        return issues

    if not isinstance(meta, dict) or not meta:
        # 这才是"JSON 变空"的典型形态：模型或中间层把结构吃了
        issues.append(Issue("bad_json", f"{stem}: .json 为空对象"))
        return issues

    nl = members.get(".nl", b"").decode("utf-8", "replace").strip()
    tag = members.get(".tag", b"").decode("utf-8", "replace").strip()

    if mode in ("nl", "both") and not nl:
        issues.append(Issue("no_nl", f"{stem}: 缺 NL 内容"))
    if mode in ("tag", "both") and not tag:
        issues.append(Issue("no_tag", f"{stem}: 缺 tag 内容"))

    if nl and (nl_min_tokens or nl_max_tokens):
        # 归档里没有 tokenizer，用字符数粗估下限：英文约 3.5~4 字符/token，
        # 这里取保守的 3，宁可少报也不误伤。真正的 token 数应在写入时
        # 由 usage.completion_tokens 记录在 meta 里。
        recorded = int(meta.get("nl_tokens") or 0)
        if recorded:
            if nl_min_tokens and recorded < nl_min_tokens:
                issues.append(Issue("out_of_budget",
                                    f"{stem}: NL {recorded} token < 下限 {nl_min_tokens}",
                                    level="warn"))
            if nl_max_tokens and recorded > nl_max_tokens:
                issues.append(Issue("out_of_budget",
                                    f"{stem}: NL {recorded} token > 上限 {nl_max_tokens}",
                                    level="warn"))

    return issues


def collect_names(out_dir: str | Path) -> set[str]:
    """收集产物里出现过的样本名（meta.name），用于和输入对账。"""
    names: set[str] = set()
    for stem, members in iter_samples(out_dir):
        raw = members.get(".json")
        if not raw:
            continue
        try:
            meta = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(meta, dict):
            name = str(meta.get("name") or "")
            if name:
                names.add(name)
            else:
                names.add(stem)
    return names


def verify_dir(
    out_dir: str | Path,
    *,
    expected: set[str] | None = None,
    mode: str = "both",
    nl_min_tokens: int = 0,
    nl_max_tokens: int = 0,
    max_samples: int = 0,
) -> VerifyReport:
    """复核整个输出目录。

    expected 是输入侧的样本名集合（通常用 flat_name）。传了它才能做数量对账，
    不传就只做内容检查——那只能发现"写了但内容空"，发现不了"整条没写"。
    """
    report = VerifyReport()

    shards = list_shards(out_dir)
    report.shards = len(shards)

    seen: set[str] = set()
    for path in shards:
        if path.suffix == ".tar":
            it = _iter_tar(path)
        elif path.suffix == ".zip":
            it = _iter_zip(path)
        else:
            it = _iter_parquet(path)
        for stem, members in it:
            report.scanned += 1
            if max_samples and report.scanned > max_samples:
                break

            name = stem
            raw = members.get(".json")
            if raw:
                try:
                    meta = json.loads(raw.decode("utf-8"))
                    if isinstance(meta, dict) and meta.get("name"):
                        name = str(meta["name"])
                except (UnicodeDecodeError, ValueError):
                    pass

            if name in seen:
                report.add(Issue("duplicate", f"{name}: 重复出现"))
            seen.add(name)

            found = verify_sample(stem, members, mode=mode,
                                  nl_min_tokens=nl_min_tokens,
                                  nl_max_tokens=nl_max_tokens)
            if found:
                for i in found:
                    report.add(i)
            else:
                report.ok += 1
        if max_samples and report.scanned > max_samples:
            break

    if expected is not None:
        report.missing = sorted(expected - seen)
        report.extra = sorted(seen - expected)
        for name in report.missing:
            report.add(Issue("missing", f"{name}: 输入有、输出没有"))

    return report
