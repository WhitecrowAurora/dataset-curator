"""画师注入的一致性校验：逐行过闸，硬断言不过的行不许进最终 manifest。

为什么单独一个只读工具，而不是塞进 merge 或 pipeline：合并产物、分片产物、
重跑一批之后的半成品都要用同一把尺子量，而「量」不该顺带写盘。这个模块只以
读模式打开被测文件，产出只写到命令行显式指定的新路径；输入文件一个字节不动。

## 四条硬断言

都能只从产物字段本身判定，不依赖模型、词表或网络：

- **A1** `tags_final` 里由画师来的条目必须带 `@` 前缀。裸名进了 tag 串，训练侧
  读到的就只是一个普通画面词，绑定关系断掉且事后无法自动还原。
- **A2** NL 里出现的每个 `@名字` 都必须能在该行的画师声明里找到。找不到说明
  这个 `@` 是模型自己造的、或者画师元数据与正文不同源——两种都不该进数据。
- **A3** `@名字` 必须与原始拼写逐字相等：大小写、空格、下划线、假名、全半角
  一律不许变。名字是绑定键而不是描述文本，差一个字符就绑不到同一批图上，
  而且这类错误在文本上看起来完全正常，只能靠逐字比较抓。
- **A4** NL 出现风格词却没有任何 `@`。这里分两档：本行**声明了画师**时是硬错误
  （画师句被模板吃掉或压根没注入）；本行没有画师时只告警——`anime style`
  这类总体风格词本来就可以独立出现，把它当成违规会制造大量误报。

## 另外三条同样拦行（W 前缀是历史命名，级别是硬错误）

门禁不只四条：下面三种坏法在文本上看起来正常，但都会让下游拿不到正确的画师绑定，
所以同样不许进最终 manifest。

- **W1** 占位符 `__ARTIST_n__` 残留：模型把占位符搬坏了或回填没跑，这意味着这一行的
  画师名根本没解析出来。
- **W2** 有 `@` 但 NL 不以模板分句开头：裸名前置、句子被挪到中段、自创句式——
  裸名前置是明确禁止的形态，不能只提醒。
- **W4** `@@` 重复 `@`：下游按 `@@name` 解析名字会得到错名字。

## 告警

- **W3** 风格词但无 `@` 且本行没有画师声明。这一类不能拦：`anime style` 之类总体
  风格词本来就允许独立出现（没有可信画师时画师句整句省略），拦了就是大面积误报。
  `--strict` 时连这条也判失败。
- **A5** 声明了画师，但 `tags_final` 里**完全找不到**这个名字——标签侧整条画师
  丢了，NL 却写对了，整行原本会通过。按归一化比较（大小写、空格/下划线、全半角
  视为同一名字），名字只要以任何写法出现过就不归它管：裸名是 A1、@ 名写错形是
  A3，同一根因只报一条。默认只告警（先量误报率），`--strict` 下升为错误；没有
  画师声明、或 tag 串缺失/为空的行同样不判。

## 字段别名与边界规则

- NL 取 `nl_final` / `caption`；tag 取 `tags_final` / `tag` / `tags`（后者支持
  `[[tag, prob], ...]` 形态）；画师声明取 `artist_tags` / `artists` / `artist` /
  `artist_tag`，也允许放在 `meta` 子字典里（分片产物的 meta 是独立一层）。
  盘上可能是任一套列名，按固定键名取值会把整批行静默判成空。
- A3 的「原始拼写」只认 `artist_tags` / `artists` / `artist`；`artist_tag` 是注入
  形态，若 NL 的写法只与 `artist_tag` 一致而与原始拼写不同，那本身就是 A3。
- 引号内（`“…”` / `「…」` / `"…"`）的 `@` 视为图内文字引用，不当作画师提及；
  紧跟在字母数字后面的 `@`（`foo@bar.com`）同样不算。声明名按最长优先匹配，
  且其后紧跟字母数字或下划线时不算命中——否则 `@wlop_fan` 会被截成 `@wlop`
  蒙混过关。未逐字命中的 `@` 会再按归一化（大小写/全半角/空格↔下划线）对一遍
  声明名：对上了报 A3「改形」，对不上才报 A2「不在声明里」。这一步按字符吞名字、
  不按分隔符截断，所以含空格的名字被改大小写时不会被截成半个词而误判成 A2。

## 门槛一览

拦行：A1 / A2 / A3 / A4 / W1 / W2 / W4；仅告警：W3、A5（`--strict` 时都拦）。

## 用法

    python -m tagger.validate_artist merged.jsonl
    python -m tagger.validate_artist merged.jsonl --strict --manifest-out clean.jsonl

- 逐条打印 `路径:行号: [级别] 代码 原因`；末尾一行汇总。
- 退出码：`0` 无硬错误；`1` 有硬错误（`--strict` 时告警也算）；`2` 用法/IO 错误。
- `--manifest-out` 只写通过的行，且拒绝指向任何输入文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

# 列名别名：读侧统一走这里，盘上是哪一套都不会被判成空。
_NL_KEYS = ("nl_final", "caption", "nl", "text")
_TAG_KEYS = ("tags_final", "tag")
_ARTIST_LIST_KEYS = ("artist_tags", "artists")
_ARTIST_SINGLE_KEY = "artist"          # 老产物兜底
_ARTIST_INJECTED_KEY = "artist_tag"   # 注入形态，不当原始拼写
# 分片产物的画师字段在 meta 这一层，合并产物在顶层；两层都要找。
_NESTED_KEYS = ("meta", "payload")

_STYLE_WORD = re.compile(r"画风|风格|(?i:art\s*style|\bstyle\b)")
_PLACEHOLDER = re.compile(r"__ARTIST_\d+__")
# 模板分句的两个合法开头（模板只有这两套）。
_TEMPLATE_HEADS = ("使用 @", "In the style of @")
_QUOTED = re.compile(r"“[^”]*”|\"[^\"]*\"|「[^」]*」|『[^』]*』|‘[^’]*’")
# 名字的边界：命中之后这些字符紧跟着，说明命中的只是更长名字的一段前缀。
_WORD_TAIL = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
# 未知 @ 之后取候选名的终止符。只用于生成可读的报错，不参与"是否合法"的判定。
_NAME_STOPS = set(" \t\r\n,，、。.．;；:：!！?？()（）[]【】<>《》\"'“”‘’「」『』/\\|@*`")

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"


class ValidateError(Exception):
    """用法或 IO 层面的错误（文件读不了、参数互斥等），不是校验结论。"""


@dataclass(frozen=True, slots=True)
class Issue:
    """一条校验结论。code 见模块文档 A1-A5 / W1-W4 / E0（行本身不是合法 JSON）。"""

    code: str
    line: int
    message: str
    severity: str = SEVERITY_ERROR

    @property
    def is_error(self) -> bool:
        return self.severity == SEVERITY_ERROR


@dataclass(slots=True)
class Mention:
    """NL 里的一处 `@名字`。position 是 `@` 的字符下标。

    `declared` 非空表示这个字面量按"归一化后相等"对回了某个声明名：也就是说
    它是同一个名字的改形（大小写/空格/下划线/全半角），诊断该报 A3（不逐字
    相等），而不是 A2（不在声明里）——两者的排障方向完全不同。
    """

    position: int
    name: str
    declared: str | None = None


@dataclass(slots=True)
class Outcome:
    """一个被测文件的结论汇总。"""

    path: Path
    lines: int = 0
    passed: int = 0
    failed: int = 0
    warned: int = 0
    errors: int = 0
    warnings: int = 0
    written: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


# ------------------------------------------------------------------ 行内取值


def _text_of(container: dict, keys: Sequence[str]) -> str:
    for key in keys:
        value = container.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _containers(row: dict) -> list[dict]:
    """行本身 + 可能存在的嵌套层（meta/payload），按优先级排列。"""
    out = [row]
    for key in _NESTED_KEYS:
        nested = row.get(key)
        if isinstance(nested, dict):
            out.append(nested)
    return out


def nl_text(row: dict) -> str:
    for container in _containers(row):
        text = _text_of(container, _NL_KEYS)
        if text:
            return text
    return ""


def tag_entries(row: dict) -> list[str]:
    """tags_final 拆成条目。支持字符串与 [[tag, prob], ...] 两种形态。"""
    raw = None
    for container in _containers(row):
        for key in _TAG_KEYS:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                raw = value
                break
        if raw is not None:
            break
        value = container.get("tags")
        if isinstance(value, str) and value.strip():
            raw = value
            break
        if isinstance(value, (list, tuple)) and value:
            names: list[str] = []
            for item in value:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, (list, tuple)) and item:
                    names.append(str(item[0]))
            if names:
                return [n.strip() for n in names if n and n.strip()]
    if raw is None:
        return []
    out = []
    for part in re.split(r"[,，]", raw):
        part = part.strip()
        if part:
            out.append(part)
    return out


def _strip_at(name: str) -> str:
    return name.strip().lstrip("@").strip()


def _as_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def declared_artists(row: dict) -> tuple[list[str], list[str]]:
    """拆出 (原始拼写, 注入形态)。

    原始拼写的优先级：`artist_tags` / `artists`（逐字原名，权威）> `artist`
    （老产物单值兜底）。`artist_tag` 只当注入形态，不作为原始拼写的依据——
    否则"注入时改了名"会被自己的注入形态洗白，A3 就查不出来了。
    两者分开存，A3 才有比较基准：只与注入形态一致而与原始拼写不同的写法必须报出来。
    """
    source: list[str] = []
    injected: list[str] = []
    containers = _containers(row)

    def push(bucket: list[str], name: str) -> None:
        name = _strip_at(name)
        if name and name not in bucket:
            bucket.append(name)

    # 先收权威列表（可能分布在不同层：顶层与 meta），再决定要不要用 artist 兜底。
    for container in containers:
        for key in _ARTIST_LIST_KEYS:
            for item in _as_strings(container.get(key)):
                if item.strip().startswith("@"):
                    push(injected, item)
                push(source, item)
    if not source:
        for container in containers:
            for item in _as_strings(container.get(_ARTIST_SINGLE_KEY)):
                push(source, item)
    for container in containers:
        for item in _as_strings(container.get(_ARTIST_INJECTED_KEY)):
            push(injected, item)
    return source, injected


# ------------------------------------------------------------------ NL 里的 @


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _QUOTED.finditer(text)]


def _inside(spans: Sequence[tuple[int, int]], index: int) -> bool:
    return any(start <= index < end for start, end in spans)


def _read_candidate(text: str, start: int) -> str:
    i = start
    while i < len(text) and text[i] not in _NAME_STOPS:
        i += 1
    return text[start:i]


def _match_declared(text: str, start: int, names: Sequence[str]) -> str | None:
    """在 start 处按最长优先匹配已声明名字；命中后紧跟字母数字/下划线不算命中。"""
    for name in names:
        if not text.startswith(name, start):
            continue
        end = start + len(name)
        if end < len(text) and text[end] in _WORD_TAIL:
            continue
        return name
    return None


def _similar_prefix(text: str, start: int, names: Sequence[str]
                    ) -> tuple[str, int] | None:
    """把 `@` 之后的文本按"归一化后相等"对回声明名，返回 (声明名, 结束下标)。

    为什么需要它：`_match_declared` 只认逐字相等，一旦名字被改形（大小写、
    空格↔下划线、全半角），`@` 后面的候选串就会被分隔符截断——`@Kagami Mochi`
    只读到 `Kagami`，跟声明 `kagami mochi` 永远对不上，于是被误报成"不在声明
    里"（A2）。这里按归一化逐字符吃掉名字，空格不会被当成终止符，命中后即可
    断定"是同一个名字的改形"（A3）。

    按归一化长度倒序尝试（长名优先）；命中后紧跟字母数字/下划线不算命中，
    免得 `@wlop_fan` 被认成 `wlop`。
    """
    ordered = sorted({n for n in names if n}, key=lambda n: len(_norm(n)),
                     reverse=True)
    for declared in ordered:
        target = _norm(declared)
        if not target:
            continue
        i, acc, ok = start, "", True
        while i < len(text) and len(acc) < len(target):
            ch = text[i]
            if ch.isspace() or ch in "_-·・.":
                i += 1
                continue
            acc += unicodedata.normalize("NFKC", ch).casefold()
            i += 1
            if not target.startswith(acc):
                ok = False
                break
        if not ok or acc != target:
            continue
        if i < len(text) and text[i] in _WORD_TAIL:
            continue
        return declared, i
    return None


def find_mentions(text: str, names: Sequence[str] = ()) -> list[Mention]:
    """找出 NL 里的 `@名字`。

    names 是已声明的画师名（按长度倒序传入更省事，这里自己排），用于精准切出
    含空格的名字；没命中声明名的 `@` 退化成"读到分隔符为止"的候选串，只为了让
    报错能指出是哪个名字。
    """
    if not text:
        return []
    spans = _quoted_spans(text)
    ordered = sorted({n for n in names if n}, key=len, reverse=True)
    out: list[Mention] = []
    i = 0
    while True:
        i = text.find("@", i)
        if i < 0:
            break
        if _inside(spans, i) or (i > 0 and text[i - 1] in _WORD_TAIL):
            i += 1
            continue
        if i + 1 < len(text) and text[i + 1] == "@":
            # @@name：跳过第一个 @，让第二个 @ 正常参与匹配（重复 @ 由 W4 报）。
            i += 1
            continue
        name = _match_declared(text, i + 1, ordered)
        if name is not None:
            out.append(Mention(i, name))
            i += 1 + len(name)
            continue
        similar = _similar_prefix(text, i + 1, ordered)
        if similar is not None:
            declared, end = similar
            out.append(Mention(i, text[i + 1:end], declared))
            i = end
            continue
        candidate = _read_candidate(text, i + 1)
        if candidate:
            out.append(Mention(i, candidate))
        i += 1 + max(1, len(candidate))
    return out


# ------------------------------------------------------------------ 逐行判定


def _norm(name: str) -> str:
    """只用于"看起来像同一个名字"的比较：全半角、大小写、分隔符差异归零。

    不做音译/罗马字转换——那种差异只能靠 A2 报"不在声明里"，不能假装相等。
    """
    text = unicodedata.normalize("NFKC", name).casefold()
    return "".join(ch for ch in text if not ch.isspace() and ch not in "_-·・.")


def _similar(name: str, candidates: Sequence[str]) -> str | None:
    target = _norm(name)
    for candidate in candidates:
        if target and target == _norm(candidate):
            return candidate
    return None


def check_tags(entries: Sequence[str], source: Sequence[str],
               injected: Sequence[str], line: int) -> list[Issue]:
    """A1：画师条目必须带 @；带 @ 但名字被改形的同样报出来。"""
    issues: list[Issue] = []
    source_set = set(source)
    for entry in entries:
        if entry.startswith("@"):
            name = entry.lstrip("@").strip()
            if not name:
                continue
            if name in source_set:
                continue
            twin = _similar(name, source) or _similar(name, injected)
            if twin is not None:
                issues.append(Issue(
                    "A3", line,
                    f"tags_final 的 @{name} 与原始拼写 @{twin} 不逐字相等"))
            continue
        if entry in source_set:
            issues.append(Issue(
                "A1", line, f"tags_final 里的画师标签 {entry!r} 缺少 @ 前缀"))
            continue
        twin = _similar(entry, source)
        if twin is not None:
            issues.append(Issue(
                "A1", line,
                f"tags_final 里的画师标签 {entry!r} 缺少 @ 前缀，"
                f"且与原始拼写 {twin!r} 不逐字相等"))
    return issues


def check_mentions(mentions: Sequence[Mention], source: Sequence[str],
                   injected: Sequence[str], line: int) -> list[Issue]:
    """A2/A3：NL 里的 @名字 必须来自声明，且与原始拼写逐字相等。"""
    issues: list[Issue] = []
    known = set(source) | set(injected)
    for mention in mentions:
        name = mention.name
        if mention.declared is not None and mention.declared != name:
            # 归一化已确认是同一个名字，只是写法被改：这一步必须报 A3。
            issues.append(Issue(
                "A3", line,
                f"NL 的 @{name} 与原始 tag {mention.declared!r} 不逐字相等"
                f"（大小写/空格/下划线/假名等被改形）"))
            continue
        if name in source:
            continue
        if name in known:
            issues.append(Issue(
                "A3", line,
                f"NL 的 @{name} 与原始拼写不一致（只与注入形态相同）"))
            continue
        twin = _similar(name, source) or _similar(name, injected)
        if twin is not None:
            issues.append(Issue(
                "A3", line,
                f"NL 的 @{name} 与原始 tag {twin!r} 不逐字相等"
                f"（大小写/空格/下划线/假名等被改形）"))
            continue
        if known:
            issues.append(Issue(
                "A2", line,
                f"NL 的 @{name} 不在该行画师声明里"
                f"（声明：{'、'.join(sorted(known))}）"))
        else:
            issues.append(Issue(
                "A2", line,
                f"NL 的 @{name} 无法核对：该行没有任何画师声明"))
    return issues


def check_artist_tag_present(entries: Sequence[str], source: Sequence[str],
                             injected: Sequence[str], line: int,
                             *, strict: bool = False) -> list[Issue]:
    """A5：声明了画师，但 tag 串里**完全找不到**这个名字。

    管的是"标签侧整条画师丢失"：NL 按模板写对了、tags_final 里连名字都没有，
    整行原本会通过。默认只告警——先把误报率量出来再决定要不要拦；`--strict`
    下升为 error。

    判据按归一化比较（大小写、空格/下划线、全半角都算同一个名字），而且**只要
    名字出现过就不报**：裸名进了 tag 串是 A1 的活（缺 @），@ 名写错形是 A3 的
    活。同一根因只报一条，否则一行会被记两次，统计里"画师丢了"会虚高。

    另外两种也不判：没有画师声明（A5 只对声明过的行负责）、tag 串缺失或为空
    （那一类产物本来就可能没有 tag 侧，报"没有画师 tag"只是噪声）。
    """
    if not entries:
        return []
    # 原始拼写与注入形态常常是同一个名字，报错里只列一次。
    declared = list(dict.fromkeys([*source, *injected]))
    if not declared:
        return []
    # 比较时统一去掉可选的 @ 前缀：@wlop 与 wlop 都是"名字出现过"。
    present = {_norm(_strip_at(entry)) for entry in entries if _strip_at(entry)}
    if present & {_norm(name) for name in declared}:
        return []
    return [Issue(
        "A5", line,
        f"本行声明了画师（{'、'.join(declared)}），但 tags_final 里完全找不到"
        f"这个名字（画师 tag 没进 tag 串）",
        SEVERITY_ERROR if strict else SEVERITY_WARN)]


def validate_row(row: object, *, line: int = 0,
                 strict: bool = False) -> list[Issue]:
    """对一行做四条断言 + 三条结构检查 + 告警。

    返回按代码顺序排列的结论；空列表 = 通过。带 error 级别的条目都会让该行
    出不了最终 manifest（`row_passes`）。`strict=True` 时 A5（画师 tag 没进
    tag 串）从告警升为错误，其余断言不受影响。
    """
    if not isinstance(row, dict):
        return [Issue("E0", line, "行不是 JSON 对象", SEVERITY_ERROR)]

    nl = nl_text(row)
    entries = tag_entries(row)
    source, injected = declared_artists(row)
    issues: list[Issue] = []

    issues.extend(check_tags(entries, source, injected, line))
    # A5 只看 tag 侧，放在 NL 早退之前：没有 NL 的行同样可能出现"画师 tag 丢了"。
    issues.extend(check_artist_tag_present(entries, source, injected, line,
                                           strict=strict))

    # 没有 NL 的行（tag 单产物、清单行）不存在画师句，A2-A4 都无从谈起：
    # 对这一类行报"没注入画师句"只会把整批非标注产物淹没成误报。
    if not nl:
        return issues

    mentions = find_mentions(nl, [*source, *injected])
    issues.extend(check_mentions(mentions, source, injected, line))

    has_at = "@" in nl
    style_word = _STYLE_WORD.search(nl)
    declared = bool(source or injected)
    if not has_at:
        if declared:
            extra = f"，正文含风格词 {style_word.group(0)!r}" if style_word else ""
            issues.append(Issue(
                "A4", line,
                f"本行声明了画师但 NL 没有任何 @（画师句未注入或被模板吃掉{extra}）"))
        elif style_word:
            issues.append(Issue(
                "W3", line,
                f"NL 出现风格词 {style_word.group(0)!r} 但没有任何 @，"
                f"且本行没有画师声明", SEVERITY_WARN))
    elif declared and not mentions and "@@" not in nl:
        # 有 @ 但都在引号里（图内文字引用）或邮箱语境里：画师句同样缺席。
        issues.append(Issue(
            "A4", line,
            "本行声明了画师，但 NL 里的 @ 都落在图内文字引用/邮箱语境里，"
            "没有画师句"))

    placeholder = _PLACEHOLDER.search(nl)
    if placeholder:
        issues.append(Issue(
            "W1", line,
            f"NL 残留占位符 {placeholder.group(0)}"
            f"（模型搬坏了占位符或回填没跑）", SEVERITY_ERROR))
    if mentions and not nl.lstrip().startswith(_TEMPLATE_HEADS):
        head = nl.lstrip()[:24]
        issues.append(Issue(
            "W2", line,
            f"有 @ 但 NL 不是以模板分句开头（开头：{head!r}）", SEVERITY_ERROR))
    if "@@" in nl:
        issues.append(Issue(
            "W4", line, "NL 出现 @@（重复 @）", SEVERITY_ERROR))
    return issues


def row_passes(issues: Sequence[Issue], *, strict: bool = False) -> bool:
    if strict:
        return not issues
    return not any(issue.is_error for issue in issues)


# ------------------------------------------------------------------ 文件级


def _iter_rows(path: Path) -> Iterator[tuple[int, object, str]]:
    """按行产出 (行号, 解析结果, 原始文本)。JSON 数组也支持，行号按元素序号。"""
    with path.open("r", encoding="utf-8") as handle:
        head = ""
        while True:
            ch = handle.read(1)
            if not ch:
                break
            if not ch.isspace():
                head = ch
                break
        handle.seek(0)
        if head == "[":
            try:
                data = json.load(handle)
            except json.JSONDecodeError as exc:
                raise ValidateError(f"{path} 不是合法 JSON 数组：{exc}") from exc
            if not isinstance(data, list):
                raise ValidateError(f"{path} 顶层是对象而不是数组")
            for index, row in enumerate(data, start=1):
                yield index, row, json.dumps(row, ensure_ascii=False)
            return
        for lineno, raw in enumerate(handle, start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                yield lineno, json.loads(text), text
            except json.JSONDecodeError as exc:
                yield lineno, None, f"{text}  # {exc.msg}"


def _write_atomic(path: Path, lines: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(tmp, path)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def validate_file(path: str | Path, *, strict: bool = False,
                  manifest_out: str | Path | None = None,
                  quiet: bool = False, keep_issues: bool = False,
                  inputs: Sequence[Path] = ()) -> Outcome:
    """校验一个产物文件。

    只读被测文件；manifest_out 必须是一个与所有输入都不同的新路径，
    否则等于把"过滤后的产物"写回被测数据，输入就不再是原始产物了。
    """
    target = Path(path)
    if not target.is_file():
        raise ValidateError(f"不是文件：{target}")
    out_path: Path | None = None
    if manifest_out is not None:
        out_path = Path(manifest_out)
        for other in (*inputs, target):
            if _same_file(out_path, other):
                raise ValidateError(
                    f"--manifest-out 不能指向输入文件：{out_path}")

    outcome = Outcome(path=target)
    kept: list[str] = []
    broken = False
    try:
        for lineno, row, raw in _iter_rows(target):
            outcome.lines += 1
            issues = validate_row(row, line=lineno, strict=strict)
            errors = [i for i in issues if i.is_error]
            warns = [i for i in issues if not i.is_error]
            if keep_issues:
                outcome.issues.extend(issues)
            outcome.errors += len(errors)
            outcome.warnings += len(warns)
            if warns:
                outcome.warned += 1
            for issue in issues:
                print(f"{target}:{issue.line}: [{issue.severity}] "
                      f"{issue.code} {issue.message}")
            if row_passes(issues, strict=strict):
                outcome.passed += 1
                if out_path is not None:
                    kept.append(raw)
            else:
                outcome.failed += 1
    except BaseException:
        broken = True
        raise
    finally:
        # 中途失败不留半截产物：它看起来会像"这批只有这么多行通过了"。
        if out_path is not None and not broken:
            _write_atomic(out_path, kept)
            outcome.written = len(kept)
    if not quiet:
        print(f"[validate] {target}: {outcome.lines} 行，通过 {outcome.passed}，"
              f"失败 {outcome.failed}，错误 {outcome.errors} 条，"
              f"告警 {outcome.warnings} 条")
        if out_path is not None:
            print(f"[validate] 通过的行已写入 {out_path}（{outcome.written} 行）")
    return outcome


def validate_paths(paths: Sequence[str | Path], *, strict: bool = False,
                   manifest_out: str | Path | None = None,
                   quiet: bool = False) -> int:
    """多文件批处理，返回退出码。manifest_out 只允许配单个输入。"""
    files = [Path(p) for p in paths]
    if manifest_out is not None and len(files) != 1:
        raise ValidateError("--manifest-out 只能配一个输入文件")
    exit_code = 0
    for path in files:
        outcome = validate_file(path, strict=strict, manifest_out=manifest_out,
                                quiet=quiet, inputs=files)
        if not outcome.ok or (strict and outcome.warnings):
            exit_code = 1
    return exit_code


# ------------------------------------------------------------------ selftest


def _check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def _codes(issues: Sequence[Issue]) -> list[str]:
    return [issue.code for issue in issues]


def _selftest_cases() -> list[tuple[str, dict, list[str]]]:
    """(名字, 行, 期望出现的代码)。空列表表示必须完全通过。"""
    return [
        ("全对-中文模板",
         {"artist_tags": ["sakimi chan"],
          "tags_final": "@sakimi chan, 1girl, blue_hair",
          "nl_final": "使用 @sakimi chan 的风格，画中一位蓝发少女坐在窗边。"},
         []),
        ("全对-英文模板",
         {"artist_tag": "@wlop", "artist": "wlop",
          "tags_final": "@wlop, warrior, rain",
          "nl_final": "In the style of @wlop, a warrior stands in the rain."},
         []),
        ("缺@",
         {"artist_tags": ["sakimi chan"],
          "tags_final": "sakimi chan, 1girl",
          "nl_final": "使用 @sakimi chan 的风格，一位少女。"},
         ["A1"]),
        ("@名不在artist_tags",
         {"artist_tags": ["sakimi chan"], "tags_final": "@sakimi chan",
          "nl_final": "使用 @someone 的风格，一位少女。"},
         ["A2"]),
        ("名字被改形-下划线",
         {"artist_tags": ["sakimi chan"], "tags_final": "@sakimi chan",
          "nl_final": "使用 @sakimi_chan 的风格，一位少女。"},
         ["A3"]),
        ("名字被改形-大小写",
         {"artist_tags": ["wlop"], "tags_final": "@wlop",
          "nl_final": "In the style of @WLOP, a warrior."},
         ["A3"]),
        ("名字被翻译",
         {"artist_tags": ["あずみ"], "tags_final": "@あずみ",
          "nl_final": "In the style of @azumi, a girl."},
         ["A2"]),
        ("风格词无@且无画师",
         {"tags_final": "1girl, anime_style",
          "nl_final": "In anime style, a girl sits by the window."},
         ["W3"]),
        ("声明画师但NL无@",
         {"artist_tags": ["wlop"], "tags_final": "@wlop, warrior",
          "nl_final": "An art style piece, a warrior stands."},
         ["A4"]),
        ("占位符残留",
         {"artist_tags": ["wlop"], "tags_final": "@wlop",
          "nl_final": "使用 @wlop 的风格，__ARTIST_0__ 站在雨里。"},
         ["W1"]),
        ("裸名前置",
         {"artist_tags": ["wlop"], "tags_final": "@wlop",
          "nl_final": "@wlop, a warrior stands."},
         ["W2"]),
        ("@ 在句尾未按模板",
         {"artist_tags": ["wlop"], "tags_final": "@wlop",
          "nl_final": "A warrior stands. In the style of @wlop"},
         ["W2"]),
        ("@@ 重复",
         {"artist_tags": ["wlop"], "tags_final": "@wlop",
          "nl_final": "使用 @@wlop 的风格，一名战士。"},
         ["W4"]),
        ("引号内的@不算画师提及",
         {"tags_final": "1girl",
          "nl_final": "海报上写着“@someone”。"},
         []),
        ("邮箱里的@不算画师提及",
         {"artist_tags": ["wlop"], "tags_final": "@wlop",
          "nl_final": "使用 @wlop 的风格，图上有联系邮箱 foo@bar.com。"},
         []),
        ("声明画师但@只在引号里",
         {"artist_tags": ["wlop"], "tags_final": "@wlop",
          "nl_final": "海报上写着“@wlop”。"},
         ["A4"]),
        ("前缀伪装",
         {"artist_tags": ["wlop"], "tags_final": "@wlop",
          "nl_final": "使用 @wlop_fan 的风格，一名战士。"},
         ["A2"]),
        ("没有NL的tag单产物行",
         {"artist_tags": ["wlop"], "tags_final": "@wlop, 1girl"},
         []),
        ("没有NL但tag缺@",
         {"artist_tags": ["wlop"], "tags_final": "wlop, 1girl"},
         ["A1"]),
        ("分片形态-pairs标签+嵌套meta",
         {"meta": {"artist_tags": ["wlop"]},
          "tags": [["@wlop", 1.0], ["1girl", 1.0]],
          "caption": "使用 @wlop 的风格，一位战士。"},
         []),
        ("多画师",
         {"artist_tags": ["wlop", "sakimi chan"],
          "tags_final": "@wlop, @sakimi chan, 1girl",
          "nl_final": "使用 @wlop、@sakimi chan 的风格，两位画师风格混合。"},
         []),
        # ---- 含空格/假名的名字被改形：必须报 A3（不逐字相等），不是 A2（不在声明里）
        ("F1 含空格名改大小写",
         {"artist_tags": ["kagami mochi"], "tags_final": "@kagami mochi",
          "nl_final": "In the style of @Kagami Mochi, a girl sits."},
         ["A3"]),
        ("F1 含空格名改写空格为下划线",
         {"artist_tags": ["kagami mochi"], "tags_final": "@kagami mochi",
          "nl_final": "使用 @Kagami_Mochi 的风格，一位少女。"},
         ["A3"]),
        ("F1 含空格名+假名混排改大小写",
         {"artist_tags": ["kagami もち"], "tags_final": "@kagami もち",
          "nl_final": "In the style of @Kagami もち, a girl."},
         ["A3"]),
        # ---- A5：声明了画师但 tag 串里没有该画师条目（默认只告警）
        ("A5 声明画师但tags无@条目",
         {"artists": ["kagami mochi"], "artist_tags": ["@kagami mochi"],
          "caption": "In the style of @kagami mochi, a girl sits.",
          "tags_final": "warrior, rain"},
         ["A5"]),
        ("A5 不触发-无画师声明",
         {"tags_final": "warrior, rain",
          "nl_final": "A warrior stands in the rain."},
         []),
        ("A5 不触发-tags有@条目",
         {"artist_tags": ["kagami mochi"], "tags_final": "@kagami mochi, 1girl",
          "nl_final": "In the style of @kagami mochi, a girl."},
         []),
        ("A5 不触发-tags_final 缺失",
         {"artist_tags": ["wlop"],
          "nl_final": "In the style of @wlop, a girl."},
         []),
        ("A5 不触发-tags_final 非字符串",
         {"artist_tags": ["wlop"], "tags_final": 123,
          "nl_final": "In the style of @wlop, a girl."},
         []),
        ("A5 不触发-tags 为对象",
         {"artist_tags": ["wlop"], "tags_final": {"a": 1},
          "nl_final": "In the style of @wlop, a girl."},
         []),
        # ---- A5 只在名字完全找不到时报：裸名归 A1、@ 名写错形归 A3，同一根因只报一条
        ("A5 不重复-裸名由 A1 负责",
         {"artist_tags": ["wlop"], "tags_final": "wlop, 1girl",
          "nl_final": "使用 @wlop 的风格，一名战士。"},
         ["A1"]),
        ("A5 只在名字完全缺失时报",
         {"artist_tags": ["wlop"], "tags_final": "1girl, solo",
          "nl_final": "In the style of @wlop, a girl sits."},
         ["A5"]),
        ("A5 不触发-@名存在",
         {"artist_tags": ["wlop"], "tags_final": "@wlop, 1girl",
          "nl_final": "In the style of @wlop, a girl sits."},
         []),
        ("A5 不触发-裸名大小写变体",
         {"artist_tags": ["wlop"], "tags_final": "WLOP, 1girl",
          "nl_final": "In the style of @wlop, a girl."},
         ["A1"]),
        ("A5 不触发-裸名尾巴下划线",
         {"artist_tags": ["wlop"], "tags_final": "wlop_, 1girl",
          "nl_final": "In the style of @wlop, a girl."},
         ["A1"]),
        ("A5 不触发-裸名空格改下划线",
         {"artist_tags": ["kagami mochi"], "tags_final": "kagami_mochi, 1girl",
          "nl_final": "In the style of @kagami mochi, a girl."},
         ["A1"]),
    ]


def _selftest_strict_cases() -> list[tuple[str, dict, list[str]]]:
    """只在 `--strict` 下才升级的用例：(名字, 行, 期望代码)。

    A5 在 strict 下从告警变错误；没有画师声明的行仍必须一条都不报。
    """
    return [
        ("A5 strict 升 error",
         {"artists": ["kagami mochi"], "artist_tags": ["@kagami mochi"],
          "caption": "In the style of @kagami mochi, a girl sits.",
          "tags_final": "warrior, rain"},
         ["A5"]),
        ("A5 无画师行 strict 仍不触发",
         {"tags_final": "warrior, rain",
          "nl_final": "A warrior stands in the rain."},
         []),
    ]


def _selftest() -> int:
    import tempfile

    failures = 0

    def judge(name: str, got: bool, want: bool, extra: str = "") -> None:
        nonlocal failures
        if got == want:
            print(f"  ok   {name}")
            return
        failures += 1
        print(f"  FAIL {name}: got={got} want={want} {extra}")

    def judge_row(name: str, row: dict, expected: list[str], *,
                  strict: bool) -> None:
        issues = validate_row(row, line=1, strict=strict)
        codes = _codes(issues)
        has_error = any(issue.is_error for issue in issues)
        label = name + ("（strict）" if strict else "")
        if expected:
            # 期望的代码必须全部出现，且不应有期望之外的硬错误。
            missing = [c for c in expected if c not in codes]
            extra_errors = [i.code for i in issues
                            if i.is_error and i.code not in expected]
            judge(label, not missing and not extra_errors, True, f"codes={codes}")
            if expected == ["W3"]:
                judge(label + "-仅是告警", has_error, False, f"codes={codes}")
            if "A5" in expected:
                # A5 的级别随 strict 变：默认告警、strict 升级成错误。
                judge(label + "-级别", has_error, strict, f"codes={codes}")
            if any(code in ("W1", "W2", "W4") for code in expected):
                judge(label + "-是硬错误", has_error, True, f"codes={codes}")
        else:
            judge(label, codes == [], True, f"codes={codes}")

    print("[selftest] 逐行判定")
    for name, row, expected in _selftest_cases():
        judge_row(name, row, expected, strict=False)
    for name, row, expected in _selftest_strict_cases():
        judge_row(name, row, expected, strict=True)

    # F1：改形的诊断码必须是 A3（同一个名字写错了），不能是 A2（名字不认识）。
    # 报成 A2 会把排障引向"名字没登记"，而真正的原因是名字被改写。
    f1_row = {"artist_tags": ["kagami mochi"], "tags_final": "@kagami mochi",
              "nl_final": "In the style of @Kagami Mochi, a girl sits."}
    f1_issues = validate_row(f1_row, line=1)
    f1_codes = _codes(f1_issues)
    judge("F1 报 A3 不报 A2", "A3" in f1_codes and "A2" not in f1_codes, True,
          f"codes={f1_codes}")
    judge("F1 候选串不被空格截断（诊断里有完整名字）",
          any("Kagami Mochi" in issue.message for issue in f1_issues), True,
          f"{[i.message for i in f1_issues]}")
    judge("F1 下划线改形同样是 A3",
          "A3" in _codes(validate_row(
              {"artist_tags": ["kagami mochi"], "tags_final": "@kagami mochi",
               "nl_final": "使用 @kagami_mochi 的风格，一位少女。"}, line=1)),
          True, "")

    # A5：默认只是告警、--strict 才拦；无画师声明/无 tag 串的行一律不判。
    a5_row = {"artists": ["kagami mochi"], "artist_tags": ["@kagami mochi"],
              "caption": "In the style of @kagami mochi, a girl sits.",
              "tags_final": "warrior, rain"}
    judge("A5 默认仅告警",
          any(i.code == "A5" and not i.is_error
              for i in validate_row(a5_row, line=1)), True, "")
    judge("A5 默认该行通过", row_passes(validate_row(a5_row, line=1)), True, "")
    judge("A5 --strict 该行不通过",
          row_passes(validate_row(a5_row, line=1, strict=True), strict=True),
          False, "")
    judge("A5 不因 tags_final 非字符串抛错",
          _codes(validate_row({"artist_tags": ["wlop"], "tags_final": 123,
                               "nl_final": "In the style of @wlop, a girl."},
                              line=1)) == [], True, "")
    judge("A5 不误伤无画师行",
          _codes(validate_row({"tags_final": "warrior, rain",
                               "nl_final": "A warrior stands."}, line=1)) == [],
          True, "")

    # A1 与 A5 不重复计数：裸名归 A1（缺 @），名字完全缺失才归 A5。
    bare_row = {"artist_tags": ["wlop"], "tags_final": "wlop, 1girl",
                "nl_final": "使用 @wlop 的风格，一名战士。"}
    bare_codes = _codes(validate_row(bare_row, line=1))
    judge("裸名只报 A1（无 A5）", bare_codes == ["A1"], True, f"codes={bare_codes}")
    judge("裸名该行仍被拦下",
          row_passes(validate_row(bare_row, line=1)), False, "")
    gone_row = {"artist_tags": ["wlop"], "tags_final": "1girl, solo",
                "nl_final": "In the style of @wlop, a girl sits."}
    judge("名字完全缺失只报 A5",
          _codes(validate_row(gone_row, line=1)) == ["A5"], True,
          f"codes={_codes(validate_row(gone_row, line=1))}")
    judge("名字完全缺失默认 warn",
          any(i.code == "A5" and not i.is_error
              for i in validate_row(gone_row, line=1)), True, "")
    judge("名字完全缺失 --strict error",
          any(i.code == "A5" and i.is_error
              for i in validate_row(gone_row, line=1, strict=True)), True, "")
    ok_row = {"artist_tags": ["wlop"], "tags_final": "@wlop, 1girl",
              "nl_final": "In the style of @wlop, a girl sits."}
    judge("@名存在时无 A1 无 A5",
          _codes(validate_row(ok_row, line=1)) == [], True, "")
    for variant in ("WLOP", "wlop_", "ｗｌｏｐ"):
        row = {"artist_tags": ["wlop"], "tags_final": f"{variant}, 1girl",
               "nl_final": "In the style of @wlop, a girl sits."}
        codes = _codes(validate_row(row, line=1))
        judge(f"变体 {variant!r} 交给 A1、不额外报 A5",
              "A5" not in codes and "A1" in codes, True, f"codes={codes}")

    # 行不是 JSON 对象也要有结论，且算硬错误。
    judge("非对象行", _codes(validate_row(["not", "a", "row"], line=7)) == ["E0"],
          True, "")
    judge("非对象行-行号", validate_row(None, line=9)[0].line == 9, True, "")

    from . import tmpdir

    with tempfile.TemporaryDirectory(prefix="validate-artist-",
                                     dir=tmpdir.ensure()) as tmp:
        root = Path(tmp)
        clean_row = {"artist_tags": ["wlop"], "tags_final": "@wlop, 1girl",
                     "nl_final": "使用 @wlop 的风格，一位战士站在雨里。"}
        bad_at_row = {"artist_tags": ["wlop"], "tags_final": "wlop, 1girl",
                      "nl_final": "使用 @wlop 的风格，一位战士站在雨里。"}
        bad_name_row = {"artist_tags": ["wlop"], "tags_final": "@wlop",
                        "nl_final": "使用 @someone 的风格，一位战士。"}
        warn_row = {"tags_final": "1girl",
                    "nl_final": "In anime style, a girl sits."}

        def dump(name: str, rows: Sequence[dict]) -> Path:
            path = root / name
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                      for r in rows) + "\n", encoding="utf-8")
            return path

        clean = dump("clean.jsonl", [clean_row, clean_row])
        mixed = dump("mixed.jsonl", [clean_row, bad_at_row, bad_name_row])
        warns = dump("warns.jsonl", [clean_row, warn_row])

        import contextlib
        import io

        def quiet_main(argv: list[str]) -> int:
            """静默跑一次 CLI：结论看退出码，findings 由单独的用例去断言。"""
            with contextlib.redirect_stdout(io.StringIO()):
                return main(argv)

        print("[selftest] CLI 退出码与打印")
        judge("全对退出 0", quiet_main([str(clean), "--quiet"]) == 0, True, "")
        judge("有失败退出 1", quiet_main([str(mixed), "--quiet"]) == 1, True, "")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main([str(mixed)])
        printed = buffer.getvalue()
        judge("打印行号", f"{mixed}:2:" in printed, True, printed[:200])
        judge("打印原因含 A1", "A1" in printed, True, printed[:200])
        judge("打印原因含 A2", "A2" in printed, True, printed[:200])
        judge("打印原因含画师名", "wlop" in printed, True, "")

        before = {p: p.read_bytes() for p in (clean, mixed, warns)}
        judge("仅告警默认退出 0", quiet_main([str(warns), "--quiet"]) == 0, True, "")
        judge("strict 下告警也算失败",
              quiet_main([str(warns), "--quiet", "--strict"]) == 1, True, "")

        # A5：声明了画师但 tag 串里没有该画师 → 默认 warn、rc=0；--strict rc=1。
        a5_path = dump("a5.jsonl", [{
            "image_id": "x", "artists": ["kagami mochi"],
            "artist_tags": ["@kagami mochi"],
            "caption": "In the style of @kagami mochi, a girl sits.",
            "tags_final": "warrior, rain"}])
        judge("A5 默认 rc=0", quiet_main([str(a5_path), "--quiet"]) == 0, True, "")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc_a5 = main([str(a5_path)])
        printed_a5 = buffer.getvalue()
        judge("A5 默认打 warn", rc_a5 == 0 and "[warn] A5" in printed_a5, True,
              printed_a5[:200])
        judge("A5 --strict rc=1",
              quiet_main([str(a5_path), "--quiet", "--strict"]) == 1, True, "")

        # W1/W2/W4 是硬错误：这三类行都不得进通过清单。
        hard = dump("hard.jsonl", [
            {"artist_tags": ["wlop"], "tags_final": "@wlop",
             "nl_final": "@wlop, a warrior stands."},              # W2 裸名前置
            {"artist_tags": ["wlop"], "tags_final": "@wlop",
             "nl_final": "使用 @wlop 的风格，__ARTIST_0__ 站在雨里。"},  # W1
            {"artist_tags": ["wlop"], "tags_final": "@wlop",
             "nl_final": "使用 @@wlop 的风格，一名战士。"},          # W4
        ])
        out_hard = root / "hard-clean.jsonl"
        rc = quiet_main([str(hard), "--quiet", "--manifest-out", str(out_hard)])
        kept_hard = [line for line in out_hard.read_text("utf-8").splitlines()
                     if line.strip()]
        judge("裸名前置/占位符/@@ 都被拦下",
              rc == 1 and kept_hard == [], True,
              f"rc={rc} kept={len(kept_hard)}")

        out = root / "clean-only.jsonl"
        rc = quiet_main([str(mixed), "--quiet", "--manifest-out", str(out)])
        kept = [json.loads(line) for line in
                out.read_text("utf-8").splitlines() if line.strip()]
        judge("manifest-out 退出 1", rc == 1, True, "")
        judge("manifest-out 只留通过行", len(kept) == 1, True, f"{len(kept)}")
        judge("manifest-out 保留原行内容", kept[0]["nl_final"] == clean_row["nl_final"],
              True, "")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = quiet_main([str(clean), "--quiet", "--manifest-out", str(clean)])
        judge("manifest-out 不许指向输入",
              rc == 2 and "manifest-out" in err.getvalue(), True,
              f"rc={rc} {err.getvalue()[:120]}")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = quiet_main([str(clean), str(clean), "--quiet",
                             "--manifest-out", str(out)])
        judge("manifest-out 只配单个输入", rc == 2, True, f"rc={rc}")
        judge("输入文件未被改动",
              all(p.read_bytes() == before[p] for p in before), True, "")

        # JSON 数组形态也要能读，且不写回。
        arr = root / "rows.json"
        arr.write_text(json.dumps([clean_row, bad_at_row], ensure_ascii=False),
                       encoding="utf-8")
        judge("JSON 数组形态", quiet_main([str(arr), "--quiet"]) == 1, True, "")

        # 坏行（不是合法 JSON）按硬错误处理，不静默跳过。
        broken = root / "broken.jsonl"
        broken.write_text('{"artist_tags": ["wlop"]}\nnot json\n', encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = main([str(broken)])
        judge("坏行报 E0", rc == 1 and "E0" in buffer.getvalue(), True,
              buffer.getvalue()[:200])

    print(f"[selftest] validate_artist: {'全部通过' if not failures else f'{failures} 项失败'}")
    return 0 if not failures else 1


# ------------------------------------------------------------------ CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tagger.validate_artist",
        description="逐行校验画师注入：A1-A4 与 W1/W2/W4 拦行，W3 与 A5 告警"
                    "（--strict 时告警也拦）；只读输入")
    parser.add_argument("paths", nargs="*", help="待校验的 JSONL（或 JSON 数组）文件")
    parser.add_argument("--strict", action="store_true",
                        help="连 W3（无画师的风格词）这类告警也判失败")
    parser.add_argument("--manifest-out", help="把通过的行写到这个新文件（不得指向输入）")
    parser.add_argument("--quiet", action="store_true", help="不打印每行通过情况与汇总")
    parser.add_argument("--selftest", action="store_true", help="跑内置样本自检")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.selftest:
        return _selftest()
    if not args.paths:
        parser.error("缺少要校验的文件")
    try:
        return validate_paths(args.paths, strict=args.strict,
                              manifest_out=args.manifest_out, quiet=args.quiet)
    except (ValidateError, OSError) as exc:
        print(f"[validate:error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
