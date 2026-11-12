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
    CAPTION_DENSE        caption 收窄版（英文）  仅"回复被截断"后的那次重打
    CAPTION_DENSE_ZH     caption 收窄版（中文）  同上，且这张图出中文
    TEXT         图中文字规则（英文）           caption 出英文时追加
    TEXT_ZH      图中文字规则（中文）           caption 出中文时追加
    ARTIST_EN    画师句规则（英文）             这张图有可信画师、且 caption 走模板句式时追加
    ARTIST_ZH    画师句规则（中文）             同上，且这张图出中文

TAG / CAPTION / CAPTION_ZH 是**强制**的：只要 mode 要产这个东西，就一定
带上对应规则，不看配置。切到"打标"tab 却不带 tag 规则，模型就不知道 tag
该怎么写——那时候"按需加载"已经等于"按需关功能"了。所以它们由 mode 决定。
`output.text_rules` 决定**额外追加哪一份**图中文字规则：它存的是文件名
（不带后缀），按名字只读那一份文件；空串表示这批不加文字约束。

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

    ## ARTIST_EN
    - one artist rule

    ## ARTIST_ZH
    - 一条中文画师规则

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
# 截断重打用的收窄版 caption 规则。为什么单开一节而不是往 CAPTION 里追加条款：
# 追加只能"再说一句别写太多"，而被打断的那类图（气泡堆满的漫画分镜）需要的
# 是**换一套交代顺序**——只说撑起画面的那几样、气泡不复述对白。两件事混在一节
# 里，正常路径也会被收窄，正常图就跟着变贫了。
SECTION_CAPTION_DENSE = "CAPTION_DENSE"
SECTION_CAPTION_DENSE_ZH = "CAPTION_DENSE_ZH"
SECTION_TEXT = "TEXT"
SECTION_TEXT_ZH = "TEXT_ZH"
SECTION_ARTIST = "ARTIST_EN"
SECTION_ARTIST_ZH = "ARTIST_ZH"

KNOWN_SECTIONS = (SECTION_TAG, SECTION_CAPTION, SECTION_CAPTION_ZH,
                  SECTION_CAPTION_DENSE, SECTION_CAPTION_DENSE_ZH,
                  SECTION_TEXT, SECTION_TEXT_ZH,
                  SECTION_ARTIST, SECTION_ARTIST_ZH)


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

    def artist_rules(self, *, zh: bool) -> str:
        """画师句规则：中文取 ARTIST_ZH，英文取 ARTIST_EN。

        与 tag/caption/text 不同，它**不是**按 mode 一刀切的：画师句只在这张图
        确实有可信画师、且 caption 走模板句式时才会出现在正文里。所以取用点
        在每张图成 prompt 的地方（`client._prompt_for_task`），不在 rules_for。
        """
        return self.get(SECTION_ARTIST_ZH if zh else SECTION_ARTIST)

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
    if section in (SECTION_CAPTION, SECTION_CAPTION_DENSE):
        return "<CAPTION> rules:"
    if section in (SECTION_CAPTION_ZH, SECTION_CAPTION_DENSE_ZH):
        return "<CAPTION> 规则："
    # TEXT / TEXT_ZH 是追加在 caption 规则尾部的条款，不需要单独标题——
    # 它们本身就是 caption 规则的一部分。
    return ""


def load_rules(only: str = "") -> RuleSet:
    """按文件名顺序读 `RULES_DIR` 下所有 `.md`，同名节取第一份。

    `only` 非空时只读该文件（文件名不带后缀）。

    ★为什么需要 `only`：`output.text_rules` 的语义是"用哪一份文字规则"，
      要能切 `图中文字规则.md` / `图中文字规则-严格.md` 这类版本。只看合并
      视图的话，同名节永远取文件名靠前的那一份，配置里选什么都没区别——
      这个开关等于不存在。

    单份文件读失败只是少一份规则，不抛异常——规则目录是给人改的。
    """
    try:
        if not RULES_DIR.is_dir():
            return RuleSet()
        files = sorted(RULES_DIR.glob("*.md"))
        if only:
            files = [p for p in files if p.stem == only]
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


def dense(zh: bool = False) -> str:
    """截断重打用的收窄版 caption 规则（空串 = 规则目录没提供）。

    为什么单开一个函数而不是并进 `rules_for`：常规路径与重打路径**同时存在**
    （重打失败后还要退回常规口径再试），塞进同一个返回值，调用方就得靠记忆
    去区分"这次拿到的是哪一版"，很容易在重试分支里拿错。

    ⚠ 只看 `prompts/` 里的 `## CAPTION_DENSE` / `## CAPTION_DENSE_ZH`，
      读不到就返回空串、不做这次重打。**不要退回常规 CAPTION**：那正是截断的
      那份规则，拿它重打一次等于原地再撞一次上限。
    """
    rs = load_rules()
    return rs.get(SECTION_CAPTION_DENSE_ZH if zh else SECTION_CAPTION_DENSE)


def rules_for(mode: str, *, zh: bool,
              text_rules: str | bool = False) -> tuple[str, str, str]:
    """按 mode 取出这一次该用的三份规则。

    返回 (tag_rules, caption_rules, text_rules)，任一为空表示该文件没提供，
    调用方应退回内置。

    `text_rules` 两种传法：
      - 字符串（正常路径）：`prompts/<名字>.md` 里的文字节，空串 = 不要文字约束
      - True/False（兼容旧调用）：走合并视图，不挑文件

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

    text = ""
    if want_nl and text_rules:
        if isinstance(text_rules, str):
            text = load_rules(only=text_rules).text_rules(zh=zh)
            if not text:
                # 指定的文件不存在、或里面没有 `## TEXT` / `## TEXT_ZH` 时退回
                # 合并视图：静默丢掉约束比用错一份更糟——文字条款管的是"怎么
                # 抄"，缺了不会报错，只会让整批产出悄悄变差。
                text = rs.text_rules(zh=zh)
        else:
            text = rs.text_rules(zh=zh)
    return tag, cap, text


def has(section: str) -> bool:
    """目录里有没有这一节（测试与诊断用）。"""
    return bool(load_rules().get(section))


def artist_rules(*, zh: bool) -> str:
    """这张图该用的画师句规则（空串 = 规则目录没提供，调用方不加）。

    单开一个函数而不是塞进 rules_for：画师规则的有无取决于**这张图有没有可信
    画师**，与 mode 无关。塞进 rules_for 会让"tag 模式不产正文"跟"没有画师"
    两种情况共用一个返回值，调用方只能再判一次，等于没封装。
    """
    return load_rules().artist_rules(zh=zh)
