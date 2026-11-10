"""可切换的 prompt 规则片段（仓库根 `prompts/*.md`）。

为什么单独一层
--------------
`_CAPTION_RULES` 这类规则块写死在 `client.py` 里，想换一套说法就得改 Python
再重新打包。可这些规则恰恰是最需要反复调的东西——主创今天要加"字体风格"，
明天要调 tag 数量，后天可能还要 A/B 两版措辞看哪个管用。每调一次都动代码、
跑全量回归，代价和收益完全不成比例。

所以规则放进 `prompts/` 目录，一份 `.md` 若干节，按用途取用。

规则分哪几种
------------
    节名          用途                          什么时候进 prompt
    ----------------------------------------------------------------
    TAG          tag 规则                       mode 含 tag（tag / both）
    CAPTION      caption 规则（英文）           mode 含 nl（nl / both）
    CAPTION_ZH   caption 规则（中文）           同上，且这张图出中文
    TEXT         图中文字规则（英文）           caption 出英文时追加
    TEXT_ZH      图中文字规则（中文）           caption 出中文时追加

TAG / CAPTION / CAPTION_ZH 是**强制**的：只要 mode 要产这个东西，就一定
带上对应规则，不看配置。切到"打标"tab 却不带 tag 规则，模型就不知道 tag
该怎么写——那时候"按需加载"已经等于"按需关功能"了。所以它们由 mode 决定。
`output.text_rules` 只决定是否**额外追加**图中文字规则。

文件格式
--------
    ## TAG
    - 一条 tag 规则

    ## CAPTION
    - one caption rule

    ## CAPTION_ZH
    - 一条中文 caption 规则

    ## TEXT
    - one text rule

    ## TEXT_ZH
    - 一条中文文字规则

`- ` 开头的行各算一条，其余非空行并到上一条。其他小节（`## 说明`）不参与渲染。

读取顺序
--------
`RULES_DIR` 下所有 `.md` 按文件名排序读一遍，同名节取第一份。
"打标规则.md" 和 "图中文字规则.md" 可以各管一段，也可以合成一份。
某个节为空时用 `client.py` 的内置兜底——规则是给人手改的，改坏一个标题
不该让整批任务起不来。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_env = os.environ.get("TAGGER_RULES_DIR", "").strip()
RULES_DIR = Path(_env) if _env else Path("prompts")

SECTION_TAG = "TAG"
SECTION_CAPTION = "CAPTION"
SECTION_CAPTION_ZH = "CAPTION_ZH"
SECTION_TEXT = "TEXT"
SECTION_TEXT_ZH = "TEXT_ZH"

KNOWN_SECTIONS = (SECTION_TAG, SECTION_CAPTION, SECTION_CAPTION_ZH,
                  SECTION_TEXT, SECTION_TEXT_ZH)


@dataclass(frozen=True, slots=True)
class RuleSet:
    """目录里读出来的全部规则。任一节缺失表示没有对应内容。"""

    sections: dict[str, str] = field(default_factory=dict)
    sources: tuple[str, ...] = ()

    def get(self, section: str) -> str:
        return self.sections.get(section, "")

    def text_rules(self, *, zh: bool) -> str:
        """图中文字规则：中文取 TEXT_ZH，英文取 TEXT。"""
        return self.get(SECTION_TEXT_ZH if zh else SECTION_TEXT)

    @property
    def used_any(self) -> bool:
        return bool(self.sections)


def _parse(md: str) -> dict[str, str]:
    """把 markdown 拆成 {节名: 规则正文}，只认 KNOWN_SECTIONS 里的节。

    ★标题一起带出来：模型看到的必须是完整的一段规则，而不是一串没有
      归属的 `- ` 行。`## TAG` 渲染成 `<TAGS> rules:`，与内置块格式对齐。
    """
    buckets: dict[str, list[str]] = {}
    current: list[str] | None = None

    for raw in md.splitlines():
        stripped = raw.strip()

        if stripped.startswith("## "):
            head = stripped[3:].strip()
            if head in KNOWN_SECTIONS:
                current = buckets.setdefault(head, [])
                title = _title_for(head)
                if title:
                    current.append(title)
            else:
                # 未知小节也要截断当前节，否则它的正文会漏进上一条规则。
                current = None
            continue

        if current is None or not stripped:
            continue

        if stripped.startswith("- "):
            current.append(stripped[2:].strip())
        elif current and current[-1] and not current[-1].startswith("<"):
            # 折行续写：并到上一条规则尾部。标题行（`<TAGS> rules:`）不参与。
            current[-1] = current[-1] + " " + stripped
        else:
            current.append(stripped)

    # 只有标题、没有正文的节不算（避免渲染出空规则块）。
    # 规则行存的是**去掉 `- ` 之后**的文本，所以"正文"= 不以标题行开头的那批。
    out: dict[str, str] = {}
    for key, lines in buckets.items():
        body = [ln for ln in lines if not ln.startswith("<")]
        if not body:
            continue
        rendered = [f"- {ln}" if not ln.startswith("<") else ln for ln in lines]
        out[key] = "\n".join(rendered)
    return out


def _title_for(section: str) -> str:
    """节名 → 模型看得懂的标题行。与内置规则块的写法保持一致。"""
    if section == SECTION_TAG:
        return "<TAGS> rules:"
    if section == SECTION_CAPTION:
        return "<CAPTION> rules:"
    if section == SECTION_CAPTION_ZH:
        return "<CAPTION> 规则："
    # TEXT / TEXT_ZH 是追加在 caption 规则尾部的条款，不需要单独标题——
    # 它们本身就是 caption 规则的一部分。
    return ""


def load_rules() -> RuleSet:
    """按文件名顺序读 `RULES_DIR` 下所有 `.md`，同名节取第一份。

    单份文件读失败只是少一份规则，不抛异常——规则目录是给人改的。
    """
    try:
        if not RULES_DIR.is_dir():
            return RuleSet()
        files = sorted(RULES_DIR.glob("*.md"))
    except OSError:
        return RuleSet()

    sections: dict[str, str] = {}
    sources: list[str] = []

    for path in files:
        try:
            md = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        got = _parse(md)
        if not got:
            continue
        sources.append(path.stem)
        for key, body in got.items():
            # 同名节取第一份：文件名靠前的赢，行为可预测。
            sections.setdefault(key, body)

    return RuleSet(sections=sections, sources=tuple(sources))


def rules_for(mode: str, *, zh: bool, text_rules: bool) -> tuple[str, str, str]:
    """按 mode 取出这一次该用的三份规则。

    返回 (tag_rules, caption_rules, text_rules)，任一为空表示该文件没提供，
    调用方应退回内置。

    ★这是"切到哪个 tab 就强制带哪套规则"的落点：tag 类 mode 必然要 tag
      规则，nl 类 mode 必然要 caption 规则，与配置无关。
    """
    rs = load_rules()
    want_tag = mode in ("tag", "both")
    want_nl = mode in ("nl", "both")

    tag = rs.get(SECTION_TAG) if want_tag else ""
    if want_nl:
        cap = rs.get(SECTION_CAPTION_ZH if zh else SECTION_CAPTION)
    else:
        cap = ""
    text = rs.text_rules(zh=zh) if (want_nl and text_rules) else ""
    return tag, cap, text


def has(section: str) -> bool:
    """目录里有没有这一节（测试与诊断用）。"""
    return bool(load_rules().get(section))
