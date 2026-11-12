"""成员端打标客户端：分发包 → vLLM 推理 → 回收产物。

流程位置
--------
    总机 split 打包 → **本模块（成员机上跑一遍）** → 交回 → 总机 merge

进来的东西是一个 zip 分发包，里面只有两样：图片（平铺，名字就是 image_id）
和 manifest.jsonl（这一包负责哪些 image_id）。出去的东西是
`<image_id>-tag.json` 与 `<image_id>-nl.json` 两个文件——注意是**两个独立文件**，
不是一个总表，理由见 merge.py：文件存在性就是完成信号，没有歧义。

四个不显然的取舍
----------------
1. **一次请求出两段，不是两次请求。**
   tag 和 caption 看的是同一张图、同一个视觉编码，分两次请求等于把 prefill
   付两遍钱。所以 prompt 要求模型按 `<TAGS>...</TAGS>` 与
   `<CAPTION>...</CAPTION>` 两个块输出，一次拿全。

2. **用块标记而不是 JSON。**
   JSON 一旦被 max_tokens 截断就是废的，解析不出任何东西——连已经说完的 tag
   都一起丢。块标记的截断是局部的：`<TAGS>` 段完整、`<CAPTION>` 段断在半句，
   至少能拿到前一半，而且断在哪儿一眼可见（能进重试，而不是静默变脏数据）。
   代价是解析要写正则，也要容忍模型少写闭合标签。

3. **客户端不算 sha256，也不重新编号。**
   image_id 由分发环节一次性分配（见 naming.py），本地再生成一次就会撞号。
   哈希同理：包内图片已经用 image_id 定位，再算一遍 sha256 是纯 CPU 浪费，
   而它唯一的作用是"同一张图出现两次"——这件事由分发/manifest 负责。

4. **跳过判定看「产物存在且非空」，不看进度记录。**
   进度文件可能比产物新（写完记录才崩、或产物被误删），
   反过来产物也可能是上一版模型打的。所以两者都要看：文件在、内容非空，
   才跳过；`--force` 时才无条件重打。

和资源守护的关系
----------------
每完成一个单元（一张图）调一次 guard 的让步（`after_unit`），这是 guard 里
验证过的做法：在任务边界 sleep(busy*(1-duty)/duty)，不碰驱动、不干扰 vLLM
自己的连续批处理。并发数 > 1 时"单元"变成"一批在途请求"，让步按批结算——
仍贴着 duty，只是粒度变粗。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tarfile
import threading
import time
import zipfile
from concurrent.futures import (ALL_COMPLETED, FIRST_COMPLETED, Future,
                                ThreadPoolExecutor, wait)
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable, Sequence

import httpx

from . import artist_phrase
from . import artist_templates
from . import nl_api
from .artist import ArtistRule, ArtistTable, format_tag, inject_into_caption
from .caption_text import tidy_caption
from .config import ConfigError, OpsConfig, OutputSpec, parse_caption_chars
from .cpu_tagger import (TagBackend, TagPrediction, TaggerUnavailable,
                         build_tag_backends, resolve_devices)
from .distribute import read_shard_manifest
from .guard import ResourceGuard
from .naming import annotation_name, normalize_ext
from .netutil import trust_env_for
from .prompt_rules import artist_rules, rules_for
from .prompt_rules import dense as _dense_caption
from .quality import QualityGate, QualityPolicy, looks_like_refusal
from .result_cache import ResultCache
from .schema import Annotation, QC
from .tag_categories import group_tags, render_groups

# prompt 只有一份，不设版本号。溯源靠 `prompt_sha`（见 _tag_payload）：它是
# prompt 全文的哈希，改一个字就会变，比手写的编号可靠——手写的那个只要有人
# 忘了改，两批指令不同的数据就会顶着同一个编号混在一起，而那是最难查的一类污染。

SYSTEM_PROMPT = (
    "You annotate images for diffusion-model training data. "
    "You are precise and literal: you describe only what is visible in the image. "
    "You never guess the artist, the character, or the series."
)

_TAG_RULES = """<TAGS> rules:
- {lo} to {hi} tags, lowercase, words joined by underscores (danbooru style: long_hair, looking_at_viewer, depth_of_field).
- Order from most to least salient; separate with commas only.
- Most images land around {anchor_lo}-{anchor_hi} tags; go higher only when the image really is that dense.
- Never pad the list to reach {lo}: near-duplicates and restatements of the same fact waste slots. On a simple image, spend the slots on framing, lighting and background instead.
- Only tag what is clearly visible: subject count and type, hair, eyes, clothing, pose, action, setting, props, camera angle, framing, lighting, colour palette, art style.
- Do not output quality or score words (masterpiece, best quality, 8k, highres, ultra detailed).
- Do not output the artist name, the character name, or the series name: you cannot know them and guessing poisons the label."""

_CAPTION_RULES = """<CAPTION> rules:
- One paragraph of {char_lo}-{char_hi} characters (roughly {sent_lo}-{sent_hi} sentences), on a single line. Stop at the upper bound: the reply is truncated there, and a caption cut mid-sentence is thrown away.
- Order the content: main subject; setting and background; composition and camera angle; lighting; any visible text, transcribed verbatim inside double quotes.
- Name the colour of every object you name, and what its surface looks like (matte, glossy, metal, cloth, skin, glass, hair). A description with no colour and no material words is incomplete.
- Describe only what is visible. Do not speculate about anything outside the frame.
- Do not name or guess the artist, character, series or studio. A signature or watermark is static text: quote it like any other visible text, but never resolve it into a name.
- Name the overall art style and medium in one clause (anime illustration, watercolour, oil painting, 3D render, photograph), and say what the line work and finish look like.
- English only, plain prose: no markdown, no bullet points, no headings, no "Caption:" prefix."""


# 英文散文的字符/句经验值。只用于把长度引导从"字符"翻译成"句数"——
# 模型对字符数没有内感受，对句数是有的（实测 v2 的平均 842 字符 ≈ 7~8 句）。
# 抽成模块级函数是因为 devtools 的 A/B 脚本要改长度引导（--caption-range），
# 它们必须和这里用同一个口径，否则会渲染出"250-500 字符（约 4-9 句）"。
_CHARS_PER_SENTENCE = 110

# 中文版 caption 规则。只替换 caption 段，<TAGS> 规则一个字不动——
# danbooru tag 是英文词表体系，中文 tag 在训练侧没有对应嵌入，混进去只是脏数据。
_CAPTION_RULES_ZH = """<CAPTION> 规则：
- 一段连续的中文散文，约 {char_lo}-{char_hi} 个汉字，单行、不换行。写到上限就停：回复会被截断，截断的 caption 一律作废。
- 内容顺序：主体；场景与背景；构图与机位；光线；画面里可见的文字，用双引号原样抄下来。
- 说到的每个物体都要给出颜色和表面质感（哑光、反光、金属、布料、皮肤、玻璃、发丝）。没有颜色和材质词的一段是不完整的。
- 只写画面里看得见的东西，不要推测画框外的内容。
- 不要点名或猜测画师、角色、系列、工作室。签名或水印属于"可见文字"，原样引用，但不要把它解释成人名。
- 用一句话说明整体画风与媒介（日式插画、水彩、油画、3D 渲染、摄影），并交代线条与完成度的特征。
- 这一段必须用简体中文写，不要用英文。纯散文：不要 markdown、不要分条、不要小标题、不要"描述："这类前缀。"""

# 图中文字的条款**不在这个文件里**：它们在 `prompts/图中文字规则.md` 的
# TEXT / TEXT_ZH 两节，由 _prompt_for 按需追加到 caption 规则后面。
# 这样改措辞（字体风格、认不准别猜之类）只需要编辑 markdown，不用碰 Python。
# 曾经有过一个 `<VISIBLE_TEXT>` 结构化 JSON 块（text_exact / language /
# script / legibility），那是规范 v0.2 §6 被理解成"另抄一份表"的产物，
# 已整体移除：主创要的是"文字按原本语言写进 caption/正文"，不是另存一份表。
#
# ★上面 _TAG_RULES / _CAPTION_RULES(_ZH) 同样是**兜底**：正常情况由
#   prompts/打标规则.md 提供，只有文件读不到时才用这里的这份。

# 中文 1 字 ≈ 1 token，英文 4.4 字符/token。同样的 token 预算，中文能写的字数
# 只有英文的 1/4.4 ≈ 0.23。
# ★ 这里曾经写 0.45（"同样信息量汉字只要 45% 字符"，那是信息密度口径）：按它算
#   出的 180~460 字 ≈ 180~460 token，叠上 tag 段（nl_tokens 是整个回复，实测
#   50~80 token）几乎每次都越过 nl_max_tokens=256，中文条目整批被扣成 3.75 分。
#   2026-11-09 用真实分片（danbooru 0000.tar，20 张全中文）实测确认。
#   注意与 quality._CJK_CHAR_SCALE（0.45）不是一回事：那边是"字符数兜底线"的
#   等效信息量换算，这边是生成引导的 token 预算换算，方向相反。
_ZH_CHAR_SCALE = 0.23
# 回复里 tag 段占的 token（按 20~40 个 danbooru tag 实测 50~80，取上限）。
# 中文长度引导的字符上限要留给 caption 的部分 = nl_max_tokens - 这个数。
_ZH_TAG_RESERVE_TOKENS = 80

# 参考 tag 段：源数据自带原始标注时（danbooru 稿），把它们作为输入而不是让模型猜。
# 没有 tag 时一个字都不加——空的 `<REFERENCE>` 会被模型读成"这次没提示"，然后
# 开始编角色名，比不给还糟。
_REF_EN = """

<REFERENCE> the image's original danbooru tags, grouped by kind:
{tags}
- Read the group names literally: `character`, `copyright` and `artist` are identities (who is in the frame, which series, who drew it); `general` and `meta` describe what is visible; `unclassified_tags` came from the source without a category — never treat one of those as an artist name.
- Claim an artist only from an `artist` tag or an artist handle given elsewhere in this prompt; never guess one.
- Use the tags to pin down the character, series, clothing and props at the level of detail they imply.
- Where a tag contradicts what you actually see, trust the image and drop that tag.
- Fold them into your own sentences; never output them as a list, and never invent a name that is not in them."""
# 平铺形态（默认）：一串逗号分隔的 tag，不做类型区分。
# ★分隔符是**逗号不是空格**（早期版本用空格）：danbooru tag 本身只含下划线和
#   字母数字，空格在它里面不合法，逗号才是这种词汇表本来的分隔符。
_REF_FLAT_EN = """

<REFERENCE> the image's original danbooru tags:
{tags}
- They tell you the character, series, clothing and props when the source knew them.
- Where a tag contradicts what you actually see, trust the image and drop that tag.
- Fold them into your own sentences; never output them as a list, and never invent a name that is not in them."""
_REF_FLAT_ZH = """
<参考> 这张图在源数据里的原始 tag：
{tags}
- 它们能告诉你角色、系列、服装与道具（当源数据知道时）。
- 某个 tag 与画面明显矛盾时以画面为准，直接丢掉那一条。
- 把它们融进你自己的句子，不要原样罗列，也不要另外编名字。"""

_REF_ZH = """
<参考> 这张图在源数据里的原始 tag，已按类别分组：
{tags}
- 组名按字面理解：`character`、`copyright`、`artist` 属于身份（画的是谁、出自哪部作品、谁画的）；`general` 与 `meta` 才描述画面；`unclassified_tags` 是源数据没给类别的，绝不能把其中任何一条当成画师名。
- 只有 `artist` 组里的 tag、或本提示别处给出的画师 handle 才算画师，不要猜。
- 用它们来确定角色、系列、服装与道具的名称和细节程度。
- 某个 tag 与画面明显矛盾时以画面为准，直接丢掉那一条。
- 把它们融进你自己的句子，不要原样罗列，也不要另外编名字。"""


# 动漫数据的风格声明（规范 v0.2 §5）：正文里必须出现"动漫风格/日式插画"这类词，
# 目的是让训练侧能一眼分出通用数据与动漫数据——不是风格形容词装饰。
# 已经有一条"必须交代画风与媒介"（v4 加的），但那条只说"点名画风"，模型会写
# "digital painting" 而不点破是动漫；动漫批次混进通用数据集时，这条是唯一
# 能在文本里把两类分开的痕迹。
_MEDIA_ZH = """

<媒体类型> 这张图来自**动漫/日式插画**数据集：正文里必须明确写出"日式动漫插画风格"
（或同义的"动漫风格"）来交代画风，不要只写"数字绘画""插画"这种含糊说法。"""

_MEDIA_EN = """

<MEDIA TYPE> This image comes from an anime / manga dataset: when you name the art style,
say explicitly that it is anime-style (e.g. "anime-style illustration"), not just
"digital painting" or "illustration"."""

# 上游 media 列的取值 → 是否动漫。只认明确的写法；`illustration` 这种不算：
# 插画未必是动漫，判错了会把通用数据标成 anime，正好违反这条要防的事。
_ANIME_WORDS = ("anime", "manga", "danbooru", "illustration_anime", "二次元",
                "动漫", "日式插画")


def is_anime(media: str | None) -> bool:
    """上游的 media/风格标签是不是动漫类。空串/未知一律 False。"""
    text = str(media or "").strip().lower()
    return bool(text) and any(w in text for w in _ANIME_WORDS)


def _media_block(media: str | None, zh: bool = False) -> str:
    """动漫批次追加风格声明。非动漫/未知返回空串。"""
    if not is_anime(media):
        return ""
    return _MEDIA_ZH if zh else _MEDIA_EN


def _ref_block(tags: Sequence[str], zh: bool = False, *,
               vocab: dict[str, str] | None = None,
               grouped: bool = False) -> str:
    """把原始 tag 渲染成 prompt 里的一段。空/全空串返回空串。

    `grouped=False`（默认）走平铺：`1girl, blue_hair, sitting`。
    `grouped=True` 才按 `tag_categories` 分成
    `character_tags:` / `artist_tags:` 等行——那是规范 §7.1 的做法，
    主创看过之后说不懂，默认不起用。

    `vocab` 显式传入是为了测试能钉住各条分支，不依赖开发机上有哪份词表。
    """
    items = [str(t).strip() for t in tags if str(t).strip()]
    if not items:
        return ""
    if grouped:
        text = render_groups(group_tags(items, vocab=vocab))
        return (_REF_ZH if zh else _REF_EN).format(tags=text)
    return (_REF_FLAT_ZH if zh else _REF_FLAT_EN).format(tags=", ".join(items))


def _sentences_for(char_lo: int, char_hi: int) -> tuple[int, int]:
    """字符区间 → 句数区间。下限至少 3 句，上限至少比下限多 1 句。"""
    lo = max(3, round(char_lo / _CHARS_PER_SENTENCE))
    return lo, max(lo + 1, round(char_hi / _CHARS_PER_SENTENCE))


# 模型拒答后的重打指令。
#
# 为什么不是"把原 prompt 再发一遍、只抬温度"：拒答是一条**模板回复**，
# "I'm sorry, I can't help with that" 这类句子对温度不敏感——同一个系统提示
# 下它会在同一处触发。抬温度只会让它换个说法继续拒。
#
# 真正能动的是两件事：
#   1. 明确它现在要做的是什么（给图写客观的视觉描述），把它从"在跟人对话"
#      拉回"在跑一个打标任务"；
#   2. 直接禁止解释与抱歉，并规定输出形状。拒答文本几乎总带着"because…"，
#      一旦允许它解释，它就会一直解释。
#
# 单独写在这里而不是进 prompts/*.md：这份不是**规则**（不描述图该怎么写），
# 而是**解除一层特定故障**的补丁，且必须与语言无关地逐字生效——改了措辞，
# 效果就变了，不适合让人随手编辑。
_FORCE_OUTPUT_EN = (
    "The image has already been reviewed and cleared for annotation. "
    "This is a routine, non-interactive labeling task: your job is to describe "
    "what is visually present, objectively and neutrally.\n"
    "Do not refuse, do not apologise, do not explain your reasoning or mention "
    "any policy. Answer with the requested block only, starting immediately "
    "with the opening tag.")
_FORCE_OUTPUT_ZH = (
    "这张图已经审核通过，本次是常规的、非交互式的标注任务：你的工作是客观、"
    "中立地描述画面里有什么。\n"
    "不要拒绝回答，不要道歉，不要解释理由，也不要提及任何规则或政策。"
    "直接按要求输出对应的块，第一个字就是开头的标记。")


def _prompt_for(output: OutputSpec, *, zh: bool = False,
                extra_rules: str = "", force_output: bool = False) -> str:
    """按配置里的预算生成 prompt。

    ★这里的长度引导与质检硬线刻意不是一个数：
      硬线是 32~256 token（容忍"图简单、话少"，不因此判废），
      引导是 400~1000 字符（≈90~230 token）。
      把引导值也降到硬线，等于告诉模型"写 32 个 token 就行"，
      那一批数据会整体变贫——而贫是没法在质检里被抓到的。

    `extra_rules` 非空表示"这是被截断后的重打"：caption 侧换成
    `prompts/*.md` 的 `## CAPTION_DENSE` 那一节（收窄版），而不是追加条款。
    空串 = 正常路径。
    """
    if output.caption_chars:
        # 显式给了长度引导就用它，不再从 token 闸门派生 —— 两者回答的
        # 是不同问题：闸门管"合格不合格"，引导管"往多长写"。
        char_lo, char_hi = output.caption_chars
    else:
        char_lo = max(400, output.nl_min_tokens * 4)
        char_hi = max(char_lo + 200, min(1200, output.nl_max_tokens * 4))

    lo, hi = output.tag_min_count, output.tag_max_count
    anchor_lo = min(max(30, lo), hi)
    anchor_hi = min(max(60, anchor_lo), hi)
    fill = dict(lo=lo, hi=hi, anchor_lo=anchor_lo, anchor_hi=anchor_hi,
                char_lo=char_lo, char_hi=char_hi)

    if zh:
        # 长度引导按中文口径重算：char 预算原本是"英文字符数"，直接拿去要求
        # 中文，模型会写出 400~1000 个汉字 ≈ 400~1000 token，超过 nl_max_tokens
        # （默认 256）被硬截断，截断的 caption 会被质检判废。
        #
        # ★这段只能跑一次。曾经上下各有一份完全相同的代码，缩放在这里被连乘
        #   两遍（0.23² ≈ 0.053），中文预算掉到 40~100 汉字，实测产出的中文
        #   caption 长度只有英文的 22%，风格描述被压成一句空话。改这里时务必
        #   确认没有第二份。
        char_lo = max(40, round(char_lo * _ZH_CHAR_SCALE))
        char_hi = max(char_lo + 60, round(char_hi * _ZH_CHAR_SCALE))
        # 再按 token 闸门夹一次。上面那步只是"换成中文口径"，没有考虑闸门里
        # 还被 tag 段吃掉一截；不夹的话上限仍会顶到 256 之上，整批中文都被打上
        # "NL 过长"扣分（不是判废，但分数系统性低于英文，混批时就是伪特征）。
        cap_hi = max(80, int(output.nl_max_tokens or 0) - _ZH_TAG_RESERVE_TOKENS)
        if char_hi > cap_hi:
            char_lo = max(40, round(char_lo * cap_hi / char_hi))
            char_hi = cap_hi
        fill.update(char_lo=char_lo, char_hi=char_hi)
        cap_fallback = _CAPTION_RULES_ZH
    else:
        # 句数口径：模型数不准字符，但数得准句子。它只是"roughly"，
        # 验收仍走 token 硬线，所以取整到句就够了。
        sent_lo, sent_hi = _sentences_for(char_lo, char_hi)
        fill.update(sent_lo=sent_lo, sent_hi=sent_hi)
        cap_fallback = _CAPTION_RULES

    # ★规则优先从 `prompts/*.md` 读；文件没提供对应节时才用上面那几份内置块。
    #   mode 决定要哪几套：tag 类带 TAG 规则，nl 类带 CAPTION 规则，text_rules
    #   开关决定要不要再追加图中文字规则。切到哪个 tab 就强制带哪套，不靠配置。
    file_tag, file_cap, file_text = rules_for(
        output.mode, zh=zh, text_rules=output.text_rules)
    tag_rules = (file_tag or _TAG_RULES).format(**fill)
    # extra_rules 非空 = 这一次是"被截断后的重打"。此时换成 CAPTION_DENSE
    # 那一节（整节替换），而不是往常规规则后面追加条款：
    #   追加只能"再说一句别写太长"，而被打断的那类图（气泡堆满的漫画分镜）
    #   要的是换一套交代顺序——只说撑起画面的那几样、气泡不复述对白。
    #   说明见 prompt_rules.SECTION_CAPTION_DENSE。
    dense_cap = (_dense_caption(zh) if extra_rules else "")
    cap_rules = (dense_cap or file_cap or cap_fallback).format(**fill)
    if file_text:
        cap_rules = cap_rules + "\n" + file_text

    mode = output.mode
    if mode == "tag":
        text = ("Produce one block about the image, in exactly this format:\n\n"
                "<TAGS>\ntag_one, tag_two, tag_three\n</TAGS>\n\n"
                f"{tag_rules}\n\nOutput the TAGS block and nothing else.")
    elif mode == "nl":
        text = ("Produce one block about the image, in exactly this format:\n\n"
                "<CAPTION>\none paragraph of description\n</CAPTION>\n\n"
                f"{cap_rules}\n\nOutput the CAPTION block and nothing else.")
    else:
        # both 模式必须写"nothing else"：模型加一句 "Here are the tags:" 不致命
        # （正则能抓），但 max_tokens 是按「NL 全量 + tag 全量 + 标记」抠出来的，
        # 那段前言是从 caption 的预算里偷的。
        text = ("Produce two blocks about the image, in exactly this format:\n\n"
                "<TAGS>\ntag_one, tag_two, tag_three\n</TAGS>\n"
                "<CAPTION>\none paragraph of description\n</CAPTION>\n\n"
                f"{tag_rules}\n\n{cap_rules}\n\n"
                "Output these two blocks, in this order, and nothing else.")
    if force_output:
        # 接在最前面而不是最后面：拒答是在**读到任务描述的第一时间**决定的，
        # 把它放在整段规则之后，模型往往已经在心里拒了。放开头相当于先解除
        # 它那层"我在被要求做不该做的事"的预设，再读具体规则。
        text = (_FORCE_OUTPUT_ZH if zh else _FORCE_OUTPUT_EN) + "\n\n" + text
    return text


# 三道正则都写成「闭合标签可有可无」：模型偶尔只写开标记就往下走，
# 那种输出仍然有值，硬要求闭标记会把它整条判废。
# 但"没有闭标记"不等于"一直到文末"：如果后面紧跟着另一段的开标记，
# 必须在那儿截断，否则 `<CAPTION>` 的正文会被当成 tag 洗进词表
# （实测会产出 captiona_woman_with 这种鬼东西）。★加新块时**每一道**的
# lookahead 都要补上，漏一道就等于让新块的正文流进上一个块。
_TAG_BLOCK = re.compile(
    r"<TAGS>(.*?)(?:</TAGS>|(?=<CAPTION>)|\Z)", re.S | re.I)
_CAPTION_BLOCK = re.compile(
    r"<CAPTION>(.*?)(?:</CAPTION>|(?=<TAGS>)|\Z)", re.S | re.I)

def hit_token_cap(tokens: int, max_tokens: int) -> bool:
    """回复是否用满了生成长度上限——也就是被服务端截断（finish_reason=length）。

    ★别用"缺 `</CAPTION>` 闭标记"来判断：模型偷懒少写闭标记很常见，那种输出
      是完整的。实测 h2 英文 769 字符、正常以句号收尾，只是漏了闭标记；拿标记
      判定会让它白跑一次重试。用满 max_tokens 才是截断的精确特征。
    """
    return max_tokens > 0 and tokens >= max_tokens

# 画师占位符相关旗标：写进产物 `flags`，可以按字符串直接筛出来。
# 保护是"模型见不到真名"的唯一保证，一旦有漏必须能被检索到，不能静默通过。
ARTIST_FLAG_LEAKED = "artist_name_leaked"
ARTIST_FLAG_UNKNOWN_PLACEHOLDER = "artist_placeholder_unknown"
# 降阈值重筛后仍然不足 tag_min_count、但够 tag_min_accept，就如实采纳并打这个
# 旗标。为什么不直接放行：这批图的 tag 数是"低于常规下限"的真实情况，事后
# 清洗/统计必须能把它们单独拎出来，否则"每条都 >= 12 个"这个口径就假了。
TAG_FLAG_LOW_COUNT = "low_tag_count"
# 未知下标（模型编造，或映射丢了半边）原样留在正文里，这里只用于告警统计。
_PLACEHOLDER_ANY = re.compile(r"__ARTIST_\d+__")

_TAG_SPLIT = re.compile(r"[,;\n、]+")
_TAG_BAD = re.compile(r"[^a-z0-9_\-.:()]+")

# 只砍两类词，别的都留：
#   质量词   —— 提示词模板的残留（"masterpiece, best quality"），不是画面事实
#   身份词   —— 模型在猜作者/角色/系列，那是它不该产出的东西，混进 tag 词汇表
#              之后训练侧的"画师绑定"就被污染了
_TAG_DROP = frozenset({
    "masterpiece", "best_quality", "high_quality", "normal_quality", "good_quality",
    "highres", "absurdres", "lowres", "ultra_detailed", "extremely_detailed",
    "detailed", "8k", "4k", "uhd", "hd",
    "score_9", "score_8_up", "score_7_up",
    "safe", "sensitive", "nsfw", "explicit", "questionable",
    "artist_name", "character_name", "series_name", "copyright_name",
    "signature", "artist_logo",
})

_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}

# 结果缓存的默认落点：与 run/fingerprint.json 同级。
# 放 out_dir 里就只能命中"同一个输出目录"，而重跑时多半会换个目录（A/B、
# 换日期、换批次名），那样缓存等于白做——所以默认跨任务共享一份。
CACHE_DB_PATH = Path("run") / "result-cache.sqlite3"



TAG_SUFFIX = "-tag.json"
NL_SUFFIX = "-nl.json"

# 质检只看最显著的前 N 个 tag。
# 这是 tag 上限提到 160 之后的必要配套：quality 的覆盖率门槛是"60% 的
# 强 tag 要被 caption 覆盖"，而 caption 只有 256 token。要求一张群像图的
# 160 个 tag 里有 96 个出现在两百来个词里，是不可能的——结果是高密度图
# 成批进重试再被判废，而那恰恰是最有价值的样本。
# 前 20 个是 prompt 里明确要求"按重要性降序"的那一段，它们必须在 caption
# 里出现，否则就是 tag 与描述各说各话。
_QC_TAG_HEAD = 20

# 连续多少条配置性错误（401/403/404）就中止整批。
# 不做这件事的代价：地址少写一个 /v1 时，30 万张会一张张走完请求、一张张失败，
# 进度条跑到 100% 才告诉你"全灭"；而真正该发生的是第 20 张就停下来。
_FATAL_BREAK = 20


class InferenceError(Exception):
    """单次请求失败。

    三个标记决定"接下来怎么办"，缺一个就会做出错误的选择：
      * `retryable`：同一张图再试一次有没有可能成功（超时、5xx、429）；
      * `fatal`：是不是与这张图无关的配置错误（密钥/模型名/地址）。这类错误
        再试只是白烧时间，连"换下一张"都没意义——整批都得停；
      * `retry_after`：服务端明确要求的等待秒数（429 的 Retry-After）。
    """

    def __init__(self, message: str, *, retryable: bool = True,
                 fatal: bool = False, retry_after: float | None = None,
                 status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.fatal = fatal
        self.retry_after = retry_after
        self.status = status


class Stopped(Exception):
    """守护判定为 stop（磁盘满等）。继续写只会产生坏产物，直接收工。"""


# ------------------------------------------------------------------ 解析


def normalize_tags(raw: str) -> list[str]:
    """把模型吐的 tag 段洗成可入训练集的形态。

    规则刻意保守：只做「必须做」的转换（小写、空白转下划线、去非法字符），
    不做同义词归并、不做单复数归一——那些属于训练侧的词表，客户端擅自归一
    会让同一张图在两台机器上产出不同的 tag。
    """
    out: list[str] = []
    seen: set[str] = set()
    for part in _TAG_SPLIT.split(raw):
        t = part.strip().lower().replace(" ", "_")
        if not t:
            continue
        t = _TAG_BAD.sub("", t)
        t = re.sub(r"_{2,}", "_", t).strip("_")
        if not t or len(t) < 2 or len(t) > 64:
            continue
        if t in _TAG_DROP or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def normalize_caption(raw: str) -> str:
    """压成一段。保留标点与引号，只去 markdown 装饰与多余空白。"""
    text = raw.strip()
    text = re.sub(r"^\s*(caption|description)\s*[:：]\s*", "", text, flags=re.I)
    text = text.replace("**", "").replace("##", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_output(text: str, mode: str) -> tuple[list[str], str]:
    """从模型输出里取出 (tags, caption)。

    没有块标记时按模式兜底：tag/nl 单模式把整段当结果；both 模式直接判空——
    在两个块的输出里靠猜切分，等于批量制造脏数据，宁可让它进重试。
    """
    body = (text or "").strip()
    if not body:
        return [], ""

    have_marker = bool(_TAG_BLOCK.search(body) or _CAPTION_BLOCK.search(body))
    if not have_marker:
        if mode == "tag":
            return normalize_tags(body), ""
        if mode == "nl":
            return [], normalize_caption(body)
        return [], ""

    tags: list[str] = []
    caption = ""
    if mode in ("tag", "both"):
        m = _TAG_BLOCK.search(body)
        tags = normalize_tags(m.group(1)) if m else []
    if mode in ("nl", "both"):
        m = _CAPTION_BLOCK.search(body)
        caption = normalize_caption(m.group(1)) if m else ""
    return tags, caption


# ------------------------------------------------------------------ 任务


@dataclass(slots=True)
class TaskRow:
    """一行待处理的活。shard 记着来源，读图时要靠它定位 zip。"""

    shard: str
    image_id: str
    file: str
    artist: str = ""
    # 这个 artist 从哪来：`table`（封包/分包时按 post id 查表烧进包里的，
    # 逐图确认过）或 `path`（目录名）。打标侧靠它决定优先级——见 _artist_names。
    artist_source: str = ""
    # 多画师全量（包内 meta 有时才有）。artist 只是第一位。
    artists: tuple[str, ...] = ()
    # 上游数据的 revision（HF commit / 本地批次号）。写进产物的
    # `source_revision`：同一份 manifest 换了上游 commit，标注结论不能混用。
    source_revision: str = ""
    # 源数据里的原始 danbooru 标注（已分类与否都行，只用于喂 prompt）。
    # 没有就是空元组——不写空段落（见 _ref_block）。
    ref_tags: tuple[str, ...] = ()
    # 上游标注的数据类别：`anime` / `photo` / ... 空串 = 不知道。
    # 规范 v0.2 §5 要求动漫样本的正文里必须出现"动漫风格"这类词（中英各按其
    # 叙述语言取词），用来把通用数据和动漫数据分开。不知道就不写——比猜错好，
    # 猜错等于往通给数据里塞"anime"字样，那正好是这条要防的事。
    media: str = ""
    rel_path: str = ""
    # HF 模式下才有值：`hf://datasets/<repo>/<shard>#<member>`。
    # 会原样写进产物，主机拿它拼总表的 image_path。
    image_path: str = ""

    @property
    def ext(self) -> str:
        return normalize_ext(self.file)


@dataclass(slots=True)
class Outcome:
    """一张图的处理结果。失败也带原因，便于按原因聚合。"""

    image_id: str
    ok: bool
    tag: dict | None = None
    nl: dict | None = None
    error: str = ""
    wall_ms: int = 0
    attempts: int = 0
    # 输出 token。API 模式按量计费时要与 prompt_tokens 一起看。
    tokens: int = 0
    # 输入 token（含图片）。本机模式下只是个数字，云端才是账单大头。
    prompt_tokens: int = 0
    # 这条撞了几次 429。单列出来，"变慢了"和"被限流"是两回事。
    rate_limited: int = 0
    # 失败原因属于配置错误（密钥/模型名/地址）时置位：上层据此熔断整批，
    # 否则一个错 key 会让 30 万张逐张演一遍同样的失败。
    fatal: bool = False
    # 这条是结果缓存命中得来的：没花 GPU。产物照写，provenance 里会标 cache_hit。
    cached: bool = False
    # 第几个推理槽（多卡时 = 第几张卡）。-1 = 单服务模式。
    # 只用于统计"每张卡各打了多少"，不进产物：换卡不该改变产出内容，
    # 进了 fingerprint 反而会让同一批数据被判定成两代。
    slot: int = -1
    # 被**主动跳过**（不是失败）：目前只有"输入图小到没有内容"这一种
    # （见 OutputSpec.min_image_bytes）。为什么单列而不复用 ok=False：
    #   失败 = 该成功却没成功，要么修参数要么修代码，重跑有意义；
    #   跳过 = 这张图本来就不该进数据集，重跑永远同样的结果。
    # 两者混在一起，失败率这个数就永远说不清，而这批数据里 5638 字节的
    # 纯色图恰好有一串（同一张占位图被复制了多次），量足以污染统计。
    skipped: bool = False
    # 模型拒答（`quality.looks_like_refusal` 命中）。
    #
    # 单独一个标记而不是从 error 串里回查：重试那条路必须先判"这是不是拒答"
    # 再决定下一步动作，靠 `"疑似拒答" in error` 去判，改一个字文案就静默失效。
    #
    # ★为什么拒答要特殊对待：质检把它判 drop，而 drop 是"别再试了"——可拒答
    #   恰恰是**唯一值得硬试一次**的失败。它大多不是图坏了，是模型给套进了
    #   "我不能描述这张图"的模板；换一句更强硬的指令 + 换温度重打，成功率高，
    #   成本就是一次推理。和其它 drop（重复率高、覆盖率崩）混在一起不重试，
    #   handful 的图就被白白丢掉。
    refused: bool = False
    # 重试后仍失败、按"坏样本"剔除出分母（见 OutputSpec.retry_failed_rounds）。
    # 置位时 outcome.ok 仍然为 False，但 commit 会写 excused.jsonl 而不是
    # failures.jsonl，并且不计入分母——那是"已经放弃"，不是"下次再做"。
    excused: bool = False
    # 静默放弃：老样本（`image_id` 不在本轮输入里）已经彻底剔除过，不再重试，
    # 也不该再算一次失败。只由"重试失败项"那条路产生。
    retired: bool = False


# 进度回调。kind ∈ {"done", "failed", "skipped", "blacklisted", "excused"}：
#   done / failed 带 outcome（failed 里含写盘失败，那种同样没有产物）；
#   skipped / blacklisted 一般不带 outcome——这两张根本没去推理，编一个出来
#   只会骗人。唯一的例外是"输入图过小"：它有 outcome（带 skipped=True 与
#   原因），因为订阅方要靠它区分"产物已有"和"这张永远不会产出"。
#   excused = 重试后仍失败、已从分母剔除的坏样本，带 outcome（ok=False）。
# 订阅方（Web UI 或任何前端）靠它推实时进度，CLI 不传就等于没有。
ProgressHook = Callable[[str, TaskRow, "Outcome | None", "ClientStats"], None]


class StageTimer:
    """分段计时：read / tag / b64 / http / parse / gate / write。

    为什么不用 cProfile：它按函数调用切分，一次请求会被切成上千个片段，
    加上 GIL 切换和 I/O 等待，结果反而看不出哪一段是瓶颈。这里只记七个
    粗粒度阶段，够回答"时间到底花在哪"——而这正是决定"要不要优化"的唯一依据。

    注意：各阶段不是互斥的。tag（CPU 反推）与 http（等 GPU）是并行跑的，
    所以各阶段的 total 加起来会超过墙钟时间，别把它当占比看；看 avg_ms
    （每次调用平均多久）和 count（发生了几次）才有意义。
    """

    STAGES = ("read", "tag", "b64", "http", "parse", "gate", "write")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total: dict[str, float] = {}
        self._count: dict[str, int] = {}

    def add(self, stage: str, seconds: float) -> None:
        with self._lock:
            self._total[stage] = self._total.get(stage, 0.0) + seconds
            self._count[stage] = self._count.get(stage, 0) + 1

    @contextmanager
    def track(self, stage: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.add(stage, time.perf_counter() - t0)

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            items = list(self._total.items())
            counts = dict(self._count)
        out: dict[str, dict[str, float]] = {}
        for stage, total in items:
            n = counts.get(stage, 0)
            out[stage] = {
                "total_ms": round(total * 1000.0, 1),
                "count": n,
                "avg_ms": round(total * 1000.0 / n, 2) if n else 0.0,
            }
        return out


@contextmanager
def _null_track():
    """占位用的空计时。没有计时器时不必在每个调用点写 if 判断。"""
    yield


@dataclass(slots=True)
class ClientStats:
    total: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    # 累计失败过多次、这次直接不理的。单列出来，否则"跳过"里混着
    # "已完成"和"永远做不成"，看报告的人会以为进度正常。
    blacklisted: int = 0
    busy: int = 0  # 其他进程正在处理，不能算已完成或写整片完成标记。
    # 因为 `skip_when_missing` 而整张跳过的数量。
    # 单独一个计数器是因为它和「产物已有」的跳过意义完全不同：
    # 前者是正常续跑，后者往往意味着画师源没配好。
    artist_skipped: int = 0
    # 因为输入图过小（无内容）被跳过的数量。与上面两个都不同：这批图**永远
    # 不会**有产物，分母里也不该算它们。单列一个数，报告才能回答"有效张数"。
    blank_skipped: int = 0
    # 重试后仍然失败、已按"坏样本"剔除出分母的数量。
    # 与 failed 的关系：failed 是"这一次没成"，excused 是"已经放弃了"。
    # 两者不能合并——失败率回答"模型好不好"，剔除数回答"这批素材能用多少"，
    # 混成一个数之后，一个 3% 的失败率到底是模型差还是素材脏就说不清了。
    excused: int = 0
    tokens: int = 0
    # 输入 token。云端按量计费时它才是大头（图片 base64 动辄几千）。
    prompt_tokens: int = 0
    # 撞 429 的总次数。为 0 时不出现在报告里，免得本机任务多一行噪音。
    rate_limited: int = 0
    # 价目表命中时才有值；None = 没配价格，只报 token（宁可不报也不编）。
    cost: float | None = None
    wall_ms: int = 0
    # done 里有多少条是直接拿缓存、一张图都没送进模型的。
    cached: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def effective_total(self) -> int:
        """分母：真正应该出产物的张数。

        扣掉两类**永远不会**有产物的图：输入无效（blank_skipped）和重试后
        确认救不回来的坏样本（excused）。不扣的话，那个 12121 里永远混着
        几十张纯色图，失败率就永远下不到 0，而这个"下不到 0"跟模型无关。
        """
        return max(0, self.total - self.blank_skipped - self.excused)

    def summary_lines(self) -> list[str]:
        lines = [
            f"总共       : {self.total}",
            f"完成       : {self.done}",
            f"其中缓存   : {self.cached}",
            f"跳过（已有）: {self.skipped}",
            f"跳过（拉黑）: {self.blacklisted}",
        ]
        if self.artist_skipped:
            lines.append(f"跳过（无画师）: {self.artist_skipped}")
            # 全部被跳过是最危险的形态：开关忘了配画师源。
            if not self.done and not self.failed:
                lines.append("警告       : 开关 artist.skip_when_missing 但一张都没拿到画师，"
                             "请检查 artist.table 或分片目录布局"
                             "（目前是全部跳过，不会有产物）")
        if self.blank_skipped:
            # 单独报，并明说"不计入分母"：这批图是输入无效，不是失败。
            # 混进失败率里会让人以为模型有问题，进而去调阈值（白调）。
            lines.append(f"跳过（无内容图）: {self.blank_skipped}"
                         "（输入过小，不计入失败；明细见 skipped.jsonl）")
        if self.excused:
            # 「剔除」与「失败」分开写：前者是已经放弃了、分母里减掉了，
            # 后者是这一趟没成、下一趟还会再做。合并之后没人分得清
            # "3% 失败"是模型不行还是素材脏。
            lines.append(f"重试后剔除 : {self.excused}（坏样本，已从分母扣除）")
        lines += [
            f"失败       : {self.failed}",
            f"有效张数   : {self.effective_total()}",
            f"输入 token : {self.prompt_tokens}",
            f"输出 token : {self.tokens}",
        ]
        if self.rate_limited:
            lines.append(f"被限流(429): {self.rate_limited} 次")
        if self.busy:
            lines.append(f"其他进程处理中: {self.busy}（中断后重跑会重新核对）")
        if self.cost is not None:
            lines.append(f"估算费用   : {self.cost:.4f}（按 configs/nl_api.json 的价目）")
        if self.done or self.failed:
            n = self.done + self.failed
            avg = self.wall_ms / n / 1000 if n else 0.0
            lines.append(f"平均耗时   : {avg:.3f}s/张")
        for reason, n in sorted(self.by_reason.items(), key=lambda kv: -kv[1])[:8]:
            lines.append(f"  {reason:<28} {n}")
        return lines


# ------------------------------------------------------------------ 读图


class ShardReader:
    """按 manifest 从包里取图。zip 与 tar 都认。

    句柄缓存 + 一把锁：`ZipFile.read` / `TarFile.extractfile` 内部都会 seek，
    多线程共用一个句柄而不加锁会读到别人的字节（错位、偶发、极难复现）。
    锁的代价在这里可以忽略——真正的瓶颈是 GPU 推理，不是这点读盘。

    为什么 tar 也走随机读而不是顺序流：本地只放一个 shard 的时候，
    顺序读会要求主流程严格按 tar 内顺序消费，而这跟"跳过已完成、并发乱序
    返回"是冲突的。tarfile 的 "r:" 模式支持按成员名取，代价只是多一次 seek。
    """

    def __init__(self, shards: Iterable[str]):
        self._handles: dict[str, object] = {}
        self._kinds: dict[str, str] = {}
        self._lock = threading.Lock()
        for s in shards:
            if s.lower().endswith((".tar", ".tar.gz", ".tgz")):
                self._handles[s] = tarfile.open(s, "r:*")
                self._kinds[s] = "tar"
            else:
                self._handles[s] = zipfile.ZipFile(s, "r")
                self._kinds[s] = "zip"

    def read(self, shard: str, member: str) -> bytes:
        handle = self._handles.get(shard)
        if handle is None:
            raise KeyError(f"分发包未打开: {shard}")
        with self._lock:
            if self._kinds[shard] == "tar":
                fh = handle.extractfile(member)       # type: ignore[union-attr]
                if fh is None:
                    raise KeyError(f"{Path(shard).name} 里没有成员 {member}")
                try:
                    return fh.read()
                finally:
                    fh.close()
            return handle.read(member)                # type: ignore[union-attr]

    def close(self) -> None:
        for handle in self._handles.values():
            try:
                handle.close()                        # type: ignore[union-attr]
            except OSError:
                pass
        self._handles.clear()


def _mime_of(name: str) -> str:
    return _MIME.get(Path(name).suffix.lower(), "image/jpeg")


# ------------------------------------------------------------------ 落盘


def _write_json_atomic(path: Path, payload: dict) -> None:
    """tmp + fsync + replace。

    直接 open(w) 写的话，断电/崩溃会留下一个能被 json.loads 读出一半的文件，
    merge 那边只看"文件在不在"，这条就会被当成有效产出收进总表。
    """
    from .storage.atomic import write_json
    write_json(path, payload)


def _existing_ok(path: Path, key: str, expect: dict | None = None) -> bool:
    """产物已存在且该字段非空才算完成。与 merge 的判空口径保持一致。

    `expect` 非空时再比对一遍 provenance：产物在、但模型 / 精度 / 像素预算 /
    prompt 已经换了，就不算"做完"。这是"续跑"最容易骗自己的地方——只看
    文件非空的话，换模型重跑会静默复用上一代模型的产出。

    旧产物缺这些字段时按"无法判定"放行：升级后的第一次运行不该把历史产物
    全部重打一遍（那可能是几百 GB 的重复推理）。
    """
    if not path.is_file():
        return False
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(obj, dict):
        return False
    if obj.get("ok") is False:
        return False
    if not str(obj.get(key) or "").strip():
        return False
    if expect:
        prov = obj.get("provenance")
        if isinstance(prov, dict):
            for name, want in expect.items():
                if name in prov:
                    have = prov.get(name)
                    # 统一成字符串比：指纹里的 max_pixels 可能是 int，
                    # 老产物里可能是 str，类型差异不该判成"参数变了"。
                    if ("" if have is None else str(have)) != (
                            "" if want is None else str(want)):
                        return False
    return True


# ------------------------------------------------------------------ 客户端


class _BatchTagExecutor:
    """单线程 + 攒批的 tag 执行器。

    为什么不用 ThreadPoolExecutor：它是"一次交一个任务"，模型永远只见到 batch=1。
    GPU 上前向的固定开销（kernel 发射 1600 次、CPU 侧 dispatch）跟 batch 无关，
    所以只有把图凑进同一次前向才拿得到收益（实测 79 张：fp32 57.8 → bf16+b4 17.3
    ms/张）。

    为什么**不等**：攒批最容易写错的地方是"等一小会儿凑更多"。这里只取队列里
    已经排着的（get_nowait），永不为了凑数阻塞——tag 与 GPU 侧的 NL 是并行跑的，
    人为等待会直接把 NL 的完成时间拖后。队列空着时它就和逐张跑一模一样。
    """

    def __init__(self, fn, *, max_batch: int = 1, stage=None):
        import queue

        self._queue = queue.Queue()
        self._fn = fn
        self._max = max(1, int(max_batch))
        self._stage = stage
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name="tag-batch",
                                        daemon=True)
        self._thread.start()

    def submit(self, item):
        fut: Future = Future()
        if self._closed:
            fut.set_exception(RuntimeError("tag 执行器已关闭"))
            return fut
        self._queue.put((item, fut))
        return fut

    def _loop(self) -> None:
        import queue

        while True:
            first = self._queue.get()
            if first is None:
                return
            batch = [first]
            while len(batch) < self._max:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._queue.put(None)
                    break
                batch.append(nxt)
            items = [it for it, _ in batch]
            try:
                outs = self._fn(items)
                if len(outs) != len(items):
                    raise RuntimeError(
                        f"tag 后端返回 {len(outs)} 条，提交了 {len(items)} 张")
            except BaseException as exc:  # noqa: BLE001 - 必须原样转给等待方
                for _, fut in batch:
                    if not fut.done():
                        fut.set_exception(exc)
                continue
            for (_, fut), out in zip(batch, outs):
                if not fut.done():
                    fut.set_result(out)

    def shutdown(self, wait: bool = False) -> None:
        self._closed = True
        self._queue.put(None)
        if wait:
            self._thread.join(timeout=5.0)


class TaggerClient:
    """单张图的完整处理：取图 → 推理（含重试）→ 质检 → 组织产物。

    这一层不做 IO 之外的决策：写文件、统计、让步都在 run() 里，
    这样并发与单线程走的是同一条提交路径，不会有两套口径。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        output: OutputSpec,
        temperature: float,
        top_p: float,
        max_tokens: int,
        timeout_s: int,
        max_attempts: int,
        gate: QualityGate,
        prov_base: dict,
        artist_rule: ArtistRule,
        use_file_url: bool = False,
        spool_dir: Path | None = None,
        seed: int | None = None,
        tag_backend: TagBackend | None = None,
        need_tags: bool = True,
        prompt_text: str | None = None,
        prompt_zh_text: str | None = None,
        system_text: str | None = None,
        variant: str = "",
        stage: StageTimer | None = None,
        pixel_cap: int = 0,
        concurrency: int = 1,
        escalate_url: str = "",
        escalate_model: str = "",
        cache: ResultCache | None = None,
        provider: str = "",
        api_key: str = "",
        caption_zh_ratio: float = 0.0,
        ref_grouped: bool = False,
        repeat_penalty: float = 0.0,
    ):
        # 推理来源："" = 本机/自建（OpenAI 兼容 + 环境指纹）；
        # openai / anthropic = 外部 API。方言差异全在 nl_api，这里只记下来。
        self.provider = nl_api.normalize_provider(provider)
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.output = output
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        # 0 = 不发这个字段（vLLM 与云端都要保持原形状）。>0 才会进请求体，
        # 且只有本地引擎吃得到，见 nl_api.build_payload。
        self.repeat_penalty = float(repeat_penalty or 0.0)
        self.max_attempts = max_attempts
        # 这一趟是不是"重试失败项"。只影响一件事：失败时判成 excused（剔除）
        # 还是普通 failed（下次还会做）。由 run_client 按 only_failed 置位。
        self._retrying_failed = False
        self.gate = gate
        self.prov_base = prov_base
        self.artist_rule = artist_rule
        # 画师对照表：配置里给了路径就优先查表，查不到再退回目录名。
        # 表是"danbooru 原始 artist tag"，目录名是人工整理的，两边都可能有缺。
        self.artist_table = ArtistTable(artist_rule.table)
        # file:// 只有本机引擎认。API 模式硬关掉：云端收不到本机路径，
        # 开着只会让每张图被 400 拒绝，而错误只说"参数非法"。
        if self.provider and use_file_url:
            print(f"[client][警告] {nl_api.LABELS[self.provider]} 不支持 file:// 传图，"
                  f"本次改回 base64。", file=sys.stderr)
            use_file_url = False
        self.use_file_url = use_file_url
        self.spool_dir = spool_dir
        self.seed = seed
        # 服务端 mm-processor-kwargs 的钳制上限（来自指纹）。0 = 未知。
        # 重试提分辨率最多抬到这里：再往上服务端还是会钳回去。
        self.pixel_cap = int(pixel_cap or 0)
        # 提示词有三种来源，优先级：prompt/*.json（WebUI 选的）> AB 变体 > 内置生成。
        # prompt_text 是整段指令（不追加），因此文件里怎么写就怎么发。
        self.prompt = prompt_text or _prompt_for(output)
        # 这一份是不是"整段替换"来的。画师句规则只在**内置生成**的那份上追加：
        # 自定义提示词是整段替换（见 prompt/README），往里塞条款等于违背它自己的口径，
        # 操作者改的那份文字会和他看到的不一样。
        self._prompt_is_custom = bool(prompt_text)
        # system 那句话同理：文件里写了就用文件的，否则用内置那份。
        self.system = system_text or SYSTEM_PROMPT
        # 中文变体：只在真要产 NL、且没被整段替换时才生成。
        # 默认 0 = 全英文，历史行为不变；要多少由上层（JobSpec）传下来。
        self.caption_zh_ratio = max(0.0, min(1.0, float(caption_zh_ratio or 0.0)))
        if prompt_zh_text:
            # 文件自带中文版：比例 > 0 才用；没写就不出中文——不能拿内置中文版
            # 去配自定义英文版，两边要求不同，出来的数据没法一起用。
            self._prompt_zh_is_custom = True
            self.prompt_zh = (prompt_zh_text
                              if self.caption_zh_ratio > 0 and output.wants_nl
                              else "")
        else:
            self._prompt_zh_is_custom = False
            self.prompt_zh = (
                _prompt_for(output, zh=True)
                if self.caption_zh_ratio > 0 and not prompt_text and output.wants_nl
                else ""
            )
        # 截断重打用的"不复述小字"版指令。只在配置给了 dense_retry_rules、
        # 且主指令不是整段替换（自定义 prompt）时才生成——自定义 prompt 是
        # "整段替换"的语义，往里插规则会让操作者看到的文字与实际发出去的不符。
        self._dense_prompt = ""
        self._dense_prompt_zh = ""
        if output.dense_retry_rules and not prompt_text and output.wants_nl:
            # 收窄版 caption 规则是 `prompts/` 里的一节（CAPTION_DENSE）。
            # 只有真的存在才准备重打指令：读不到就用常规规则重打一次等于原地
            # 再撞一次上限，白白多花一次 800 token。
            if _dense_caption(False):
                self._dense_prompt = _prompt_for(
                    output, extra_rules=output.dense_retry_rules)
            # 中文版也要一份：中文图的正文指令与英文不同（字数按汉字算），
            # 拿英文那份去配中文图，模型会按英文口径写，长度直接失控。
            if self.caption_zh_ratio > 0 and _dense_caption(True):
                self._dense_prompt_zh = _prompt_for(
                    output, zh=True, extra_rules=output.dense_retry_rules)
        self.variant = variant
        # 参考 tag 是平铺还是分组。默认平铺——分组是规范 §7.1 的做法，
        # 主创确认不需要。两种形态渲染出的 prompt 不同，指纹里要分开。
        self.ref_grouped = bool(ref_grouped)
        # 分段计时器。默认自己建一个，好让单独调用 TaggerClient 的代码
        # （测试、基准脚本）不用关心它；正式流程会把它传进来以便汇总输出。
        self.stage = stage or StageTimer()
        # tag_backend 非空时，output 已经被收窄成"VLM 只出 NL"；
        # need_tags 记的是"本次要不要写 tag 文件"，两个概念不能混。
        self.tag_backend = tag_backend
        self.need_tags = need_tags and (output.wants_tag or tag_backend is not None)
        self._tag_pool: _BatchTagExecutor | None = None
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            # 鉴权头挂在客户端上而不是每次 post 传：两种方言的头不一样
            # （Bearer vs x-api-key + anthropic-version），但都不随单张变化。
            headers=nl_api.auth_headers(self.provider, self.api_key),
            # 连接池要跟并发同步放大。写死 32 的话，并发调到 64 时后一半请求
            # 在客户端排队，看起来"并发没生效"，而服务端其实还有空位。
            limits=httpx.Limits(
                max_connections=max(32, int(concurrency) * 2),
                max_keepalive_connections=max(16, int(concurrency)),
            ),
            # 本机地址不吃系统代理。打标机常开着 Clash 一类的代理，
            # 它的规则未必放过 127.0.0.1：轻则每张图的 base64 多绕一圈
            # （吞吐直接砍半），重则请求根本到不了 vLLM，报出来的却是
            # "服务端超时"。远程地址维持默认，那边可能真需要代理。
            trust_env=trust_env_for(self.base_url),
        )
        # 重试升级：质检判定"重试用尽仍不合格"时可以换一个更大的模型再打一次。
        # vLLM 一个进程只服务一套权重，所以升级目标只能是**另一个地址**上的
        # 第二份服务。地址/模型名留空 = 不启用（默认），此时 model_hint 被忽略。
        # 独立连接池：两个服务可能在不同机器，超时与代理策略都该各算各的。
        self.escalate_model = (escalate_model or "").strip()
        self._escalate_http: httpx.Client | None = None
        self._escalate_base = (escalate_url or "").strip().rstrip("/")
        if self.escalate_model and self._escalate_base:
            self._escalate_http = httpx.Client(
                timeout=httpx.Timeout(timeout_s, connect=10.0),
                # 升级服务可能是另一台机器上的 vLLM，也可能是同一家 API 的
                # 大模型档：方言跟着主服务走，密钥也复用（同账号）。
                headers=nl_api.auth_headers(self.provider, self.api_key),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                trust_env=trust_env_for(self._escalate_base),
            )
        else:
            # 只配了一半要报出来：静默不生效比报错更难查。
            if self.escalate_model or self._escalate_base:
                print("[client][警告] 重试升级需要同时给地址与模型名，"
                      "当前只有一半，升级不会生效。", file=sys.stderr)
            self.escalate_model = ""

        # 结果缓存：None = 不启用（默认）。prefix 是"一次运行内恒定的参数指纹"，
        # 每张图只再拼一个内容哈希就能查表，不必逐张重算参数。
        self.cache = cache
        self.cache_prefix = self._cache_fingerprint() if cache is not None else ""

    # -------------------------------------------------------------- 缓存

    def _policy_fingerprint(self) -> dict:
        """质检策略也是"影响产出"的参数：判定线一变，重试几次、最后留下哪条
        文本都会变。漏掉它的后果是换阈值重跑拿到旧阈值的产出。"""
        policy = getattr(self.gate, "policy", None)
        names = getattr(policy, "__slots__", ()) if policy is not None else ()
        return {name: getattr(policy, name, None) for name in names}

    def _cache_fingerprint(self) -> str:
        """一次运行内恒定的参数指纹，拼在内容哈希前面。

        ★加字段的门槛很低，删字段的门槛极高：漏一项不是"少命中"，
        而是"换参数重跑拿到旧结果、还标成了新参数"，那种污染事后查不出来。
        """
        parts = {
            # 3：加入 min_bytes / refusal_retry / dense_retry_tag_max 三项。
            # 抬版本等于让老缓存整体失效——这三项都改变"哪些图出产物"，
            # 靠新增字段名区分不够：老缓存里根本没有这几个键，比不出来。
            "cache_schema": 3,
            "prompt": hashlib.sha256(self.prompt.encode("utf-8")).hexdigest(),
            # 自定义提示词可以连 system 一起换：同一条 prompt 配不同 system
            # 是两套指令，不进指纹就会跨套命中缓存。
            "system": hashlib.sha256(self.system.encode("utf-8")).hexdigest(),
            # 中文变体与比例必须进指纹：漏了它，先按全英文跑过一批、再把比例
            # 调到 30% 重跑，被抽中的那批会命中英文缓存，拿回英文结果。
            "prompt_zh": (hashlib.sha256(self.prompt_zh.encode("utf-8")).hexdigest()
                          if self.prompt_zh else ""),
            "zh_ratio": round(self.caption_zh_ratio, 4),
            # 文字规则是从 prompts/*.md 读的，内容已经落在上面两个哈希里；
            # 这里再记文件名，是为了事后能一眼看出"这批用的哪份规则"——
            # 光看哈希没人知道它是哪一版措辞。
            "text_rules": self.output.text_rules,
            # 平铺与分组的参考段是两套指令，同一张图不能跨这两档命中缓存。
            "ref_grouped": self.ref_grouped,
            "mode": self.output.mode,
            "nl_budget": [self.output.nl_min_tokens, self.output.nl_max_tokens],
            "tag_count": [self.output.tag_min_count, self.output.tag_max_count],
            # 四条兜底旋钮都改变"最后留下哪批 tag"，属于影响产出的参数，
            # 不进指纹的话：开了兜底重跑，缓存会把老口径的产出原样还回来。
            "tag_rescue": [round(float(self.output.tag_fallback_threshold), 4),
                           int(self.output.tag_min_accept),
                           round(float(self.output.tag_retry_threshold), 4),
                           int(self.output.tag_overflow_count)],
            "dense_rules": [self.output.dense_retry_rules,
                            int(self.output.dense_retry_tag_max)],
            # 字节门槛决定"哪些图根本不出产物"。不进口的话：先把门槛设成 0
            # 跑一批（小图也出了产物），再把门槛调到 10240 重跑，小图会命中
            # 上一轮的缓存、照旧出产物——门槛形同虚设，而且看不出为什么。
            "min_bytes": int(self.output.min_image_bytes),
            # 拒答是否重打：开着时拒答的图会多打一次并可能产出，关着时直接失败。
            # 两者的产出集合不同，不能跨口径命中。
            "refusal_retry": bool(self.output.refusal_retry),
            "model": [self.prov_base.get("model_id"),
                      self.prov_base.get("model_revision"),
                      self.prov_base.get("precision"),
                      self.prov_base.get("quantization")],
            "pixels": self.prov_base.get("max_pixels"),
            "caption_chars": self.prov_base.get("caption_chars"),
            # 同一个模型名在"本机 vLLM"和"云端 API"上是两套输出（量化/版本/
            # 上下文都不同），不漏这一项就会跨来源命中缓存。
            "provider": self.provider,
            "sample": [round(float(self.temperature), 4),
                       round(float(self.top_p), 4),
                       int(self.max_tokens), self.seed],
            "escalate": [self._escalate_base, self.escalate_model],
            "tag_backend": (self._tag_config_fingerprint()
                            if self.tag_backend is not None else None),
            "gate": self._policy_fingerprint(),
        }
        raw = json.dumps(parts, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _tag_config_fingerprint(self) -> str:
        from .local_tag import config_fingerprint
        return config_fingerprint(self.tag_backend)

    def _cache_key(self, data: bytes, task: TaskRow,
                   context: tuple | None = None) -> str:
        names, prompt, placeholders = context or self._inference_context(task)
        raw = json.dumps({"prompt": prompt, "artists": names,
                          "placeholders": placeholders,
                          "language": "zh" if self._wants_zh(task) else "en",
                          "mime": _mime_of(task.file)},
                         ensure_ascii=False, sort_keys=True)
        identity = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"{self.cache_prefix}:{identity}:{hashlib.sha256(data).hexdigest()}"

    def _cached_outcome(self, task: TaskRow, record: dict,
                        started: float) -> Outcome | None:
        """把缓存记录还原成一次正常产出。

        payload 一律**重新生成**而不是原样吐回：provenance / ts / 画师注入
        都该反映"本次运行"，否则产物里的时间戳和指纹会是指鹿为马。
        """
        try:
            temperature = float(record.get("temperature", self.temperature))
            attempts = int(record.get("attempts", 0))
            tokens = int(record.get("tokens", 0))
            prompt_tokens = int(record.get("prompt_tokens", 0) or 0)
            escalated = bool(record.get("escalated", False))
            tag_rec = record.get("tag") or None
            nl_rec = record.get("nl") or None
        except (TypeError, ValueError):
            return None
        if tag_rec is None and nl_rec is None:
            return None
        want_tag = self.need_tags          # 已经含"CPU 后端在场就要写 tag"这条
        want_nl = self.output.wants_nl
        if (want_tag and tag_rec is None) or (want_nl and nl_rec is None):
            return None
        tag_payload = None
        nl_payload = None
        if want_tag and tag_rec is not None:
            tags = [(str(t), float(p)) for t, p in tag_rec.get("tags") or []]
            tag_payload = self._tag_payload(
                task, tags, temperature, tokens, attempts,
                rating=tag_rec.get("rating") or None,
                escalated=escalated, cache_hit=True,
                prompt_tokens=prompt_tokens,
                # 兜底留痕跟着缓存走，否则重跑时那批 low_tag_count 会消失。
                flags=[str(f) for f in (tag_rec.get("flags") or [])],
                tag_rescored=int(tag_rec.get("rescored") or 0))
        if want_nl and nl_rec is not None:
            caption = str(nl_rec.get("caption") or "")
            artist_flags = list(nl_rec.get("artist_flags") or [])
            # 缓存保存的是回填后的正文。残留占位符或已有画师诊断必须重算。
            if not caption or artist_flags or _PLACEHOLDER_ANY.search(caption):
                return None
            verdict = SimpleNamespace(score=float(nl_rec.get("score") or 0.0),
                                      reasons=list(nl_rec.get("reasons") or []))
            nl_payload = self._nl_payload(
                task, caption,
                int(nl_rec.get("tag_count") or 0), temperature, tokens,
                attempts, verdict, escalated=escalated, cache_hit=True,
                artist_flags=artist_flags,
                prompt_tokens=prompt_tokens)
        if tag_payload is None and nl_payload is None:
            return None
        # 命中的这条没花 GPU：wall_ms 如实记成"还原产物用了多久"，
        # 否则"平均耗时"会被缓存命中拉成假象。
        return Outcome(task.image_id, True, tag=tag_payload, nl=nl_payload,
                       wall_ms=int((time.perf_counter() - started) * 1000),
                       attempts=attempts, tokens=0, cached=True)

    def artifact_expect(self, *, tag: bool) -> dict:
        """产物里 provenance 应当匹配的参数（续跑校验用）。

        只收"稳定"字段：model_id / revision / 精度 / 像素预算 / prompt 指纹 /
        seed。**不收 temperature**——产物记的是重试后实际用的那一档，
        拿它比会让每条重试过的产物被判成"参数变了"，进而无限重打。
        """
        expect = {
            "model_id": self.prov_base.get("model_id"),
            "model_revision": self.prov_base.get("model_revision"),
            "precision": self.prov_base.get("precision"),
            "max_pixels": self.prov_base.get("max_pixels"),
            "prompt_sha": hashlib.sha256(
                self.prompt.encode("utf-8")).hexdigest()[:12],
            "seed": self.seed,
        }
        if tag:
            expect["tag_backend"] = (self.tag_backend.name
                                     if self.tag_backend is not None else "vlm")
            expect["tag_precision"] = (self.tag_backend.precision
                                       if self.tag_backend is not None else "vlm")
            expect["tag_preprocess"] = (getattr(self.tag_backend, "preprocess", "pil")
                                        if self.tag_backend is not None else "vlm")
        return expect

    # -------------------------------------------------------------- 推理

    def close(self) -> None:
        self._http.close()
        if self._escalate_http is not None:
            self._escalate_http.close()
            self._escalate_http = None
        if self._tag_pool is not None:
            self._tag_pool.shutdown(wait=False)
            self._tag_pool = None

    def _tag_executor(self) -> _BatchTagExecutor:
        """CPU tag 专用执行器，固定 1 个 worker。

        CPU 侧并行没有收益：同一个模型多线程只是互相抢核，而 tagger 的瓶颈
        是单张前向。1 个 worker 也天然限制住"GPU 已经跑完、CPU 还在追"的
        堆积量——不会攒出几百张待算的图。

        批处理顺带在这里解决：worker 每次开工时把队列里**已经排着**的图一起
        取走（上限 = 后端的 batch_size）。不额外等待，所以 batch=1 时行为与
        逐张跑完全一致，攒批也没引入任何人为延迟。
        """
        if self._tag_pool is None:
            self._tag_pool = _BatchTagExecutor(
                self._cpu_tags_batch,
                max_batch=getattr(self.tag_backend, "batch_size", 1) or 1,
                stage=self.stage,
            )
        return self._tag_pool

    def _cpu_tags_batch(self, batch: list[bytes]) -> list[TagPrediction]:
        """在 tag 线程里计时。必须在这里测，不能在外面测 .result() 的等待——
        那个等待里混着 GPU 的时间，量出来的是"谁先完成"而不是"CPU 花了多久"。

        这里**不再自己解码**：原始字节直接交给后端。原先客户端先
        `Image.open().convert("RGB")` 一遍、后端 `_open_rgb` 再 convert 一遍，
        1024² 的图每张白付一次全尺寸拷贝（实测 3~5 ms）。解码只该有一次，
        放在最懂图片格式的那一层（后端可能换 torchvision 或 GPU 解码）。

        返回的是完整的 TagPrediction 而不是光秃秃的 tag 列表：rating 那一栏
        要跟着走到产物里（它不属于 tag，但分类头已经算出来了）。
        """
        with self.stage.track("tag"):
            preds = self.tag_backend.predict_batch(batch)
            # 后端会把上一次的耗时拆成 解码 / 前向 报上来（tag 执行器是单
            # worker，读到的就是本次）。没有拆分的后端这里两个都是 0，
            # 那就只有 "tag" 一段，不编数字。
            split = getattr(self.tag_backend, "last_timings", None) or {}
        if split.get("decode"):
            self.stage.add("tag_decode", split["decode"] / 1000.0)
        if split.get("forward"):
            self.stage.add("tag_fwd", split["forward"] / 1000.0)
        return preds

    def _budget_data(self, data: bytes | None, budget: int) -> bytes | None:
        """需要客户端出手的场合才缩图。

        预算 ≥ 服务端钳制时，服务端 smart_resize 做的是同一件事，客户端
        再缩一道只是白付一次解码，还把像素口径从"服务端算法"换成"本地
        算法"。只有当预算低于钳制（serve 留了余量、或没有指纹可依）时，
        基线才必须由客户端自己执行。
        """
        if not budget or not data:
            return data
        if self.pixel_cap and budget >= self.pixel_cap:
            return data
        return _resize_to_budget(data, budget)

    def _image_blob(self, data: bytes, mime: str,
                    file_name: str) -> tuple[nl_api.ImageBlob, Path | None]:
        """返回 (待发送的图片, 临时文件路径)。

        默认走 base64：图片本来就在 zip 里，用 file:// 必须先落盘，
        落盘意味着「磁盘要够、服务端要放行该目录、用完要删」三件额外的事。
        只有 --use-file-url 且显式开了服务端白名单时才值得省那 33% 体积。
        """
        if self.use_file_url and self.spool_dir is not None:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            # 名字用 image_id + 原扩展名：vLLM 按扩展名选解码器，丢了会被当 jpeg
            path = self.spool_dir / f"{Path(file_name).stem}{normalize_ext(file_name)}"
            path.write_bytes(data)
            return nl_api.blob_from_path(path, mime), path
        # blob_from_bytes 会顺手把 Anthropic 不认的 bmp/tiff 转成 PNG。
        return nl_api.blob_from_bytes(data, mime, provider=self.provider), None

    def _protect_artist_prompt(self, names: Sequence[str],
                               prompt: str) -> tuple[str, dict[str, str]]:
        """送模型前把 prompt 里的画师名换成占位符。

        这是"名字绝对原样"唯一从根上成立的保证：模型从头到尾没见过原名，
        也就无从翻译、音译、改大小写。盘面上名字有三种形态，一并保护——
        裸名、`@名`（prompt 里本来就带 @ 的引用）、以及 danbooru 的下划线
        形态（`sakimi_chan`）；带空格与纯 CJK 的名字都走同一条路，不因词边界
        漏保护（`sakimi chan`、`竹うら` 都有用例）。

        映射值统一归到**声明名**：回填出来必须是与原始 tag 逐字相等的
        `@原名`，不能是 prompt 里那个下划线变体——否则一致性校验会按"改形"
        判它违规。映射与 prompt 同寿命，重试由调用方复用同一份。
        """
        if not names or not prompt:
            return prompt, {}
        variants: dict[str, str] = {}
        for name in names:
            if not name:
                continue
            variants[name] = name
            if " " in name:
                variants[name.replace(" ", "_")] = name
        if not variants:
            return prompt, {}
        protected, mapping = artist_phrase.protect(prompt, list(variants))
        return protected, {ph: variants.get(surface, surface)
                           for ph, surface in mapping.items()}

    def _restore_artist_caption(self, caption: str,
                                mapping: dict[str, str]) -> tuple[str, list[str]]:
        """回填模型输出里的占位符 → (文本, 未知占位符列表)。

        已知下标回填成 `@原名`；未知下标原样保留——把猜的名字写进正文，比留
        一个显眼的占位符更难排查。未知项交给调用方记 flag，不静默吞掉。
        """
        if not caption:
            return caption, []
        text = artist_phrase.restore(caption, mapping)
        return text, sorted(set(_PLACEHOLDER_ANY.findall(text)))

    def _artist_ref_filter(self, task: TaskRow,
                           tags: Sequence[str]) -> tuple[str, ...]:
        """把画师 tag 从参考 tag 段里剔掉。

        为什么剔：画师名送模型前已经被换成占位符，模型看到的是一串
        ``__ARTIST_n__`` 令牌——它既拿不到「这是某画师」的语义（那正是设计
        意图），又可能把令牌当成普通 tag 抄进正文，回填后正文里就多出一次
        画师名，和开头那句模板撞车。所以参考段里干脆不出现画师条目，
        画风信息由开头那句模板句子承担。

        比较用归一形：去掉 ``@`` 前缀、空格与下划线互换、忽略大小写，
        danbooru 的 ``sakimi_chan`` 与声明名 ``sakimi chan`` 视为同一条。
        没有画师名时原样返回，不做任何多余改动。
        """
        names, _ = self._artist_names(task)
        if not names:
            return tuple(tags)

        def norm(value: str) -> str:
            return (value or "").strip().lstrip("@").casefold().replace(" ", "_")

        banned = {norm(n) for n in names if n}
        return tuple(t for t in tags if norm(str(t)) not in banned)

    def _artist_prompt_block(self, task: TaskRow) -> str:
        """这张图要不要追加画师句规则（`prompts/画师规则.md` 的 ARTIST_EN/ARTIST_ZH）。

        三个条件同时成立才加：

          1. 这张图有可信画师（`_artist_names` 非空）——没有画师就没有画师句，
             提示里也不需要"画师句怎么摆"这一套；
          2. caption 侧真的会注入（`inject_caption`）——只往 tag 串注入的批次，
             正文开头并没有那句话，加了等于请模型自己补一句；
          3. 句式走模板（`nl_style == "template"`）——裸名回退路径的形状是
             `@名字` 直接顶在开头，与规则里"禁止裸名开头"正好相反；
          4. 这一份指令是**内置生成**的，不是 `prompt/*.json` 的整段替换——
             自定义提示词里追加条款，操作者看到的文字就和他改了的那份不一致。

        规则文本全部来自 markdown：读不到就返回空串，提示与加规则之前逐字相同。
        """
        if not (self.artist_rule.enabled and self.artist_rule.inject_caption):
            return ""
        if self.artist_rule.nl_style != "template":
            return ""
        zh = self._wants_zh(task)
        # 自定义提示词是整段替换（`prompt/*.json`，见 prompt/README）：操作者看到
        # 什么就发什么，往里追加条款会让他改的那份和他以为的不一样。内置生成的
        # 那份才是"规则文件说加就加"的对象。
        if self._prompt_zh_is_custom if zh else self._prompt_is_custom:
            return ""
        names, _ = self._artist_names(task)
        if not names:
            return ""
        rules = artist_rules(zh=zh)
        # 空两行再接：与 <MEDIA>/<REFERENCE> 段同形，否则会粘在上一条规则的行尾
        # （`中文版- 画师名…`），模型会把两条读成一条。
        return ("\n\n" + rules) if rules else ""

    def _prompt_for_task(self, task: TaskRow) -> str:
        """这张图该用哪份指令。

        ★按 (seed, image_id) 决定，不按处理顺序：断点续跑、并发乱序、整批重跑
        都必须落到同一结果。按顺序抽签的话，续跑一次"30% 中文"就变成了另一批
        图，两次产出拼不回一批数据。

        参考 tag 段接在末尾：它跟着这张图走，同一张图重试时必须是同一段
        （重试换成另一套 tag 等于换了一道题）。
        """
        zh = self._wants_zh(task)
        base = self.prompt_zh if zh else self.prompt
        return base + self._artist_prompt_block(task) + _media_block(
            task.media, zh=zh) + _ref_block(
            self._artist_ref_filter(task, task.ref_tags), zh=zh,
            grouped=self.ref_grouped)

    def _inference_context(self, task: TaskRow) -> tuple:
        names, _ = self._artist_names(task)
        prompt, placeholders = self._protect_artist_prompt(
            names, self._prompt_for_task(task))
        return names, prompt, placeholders

    def _dense_prompt_for_task(self, task: TaskRow) -> str | None:
        """截断重打用的"不复述小字"版指令；没配就返回 None（不做这次重打）。

        为什么换指令而不是抬 max_tokens：被打断的多是文字密集图，模型把成串的
        包装小字全抄进了 TAGS 段，token 预算在 tag 上就见底了，caption 一个字
        都没出来。这时候"写更长一点"只会让它抄得更多；要它少抄才解得开。

        追加的那份规则文件与主规则一样从 `prompts/` 读，措辞归主创改；
        text_rules（图中文字规则）照旧带——这份只是**额外**收窄，不是推翻它。

        ★画师/媒体/参考 tag 三段必须照常接上：重打是对**同一张图**再打一次，
          换了指令但不能换题。《REFERENCE》段尤其不能丢——它是这条样本的原
          tag，丢了重打出来的 tag 就跟这条数据的来源断开。所以这里用主指令的
          拼装链，只把 base 换成密集版。
        """
        if not self._dense_prompt:
            return None
        names, _ = self._artist_names(task)
        zh = self._wants_zh(task)
        base = self._dense_prompt_zh if (zh and self._dense_prompt_zh) \
            else self._dense_prompt
        prompt, _ph = self._protect_artist_prompt(
            names,
            base + self._artist_prompt_block(task) + _media_block(task.media, zh=zh)
            + _ref_block(self._artist_ref_filter(task, task.ref_tags), zh=zh,
                         grouped=self.ref_grouped))
        return prompt

    def _force_prompt_for_task(self, task: TaskRow) -> str:
        """拒答重打用的"别再拒、直接出块"版指令。

        只在质检判定为拒答之后才发一次。为什么不复用主指令 + 抬温度见
        `_FORCE_OUTPUT_EN` 的注释：拒答对温度不敏感，它是模板回复。

        与密集版同一套拼装链（画师/媒体/参考 tag 三段照常接）：重打的是
        **同一张图同一道题**，只是换掉了"要不要回答"这一层。
        """
        names, _ = self._artist_names(task)
        zh = self._wants_zh(task)
        base = self._prompt_for(self.output, zh=zh, force_output=True) \
            if not (self._prompt_is_custom or (zh and self._prompt_zh_is_custom)) \
            else None
        if base is None:
            # 自定义提示词是整段替换：操作者要什么就发什么，不能偷偷在前面
            # 挂一段我们自己写的话。改写那句话的责任留给提示词作者。
            return ""
        prompt, _ph = self._protect_artist_prompt(
            names,
            base + self._artist_prompt_block(task) + _media_block(task.media, zh=zh)
            + _ref_block(self._artist_ref_filter(task, task.ref_tags), zh=zh,
                         grouped=self.ref_grouped))
        return prompt

    def _tag_cap(self, retried: int) -> int:
        """这一轮允许留多少个 tag。

        截断重打那一轮要压到 `dense_retry_tag_max`：文字密集图被打断，是因为
        TAGS 段先把预算吃光了，只改 prompt 措辞是软约束，模型照样能列满。
        压上限是硬闸，且**只管这一次重打**——落盘后仍守 tag_max_count。

        `retried=0` 表示正常轮次；>0 表示已经换过密集指令。
        """
        dense = int(self.output.dense_retry_tag_max or 0)
        if retried and dense and dense < self.output.tag_max_count:
            return max(self.output.tag_min_count, dense)
        return self.output.tag_max_count

    def _thresholds_at(self, line: float | None) -> dict[str, float] | None:
        """当前阈值表把通用档换成 `line`（其余类别阈值原样保留）。

        只改 default 而不是整表替换：character 之类单独调高的类别是刻意的
        （角色名错标代价大），降线救"tag 太少"时顺手把它放宽是另一件事。
        """
        if line is None or line <= 0:
            return None
        thr = dict(getattr(self.tag_backend, "thresholds", None) or {})
        thr["default"] = float(line)
        return thr

    def _rescore_at(self, data: bytes | None, tag_future, line: float,
                    with_prob: bool):
        """换一条判定线重筛这张图的 CPU tag；`line` 为 0 时不动作。

        优先用上次前向的分数快照（不重跑前向）——tag 与 VLM 并行起跑，
        等 `tag_future.result()` 时才拿得到分数，此时再算一遍纯属白烧。
        拿不到快照就退回"用这张图再算一遍"，语义相同、只是慢一点。
        两条路都失败就返回空，调用方按原结果继续走。
        """
        thr = self._thresholds_at(line)
        if thr is None or self.tag_backend is None:
            return []
        # 快照的下标是"这张图在本批里的位置"。tag 执行器一次只提交一张，
        # 所以恒为 0；改成整批提交时这里必须跟着变（rescore_last 会返回空，
        # 退回重算，结果仍然对，只是慢）。
        idx = self._last_tag_index or 0
        # ★只对"装得下快照"的后端走快照路径：MultiDevice 没有 rescore_last，
        #   而 wd-eva02 / pixai 各有各的分数结构（列表 / 字典），所以这里只
        #   判断"有没有这个方法"，不猜结构——方法自己知道怎么筛。
        pick: dict[str, float] = {}
        try:
            if hasattr(self.tag_backend, "rescore_last"):
                pick = self.tag_backend.rescore_last(idx, thr) or {}
            if not pick and data is not None and hasattr(
                    self.tag_backend, "predict_batch_with"):
                preds = self.tag_backend.predict_batch_with([data], thr)
                pick = dict(preds[0].tags) if preds else {}
        except Exception:            # noqa: BLE001 - 重筛失败不该拖垮这一张
            pick = {}
        if not pick:
            return []
        pairs = sorted(pick.items(), key=lambda kv: -kv[1])
        if with_prob:
            return [(t, float(p)) for t, p in
                    pairs[: self.output.tag_max_count]]
        return [t for t, _ in pairs[: self.output.tag_max_count]]

    def _wants_zh(self, task: TaskRow) -> bool:
        """这张图出中文还是英文。

        ★必须是纯函数：选指令（`_prompt_for_task`）和写产物（`_annotation_meta`）
        两处都要调它。分成两份实现就会出现"正文写的是中文、字段记的是 en"——
        而这类错在抽查里几乎发现不了，只能靠字段对账。

        用 (seed, image_id) 而不是处理顺序：断点续跑、并发乱序、整批重跑都必须
        落到同一结果，否则"30% 中文"两次跑出来是两批不同的图，拼不回一批数据。

        ★source 也要进哈希（规范 v0.2 §5 写的是 hash(source + stable_image_id
        + seed)）：只按 image_id 抽时，某个来源的 id 恰好成段或带规律（Pixiv
        的 pid 段、内部批次连号），那一整段就会集体中签或集体不中——全局看
        是 25%，拆到每个目录却是"这个 90%、那个 0%"，分层配比直接作废。
        """
        if not self.prompt_zh:
            return False
        source = task.shard or ""
        digest = hashlib.sha256(
            f"{source}:{task.image_id}:{self.seed}".encode("utf-8")).digest()
        roll = int.from_bytes(digest[:4], "big") % 10000
        return roll < self.caption_zh_ratio * 10000

    def _annotation_meta(self, task: TaskRow) -> dict:
        """文档 §8 要求随产物落盘的溯源字段。

        四个字段各自解决一类"事后查不出来"：

        - `caption_language`：这次正文到底是 en 还是 zh。以前语言只用于选指令，
          选完就丢了，产物里查不到，没法按 75/25 的配比审计，也筛不出"该出中文
          却出了英文"的那批。
        - `tags_original`：原 Tag 全量。`tag` 是新生成的那份，原 Tag 只在 prompt
          里当过参考、产物里没留痕——"Anime 原 Tag 必须保留"就无从验证。
        - `annotation_model` / `annotation_version`：模型与其 revision，拼上提示词
          版本。只看 `provenance` 也能查到，但那是嵌套结构，下游要写代码翻；
          放顶层是为了让筛选/对账用一行 SQL 就能做。
        - `source_revision`：数据来源的 revision（HF commit 或本地批次号）。
          同一份 manifest 换了上游 commit，标注结论就不能混用。
        """
        base = self.prov_base
        rev = base.get("model_revision")
        return {
            "caption_language": "zh" if self._wants_zh(task) else "en",
            "tags_original": list(task.ref_tags),
            "annotation_model": str(base.get("model_id") or "unknown"),
            # 规范 v0.2 §8 要这个字段。值改成"模型 revision + prompt 指纹"：
            # 没有版本号了，但这两段拼起来仍能唯一定位到"哪套模型、哪套指令"。
            "annotation_version": (
                f"{rev or 'unknown'}+"
                f"{hashlib.sha256(self.prompt.encode('utf-8')).hexdigest()[:12]}"),
            "source_revision": task.source_revision or "",
        }

    def _infer(self, image: nl_api.ImageBlob, temperature: float,
               prompt: str | None = None) -> tuple[str, int, int]:
        """一次推理（主服务）→ (文本, 输入 token, 输出 token)。

        `prompt` 是这一条专用的指令（中文条目带中文那份）；不给就用实例默认。
        ★测试与基准脚本替换本方法时，签名要对齐这个第三个参数（给默认值即可）。
        """
        return self._infer_at(image, temperature, escalate=False, prompt=prompt)

    def _infer_at(self, image: nl_api.ImageBlob, temperature: float, *,
                  escalate: bool = False,
                  prompt: str | None = None) -> tuple[str, int, int]:
        """一次推理，可指名打升级服务。

        拆成两个入口是刻意的：`_infer` 是"正常路径"的唯一形状，测试与基准
        脚本替换它就能接管全部推理；只有真的发生了升级才会走 `_infer_at`。
        合成一个带默认参数的方法会让每个调用方都要跟着改。
        """
        http, base_url, model_name = self._target(escalate)
        # 端点拼装与请求体形状全交给 nl_api：`/v1` 重复、system 位置、
        # 图片形态、Anthropic 不认 seed 这些坑都在那一层处理完了。
        try:
            url = nl_api.endpoint(base_url, self.provider)
            payload = nl_api.build_payload(
                self.provider, model=model_name, system=self.system,
                prompt=prompt or self.prompt, image=image, temperature=temperature,
                top_p=self.top_p, max_tokens=self.max_tokens, seed=self.seed,
                repeat_penalty=self.repeat_penalty)
        except ValueError as exc:
            raise InferenceError(str(exc), retryable=False) from exc
        try:
            r = http.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise InferenceError(f"超时: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise InferenceError(f"连接失败: {type(exc).__name__}: {exc}") from exc

        if r.status_code >= 400:
            # 分档而不是"400 以上一律不重试"：云端批量最先撞上的是 429，
            # 把它当永久失败会让整批数据在限流窗口里变成废品；反过来把
            # 401/404 当可重试，密钥写错时每张图都要白打三次。
            retryable, fatal = nl_api.classify_status(r.status_code)
            wait = nl_api.retry_after_seconds(getattr(r, "headers", None))
            raise InferenceError(
                f"HTTP {r.status_code}: {r.text[:200]}",
                retryable=retryable, fatal=fatal, retry_after=wait,
                status=r.status_code)

        try:
            return nl_api.parse_response(self.provider, r.json())
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise InferenceError(f"响应结构不对: {type(exc).__name__}") from exc

    def _target(self, escalate: bool) -> tuple[httpx.Client, str, str]:
        """这一轮该打哪个服务。升级没配置时永远回主服务。"""
        if escalate and self._escalate_http is not None and self.escalate_model:
            return self._escalate_http, self._escalate_base, self.escalate_model
        return self._http, self.base_url, self.model_name

    # -------------------------------------------------------------- 溯源

    def _provenance(self, temperature: float, *, tag: bool = False,
                    escalated: bool = False, cache_hit: bool = False) -> dict:
        base = dict(self.prov_base)
        # 升级重试用的是另一套权重，必须如实记下来：否则事后按 model_id 分组
        # 会把两套模型混成一批，而这批里只有少数样本来自大模型——那是最难
        # 发现的一类污染。`escalated` 让清洗侧一行就能把这类批次挑出来。
        if escalated:
            base["escalated"] = True
            base["escalated_model"] = self.escalate_model
        base["temperature"] = round(temperature, 3)
        # prompt 指纹。事后想回答"这批的 12 个 tag 是哪套指令逼出来的"，
        # 只能靠它——版本号是手写的，漏 bump 就失效，哈希不会。
        base["prompt_sha"] = hashlib.sha256(
            self.prompt.encode("utf-8")).hexdigest()[:12]
        if self.variant:
            base["variant"] = self.variant
        base["seed"] = self.seed
        # 缓存命中要标出来。这批样本是"上次同参数的结果"，没有经历本次推理；
        # 按 provenance 分组做清洗/对账时，混在一起会掩盖这层差别。
        if cache_hit:
            base["cache_hit"] = True
        if tag:
            # 只写进 tag 产物：这两个字段描述的是 tagger，NL 那侧没有这个概念。
            # 为什么非写不可：fp32 / tf32 / bf16 出的 tag 集合不一样（实测
            # bf16 每 79 张会有 2 个 tag 真改判），事后想回答"这批为什么
            # 多出几个 tag"，没有这个字段就只能靠猜。
            # 注意与 base["precision"] 区分：那个是 VLM 的精度，两者会打架。
            base["tag_backend"] = self.tag_backend.name if self.tag_backend else "vlm"
            base["tag_precision"] = (self.tag_backend.precision
                                     if self.tag_backend else "vlm")
            # 预处理在上卡还是 CPU：重采样实现不同，tag 集合会有差异
            # （线稿/高对比图约 1 成 tag 会变，平滑图实测 40/40 完全一致）。
            # 事后想回答"这批怎么多了几个 tag"，得能查到这一维。
            base["tag_preprocess"] = (getattr(self.tag_backend, "preprocess", "pil")
                                      if self.tag_backend else "vlm")
        return base

    # -------------------------------------------------------------- 单张

    def process(self, task: TaskRow, data: bytes | None = None,
                reader: ShardReader | None = None) -> Outcome:
        """取图 → 重试循环 → 组织两个 payload。异常不外抛，全部折成失败原因。

        `data` 由调用方给（parquet 流式模式下图片已经在内存里），
        给不了才回去问 reader 要（zip/tar 模式按需读，省内存）。
        """
        started = time.perf_counter()
        if data is None:
            if reader is None:
                return Outcome(task.image_id, False,
                               error="没有图片数据，也没有可用的读取器")
            try:
                with self.stage.track("read"):
                    data = reader.read(task.shard, task.file)
            except (KeyError, OSError, zipfile.BadZipFile, tarfile.TarError,
                    RuntimeError) as exc:
                return Outcome(task.image_id, False,
                               error=f"读图失败: {type(exc).__name__}: {exc}")

        # 结果缓存：命中就直接产出，连缩放/base64 与 CPU tag 都省掉。
        # 放在最前面是刻意的——越早命中，省下的越多；这里的判断只有一次
        # 哈希 + 一次 sqlite 点查。
        cache_key = None
        context = self._inference_context(task)
        if self.cache is not None and data is not None:
            cache_key = self._cache_key(data, task, context)
            record = self.cache.get(cache_key)
            if record is not None:
                try:
                    hit = self._cached_outcome(task, record, started)
                except (TypeError, ValueError, AttributeError):
                    hit = None
                if hit is not None:
                    return hit
                self.cache.delete(cache_key)

        # 输入字节门槛：小到没有内容的图在**入口**就断掉。
        # 位置：缓存之后（命中优先，这类图本来也不会有缓存）、解码与 base64
        # 之前（不然白解一张图，缩放还可能对 5 KB 的图报错）。
        # ★判据只看字节数：解码算方差要先把图解出来，为了丢掉它反而更贵。
        floor = int(self.output.min_image_bytes or 0)
        if floor and len(data) < floor:
            return Outcome(
                task.image_id, False, skipped=True,
                error=(f"输入图过小（{len(data)} < {floor} 字节），"
                       f"按无内容图跳过"))

        mime = _mime_of(task.file)
        # 基线像素预算（attempt 0 的口径）。重试时按质检给出的 RetryPlan
        # 抬高（见 _run），这里先把基线执行掉。缩过的字节只进 image，
        # `data` 必须保持原始字节：重试提分辨率要从原始字节重组，从缩过的
        # 字节出发的话，抬再高也回不去。
        with self.stage.track("b64"):
            image, spool = self._image_blob(
                self._budget_data(data, int(self.prov_base.get("max_pixels") or 0)),
                mime, task.file)

        # CPU tag 与 GPU 推理同时起跑。GPU 每张 1~3 秒，CPU 这段时间正好把
        # tag 算完；串行做的话总耗时 = 两者相加，白等一半。
        # 只在"tag 由 CPU 出"时提交，VLM 自己吐 tag 的模式下没有这一步。
        # 提交原始字节：tag 后端有自己的输入口径（如 wd-eva02 的 448²），
        # VLM 的像素预算不该改变 tag 的输入。
        tag_future = None
        if self.tag_backend is not None and self.need_tags:
            tag_future = self._tag_executor().submit(data)

        try:
            return self._run(task, image, started, tag_future, data=data,
                             cache_key=cache_key, context=context)
        finally:
            # spool 文件只在 --use-file-url 时存在。必须每张都删：
            # 一万张图的副本能吃掉几十 GB，而它们全都已经躺在 zip 里了。
            if spool is not None:
                try:
                    spool.unlink()
                except OSError:
                    pass

    def _run(self, task: TaskRow, image: nl_api.ImageBlob, started: float,
             tag_future: Future | None = None, *,
             data: bytes | None = None,
             cache_key: str | None = None,
             context: tuple | None = None) -> Outcome:
        """重试循环。拆出来是为了让 spool 清理只在 process 的 finally 里写一次。

        `data` 是原始图片字节：重试提分辨率时要按抬高的预算重新组装
        image，从原始字节出发（而不是从上一轮缩过的字节出发）。
        """
        want_tag = self.output.wants_tag      # VLM 是否产出 tag（CPU 模式下恒 False）
        want_nl = self.output.wants_nl
        temperature = self.temperature
        attempts = 0
        reasons: list[str] = []
        # 输入与输出分开累计：图片走 base64 时输入 token 是账单大头，
        # 只记输出会让成本估算少一个数量级（本机模式下前者只是个数字）。
        tokens_total = 0
        prompt_total = 0
        # 撞了几次 429。限流不该只表现为"变慢了"，得能在报告里看见。
        rate_limited = 0
        # 配置性错误（密钥/模型名/地址）：这张失败，而且上层要据此熔断整批。
        fatal = False
        # 当前生效的像素预算。attempt 0 是基线，重试按 RetryPlan 抬。
        budget = int(self.prov_base.get("max_pixels") or 0)
        # 这一条最终是否走了升级服务。一旦升上去就不再退回：重试的语义是
        # "小模型救不回来"，退回去只会重复同一份失败。
        escalated = False
        # 质检的 CLIP 需要一张 PIL 图。只在真的启用 CLIP 时才解码——正常
        # 打标路径一张图只解一次（在后端），这里多解一次是实打实的成本。
        qc_image = None
        qc_image_ready = False

        # 这条的指令在进重试循环之前定死：重试是对同一张图重打，语言不能变
        # （第一次中文、第二次英文的话，质检口径和产物都对不上）。
        # 占位符映射与 prompt 一起定死：重试重新编号会让回填串到别人身上。
        artist_names, task_prompt, artist_placeholders = (
            context or self._inference_context(task))
        # 截断重打用的备用指令（没配 dense_retry_rules 时为 None）。
        dense_prompt = self._dense_prompt_for_task(task)
        prompt_swapped = 0
        # 拒答重打用的强制输出指令，**懒生成**：绝大多数图根本不会拒答，
        # 每张都拼一遍等于给 30 万张图各串一次多余的字符串。
        force_prompt: str | None = None
        force_swapped = 0
        # 这一轮是否命中过拒答。用 flags 而不是最后再 `looks_like_refusal(text)`：
        # 拒答被重打救回来之后 text 已经成了正常产出，事后再判会漏掉"它拒过"
        # 这个事实，而拒答计数是要上报的。
        refused_hit = False
        # 快照里"这张图"的下标：tag 执行器一次只跑一张，所以恒为 0；留着变量
        # 是为了哪天改回整块提交时不至于悄悄错位（rescore_last 会返回空）。
        self._last_tag_index = 0

        while True:
            attempts += 1
            try:
                with self.stage.track("http"):
                    if escalated:
                        text, p_tok, tokens = self._infer_at(
                            image, temperature, escalate=True, prompt=task_prompt)
                    else:
                        text, p_tok, tokens = self._infer(
                            image, temperature, task_prompt)
            except InferenceError as exc:
                reasons = [str(exc)]
                if exc.status == 429:
                    rate_limited += 1
                if exc.fatal:
                    # 密钥/模型名/地址错了：再试同一张是白烧，换下一张也是白烧。
                    # 记下来交给 run_batch 熔断，别让 30 万张一张张演一遍。
                    fatal = True
                    break
                if not exc.retryable or attempts > self.max_attempts:
                    break
                # 服务端给了 Retry-After 就照办（429 常见），否则指数退避。
                # 上限压到 60s：真被限到一个小时，这一批就该停下而不是干等。
                wait = exc.retry_after
                if wait is None:
                    wait = min(2.0 * attempts, 10.0)
                time.sleep(min(float(wait), 60.0))
                continue

            tokens_total += tokens
            prompt_total += p_tok
            with self.stage.track("parse"):
                tags, caption = parse_output(text, self.output.mode)

            # 画师占位符：prompt 已保护过，模型的原始输出里若还能搜到真名，
            # 说明保护漏了名字、或模型自己编了名字。引号中的签名/水印按图内
            # 文字处理，不当作作者归因；未知占位符仍检查整段正文。
            # 随后把已知占位符回填成 `@原名`；未知下标保留并记 flag（没声明画师
            # 的条目也一样记：那时正文里的下标只可能是模型自己编的）。
            artist_flags: list[str] = []
            if caption:
                if artist_names and artist_phrase.leaked_names(caption,
                                                              artist_names,
                                                              ignore_quoted=True):
                    artist_flags.append(ARTIST_FLAG_LEAKED)
                caption, unknown_placeholders = self._restore_artist_caption(
                    caption, artist_placeholders)
                if unknown_placeholders:
                    artist_flags.append(ARTIST_FLAG_UNKNOWN_PLACEHOLDER)

            # 引文清洗放在质检之前：质量门会数引文、判长度，洗过的文本才是
            # 真正入库的那份，拿未洗的去过门等于对错的文本打分。
            #   1. 重复引用去掉引号（同一段文字只留第一次）
            #   2. 砍掉末尾那段"画面中可见文字：XXX 为某某字体"的复述
            # 两步都只动引号和复述段，不删画面描述，长度损失很小。
            if caption:
                with self.stage.track("text_clean"):
                    # 最后一道是 infra 的 token 硬保险：caption 会作为单条纯文本
                    # 送进 text encoder，超线就被无提示截断。上限从质检策略取，
                    # 置 0 可关掉。
                    limit = getattr(getattr(self.gate, "policy", None),
                                    "caption_token_limit", 0)
                    caption = tidy_caption(caption, token_limit=int(limit or 0))

            # 显式空产出检查必须放在质检之前：质量门在 tags 为空时
            # tag_coverage 返回 1.0（"没有强 tag，视为全覆盖"），
            # 于是"模型一个 tag 都没给"会被判成合格。这个洞只能在这里补。
            reasons = []
            if want_nl and artist_flags:
                reasons.extend(artist_flags)
                if self.cache is not None and cache_key is not None:
                    self.cache.delete(cache_key)
            if want_tag and not tags:
                reasons.append("没有产出 tag")
            if want_nl and not caption:
                reasons.append("没有产出 caption")
            # 截断单独判：用满 max_tokens 说明回复被砍断。文字密集的图上这条是
            # 真会触发的（包装小字成串，模型在 TAGS 段就写满了，caption 一个字
            # 都没出来）。上面两条拦不住——它要么非空、要么正好是空 caption。
            if want_nl and hit_token_cap(tokens, self.max_tokens):
                reasons.append(f"回复被截断（用满 max_tokens={self.max_tokens}）")

            # tag 数量也是 prompt 里的硬约定，但质量门不管它（那是文本质量维度）。
            # 多给的按重要性顺序截断——prompt 要求降序输出，截掉的正好最不重要，
            # 比整条判废合理；少给的只能重试，那是模型敷衍。
            #
            # ★但"重试"对 CPU 分类头从来是空转：它是确定性的，同图同权重分数
            #   一个不差。所以 tag 数量的两条补救都改成**换判定线重筛**：
            #   少了降线（tag_fallback_threshold）、多了抬线（tag_retry_threshold）。
            #   0 = 关，行为与老版本完全一致。
            tag_rescored = 0        # >0 表示这一条换过线重筛（只是留痕用）
            tag_capped = False
            low_tag = False
            # 这一轮的上限：截断重打时会临时压到 dense_retry_tag_max。
            # ★必须是"这一轮"的局部值，不能改进 output：output 是整批共用的，
            #   改了它等于让别的图也跟着变窄。
            cap = self._tag_cap(prompt_swapped)
            if want_tag and tags:
                if len(tags) < self.output.tag_min_count:
                    tags = self._rescore_at(
                        data, tag_future, self.output.tag_fallback_threshold,
                        with_prob=False) or tags
                    tag_rescored += 1
                    # 够 tag_min_accept 就如实采纳：这批图的 tag 少不是模型敷衍，
                    # 是图本身就只有这么多东西。低于它仍然判失败（图上真没内容）。
                    if (len(tags) < self.output.tag_min_count
                            and self.output.tag_min_accept
                            and len(tags) >= self.output.tag_min_accept):
                        low_tag = True
                    if len(tags) < self.output.tag_min_count and not low_tag:
                        reasons.append(
                            f"tag 过少（{len(tags)} < {self.output.tag_min_count}）")
                if len(tags) > cap:
                    # 抬线重筛只对"被小字撑爆"的图有意义：触发线必须高于上限，
                    # 否则正常图也会被抬线，把该留的 tag 一并削掉。
                    over = max(cap, self.output.tag_overflow_count or 0)
                    if self.output.tag_retry_threshold and len(tags) > over:
                        tags = self._rescore_at(
                            data, tag_future, self.output.tag_retry_threshold,
                            with_prob=False) or tags
                        tag_rescored += 1
                if len(tags) > cap:
                    tags = tags[: cap]
                    tag_capped = True

            if reasons:
                if attempts > self.max_attempts:
                    break
                if artist_flags:
                    policy = getattr(self.gate, "policy", None)
                    temperature = min(
                        temperature + float(getattr(policy, "retry_temp_step", 0.15)),
                        float(getattr(policy, "retry_temp_cap", 0.7)))
                # 截断的补救：换一份"不复述小字"的指令再打一次，而不是单纯
                # 抬温度（抬温度只会让它写得更啰嗦）。只在第一次截断时换。
                if (hit_token_cap(tokens, self.max_tokens)
                        and dense_prompt and prompt_swapped < 1):
                    task_prompt = dense_prompt
                    prompt_swapped = 1
                    continue
                # 拒答的补救：换"直接出块、别解释"的指令再打一次。
                # 这条要走在下面质检的 drop 判定**之前**才有意义——质检那边
                # 已经改成把拒答判 retry（见 quality.evaluate），但这里靠的是
                # 文本里的拒答特征，不依赖质检有没有开。
                if (looks_like_refusal(text or "") and force_swapped < 1
                        and force_prompt is None):
                    # 已生成过（哪怕是空串=自定义提示词不让改）就不再试，
                    # 用 None 当"还没生成过"、"" 当"生成过但不可用"。
                    force_prompt = self._force_prompt_for_task(task) or ""
                    if force_prompt:
                        task_prompt = force_prompt
                        force_swapped = 1
                        refused_hit = True
                        continue
                continue

            head = tags[:_QC_TAG_HEAD]
            ann = Annotation(
                image_sha256="",          # 客户端不算哈希，见模块头说明
                image_path=task.file,
                tags=[(t, 1.0) for t in head],
                caption_dense=caption,
                tag_line=", ".join(head),
                qc=QC(caption_chars=len(caption), nl_tokens=tokens),
            )
            with self.stage.track("gate"):
                # CLIP 打分要的是图本身。解码失败就当没有图（跳过 L2），
                # 不能因为"打分打不了"把一条已经合格的样本判废。
                if not qc_image_ready:
                    qc_image_ready = True
                    # getattr：测试与基准脚本会塞只实现 evaluate 的假质检门，
                    # 不能因为缺少 clip 属性把整条打标路径带崩。
                    if getattr(self.gate, "clip", None) is not None and data is not None:
                        try:
                            from io import BytesIO

                            from PIL import Image

                            with Image.open(BytesIO(data)) as im:
                                qc_image = im.convert("RGB")
                        except Exception:         # noqa: BLE001
                            qc_image = None
                # 不传 force_refusal_retry：拒答要不要重试由 acquire 这一层
                # 自己判（见上面 refused_hit 那段），质量门的默认行为就是
                # "拒答按可重试处理"。传进去等于要求每个外部假门也实现这个
                # 新参数——测试和基准脚本里的门只实现了 evaluate，会直接
                # TypeError 把整条打标路径带崩。
                verdict = self.gate.evaluate(
                    ann, attempt=attempts - 1, temperature=temperature,
                    max_pixels=budget, image=qc_image,
                )
                # 明确要求"拒答即判死"的场合（output.refusal_retry=false）
                # 在这里覆盖：本层拿到的 verdict 就是最终结论，改它和让
                # 质量门自己返回 drop 等价，但不用改任何外部门的签名。
                # 用 getattr 取 refused：外部假门返回的 verdict 可能没有这个
                # 字段（它是在本次改动里才加上的），缺字段就当"没拒答"。
                if (getattr(verdict, "refused", False)
                        and not self.output.refusal_retry):
                    verdict.level = "drop"
            # 拒答但指令已经换过一次了：再走一轮 retry 也只是同一份强制性
            # 指令重发，模型会给出同一句抱歉。直接判死，省一次推理。
            if verdict.level == "retry" and force_swapped and any(
                    "疑似拒答" in r for r in verdict.reasons):
                verdict.level = "drop"
            if verdict.level == "pass":
                tags_with_prob: list[tuple[str, float]] = [(t, 1.0) for t in tags]
                # 评级分数（只有 wd-eva02 这类分级头后端会给）。它不进 tag 串，
                # 只作为独立字段落进产物，供数据集清洗/剔除误标用。
                rating: dict[str, float] = {}

                if tag_future is not None:
                    try:
                        pred = tag_future.result()
                        tags_with_prob, rating = pred.tags, pred.rating
                    except Exception as exc:      # noqa: BLE001 - 后端异常花样多
                        return Outcome(
                            task.image_id, False,
                            error=f"CPU tag 反推失败: {type(exc).__name__}: {exc}",
                            wall_ms=int((time.perf_counter() - started) * 1000),
                            attempts=attempts, tokens=tokens_total,
                        )
                    # 分类头是确定性的：同一张图再算一次结果不变，数量不够就
                    # 重试是纯浪费。补救只能换判定线，见 _rescore_at。
                    if len(tags_with_prob) < self.output.tag_min_count:
                        tags_with_prob = self._rescore_at(
                            data, tag_future, self.output.tag_fallback_threshold,
                            with_prob=True) or tags_with_prob
                        tag_rescored += 1
                    elif (len(tags_with_prob) > cap
                          and self.output.tag_retry_threshold
                          and len(tags_with_prob) > max(
                              cap, self.output.tag_overflow_count or 0)):
                        tags_with_prob = self._rescore_at(
                            data, tag_future, self.output.tag_retry_threshold,
                            with_prob=True) or tags_with_prob
                        tag_rescored += 1
                    if len(tags_with_prob) > cap:
                        tags_with_prob = tags_with_prob[: cap]
                        tag_capped = True
                    # 这里的判定必须与上面 VLM 分支**同口径**，否则同一个问题会
                    # 按"tag 走 CPU"还是"tag 走 VLM"给出两种结论。
                    if len(tags_with_prob) < self.output.tag_min_count:
                        if (self.output.tag_min_accept
                                and len(tags_with_prob) >= self.output.tag_min_accept):
                            low_tag = True
                        else:
                            return Outcome(
                                task.image_id, False,
                                error=(f"tag 过少（{len(tags_with_prob)} < "
                                       f"{self.output.tag_min_count}），"
                                       f"后端 {self.tag_backend.name if self.tag_backend else '?'}"
                                       f" 的阈值偏低"),
                                wall_ms=int((time.perf_counter() - started) * 1000),
                                attempts=attempts, tokens=tokens_total,
                            )

                # 只写本模式该产的文件：tag 模式下多写一个 -nl.json，
                # 会让 merge 在 require=tag 时多找一条"本不该存在"的记录，
                # 更糟的是那份 caption 没经过任何按模式裁剪的约束。
                # 先进缓存再返回：存的是"模型产物"（tag+概率、rating、caption、
                # 质检分），**不存 provenance/ts/画师注入**——那三样在命中时
                # 必须按本次运行重新生成，否则产物会指鹿为马。
                # ★兜底留痕（flag / 是否重筛）要进缓存：否则重跑同一条时缓存
                #   命中，low_tag_count 会静默消失，那批数据看起来就"正常"了。
                tag_flags_list = [TAG_FLAG_LOW_COUNT] if low_tag else []
                if self.cache is not None and cache_key is not None:
                    self.cache.put(cache_key, {
                        "temperature": round(float(temperature), 4),
                        "attempts": attempts,
                        "tokens": tokens_total,
                        "prompt_tokens": prompt_total,
                        "escalated": escalated,
                        "tag": ({"tags": [[t, round(float(p), 4)]
                                          for t, p in tags_with_prob],
                                 "rating": {k: round(float(v), 4)
                                            for k, v in (rating or {}).items()},
                                 "flags": tag_flags_list,
                                 "rescored": int(tag_rescored)}
                                if self.need_tags else None),
                        "nl": ({"caption": caption,
                                "score": float(verdict.score),
                                "reasons": list(verdict.reasons),
                                "artist_flags": list(artist_flags),
                                "tag_count": len(tags_with_prob)}
                               if want_nl else None),
                    })
                return Outcome(
                    task.image_id, True,
                    tag=(self._tag_payload(task, tags_with_prob, temperature,
                                           tokens, attempts, rating=rating,
                                           escalated=escalated,
                                           prompt_tokens=prompt_total,
                                           flags=(tag_flags_list),
                                           tag_rescored=tag_rescored)
                         if self.need_tags else None),
                    nl=(self._nl_payload(task, caption, len(tags_with_prob),
                                         temperature, tokens, attempts, verdict,
                                         escalated=escalated,
                                         artist_flags=artist_flags,
                                         prompt_tokens=prompt_total)
                       if want_nl else None),
                    wall_ms=int((time.perf_counter() - started) * 1000),
                    attempts=attempts, tokens=tokens_total,
                    prompt_tokens=prompt_total, rate_limited=rate_limited,
                )
            reasons = verdict.reasons or [f"质检未通过（{verdict.score:.2f}）"]
            if verdict.level == "drop" or attempts > self.max_attempts:
                break
            if verdict.retry is not None:
                temperature = verdict.retry.temperature
                # 升级目标只在质检点名要升级、且确实配了第二个服务时才切。
                # 切了之后重试不再退回小模型（见上面的 escalated 说明）。
                if (getattr(verdict.retry, "model_hint", "same") == "escalate"
                        and self._escalate_http is not None and not escalated):
                    escalated = True
                # 重试提分辨率：质检按基线×scale^k 给出更高预算，客户端
                # 从原始字节按新预算重组图片。服务端最多钳到 pixel_cap，
                # 抬过头没有意义；预算没超过当前值就不动（温度照常变）。
                nxt = int(getattr(verdict.retry, "max_pixels", 0) or 0)
                if nxt > budget and data is not None:
                    if self.pixel_cap and nxt > self.pixel_cap:
                        nxt = self.pixel_cap
                    if nxt > budget:
                        budget = nxt
                        with self.stage.track("b64"):
                            # use_file_url 模式下 _image_blob 落的是同名
                            # spool 文件，process 的 finally 会统一清掉。
                            image, _ = self._image_blob(
                                self._budget_data(data, budget),
                                _mime_of(task.file), task.file)

        # 拒答标记：这一条最终失败时，调用方（重试失败项那条路）要靠它判断
        # "是不是已经硬试过一次了"。没有它就只能去 error 串里找"疑似拒答"，
        # 那种判法改一个字文案就悄悄失效。
        #
        # excused 只在这条路**是重试轮**（run_client 传了 only_failed）时才置位：
        # 普通打标那趟失败就是失败，要留在 failures.jsonl 里等下次，不能直接判死。
        # refused 只看"这一轮真的拿到过文本"。inference 直接抛错（401/429/超时）
        # 时 text 根本没被赋过值，那时判 `looks_like_refusal(text or "")` 会
        # 触发 UnboundLocalError——把一次网络故障变成一条看不懂的崩溃。
        return Outcome(
            task.image_id, False,
            error="; ".join(reasons)[:400],
            wall_ms=int((time.perf_counter() - started) * 1000),
            attempts=attempts, tokens=tokens_total,
            prompt_tokens=prompt_total, rate_limited=rate_limited, fatal=fatal,
            refused=bool(refused_hit),
            excused=bool(getattr(self, "_retrying_failed", False)),
        )

    # -------------------------------------------------------------- 产物

    def _artist_names(self, task: TaskRow) -> tuple[list[str], str]:
        """(名字列表, 来源)。

        优先级：**包内 table 口径 > 查表 > 包内 path 口径 > 空**。

        为什么包内 table 口径排第一：它是封包/分包时按 post id 查好、跟着图
        一起封进去的值，等于"这张图确认过的答案"；再按分片去查一次表，只会
        把确定值换成依赖分片命名的推测值——一旦数据重新切片，分片名对不上，
        确定值就被一个错表或空值顶掉。

        path 口径（目录名）不享有这个特权：它只是从路径顺手拿的，表才是
        danbooru 的原始标注。两者都没有就返回空——不猜、不拿上级目录凑。
        """
        if not self.artist_rule.enabled:
            return [], ""
        if task.artist_source == "table":
            packed = list(task.artists) or ([task.artist] if task.artist else [])
            if packed:
                return packed, "table"
        if self.artist_table.active:
            names = self.artist_table.lookup(task.image_id, task.shard)
            if names:
                return list(names), "table"
        if task.artists:
            return list(task.artists), task.artist_source or "path"
        if task.artist:
            return [task.artist], "path"
        return [], ""

    def _artist_tag_of(self, task: TaskRow) -> str:
        """注入形态的画师标签。空串表示"不注入"。

        `artist` 字段（json 里）与 `artist_tag`（caption 里）是两件事：
        前者是分发时记录的事实，写进产物只为了追溯，永远照写；
        后者会改变 caption 的文本分布，属于数据集口径，只在配置启用时才给。
        manifest 里一定有画师名（split 阶段强制提取），若拿"有名字"当"该注入"，
        artist.enabled: false 就形同虚设了。

        名字来自表还是目录名由 _artist_names 决定；多画师时**只注入第一个**——
        caption 开头连写几个 @ 会把画师绑定稀释掉，其余名字仍完整留在 json 的
        `artists` 字段里，下游要拆分随时能拿。
        """
        names, _ = self._artist_names(task)
        if not names:
            return ""
        return format_tag(names[0], self.artist_rule)

    def _artist_fields(self, task: TaskRow) -> dict:
        """产物里与画师相关的字段。tag 串与 nl 两处共用一份口径。

        `artist_source` 只在查表命中时写（目录名是默认行为，写进去等于噪音）；
        `artists` 只在多于一人才写（单画师时 artist 已经够了）。
        """
        names, source = self._artist_names(task)
        out: dict = {
            "artist": names[0] if names else task.artist,
            "artist_tag": format_tag(names[0], self.artist_rule) if names else "",
        }
        if len(names) > 1:
            out["artists"] = list(names)
        if source == "table":
            out["artist_source"] = "table"
        return out

    def _tag_payload(self, task: TaskRow, tags: Sequence[tuple[str, float]],
                     temperature: float, tokens: int, attempts: int,
                     *, rating: dict[str, float] | None = None,
                     escalated: bool = False, cache_hit: bool = False,
                     prompt_tokens: int = 0, flags: Sequence[str] = (),
                     tag_rescored: int = 0) -> dict:
        fields = self._artist_fields(task)
        names = [t for t, _ in tags]
        pairs = [[t, round(float(p), 4)] for t, p in tags]
        if fields["artist_tag"] and self.artist_rule.inject_tags:
            # 画师标签也进 tag 串，并且放最前：训练时 tag 是条件，
            # 只有写进文本才能把"这个作者的画风"绑到 @name 上，只放 JSON
            # 字段训练侧读不到。它不是 danbooru 词汇，所以给 1.0 固定分、
            # 排在最前，下游靠 @ 前缀就能和普通 tag 区分开。
            names = [fields["artist_tag"], *names]
            pairs = [[fields["artist_tag"], 1.0], *pairs]
        payload = {
            "image_id": task.image_id,
            # merge 读的就是这个字段，必须是逗号分隔的字符串
            "tag": ", ".join(names),
            # prob 是有真值的：CPU tagger（分类头）会给分数。
            # VLM 出 tag 时只能填 1.0，那时 tag_backend="vlm"，
            # 下游看到这个字段就知道概率该不该当真。
            "tags": pairs,
            "tag_backend": self.tag_backend.name if self.tag_backend else "vlm",
            **fields,
            # 溯源四件套。tag 侧也带 `caption_language`：同图两份文件同值，
            # merge 时无论先读到哪份都能拿到语言，不用赌顺序。
            **self._annotation_meta(task),
            "ok": True,
            "attempts": attempts,
            "completion_tokens": tokens,
            # 输入 token 只在 API 模式有意义（本机不计费），为 0 就不写字段，
            # 老产物的形态保持逐字不变。
            "prompt_tokens": prompt_tokens or None,
            "provenance": self._provenance(temperature, tag=True,
                                           escalated=escalated,
                                           cache_hit=cache_hit),
            "ts": time.time(),
        }
        if not prompt_tokens:
            payload.pop("prompt_tokens")
        # 顶层也标一份：清洗脚本不必钻进 provenance 就能筛掉缓存命中的行。
        if cache_hit:
            payload["cache_hit"] = True
        # 分级分数单列。它**不能**并进 tag 串（描述的是审核等级，不是画面内容），
        # 但丢掉又太可惜——分类头每张图都算过了。空字典不写字段，
        # 老产物的形态保持不变。
        if rating:
            payload["rating"] = {k: round(float(v), 4) for k, v in rating.items()}
        # 只有 HF 模式才有显式路径（本地分发靠 image_id 就能回指）。
        # 写进产物而不是让 merge 现拼：拼装需要知道仓库名和分片名，
        # 而那两样只有产出方知道。
        if task.image_path:
            payload["image_path"] = task.image_path
        # 兜底动作留痕：事后要回答"这批为什么有 5 个 tag 的条目"、"有多少条是
        # 抬线救回来的"，只能靠这两个字段，否则它们看起来和正常产出没差别。
        if flags:
            payload["flags"] = list(flags)
        if tag_rescored:
            payload["tag_rescored"] = "fallback" if low_tag else "count"
        return payload

    def _inject_artist_caption(self, caption: str, fields: dict,
                               task: TaskRow) -> str:
        """把画师句写进 NL 开头。

        默认走句式模板：位置固定开头、整句是自然描述、名字逐字原样（模板层
        不做任何清洗）。`artist.nl_style: bare` 回退到旧的"裸 @名 直接接
        正文"，供对比与回滚；两条路径共用 `inject_caption` 开关。

        多画师交给模板层连接并截断到前 3 个：正文开头连写一长串 `@` 会把
        绑定稀释掉，完全不写又丢掉共享画风信息，取"少量、按原 tag 顺序"。
        """
        if not fields.get("artist_tag") or not self.artist_rule.inject_caption:
            return caption
        if self.artist_rule.nl_style == "bare":
            return inject_into_caption(caption, fields["artist_tag"],
                                       self.artist_rule)
        names = list(fields.get("artists") or [])
        if not names and fields.get("artist"):
            names = [fields["artist"]]
        sentence = artist_templates.render(
            names, "zh" if self._wants_zh(task) else "en")
        return artist_templates.insert(caption, sentence)

    def _nl_payload(self, task: TaskRow, caption: str, tag_count: int,
                    temperature: float, tokens: int, attempts: int, verdict,
                    *, escalated: bool = False, cache_hit: bool = False,
                    artist_flags: Sequence[str] = (),
                    prompt_tokens: int = 0) -> dict:
        artist = self._artist_fields(task)
        text = self._inject_artist_caption(caption, artist, task)
        payload = {
            "image_id": task.image_id,
            "caption": text,
            **artist,
            # 画师是怎么注入的：``template``（句式）/``bare``（裸 @名前置）/空串。
            # 下游（封包侧）据此决定要不要再补一次绑 @名，不用猜正文开头长什么样。
            "artist_injected": ("" if (not artist.get("artist_tag")
                                      or not self.artist_rule.inject_caption
                                      or not text)
                                else ("bare" if self.artist_rule.nl_style == "bare"
                                      else "template")),
            "tag_count": tag_count,
            "nl_tokens": tokens,
            # 见 _tag_payload：本机模式不写，API 模式才有依据核算成本。
            "prompt_tokens": prompt_tokens or None,
            "chars": len(text),
            "score": verdict.score,
            # 质检旗标 + 画师占位符旗标（去重保序）。后者可按字符串直接检索：
            # `artist_name_leaked`（回填后正文仍出现真名）/
            # `artist_placeholder_unknown`（残留未知下标）。
            "flags": list(dict.fromkeys([*list(verdict.reasons or ()),
                                         *list(artist_flags or ())])),
            **self._annotation_meta(task),
            "ok": True,
            "attempts": attempts,
            "provenance": self._provenance(temperature, escalated=escalated,
                                           cache_hit=cache_hit),
            "ts": time.time(),
        }
        if not prompt_tokens:
            payload.pop("prompt_tokens")
        if cache_hit:
            payload["cache_hit"] = True
        if task.image_path:
            payload["image_path"] = task.image_path
        return payload


# ------------------------------------------------------------------ 批处理


def _rel_for_layout(task: TaskRow) -> str:
    """mirror 布局用的相对路径。

    manifest 的 rel_path 形如 `dist-0003.zip::artist/0001.jpg`，把 zip 名当目录
    会产出一个谁都不认识的层级；member（zip 内原始路径）才是镜像该走的那条。
    """
    return task.rel_path or task.file


def _artifact_paths(out_dir: Path, task: TaskRow, layout: str) -> tuple[Path, Path]:
    rel = _rel_for_layout(task)
    tag = out_dir / annotation_name(task.image_id, "tag", layout=layout,
                                    rel_path=rel, style="dash")
    nl = out_dir / annotation_name(task.image_id, "nl", layout=layout,
                                   rel_path=rel, style="dash")
    return tag, nl


def _ref_tags_of(row: dict) -> tuple[str, ...]:
    """从 manifest / parquet 行里取原始标注。

    不同来源写法不同（list、逗号分隔、空格分隔），这里统一成元组；一个都没有
    就返回空，调用方据此决定加不加 `<REFERENCE>` 段——空段落比不加更糟。
    """
    for key in ("ref_tags", "tags_original", "danbooru_tags", "tags"):
        val = row.get(key)
        if not val:
            continue
        parts = re.split(r"[,;]+|\s+", val) if isinstance(val, str) else [str(x) for x in val]
        got = tuple(p.strip() for p in parts if p and p.strip())
        if got:
            return got
    return ()


def _hf_task(sample: dict, shard: str) -> TaskRow:
    """parquet 的一行 → 任务。shard 里存的是分片名（只用于日志/失败明细）。"""
    path = str(sample.get("image_path") or f"{sample['image_id']}.jpg")
    return TaskRow(
        shard=shard,
        image_id=str(sample["image_id"]),
        file=path,
        artist=str(sample.get("artist") or ""),
        # 封包产物的 parquet 带这两列（见 pack.py）。没有就是空，
        # 优先级自动退回"查表 > 目录名"。
        artist_source=str(sample.get("artist_source") or ""),
        artists=tuple(sample.get("artists") or ()),
        source_revision=str(sample.get("source_revision")
                            or sample.get("revision") or ""),
        ref_tags=_ref_tags_of(sample),
        # 上游给的数据类别（anime / photo / …）。没有就空串，prompt 里不加
        # 动漫声明——见 _media_block。
        media=str(sample.get("media") or sample.get("dataset_media") or ""),
        rel_path=path,
        image_path=path,
    )


def load_failure_counts(path: Path | None) -> dict[str, int]:
    """failures.jsonl 里每个 image_id 累计失败过几次。

    这个数是**跨运行累计**的：每次失败都追加一行，重跑时先读旧账。
    没有它的话，一张必然失败的图（坏文件、模型对它稳定吐空）会在每次
    重跑时都重试 3 次再失败——3M 规模下这类样本的浪费是小时级的。
    """
    from .storage.collections import SpillMap
    counts = SpillMap()
    if path is None or not path.is_file():
        return counts
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                iid = str(obj.get("image_id") or "")
                if iid:
                    counts[iid] = counts.get(iid, 0) + 1
    except OSError:
        pass
    return counts


def load_failed_ids(path: Path | None) -> dict[str, dict]:
    """failures.jsonl → {image_id: 最近一条记录}，只留**当前仍失败**的。

    给「重试失败项」用：要重试的**是这些 id**，不是"整个分片再跑一遍"。
    同一 id 失败过多次时保留最后一条（attempts 最大、错误最新），前面那些
    是它越变越糟的过程，对决定"还试不试"没有帮助。

    `resolved: true` 是冲销行：这张后来重试成功了（或被字节门槛判为跳过）。
    failures.jsonl 是**追加账**，旧失败行不会删，所以必须按"最后一条"判状态；
    最后一条是冲销就把它从待办里剔除，否则侧栏数字虚高、服务端 note 虚报，
    「重试失败」还会把已经成功的图一遍遍捞回来重打。

    读不出来（文件不存在 / 行坏了）就当空——重试是补救动作，不该因为账本
    格式问题把整批任务拦下来。
    """
    out: dict[str, dict] = {}
    if path is None or not path.is_file():
        return out
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                iid = str(obj.get("image_id") or "")
                if iid:
                    out[iid] = obj
    except OSError:
        pass
    return {k: v for k, v in out.items() if not v.get("resolved")}


def _unexcuse(path: Path, image_id: str) -> None:
    """把一张图从 excused.jsonl 里删掉（重试后它成功了）。

    为什么是整文件重写而不是 append 一条 "ok"：账本只回答一个问题——
    "这张图现在还算不算坏样本"。留着历史会让每个读者都要自己算一遍
    "后者是否推翻了前者"，而这个问题不该由读取方回答。

    量很小（剔除了几十张，重试时更少），重写代价可以忽略。写坏的窗口用
    `os.replace` 兜住：中途崩了旧的账本还在，不会留下半截文件。
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    kept = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            kept.append(line)          # 坏行原样留着，别替人删证据
            continue
        if isinstance(obj, dict) and str(obj.get("image_id") or "") == image_id:
            continue
        kept.append(line)
    if len(kept) == len(lines):
        return                          # 本来就不在账上，什么也不用做
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def load_excused_ids(path: Path | None) -> set[str]:
    """excused.jsonl 里的 image_id 集合（已被剔除的坏样本）。

    重试那条路必须读它：同一张图可能在上一次重试里就已经被判死了，
    只读 failures 会把它又捞出来重跑一轮——每次「重试失败项」都白跑一遍
    那几张最脏的图，而它们的结论早就写好了。
    """
    ids: set[str] = set()
    if path is None or not path.is_file():
        return ids
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("image_id"):
                    ids.add(str(obj["image_id"]))
    except OSError:
        pass
    return ids


def _already_done(client: TaggerClient, task: TaskRow, out_dir: Path, layout: str,
                  require: str | None = None) -> bool:
    """已有有效产物就跳过图片；换模型或产出模式须强制重做才重算。

    require 是"这一趟只补哪一侧"（tag / nl）。派工说这片缺 tag、本机只有
    NL 产物时，按"任一侧有就算做过"会整张跳过，tag 永远补不上——所以只补
    一侧时判据必须只看那一侧。None 保持历史口径（任一侧），普通续跑不受影响。
    """
    tag_path, nl_path = _artifact_paths(out_dir, task, layout)
    if require == "tag":
        return _existing_ok(tag_path, "tag")
    if require == "nl":
        return _existing_ok(nl_path, "caption")
    return (_existing_ok(tag_path, "tag") or _existing_ok(nl_path, "caption"))


def _write_excluded_row(sink, task, kind: str, reason: str) -> None:
    """把"这张确定无产物"写进产物流（jsonl 才有；其它形态静默跳过）。

    为什么不用 sink.add(...)：tag/nl 传 None 会把它们序列化成空串，看起来
    像"跑过但什么都没写出来"，而空内容是失败信号不是结论。剔除记录必须
    带 excluded 标记，读侧才能把它和真正的空产出区分开。

    落盘失败只告警：缺失一行剔除记录会让合并时多报一张缺失，但绝不该让
    一张坏图的归档动作把整批打标打断。
    """
    fn = getattr(sink, "add_excluded", None)
    if fn is None:
        return                       # parquet / json 形态：没有承载位置
    try:
        fn(task.image_id, kind, reason or "", task.shard)
    except Exception as exc:         # noqa: BLE001
        print(f"[client][警告] 剔除记录写不进产物：{task.image_id}（{exc}）",
              file=sys.stderr, flush=True)


def run_batch(
    client: TaggerClient,
    items: Iterable,
    reader: ShardReader | None,
    *,
    out_dir: Path,
    layout: str,
    force: bool,
    concurrency: int,
    guard: ResourceGuard | None,
    stats: ClientStats,
    failures: Path | None,
    verbose: bool,
    fail_counts: dict[str, int] | None = None,
    max_failures: int = 0,
    stage_timer: StageTimer | None = None,
    sink=None,
    done_ids: set[str] | None = None,
    on_progress: ProgressHook | None = None,
    should_stop: Callable[[], bool] | None = None,
    paused: Callable[[], bool] | None = None,
    clients: Sequence[TaggerClient] | None = None,
    work=None,
    retry_budget: int = 0,
    only_ids: set[str] | None = None,
    parts_env: str = "",
) -> ClientStats:
    """跑完一批。单写者：文件只在主线程写，worker 只算。

    这么分是因为"目录里多一个文件"与"两个线程同时 rename"这两件事凑在一起
    调试成本极高，而写两个小 json 根本不值得并行。

    `on_progress` 在**主线程**上逐张回调（跳过/拉黑也算），所以订阅方可以直接
    改自己的状态，不必加锁；传 None 就是 CLI 的老行为，一个字都不变。

    `should_stop` 是协作式停止（Web 的停止按钮）：串行路径当场抛 `Stopped`；
    并发路径停补新任务、等在途请求收尾后正常返回——在途的几张不写产物，
    重跑会被重做，与 guard 熔断同一套语义。

    `clients` 是多卡（数据并行）的入口：每张卡一个 TaggerClient，各自指向
    自己那个推理服务。任务按轮询分发，**每个槽仍然并发 `concurrency` 条**，
    所以总在途 = len(clients) × concurrency。写盘依旧只有主线程一份——
    这是多卡只改"算"不改"写"的关键：每张图落到哪个 json 与它跑在哪张卡上
    无关，产物不需要事后合并。

    `paused` 是协作式暂停（Web 的暂停按钮）。语义与 stop 不同：
    **不是结束，而是原地挂起**——在途的几张按批结清后不再发新任务，主循环
    在 `_wait_resume` 里空转，恢复后从原位置继续发。所以暂停期间已完成的图
    都已经落盘，关掉程序再打开也能接着做（续跑靠产物 + 指纹判定，见
    `_already_done`）。stop 优先于 resume：暂停中收到控制请求会当场抛
    `Stopped`，不会把主循环卡在等恢复上。

    `retry_budget`：这一趟是不是"重试失败项"那一轮（>0 表示是，且这是第几次）。
    只有 >0 时失败才会被**剔除**（写 excused.jsonl、从分母扣掉）而不是记进
    failures.jsonl。正常打标那一趟永远不剔除：第一次没成不代表救不回来，
    那时把图判死等于用一次失败下结论。

    `parts_env`：FOXLABEL_PARTS 的值（tag / nl / 空）。决定"已有产物"的判定
    要不要按侧粒子看——只补 nl 的片，已有 tag 产物不代表做完，反之亦然。
    ★必须做成参数：它原来直接读模块里的自由变量，只有 run_client 里定义过，
      run_batch 单独被调（Web、测试）时就是 NameError，而那个错会被外层当成
      "这一片跑挂了"，现象是整片没产物却不报编译期问题。

    `only_ids`：「重试失败项」那条路用。只处理这个集合里的 image_id，
    其余的直接跳过且**不计入 total**——分母必须是这次真要做的张数。
    """
    # 注意：这里不再 len(tasks)。parquet 模式的任务是流式生成的，
    # 长度事先根本不知道（要先读完分片才能数清），所以 total 在迭代时累加。
    if done_ids is None:
        done_ids = set()

    # parts_env 由调用方从 FOXLABEL_PARTS 读好传进来（见 run_client）。
    # 这里只做一次归一：不认识的取值一律当"不限制"，绝不能因为拼错就变成
    # "只做某一侧"——那会安静地把另一侧留空。
    parts_env = str(parts_env or "").strip().lower()
    if parts_env not in ("tag", "nl"):
        parts_env = ""
    allow_parts = bool(parts_env)
    # 产出模式必须能产出被点名的那一侧。指向反了（parts=nl 而 mode=tag）时
    # 本侧**永远产不出**：跳过判定按本侧查、产物里却没有本侧，于是每张图
    # 白跑一遍还照常计入 done——"跑得很正常、账本也在涨、就是没补上"。
    # 放在这里而不是 run_client：run_batch 才是真正的执行入口。
    _mode = str(getattr(getattr(client, "output", None), "mode", "both") or "both")
    if allow_parts and _mode not in ("both", parts_env):
        raise ConfigError(
            f"派工要求这一片只补 {parts_env}，但产出模式是 {_mode}，"
            f"本侧永远不会产生内容。把产出模式改成 both 或 {parts_env}。")

    # 惰性打开：一次都没失败时不该在产物目录里留一个空的 failures.jsonl，
    # 那看起来像"有失败明细而内容被吞了"。
    fail_holder: list = []

    def fail_fh():
        if failures is None:
            return None
        if not fail_holder:
            failures.parent.mkdir(parents=True, exist_ok=True)
            fail_holder.append(failures.open("a", encoding="utf-8", buffering=1))
        return fail_holder[0]

    # 剔除账（坏样本）：与 failures.jsonl 同一个目录、同样的行格式。
    # 为什么不复用 failures：两者的**处理方式**相反——failures 是"下次还会再
    # 做"，excused 是"已经放弃、不要再来找我"。写在一起，下次续跑会把它们
    # 又捞出来重跑一遍，"剔除"就变成了一个假动作。
    excused_path = (failures.with_name("excused.jsonl")
                    if failures is not None else None)
    excused_holder: list = []

    def excused_fh():
        if excused_path is None:
            return None
        if not excused_holder:
            excused_path.parent.mkdir(parents=True, exist_ok=True)
            excused_holder.append(
                excused_path.open("a", encoding="utf-8", buffering=1))
        return excused_holder[0]

    # 无内容图的明细。config 与 stats.summary_lines 都写着"明细见
    # skipped.jsonl"，但原来只累计计数、从不落盘；封包/验收侧想把这些图从
    # 应产出清单里排除时，没有任何地方能查到是哪些 id。
    skipped_path = (failures.with_name("skipped.jsonl")
                    if failures is not None else None)
    skipped_holder: list = []

    def skipped_fh():
        if skipped_path is None:
            return None
        if not skipped_holder:
            skipped_path.parent.mkdir(parents=True, exist_ok=True)
            skipped_holder.append(
                skipped_path.open("a", encoding="utf-8", buffering=1))
        return skipped_holder[0]

    def resolve_failure(task: TaskRow) -> None:
        """往失败账追加一条"这张已经了结"的冲销行（重试成功 / 被跳过）。

        failures.jsonl 是事件流：上一轮失败写的一行，重试成功了也不会消失。
        读取方只看 id 集合的话，已经打好的图会一直算"还欠着"——侧栏数字虚高、
        note 虚报，而且「重试失败」每点一次都会把它们再捞回来重打。

        ★为什么不改成"成功时把那行删掉"：失败分支是多进程并发追加（FileLock +
          append），删行要整文件重写、和它们抢同一把锁，几万张的批次里每成功
          一张就重写一次账本，IO 会炸。追加一行是 O(1)，天然与失败写共用同
          一把锁，读取方按"同一 id 最后一条为准"还原当前状态即可。

        ★只在重试轮写：正常打标那一趟这张图根本不在失败账里，凭空多一行
          resolved 反而会污染统计（一条没有对应失败的"了结"）。
        """
        if only_ids is None or task.image_id not in only_ids:
            return
        fh = fail_fh()
        if fh is None:
            return
        line = json.dumps({"image_id": task.image_id, "resolved": True,
                           "ts": time.time()}, ensure_ascii=False)
        from .storage.locking import FileLock
        with FileLock(failures.with_suffix(".lock")):
            fh.write(line + "\n")
            fh.flush()

    processed = 0
    warned: list = []
    # 连续配置性错误的计数。成功一张就清零：一个错 key 的表现是"每张都 401"，
    # 而偶发的一张 404（图坏了/被内容策略拒了）不该被当成密钥错误。
    fatal_streak = [0]

    def emit(kind: str, task: TaskRow, outcome: Outcome | None) -> None:
        """通知订阅方。**故意吞异常**：前端一个 KeyError 不该让三百万张的批
        停在第 3 张；但完全不吭声又会让人以为"进度不动了"，所以只报第一次。
        放在 stats 更新之后调用，订阅方拿到的一定是已结账的数。
        """
        if on_progress is None:
            return
        try:
            on_progress(kind, task, outcome, stats)
        except Exception as exc:
            if not warned:
                warned.append(1)
                print(f"[client][on_progress 异常] {type(exc).__name__}: {exc}"
                      "（后续同类异常不再提示）", file=sys.stderr, flush=True)

    def commit(task: TaskRow, outcome: Outcome, slot: int = 0) -> None:
        nonlocal processed
        processed += 1
        # 先把槽号落进 outcome 再 emit：订阅方按它累加"每张卡各打了多少"。
        outcome.slot = slot
        # 失败的请求同样花掉了输入 token（云端按请求计费），别只统计成功的。
        stats.prompt_tokens += outcome.prompt_tokens
        stats.tokens += outcome.tokens
        stats.rate_limited += outcome.rate_limited
        if not outcome.ok:
            if work is not None:
                work.release(task.image_id)
            # ★跳过与失败必须在这一步之前分开：跳过的不进 failures.jsonl、
            #   不算 failed、不参与熔断。理由是它们的"补救"是一个都不做
            #   （输入图本身就是空的），而 failures.jsonl 的语义是"这张该成
            #   却没成"。混进去之后，靠 failures 累计次数跳过重试的逻辑
            #   会把它们当成需要反复重试的坏样本，白跑 3 轮。
            if outcome.skipped:
                stats.blank_skipped += 1
                stats.wall_ms += outcome.wall_ms
                print(f"[client][跳过] {task.image_id}  {outcome.error}",
                      file=sys.stderr, flush=True)
                # 明细单独归档：这类图永远不会有产物，验收/封包侧要能查到
                # "它不是漏做了，是输入无效"，否则会被当成缺失而卡住整批。
                fh = skipped_fh()
                if fh is not None:
                    from .storage.locking import FileLock
                    with FileLock(skipped_path.with_suffix(".lock")):
                        fh.write(json.dumps({
                            "image_id": task.image_id, "file": task.file,
                            "shard": Path(task.shard).name,
                            "error": outcome.error, "ts": time.time(),
                        }, ensure_ascii=False) + "\n")
                        fh.flush()
                # 小图被字节门槛拦下也是"了结"：不冲销的话，它会永远留在
                # 失败账里，每次点「重试失败」都被捞出来再跳一次。
                resolve_failure(task)
                # 也写进产物 jsonl：最终产物是所有人上传后合并成一整份，
                # 到时本机这份 skipped.jsonl 早就不在手上了，只有跟着行走，
                # 合并侧才知道"这张是输入无效、不是漏做"。
                _write_excluded_row(sink, task, "skipped", outcome.error)
                emit("skipped", task, outcome)
                return
            # 已彻底剔除的老样本：不重跑、不算失败、不进任何一本账。
            # 它之所以出现在这里，是因为"重试失败项"按分片捞了几千个 id，
            # 其中大部分可能早就被剔除了（那时它已经没有机会出现在输入里）。
            if outcome.retired:
                stats.wall_ms += outcome.wall_ms
                emit("retired", task, outcome)
                return
            line = json.dumps({
                "image_id": task.image_id, "file": task.file,
                "shard": Path(task.shard).name, "error": outcome.error,
                "attempts": outcome.attempts, "ts": time.time(),
            }, ensure_ascii=False)
            # 坏样本剔除：重试轮次已经用完，仍失败。
            # 与上面的普通失败**分开记**：这一张以后不会再试，写进 failures
            # 只会让下一次续跑把它重新捞出来，形成"重跑 → 还失败 → 重跑"的死循环。
            #
            # ★只在重试轮里剔除（retry_budget > 0）。正常打标那一趟永远不剔除：
            #   第一次没成不代表救不回来，那时候就把图判死等于拿一次失败下结论，
            #   而那正是"失败项还有救"这个前提本身。
            if outcome.excused and retry_budget > 0:
                stats.excused += 1
                stats.wall_ms += outcome.wall_ms
                fh = excused_fh()
                if fh is not None:
                    from .storage.locking import FileLock
                    with FileLock(excused_path.with_suffix(".lock")):
                        fh.write(line + "\n")
                        fh.flush()
                print(f"[client][剔除] {task.image_id}  {outcome.error}",
                      file=sys.stderr, flush=True)
                _write_excluded_row(sink, task, "excused", outcome.error)
                emit("excused", task, outcome)
                return
            stats.failed += 1
            stats.wall_ms += outcome.wall_ms
            for r in (outcome.error or "未知原因").split("; ")[:3]:
                key = r.split(":")[0].strip()[:60]
                stats.by_reason[key] = stats.by_reason.get(key, 0) + 1
            if outcome.fatal:
                fatal_streak[0] += 1
                if fatal_streak[0] >= _FATAL_BREAK:
                    # 这里是"整批停下"而不是"这张跳过"：密钥/模型名/地址错了，
                    # 后面每一张都会以同样的方式失败，跑完只会得到一目录失败明细。
                    raise Stopped(
                        f"连续 {fatal_streak[0]} 条配置性错误（{outcome.error[:120]}），"
                        f"已中止整批——请先修好密钥 / 模型名 / 地址")
            fh = fail_fh()
            if fh is not None:
                from .storage.locking import FileLock
                with FileLock(failures.with_suffix(".lock")):
                    fh.write(line + "\n")
                    fh.flush()
            print(f"[client][失败] {task.image_id}  {outcome.error}", file=sys.stderr, flush=True)
            emit("failed", task, outcome)
            return

        # mirror 布局的图真的带子目录，而 image_id 是全局序号（`画师-000123`），
        # 两者对不上。产物里不记 rel_path 的话，合并侧只能拼成扁平路径，训练
        # 侧会成批找不到图；而且这个错要到训练时才发现。
        # 只对 mirror 写：flat 布局下子目录已经不存在，记下来反而会让合并侧
        # 拼出一批不存在的层级。
        if layout == "mirror":
            rel = _rel_for_layout(task)
            if outcome.tag is not None:
                outcome.tag["rel_path"] = rel
            if outcome.nl is not None:
                outcome.nl["rel_path"] = rel

        tag_path, nl_path = _artifact_paths(out_dir, task, layout)
        if work is not None and not work.owns(task.image_id):
            raise RuntimeError(f"图片领取已失效：{task.image_id}")
        try:
            with (stage_timer.track("write") if stage_timer is not None
                  else _null_track()):
                if sink is not None:
                    # shard 只对 jsonl 的"一包一文件"有意义，其余 sink 忽略。
                    sink.add(task.image_id, outcome.tag, outcome.nl, task.shard)
                else:
                    if outcome.tag is not None:
                        _write_json_atomic(tag_path, outcome.tag)
                    if outcome.nl is not None:
                        _write_json_atomic(nl_path, outcome.nl)
                    if work is not None:
                        work.release(task.image_id)
        except OSError as exc:
            if work is not None and sink is None:
                work.release(task.image_id)
            stats.failed += 1
            print(f"[client][写入失败] {task.image_id}: {exc}", file=sys.stderr, flush=True)
            # 算完成后挂了写盘，结果同样是"这张没有产物"，对订阅方而言就是 failed。
            emit("failed", task, outcome)
            return

        stats.done += 1
        stats.wall_ms += outcome.wall_ms
        # token 已在上面对所有结果统一累加过；这里只负责把连续失败计数清零。
        fatal_streak[0] = 0
        if outcome.cached:
            stats.cached += 1
        # 这张图以前被判过"坏样本"，这次换了参数又打成功了：把它从剔除账里
        # 撤回来。不撤的话它会被永远当成坏样本，每次重试都被 retired 拦掉，
        # 剔除统计也一直虚高一张。写在产物之后、done 之前——产物落了盘才算数。
        if only_ids and task.image_id in only_ids and excused_path is not None:
            _unexcuse(excused_path, task.image_id)
        # 失败账里那条旧失败行到此为止：这张已经有产物了，下一轮不该再捞它。
        resolve_failure(task)
        if verbose:
            n = stats.skipped + processed
            print(f"  [{n}/{stats.total}] {task.image_id}  "
                  f"{outcome.wall_ms / 1000:.2f}s  {outcome.tokens} tok  "
                  f"{outcome.attempts} 次", flush=True)
        elif processed % 100 == 0:
            print(f"[client] {stats.skipped + processed}/{stats.total}  "
                  f"完成 {stats.done}  失败 {stats.failed}", flush=True)
        emit("done", task, outcome)

    def pending_items():
        """惰性生成待处理项。

        **不能先收集成 list**：parquet 是流式的，一个分片上百万行的话，
        先攒 list 等于把整个数据集解码进内存。跳过判定放在这里，
        逐项吐、逐项做。
        """
        for item in items:
            if _stopping():
                return
            if isinstance(item, tuple):
                task, data = item
            else:
                task, data = item, None
            # 「只重试失败项」：不在这份 id 集里的直接不看，**连 total 都不加**。
            # 分母必须是"这次真的要做的张数"，否则进度条会停在 80/12121，
            # 看起来像任务卡死。放在 stats.total 之前是刻意的。
            if only_ids is not None and task.image_id not in only_ids:
                continue
            stats.total += 1
            # 先查黑名单：字典查找比读两个 json 便宜，也更能说明问题
            if (max_failures and fail_counts
                    and fail_counts.get(task.image_id, 0) >= max_failures):
                stats.blacklisted += 1
                if verbose:
                    print(f"  [拉黑] {task.image_id}"
                          f"（累计失败 {fail_counts[task.image_id]} 次）", flush=True)
                emit("blacklisted", task, None)
                continue
            # parquet 模式下"文件存在"这条判定不成立（一个分片里有很多行），
            # 改成查启动时建好的 id 集合。两者语义相同：这张已经做完了。
            # ★只补一侧时（allow_parts）这个索引**不能信**：它同样是"任一侧
            #   有产物就算完成"，用它跳过会让被点名那一侧永远补不上。
            #   sink 形态没有逐图文件可查，只能靠下面的 claim 锁互斥，
            #   重复劳动由 reclaim + 落锁保证不冲突。
            if sink is not None and not allow_parts:
                if not force and task.image_id in done_ids:
                    stats.skipped += 1
                    if verbose:
                        print(f"  [跳过] {task.image_id}（产物里已有）", flush=True)
                    emit("skipped", task, None)
                    continue
            elif sink is None and not force and _already_done(
                    client, task, out_dir, layout, require=parts_env):
                stats.skipped += 1
                if verbose:
                    print(f"  [跳过] {task.image_id}（产物已存在）", flush=True)
                emit("skipped", task, None)
                continue
            # 画师缺失时整张跳过：配置 artist.skip_when_missing，默认关。
            # 判定前移到领任务之前，是为了不烧算力——查表是纯 IO，打一张要几秒，
            # 真实数据集约 4.6% 的图在 danbooru 上没有作者标注。
            _arule = getattr(client, "artist_rule", None)
            if (_arule is not None and _arule.enabled
                    and getattr(_arule, "skip_when_missing", False)
                    and not client._artist_names(task)[0]):
                stats.artist_skipped += 1
                if verbose:
                    print(f"  [跳过] {task.image_id}（无画师）", flush=True)
                emit("skipped", task, None)
                continue
            # 只补一侧时，完成索引（work / done_ids 都是"任一侧有产物就算完成"）
            # 不能当判据：它会把这个 id 判成 done，tag 侧那空缺就永远补不上。
            # 此时改为直接看本侧产物文件——这条路跟上面 sink 分支同一个理由，
            # 只是"哪一侧算有效"由派工点名。
            if allow_parts and not force and _already_done(
                    client, task, out_dir, layout, require=parts_env):
                stats.skipped += 1
                if verbose:
                    print(f"  [跳过] {task.image_id}（{parts_env} 侧产物已有）", flush=True)
                emit("skipped", task, None)
                continue
            if work is not None:
                status = work.claim(task.image_id, force=force)
                if status != "claimed":
                    if status == "busy":
                        # 别人正在做：不管补哪一侧都得让开，两个进程同时写
                        # 同一份产物才是真事故。
                        stats.busy += 1
                        emit("busy", task, None)
                        continue
                    if status == "done" and not allow_parts:
                        stats.skipped += 1
                        emit("skipped", task, None)
                        continue
                    if status == "done":
                        # 只补一侧时的 done：索引口径是"任一侧有产物"，本侧还缺，
                        # 所以这不是"做完"，是"该重做本侧"。
                        # ★必须 reclaim 而不是直接往下走：claim 返回 done 时
                        #   **没有插 claims 行**，落盘前的 owns() 断言会抛
                        #   "领取已失效"，整批直接中断。reclaim 撤销 done 记录
                        #   并真正拿到锁，别人的 busy 也照旧让开。
                        status = work.reclaim(task.image_id)
                        if status != "claimed":
                            stats.busy += 1
                            emit("busy", task, None)
                            continue
                # 另一个进程可能在初次检查之后、领取之前刚刚完成逐图文件。
                # （sink 形态下产物在分片里，逐图文件查不到，只能靠 claim 的锁
                #   互斥，所以这里仍只查 sink is None 的那条路。）
                if sink is None and not force and _already_done(
                        client, task, out_dir, layout, require=parts_env):
                    work.release(task.image_id)
                    stats.skipped += 1
                    emit("skipped", task, None)
                    continue
            yield task, data

    # 多卡：每个 client 一个槽，槽内再并发。没给 clients 就是单服务、单槽。
    slots: list[TaggerClient] = list(clients) if clients else [client]
    per_slot = max(1, int(concurrency))
    workers = per_slot * len(slots)
    if len(slots) > 1:
        print(f"[client] 多卡   : {len(slots)} 个槽，每槽 {per_slot} 并发"
              f"（合计 {workers} 条在途）", flush=True)

    def _stopping() -> bool:
        return should_stop is not None and should_stop()

    def _wait_resume() -> None:
        """暂停时原地等。0.2s 轮询而不是 Condition：这里等的只是一个 bool
        变假，用 Condition 就得在 HTTP 线程里配对加锁与通知，漏一处就是
        永久挂起；而 0.2s 的唤醒代价在"打一张要几秒"的量级上可以忽略。"""
        if paused is None or not paused():
            return
        while paused():
            if _stopping():
                raise Stopped("暂停期间收到停止请求")
            time.sleep(0.2)

    def _result(fut: Future, task: TaskRow) -> Outcome:
        try:
            return fut.result()
        except Exception as exc:      # 兜底：worker 里的 bug 不该中断整批
            return Outcome(task.image_id, False,
                           error=f"内部异常: {type(exc).__name__}: {exc}")

    def _flush_sink() -> None:
        """把攒在内存里的产物推到盘上。

        暂停/停止都是**会话边界**：用户很可能按完就关程序，而分片 sink
        （parquet / jsonl）默认攒满 2000 行才写一片。不在这里 flush 的话，界面上
        「已完成 300 张」会随着关闭凭空消失，下次继续还得重打——这正是"暂停要
        保存进度"的反面。json 布局是逐张写的，不受影响。
        """
        if sink is None:
            return
        try:
            sink.flush()
        except Exception as exc:      # noqa: BLE001 - 落盘失败不该盖住暂停本身
            print(f"[client][警告] 落盘失败（暂停/停止时）：{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)

    try:
        if workers <= 1:
            for task, data in pending_items():
                if _stopping():
                    _flush_sink()
                    raise Stopped("收到停止请求")
                if paused is not None and paused():
                    _flush_sink()
                    _wait_resume()
                if guard is not None:
                    _check_guard(guard)
                outcome = slots[0].process(task, data, reader)
                commit(task, outcome, 0)
                if guard is not None:
                    guard.after_unit()
            if _stopping():
                _flush_sink()
                raise Stopped("收到停止请求")
            return stats

        with ThreadPoolExecutor(max_workers=workers) as pool:
            # 键里带上槽号：结算时要按它记"这张是哪张卡打的"。
            pending: dict[Future, tuple[TaskRow, int]] = {}
            it = pending_items()
            exhausted = False

            def submit_next(slot: int) -> bool:
                nonlocal exhausted
                if exhausted:
                    return False
                if _stopping():
                    return False      # 停补新任务；在途的等它们自然收尾
                if paused is not None and paused():
                    return False      # 暂停也停补，但主循环会等回来接着发
                try:
                    task, data = next(it)
                except StopIteration:
                    exhausted = True
                    return False
                if guard is not None:
                    _check_guard(guard)
                # 只补回刚空出的槽，保证快卡不会把请求堆到仍在忙的慢卡。
                fut = pool.submit(slots[slot].process, task, data, reader)
                pending[fut] = (task, slot)
                return True

            def drain() -> None:
                """把在途的都结清。暂停是"这一批做完再挂起"，不是"现在就停"：
                中途丢掉的请求不写产物，重跑会被重做，而用户点暂停时看到的
                进度会凭空回退几张。"""
                while pending:
                    done, _ = wait(pending, return_when=ALL_COMPLETED)
                    for fut in done:
                        task, slot = pending.pop(fut)
                        commit(task, _result(fut, task), slot)
                        if guard is not None:
                            guard.after_unit()
                _flush_sink()

            for index in range(workers):
                if not submit_next(index % len(slots)):
                    break

            while True:
                # 全部提交完且没有在途：真的跑完了。先判这个，否则"暂停"会
                # 卡在一批已经做完的图上，状态一直显示暂停。
                if exhausted and not pending:
                    break

                if paused is not None and paused() and not _stopping():
                    drain()
                    _wait_resume()          # 期间收到 stop 会抛 Stopped
                    for index in range(workers):
                        if not submit_next(index % len(slots)):
                            break
                    continue

                if not pending:
                    _flush_sink()           # 停止也是会话边界，别把内存里的行丢掉
                    break                   # 被 stop 掐住，不再补新任务

                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    task, slot = pending.pop(fut)
                    commit(task, _result(fut, task), slot)
                    # 让步按批结算：一个 worker 回来 ≈ 一个批次的边界
                    if guard is not None:
                        guard.after_unit()
                    submit_next(slot)
        if _stopping():
            _flush_sink()
            raise Stopped("收到停止请求")
        return stats
    finally:
        for fh in fail_holder:
            fh.close()


def _check_guard(guard: ResourceGuard) -> None:
    st = guard.before_unit()
    if not st.can_run:
        raise Stopped(st.describe())


# ------------------------------------------------------------------ 组装


def resolve_concurrency(value: int | None, fp: dict, provider: str = "") -> int:
    """在途请求数。不填时跟服务端的并发走。

    为什么默认不再是 1：vLLM 的吞吐全在连续批处理上，客户端一次只送一条时
    每次 batch 都只有一条，30M 量级下等于把机器闲置着跑。而盯着服务端的
    max_num_seqs 是唯一不需要人拍脑袋的锚点——它就是这个服务愿意同时处理
    多少条请求。

    llama.cpp 的锚点是 `-np`（槽位数），不是 max_num_seqs：那条参数根本
    没进 llama-server 的命令行，指纹里的值只是分档表按 vLLM 口径写的。
    所以按 engine 取字段，缺 engine 的旧指纹仍走 max_num_seqs。

    上限压到 32：再多只是让请求在服务端排队，吞吐不变而延迟与内存都涨
    （每张图的原始字节还压在客户端手里）。真正的数用 `tagger bench` 量。
    """
    if value is not None:
        return max(1, int(value))
    if str(fp.get("engine") or "") == "llamacpp":
        try:
            slots = int(fp.get("slots") or 0)
        except (TypeError, ValueError):
            slots = 0
        if slots > 0:
            return max(1, min(slots, 32))
    try:
        seqs = int(fp.get("max_num_seqs") or 0)
    except (TypeError, ValueError):
        seqs = 0
    if seqs <= 0:
        # API 模式没有指纹可依：云端按"每分钟请求数"限流，与本机显存无关，
        # 所以用该来源的保守起点而不是 4。本机缺指纹时仍是 4。
        if provider:
            return max(1, nl_api.default_concurrency(provider) or 4)
        # 指纹缺失（没跑过 serve，或跨机读取不到）：4 是保守值，
        # 但比 1 强得多，而且单卡 4B 上也确实装得下。
        return 4
    return max(1, min(seqs, 32))


def load_fingerprint(path: str | Path | None) -> dict:
    """读 `tagger serve` 写下的环境指纹。

    缺了也能跑，但 provenance 会残——而残缺的 provenance 意味着这批数据
    事后无法回答"在什么环境下产的"。所以缺的时候要显式提示一次，
    不能静默空着。
    """
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        print(f"[client][警告] 没找到环境指纹 {p}。"
              f"provenance 将不完整（建议先在起服务的那台机器上跑 `tagger serve`）。",
              file=sys.stderr)
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[client][警告] 指纹读不动（{exc}），provenance 将不完整。", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _prov_base(fp: dict, *, model_id: str, max_pixels: int, tier: str,
               caption_chars: tuple[int, int] | None = None) -> dict:
    """Provenance 的固定部分。每张都带一份，几百字节换可追溯性，值。"""
    return {
        # 推理来源。`local` 是本机/自建 vLLM·llama.cpp；`openai` / `anthropic`
        # 是外部 API。同一个 model_id 在两处是两套输出（量化、版本、上下文都
        # 不同），所以它和 precision 同级记下来——否则混批事后无从分辨。
        "provider": str(fp.get("provider") or "local"),
        "model_id": model_id or str(fp.get("model_id") or "unknown"),
        "model_path": str(fp.get("model_path") or "unknown"),
        "model_revision": fp.get("model_revision"),
        "precision": str(fp.get("precision") or "unknown"),
        "max_pixels": int(max_pixels or fp.get("max_pixels") or 0),
        # 描述长度引导。它直接决定产出分布（实测 400-1024 → 250-500 会少三成
        # 字数、快三成半），所以和 precision 同级记下来；否则两批数据事后
        # 无法判断是否同源。
        "caption_chars": list(caption_chars) if caption_chars else None,
        "vllm_version": fp.get("vllm_version"),
        "tier": tier or "",
        "device": str(fp.get("device") or ""),
    }


def _resize_to_budget(data: bytes, max_pixels: int) -> bytes:
    """把超过像素预算的图片等比缩小。不超预算原样返回（字节不动）。

    为什么客户端要做这件事：重试提分辨率（quality.retry_pixels_scale）的
    前提是"基线预算 < 服务端上限"，这时服务端只钳到上限，基线只能靠
    客户端自己执行。缩放算法与 pack 同一套口径：等比、alpha 铺白底、
    JPEG q95——落进包里的本来就该是这个形态。
    """
    from io import BytesIO

    from PIL import Image

    try:
        im = Image.open(BytesIO(data))
        im.load()
    except Exception:          # noqa: BLE001 - 解不开的图交给服务端去报错
        return data
    w, h = im.size
    if w <= 0 or h <= 0 or w * h <= max_pixels:
        return data
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[-1])
        im = flat
    else:
        im = im.convert("RGB")
    scale = (max_pixels / (w * h)) ** 0.5
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    buf = BytesIO()
    im.resize((nw, nh), Image.LANCZOS).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _as_paths(paths) -> list:
    """统一成路径列表。

    CLI 的 --shard 是 nargs+（列表），而 Web 侧 JobSpec.input_dir 是一个字符串。
    直接 `for raw in "<路径>"` 会逐字符遍历，报出来的是"分发包不存在: D"——
    这个坑在这里堵掉，别指望每个调用方都记得套一层 list。
    """
    if isinstance(paths, (str, Path)):
        return [paths]
    return list(paths or [])


# 本项目 Packer 产出的 webdataset 后缀；tar.gz / tgz 也要认。
_WEBDS_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")


def _is_webdataset(p: Path) -> bool:
    return p.name.lower().endswith(_WEBDS_SUFFIXES)


def classify_local_inputs(paths) -> tuple[list[Path], list[Path]]:
    """把本地输入分成 (parquet 分片, webdataset 包)。

    目录按内容判：有 .parquet 就当 parquet 批，有 .zip/.tar 就当分发包。
    两者混在一起直接报错——它们的 image_id 口径和"整片完成"标记都不通用，
    拼一起只会得到一份对不上账的产物。
    """
    parquet: list[Path] = []
    shards: list[Path] = []
    for raw in _as_paths(paths):
        p = Path(raw)
        if p.is_dir():
            parquet.extend(sorted(p.glob("*.parquet")))
            shards.extend(sorted(q for q in p.iterdir()
                                 if q.is_file() and _is_webdataset(q)))
        elif p.is_file():
            if p.name.lower().endswith(".parquet"):
                parquet.append(p)
            elif _is_webdataset(p):
                shards.append(p)
            else:
                raise ConfigError(
                    f"不认识的分片格式: {p.name}"
                    f"（支持 .parquet / .zip / .tar / .tar.gz / .tgz）")
        else:
            raise ConfigError(f"分片不存在: {p}")
    if parquet and shards:
        raise ConfigError(
            "本地输入里同时有 parquet 和 zip/tar：两种格式的编号口径不同，"
            "请分两次跑（parquet 走流式，zip/tar 走解包）。")
    if not parquet and not shards:
        raise ConfigError(
            f"{paths} 下没有 .parquet / .zip / .tar 分片。"
            f"散图目录要先在「数据打包」页封包。")
    return parquet, shards


def collect_shards(paths: Sequence[str] | str) -> list[Path]:
    """展开 --shard：可以给包，也可以给装包的目录。"""
    out: list[Path] = []
    for raw in _as_paths(paths):
        p = Path(raw)
        if p.is_dir():
            found = sorted(p.glob("*.zip"))
            if not found:
                raise ConfigError(f"{p} 里没有 .zip 分发包")
            out.extend(found)
        elif p.is_file():
            out.append(p)
        else:
            raise ConfigError(f"分发包不存在: {p}")
    # 同一个包被写两遍（目录 + 具体文件）时去重，避免整包重打
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def load_tasks(shards: Sequence[Path], *, hf_repo: str = "") -> list[TaskRow]:
    """按 manifest 建任务表。

    同一个 image_id 出现在两个包里是分发错误（不是"两个人抢活"）：
    merge 会把它判成内容冲突。这里先发现、先报出来，别等交回去才炸。

    tar（webdataset）没有 manifest.jsonl，成员名本身就是清单：
    按「同 stem 不同后缀」归组，取图片成员当 file（见 hf_source.read_tar_samples）。
    """
    from . import hf_source

    from .storage.collections import SpillList, SpillMap
    tasks = SpillList(encode=asdict, decode=lambda v: TaskRow(**v))
    seen = SpillMap()
    dups = SpillList()

    for shard in shards:
        try:
            if str(shard).lower().endswith((".tar", ".tgz", ".tar.gz")):
                rows = hf_source.read_tar_samples(shard)
                if hf_repo:
                    rows = hf_source.merge_manifests(rows, repo=hf_repo,
                                                     shard=shard.name)
            else:
                rows = read_shard_manifest(shard)
        except (KeyError, OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
            print(f"[client][警告] 读不动 {shard.name}: {exc}", file=sys.stderr)
            continue
        for row in rows:
            image_id = str(row.get("image_id") or "").strip()
            if not image_id:
                print(f"[client][警告] {shard.name} 有 manifest 行缺 image_id，已跳过",
                      file=sys.stderr)
                continue
            if image_id in seen:
                dups.append(f"{image_id}（{Path(seen[image_id]).name} / {shard.name}）")
                continue
            seen[image_id] = str(shard)
            tasks.append(TaskRow(
                shard=str(shard),
                image_id=image_id,
                file=str(row.get("file") or f"{image_id}{normalize_ext(image_id)}"),
                artist=str(row.get("artist") or ""),
                # 分包/封包时查好的画师，带着来源标记随包走（见 _artist_names）
                artist_source=str(row.get("artist_source") or ""),
                artists=tuple(row.get("artists") or ()),
                source_revision=str(row.get("source_revision")
                                    or row.get("revision") or ""),
                ref_tags=_ref_tags_of(row),
                media=str(row.get("media") or row.get("dataset_media") or ""),
                # rel_path 里存"镜像布局该走的路径"：优先 zip 内的原始 member，
                # 它才是训练侧认识的层级；散图没有 member 时退回 rel_path。
                rel_path=str(row.get("member") or row.get("rel_path") or ""),
                image_path=str(row.get("image_path") or ""),
            ))

    if dups:
        print(f"[client][警告] {len(dups)} 个 image_id 在多个包里重复出现，"
              f"只处理第一份（其余会导致 merge 判成内容冲突）：", file=sys.stderr)
        for d in dups[:10]:
            print(f"    {d}", file=sys.stderr)
        if len(dups) > 10:
            print(f"    … 其余 {len(dups) - 10} 个略", file=sys.stderr)

    # 按 image_id 排序：分包表本身就是这个序，顺序处理好让日志与产出一致，
    # 中断重跑时人的直觉也对得上（"我做到 leilei-003200 了"）。
    tasks.sort(key=lambda t: t.image_id)
    seen.close()
    return tasks


def _filter_shards(names: list[str], wanted) -> list[str]:
    """按文件名挑分片（成员"只处理某个文件"的流程）。wanted 空 = 全部。

    精确匹配而不是 glob：文件名里的点在 glob 里是"任意字符"，`shard-1.parquet`
    会顺带命中 `shard-1xparquet` 这种名字。挑了但仓库里没有的，点名警告——
    静默少跑一片，对账时就是一片"整包没交"。
    """
    want = {str(w).strip() for w in (wanted or []) if str(w).strip()}
    if not want:
        return names
    hit = [n for n in names if n in want]
    missing = sorted(want - set(hit))
    if missing:
        print(f"[client][警告] 请求的分片里有 {len(missing)} 个在仓库中不存在："
              f"{', '.join(missing[:8])}{'…' if len(missing) > 8 else ''}",
              file=sys.stderr)
    return hit


def run_client(args, ops: OpsConfig, table: dict,
               on_progress: ProgressHook | None = None, *,
               should_stop: Callable[[], bool] | None = None,
               paused: Callable[[], bool] | None = None,
               on_log=None, guard: "ResourceGuard | None" = None,
               only_failed: bool = False,
               retry_budget: int = 1,
               parts_env: str = "") -> int:
    """CLI 入口的实体。参数解析留在 cli.py，这里只做组装与执行。

    `on_progress` 原样透传给 `run_batch`（Web 层靠它推实时进度）；
    CLI 不传，行为与以前完全一致。

    `should_stop`：协作式停止，原样透传给 `run_batch`；触发后 run_client
    返回码非 0，Web 层结合自己的停止标志区分"失败"与"已停止"。

    `on_log`：关键横幅/汇总同时回调给订阅方（Web 的日志事件流）。
    print 保持不变——终端里跑还是老样子，回调只是"多一份"。

    `guard`：外部预构造的 ResourceGuard（Web 侧带 on_state 事件接线）。
    传了就以它为准、由本函数负责 start/stop；不传且 args.no_guard 为假
    时按老路径自建。两个来源只会存在一个 watchdog。

    `only_failed`：只重打 failures.jsonl 里列出的 id，而不是整片重跑。
    这是「重试失败项」按钮的落点——整片重跑虽然靠产物跳过也能达到同样的
    效果，但它要扫完 12121 张才知道哪 80 张该做，而且 `stats.total` 会变成
    整片张数，进度条上看起来像"从头又跑了一遍"。

    `retry_budget`：允许这样重试几轮。>0 时仍失败的样本会被剔除
    （excused.jsonl + 从分母扣除）而不是继续留在 failures 里——见
    `OutputSpec.retry_failed_rounds`。
    """

    def _log(msg: str) -> None:
        print(msg, flush=True)
        if on_log is not None:
            try:
                on_log(msg)
            except Exception:
                pass      # 日志回调不该比任务本身更娇气

    defaults = table.get("defaults") or {}
    output = ops.output_spec()
    # Web 端把界面滑块上的值覆盖进来；CLI 不传就沿用配置的 output 段。
    # 夹在 0~1：界面上是 0-100 的百分数，换算出错也只会退到两端，不会炸整批。
    if getattr(args, "caption_zh_ratio", None) is not None:
        try:
            output.caption_zh_ratio = min(1.0, max(
                0.0, float(args.caption_zh_ratio)))
        except (TypeError, ValueError):
            pass
    mode = str(args.mode or output.mode).lower()
    if mode not in ("nl", "tag", "both"):
        raise ConfigError(f"--mode 只能是 nl / tag / both，收到 {mode!r}")

    # ---- 推理来源。三种走同一套主循环，只有方言与"有没有环境指纹"不同。
    try:
        provider = nl_api.normalize_provider(getattr(args, "api_provider", ""))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    api_key = str(getattr(args, "api_key", "") or "").strip()
    if provider:
        base_url, api_key = nl_api.resolve_credentials(
            provider, getattr(args, "api_base", ""), api_key)
        if not str(args.model_name or "").strip():
            raise ConfigError(
                f"{nl_api.LABELS[provider]} 必须给模型名（--model-name / 表单「模型名」）："
                f"服务端不认识本地占位名 tagger。")
        _log(f"[client] 推理来源 : {nl_api.LABELS[provider]}  {base_url}  "
             f"模型 {args.model_name}"
             + ("" if api_key else "（未填密钥，只对免鉴权的自建网关可行）"))
    else:
        base_url = str(args.url)
    # 长度引导：命令行/任务表单显式给了就覆盖配置。它只改"往多长写"，
    # 不动 nl_min/max_tokens 这两个闸门——否则调长度会顺手把合格线也挪了。
    caption_chars = output.caption_chars
    if getattr(args, "caption_chars", None):
        caption_chars = parse_caption_chars(args.caption_chars)
    output = OutputSpec(mode=mode, nl_min_tokens=output.nl_min_tokens,
                        nl_max_tokens=output.nl_max_tokens,
                        tag_min_count=output.tag_min_count,
                        tag_max_count=output.tag_max_count,
                        caption_chars=caption_chars,
                        # ★重建 OutputSpec 时每个字段都要抄过来。漏 caption_zh_ratio
                        # 的后果是"表单/命令行给了比例、跑出来一个中文字都没有"，
                        # 而且不报错——这一条是被真实分片测出来的。
                        caption_zh_ratio=output.caption_zh_ratio,
                        text_rules=output.text_rules)

    quality: QualityPolicy = ops.quality_policy()
    artist_rule: ArtistRule = ops.artist_rule()

    # AB 变体。这里只做"取值"，渲染放到 output 收窄之后——CPU tag 模式会把
    # output 改成 nl，而 prompt 必须按收窄后的规格生成，先渲染会得到一份
    # 要求模型吐 tag 的指令，白费 token 还污染产物。
    from .abtest import pick_variant

    variant_name = str(getattr(args, "variant", "") or "")
    variant = pick_variant(table.get("variants") or {}, variant_name) if variant_name else None

    # ---- 双线：CPU 出 tag，GPU 出 NL
    tagging = ops.tagging or {}
    backend_name = str(args.tag_backend or tagging.get("backend") or "pixai")
    need_tags = output.wants_tag
    # 模型名必须跟后端绑定：配置里只有一个 `model` 字段时，切后端就会把
    # pixai 的仓库名喂给 wd-eva02（实测直接报"不像 WD 仓库"）。
    # 所以优先查按后端分组的 models 表，单个 model 字段只作为老配置的兜底。
    models_map = {str(k): str(v) for k, v in (tagging.get("models") or {}).items()}
    model_for_backend = (args.tag_model
                         or models_map.get(backend_name)
                         or tagging.get("model"))
    # 设备清单。`auto` 只给一张卡（老行为不变）；要多卡必须显式点名，
    # 因为每张卡各加载一份权重——显存与启动时间都跟着翻。
    tag_devices = resolve_devices(args.tag_device or tagging.get("device"))
    # 精度默认 tf32：79 张真实素材上实测 0 个 tag 翻面、概率最大漂移 0.0027，
    # 前向 1.33×。要逐位复现老口径就显式 --tag-precision fp32。
    tag_precision = str(getattr(args, "tag_precision", None)
                        or tagging.get("precision") or "tf32")
    tag_batch = int(getattr(args, "tag_batch", None) or tagging.get("batch") or 1)
    # 预处理：pil = 现状（CPU 缩放/归一化）；tensor = 缩放/归一化上卡。
    # 解码始终在 CPU——nvJPEG 逐张比 PIL 还慢，只有批量才赢，而这里是逐张喂。
    tag_preprocess = str(getattr(args, "tag_preprocess", None)
                         or tagging.get("preprocess") or "pil")
    tag_backend = build_tag_backends(
        backend_name,
        devices=tag_devices,
        model=model_for_backend,
        model_dir=args.tag_model_dir,
        keep=tagging.get("keep_categories"),
        thresholds={str(k): float(v) for k, v in (tagging.get("thresholds") or {}).items()},
        # ★CLI 这里传 output.tag_max_count，web 那边用 build_backend 的默认
        #   160——两条入口口径不一，但**不要**顺手"修统一"。这个数是 top-N
        #   天花板（先按阈值筛再截），而 output.tag_max_count 是 VLM 生成 tag
        #   的 token 预算；统一到 64 会把文字密集图上 125~152 个 tag 截掉。
        #   完整理由见 cpu_tagger.build_backend 的 docstring。
        max_tags=output.tag_max_count,
        token=args.hf_token or os.environ.get("HF_TOKEN"),
        verbose=not args.quiet,
        precision=tag_precision,
        batch_size=tag_batch,
        preprocess=tag_preprocess,
    )
    if tag_backend is not None:
        if not output.wants_nl:
            raise ConfigError(
                "--mode tag 不能配 CPU tag 后端：tag 由后端产出，VLM 就没有活干了。\n"
                "          用 --mode nl 或 both，或者 --tag-backend none 让 VLM 出 tag。")
        # 预加载：缺依赖、下不到模型、权重对不上，现在就说，
        # 别等跑到第 500 张才发现整批 tag 全是空的。
        try:
            tag_backend.load()
        except TaggerUnavailable as exc:
            raise ConfigError(f"tag 后端加载失败：{exc}") from exc

        if len(tag_devices) > 1:
            _log(f"[client] tag 多卡 : {len(tag_devices)} 份权重"
                 f"（{', '.join(tag_devices)}），每批按卡数等分")
        # 实测（RTX 4090 主机 + 消费级 CPU）：pixai 1008²/486M 在 CPU 上
        # 约 16 s/张，wd-eva02 448²/eva02-large 约 2.3 s/张。而 GPU 上的
        # VLM 通常 1~3 s/张——用 pixai 跑 CPU，整条流水线会被它拖慢 5~8 倍，
        # "双线并行"退化成"CPU 串行 + GPU 空转"。
        if tag_backend.name == "pixai" and tag_devices == ["cpu"]:
            print("[client][警告] pixai 在 CPU 上实测约 16 秒/张，会是整条流水线的瓶颈。"
                  "\n          要跟上 GPU（1~3 秒/张）请换 --tag-backend wd-eva02"
                  "（约 2.3 秒/张，16473 词表）。",
                  file=sys.stderr)
        # VLM 收窄成只出 NL；tag_min/max 保留下来给数量校验用
        output = OutputSpec(mode="nl",
                            nl_min_tokens=output.nl_min_tokens,
                            nl_max_tokens=output.nl_max_tokens,
                            tag_min_count=output.tag_min_count,
                            tag_max_count=output.tag_max_count,
                            caption_chars=output.caption_chars,
                            caption_zh_ratio=output.caption_zh_ratio,
                            text_rules=output.text_rules)

    # 变体可以带自己的生成上限与温度；命令行显式给了就以命令行为准。
    variant_max_tokens = variant.max_tokens if variant is not None else None
    max_tokens = int(args.max_tokens or variant_max_tokens
                     or defaults.get("max_tokens", 800))
    # 生成上限必须放得下"NL 全量 + tag 全量 + 两段标记"。放不下时的表现是
    # `<CAPTION>` 说到一半断掉或后面几十个 tag 直接消失——两种都不会报错，
    # 只会静默产出一条短数据。所以这里按最坏情况估一次，不够就抬。
    need_tokens = 64 + (output.nl_max_tokens if output.wants_nl else 0)
    if output.wants_tag:
        need_tokens += output.tag_max_count * 3      # 一个 danbooru tag ≈ 2~3 token
    if max_tokens < need_tokens:
        print(f"[client][警告] max_tokens={max_tokens} 放不下 "
              f"{output.tag_max_count} 个 tag + {output.nl_max_tokens} token 的 NL，"
              f"本次抬到 {need_tokens}（根治请改 configs/tiers.yaml 的 "
              f"defaults.max_tokens）。", file=sys.stderr)
        max_tokens = need_tokens

    temperature = float(args.temperature if args.temperature is not None
                        else (variant.temperature if variant is not None
                              and variant.temperature is not None
                              else defaults.get("temperature", 0.2)))

    # 变体渲染必须发生在调用 TaggerClient 之前、且在 output 收窄之后：
    # prompt 里的长度/数量约束是从 output 生成的，渲染得早会用错规格。
    prompt_text = variant.render_prompt(_prompt_for(output)) if variant is not None else None
    # WebUI 在 prompt/*.json 里选的那份走同一条路（整段替换）。与变体同时存在时
    # 以文件为准：那是用户此刻在页面上明确选的东西，CLI 的变体是配置层的默认值。
    chosen_prompt = str(getattr(args, "prompt_text", "") or "").strip()
    if chosen_prompt:
        prompt_text = chosen_prompt
    prompt_zh_text = str(getattr(args, "prompt_zh_text", "") or "").strip() or None
    system_text = str(getattr(args, "system_text", "") or "").strip() or None
    if (chosen_prompt and not prompt_zh_text
            and float(getattr(output, "caption_zh_ratio", 0.0) or 0.0) > 0
            and output.wants_nl):
        # 自定义提示词没写中文版：这批会全出英文。说一句，别让人跑完才发现
        # "比例明明设了 30%，怎么一条中文都没有"。
        print("[client][警告] 这份自定义提示词没有中文版（prompt_zh），"
              "本次 caption_zh_ratio 不生效，全部出英文。", file=sys.stderr)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if provider:
        # 外部 API 没有 `tagger serve` 写下的指纹，别去读那个文件——
        # 读不到会打一句"建议先跑 serve"，而这里根本不该跑 serve。
        # 需要的三项（来源 / 模型 / 精度）就地造出来。
        fp = {"provider": provider,
              "model_id": str(args.model_name or ""),
              "precision": "api"}
        # API 侧没有服务端的 max_pixels 钳制，基线预算只能靠客户端执行；
        # server_cap 留给 0，重试提分辨率不会被钳回去。
        fp.pop("max_pixels", None)
    else:
        fp = load_fingerprint(args.fingerprint)
    model_id = str(args.model_id
                  or (args.model_name if provider else "")
                  or fp.get("model_id") or "")
    # 基线预算的取值顺序：命令行 > 配置 > 指纹。配置优先于指纹是刻意的：
    # "serve 时用更大的 --max-pixels 留出余量、客户端维持配置里的基线"
    # 正是重试提分辨率的打开方式——若指纹优先，基线永远等于服务端上限，
    # 重试就没有可抬的空间。
    if provider:
        # 云端没有服务端钳制，基线只能靠客户端执行。取值顺序与本地不同：
        # 配置里的 max_pixels 是给本地 serve 留余量用的（例如 1024×1024 配
        # 1536 的上限），套到云端只会白付像素钱还看不到更多细节，所以来源
        # 推荐值排在配置前面。
        base_pixels = (int(args.max_pixels or 0)
                       or nl_api.default_max_pixels(provider)
                       or int(defaults.get("max_pixels", 0) or 0))
    else:
        base_pixels = int(args.max_pixels
                          or defaults.get("max_pixels", 0)
                          or fp.get("max_pixels") or 0)
    prov_base = _prov_base(
        fp, model_id=model_id,
        max_pixels=base_pixels,
        tier=str(args.tier or ""),
        caption_chars=output.caption_chars,
    )
    server_cap = int(fp.get("max_pixels") or 0)
    if (quality.retry_pixels_scale > 1 and server_cap
            and base_pixels and base_pixels >= server_cap):
        print(f"[client][警告] 基线预算 {base_pixels} ≥ 服务端上限 {server_cap}，"
              f"重试提分辨率不会生效（全被服务端钳回基线）。\n"
              f"          要启用：起服务时用更大的 --max-pixels 留余量"
              f"（如 {quality.retry_pixels_cap}），客户端维持配置默认。",
              file=sys.stderr)
    if variant_name:
        prov_base["variant"] = variant_name
    prompt_name = str(getattr(args, "prompt_name", "") or "").strip()
    if prompt_name:
        # 只作溯源备注，不参与"同源判定"：判断用的是 prompt 文本的 sha，
        # 给文件改名不该让历史产物被判成"参数变了"从而白重打一遍。
        prov_base["prompt_name"] = prompt_name

    concurrency = resolve_concurrency(args.concurrency, fp, provider)
    gate = QualityGate(quality, verbose=False)
    if quality.enable_clip:
        # 链路已经接通（图片在 _run 里解码后传给 gate），但打分模型是同一个
        # 实例上的前向，并发会互相踩权重与显存——锁只能保证不崩。所以并发
        # 摁到 1，并把代价说清楚：30M 量级下这是吞吐的生死线。
        if not gate.clip or not gate.clip.available:
            print(f"[client][警告] quality.enable_clip 已开启，但 "
                  f"{gate.clip.reason if gate.clip else 'CLIP 未初始化'}。"
                  f"本次将只跑 L1 规则层。", file=sys.stderr)
        elif concurrency > 1:
            print("[client][警告] CLIP 打分不是线程安全的，并发被强制改成 1。"
                  "启用 L2 会让整条流水线慢一个数量级，建议只用小批量抽检。",
                  file=sys.stderr)
            concurrency = 1

    # 结果缓存：只有显式开启才建库。首轮命中率为 0 而图哈希本身有成本，
    # 所以默认关闭（理由见 result_cache 模块头）。
    cache: ResultCache | None = None
    if bool(getattr(args, "cache", False)):
        cache_path = Path(getattr(args, "cache_db", "") or CACHE_DB_PATH)
        max_gb = float(getattr(args, "cache_max_gb", 8.0) or 0.0)
        cache = ResultCache(cache_path, max_bytes=int(max_gb * (1 << 30)))
        print(f"[client] 结果缓存 : {cache_path}"
              f"（上限 {max_gb:.1f} GB；同图同参直接复用，不送模型）", flush=True)

    stage = StageTimer()
    # 多卡（数据并行）：主地址是第一张卡，args.urls 是其余的。
    # 去重并保序——同一个服务被当成两个槽，只会让并发翻倍而吞吐不变，
    # 排查时表现成"并发调了没效果"。
    # 主地址用 base_url：API 模式下它来自 --api-base，与 --url 不是一回事。
    service_urls = [base_url]
    for extra in (getattr(args, "urls", None) or []):
        u = str(extra or "").strip()
        if u and u.rstrip("/") not in {x.rstrip("/") for x in service_urls}:
            service_urls.append(u)

    # 所有槽共用同一套参数：换卡不该改变产出，只改变谁算。
    client_kwargs: dict = dict(
        model_name=args.model_name,
        output=output,
        temperature=temperature,
        top_p=float(args.top_p if args.top_p is not None else defaults.get("top_p", 0.9)),
        max_tokens=max_tokens,
        repeat_penalty=float(defaults.get("repeat_penalty", 0.0) or 0.0),
        timeout_s=int(args.timeout or defaults.get("request_timeout_s", 90)),
        max_attempts=int(quality.max_attempts),
        gate=gate,
        prov_base=prov_base,
        artist_rule=artist_rule,
        # file:// 只有本机引擎认；API 模式下强制走 base64（客户端里还会再兜一次）。
        use_file_url=bool(args.use_file_url) and not provider,
        spool_dir=out_dir / ".spool",
        seed=args.seed,
        tag_backend=tag_backend,
        need_tags=need_tags,
        prompt_text=prompt_text,
        prompt_zh_text=prompt_zh_text,
        system_text=system_text,
        variant=variant_name,
        stage=stage,
        pixel_cap=server_cap,
        concurrency=concurrency,
        escalate_url=str(getattr(args, "escalate_url", "") or ""),
        escalate_model=str(getattr(args, "escalate_model", "") or ""),
        cache=cache,
        provider=provider,
        api_key=api_key,
        # 中文 NL 比例来自 output 段（tiers.yaml）。0 = 全英文，老批次照旧。
        caption_zh_ratio=float(getattr(output, "caption_zh_ratio", 0.0) or 0.0),
    )
    clients = [TaggerClient(base_url=u, **client_kwargs) for u in service_urls]
    client = clients[0]
    if len(clients) > 1:
        _log(f"[client] 多卡服务 : {len(clients)} 个 "
             f"（{', '.join(service_urls)}）")
        if quality.enable_clip:
            # CLIP 打分占着同一个进程内的模型实例，多线程会互相踩权重。
            # 上面已经把这卡上的并发摁到 1，多卡的话还得只留一个服务，
            # 否则 N 张卡同时打分等于把并发又抬回 N。
            _log("[client][警告] CLIP 打分不是线程安全的，多卡降级为只用一个服务。")
            clients = clients[:1]

    stats = ClientStats()
    cache_info: dict = {}
    failures = Path(args.failures) if args.failures else out_dir / "failures.jsonl"
    layout = str(args.layout or (ops.distribute or {}).get("layout", "flat"))
    remaining = int(args.limit) if args.limit else 0

    # 产物形态。json = 每图两文件（默认，完成信号最直接）；
    # parquet = 攒成片（千万级时文件数降 4 个数量级，见 artifacts.py 的说明）；
    # jsonl = 同样攒成片，但一片是纯文本、不要 pyarrow，坏行能直接看出来。
    artifact_format = str(getattr(args, "artifact_format", "json") or "json").lower()
    rows_per_file = int(getattr(args, "parquet_rows", 2000) or 2000)
    sink = None
    work = None
    done_ids = None
    # 派工点名补哪一侧时，"已有产物"的判据要收紧（见 run_batch 的 parts_env）。
    # 归一放在这里，日志与跳过判定读的是同一个值，不会各说各话。
    parts_want = str(parts_env or os.environ.get("FOXLABEL_PARTS", "")).strip().lower()
    if parts_want not in ("tag", "nl"):
        parts_want = ""
    # 跨运行累计的失败账。同一张图失败到 max_failures 次就不再碰它，
    # 否则每次重跑都要为同一批坏样本重烧 3 次推理。
    max_failures = int(getattr(args, "max_failures", 3) or 0)
    fail_counts = load_failure_counts(failures)
    if fail_counts:
        black = sum(1 for n in fail_counts.values() if max_failures and n >= max_failures)
        _log(f"[client] 历史失败 : {len(fail_counts)} 个 id，"
             f"其中 {black} 个已达上限（本次跳过）")

    # ---- 只重试失败项
    # 与"整片重跑"的区别不在结果（成功过的都会跳过），而在**代价与进度口径**：
    # 整片重跑要扫完全部输入才知道该做哪几张，进度分母是整片张数；这里只把
    # failures.jsonl 里的 id 拿出来做，分母就是失败数。用户点的是"重试失败的"，
    # 界面上就该只看到那几十张在动。
    retry_budget = max(0, int(retry_budget or 0))
    retry_ids: set[str] | None = None
    if only_failed:
        excused_file = failures.with_name("excused.jsonl") if failures else None
        retired_ids = load_excused_ids(excused_file)
        wanted = load_failed_ids(failures)
        # 剔除过的 id 不再重试：它们的结论上次已经写死了。老样本随时可能被
        # 人工从 excused.jsonl 里删掉，那时它自然回到"可重试"的集合里。
        retry_ids = {k for k in wanted if k not in retired_ids}
        if not retry_ids:
            _log("[client] 没有需要重试的失败项"
                 + (f"（另有 {len(retired_ids)} 个已剔除的坏样本不会再试）"
                    if retired_ids else ""))
            return 0
        _log(f"[client] 只重试失败项 : {len(retry_ids)} 张"
             f"（允许重试 {retry_budget} 轮，仍失败则剔除出分母）")
        # 这条路上的失败要判成"剔除"而不是"下次再做"。多卡时每一路的
        # client 都要置位，否则失败落在别的卡上就还是老行为。
        for _c in (clients or ([client] if client is not None else [])):
            if _c is not None:
                _c._retrying_failed = True
        # 「已失败过 3 次就拉黑」在这条路上必须关掉，否则一条都做不了：
        # 这些图本来就是因为失败过才被选出来的，用同一个上限拦自己是自相矛盾。
        max_failures = 0
        fail_counts = {}
        if retry_budget <= 0:
            # 0 轮意思就是"不许剔除"：失败项照老样子再记一次失败账，
            # 留给人工看。这也是唯一"重试完还留在 failures 里"的组合。
            _log("[warn] retry_failed_rounds=0：本次失败项不会被剔除，"
                 "仍会留在 failures.jsonl 里")

    # 分片级完成标记：HF 模式下重跑时整片跳过。
    # 存在单独的 .hf-done 目录而不是产物目录根下，免得被 merge 的扫描扫到。
    done_dir = out_dir / ".hf-done"

    def shard_done(name: str) -> bool:
        # 旧版在中止或 Parquet 未落盘时也会写标记，需逐图核验一次。
        # complete-v3 = "有条件完成"（剔过坏样本），同样算完成——但多一段
        # 剔除数，重跑日志里能对上是哪几片。
        try:
            marker = (done_dir / f"{Path(name).name}.done").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        return (marker.startswith(f"complete-v2\t{name}\t")
                or marker.startswith(f"complete-v3\t{name}\t"))

    def mark_shard_done(name: str, excused: int = 0) -> None:
        # 分片 sink 会攒行；必须先成功封盘，再承诺下次可整片跳过。
        if sink is not None:
            sink.flush()
        done_dir.mkdir(parents=True, exist_ok=True)
        from .storage.atomic import write_bytes
        # 两种版本分开写：剔除过的片不该看起来和干净片一样。差一个字段
        # 就少一次"这片明明标了完成，为什么少 4 张"的排查。
        payload = (f"complete-v3\t{name}\t{time.time():.0f}\texcused={excused}\n"
                   if excused else f"complete-v2\t{name}\t{time.time():.0f}\n")
        write_bytes(done_dir / f"{Path(name).name}.done", payload.encode("utf-8"))

    # guard 的磁盘检查盯产物目录：满盘时最先坏的是正在写的 json。
    # Web 传入的 guard 优先——它带 on_state 事件接线，自建会出现两个
    # watchdog 各自采样、各自熔断的鬼畜局面。
    if guard is None and not args.no_guard:
        guard = ResourceGuard(
            ops.guard_policy(),
            data_dir=out_dir,
            device_name=str(fp.get("device") or ""),
            verbose=True,
        )

    def run_group(group: list[Path], *, hf_repo_name: str = "") -> tuple[int, bool]:
        """处理一组包。本地模式一组，HF 模式一个 tar 一组（下完就删）。

        返回 (处理数, 是否被 --limit 截断)。截断的整片**不能**标记完成，
        否则片尾没跑的任务下次被整片跳过——丢片不报错，是最贵的那种 bug。
        """
        nonlocal remaining
        limit_total = int(getattr(args, "limit", 0) or 0)
        try:
            reader = ShardReader([str(s) for s in group])
        except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise ConfigError(
                f"打不开 {group[0].name}（{exc}）：文件可能没传完，重新下一份再跑。") from exc
        try:
            tasks = load_tasks(group, hf_repo=hf_repo_name)
            full_count = len(tasks)
            if remaining:
                tasks = tasks[:remaining]
                remaining -= len(tasks)
            if not tasks:
                return 0, bool(limit_total) and full_count > 0
            print(f"[client] 本批 : {group[0].name}  任务 {len(tasks)} 张", flush=True)
            run_batch(
                client, ((t, None) for t in tasks), reader,
                out_dir=out_dir,
                layout=layout,
                force=bool(args.force),
                concurrency=concurrency,
                guard=guard,
                stats=stats,
                failures=failures,
                verbose=not args.quiet,
                fail_counts=fail_counts,
                max_failures=max_failures,
                stage_timer=stage,
                sink=sink,
                done_ids=done_ids,
                work=work,
                on_progress=on_progress,
                should_stop=should_stop,
                paused=paused,
                clients=clients,
                retry_budget=retry_budget,
                only_ids=retry_ids,
                parts_env=parts_want,
            )
            return len(tasks), bool(limit_total) and full_count > len(tasks)
        finally:
            reader.close()

    def _run_parquet_entries(entries: list[tuple[str, str]], *,
                             token: str | None = None,
                             shard_mark: bool = True,
                             remove_after: set[str] | None = None) -> None:
        """流式打一批 parquet。entries = [(日志显示名, 数据源)]。

        数据源既可以是 `hf://datasets/<repo>/<file>`，也可以是本地绝对路径——
        `iter_parquet_samples` 两种都认，本地文件同样按 row group 流式读，
        不解包、不整表落盘。所以"本地 parquet"与"HF parquet"共用这一条路，
        跳过/失败账/产物写法全都一样。

        shard_mark：只有 HF 需要"整片完成"标记（省掉重复下载）。本地靠产物里的
        done_ids 逐行跳过就够；而且各目录的 pack-00000.parquet 会重名，
        拿文件名当标记键会让两批数据互相跳过。
        """
        nonlocal remaining
        img_col = str(getattr(args, "hf_image_column", "image") or "image")
        tpl = str(getattr(args, "hf_path_template", "{image_id}.jpg") or "{image_id}.jpg")
        _log(f"[client] 分片数 : {len(entries)}   (流式读取，不落盘)")
        _log(f"[client] 图片列 : {img_col}    image_path 模板 {tpl}")
        _log("")

        for idx, (name, uri) in enumerate(entries, 1):
            if should_stop is not None and should_stop():
                raise Stopped("收到停止请求")
            if remaining == 0 and getattr(args, "limit", None):
                break
            if shard_mark and not args.force and shard_done(name) and not args.limit:
                _log(f"[client] [{idx}/{len(entries)}] {name}  已完成，整片跳过")
                continue
            _log(f"[client] [{idx}/{len(entries)}] {name}")
            if shard_mark:
                (done_dir / f"{Path(name).name}.done").unlink(missing_ok=True)
            before_failed = stats.failed
            before_blacklisted = stats.blacklisted
            before_busy = stats.busy
            before_excused = stats.excused
            samples = hf_source.iter_parquet_samples(
                uri,
                image_column=img_col, token=token, repo="",
                shard=name, path_template=tpl,
            )
            counter = {"n": 0}
            truncated = {"v": False}

            def gen():
                for sample in samples:
                    if remaining and counter["n"] >= remaining:
                        # --limit 在这里把流掐断：这片没跑完，
                        # 调用方不能标记整片完成。
                        truncated["v"] = True
                        return
                    counter["n"] += 1
                    yield _hf_task(sample, name), sample["data"]

            run_batch(
                client, gen(), None,
                out_dir=out_dir,
                layout=layout,
                force=bool(args.force),
                concurrency=concurrency,
                guard=guard,
                stats=stats,
                failures=failures,
                verbose=not args.quiet,
                fail_counts=fail_counts,
                max_failures=max_failures,
                stage_timer=stage,
                sink=sink,
                done_ids=done_ids,
                work=work,
                on_progress=on_progress,
                should_stop=should_stop,
                paused=paused,
                clients=clients,
                retry_budget=retry_budget,
                only_ids=retry_ids,
                parts_env=parts_want,
            )
            if remaining:
                remaining = max(0, remaining - counter["n"])
            # 只有"整片跑完且零（未解决的）失败"才标记。被 --limit 截断的片
            # 不标，否则片尾没跑的样本下次被整片跳过，永远静默丢失。
            #
            # ★"剔除"不算未解决：被剔除的样本已经写进 excused.jsonl、从分母
            #   扣掉了，它们不会因为"下次再跑"而变好。把它们算进 failed 会让
            #   整个分片永远标不成完成，而下一次重试也只会得到同样的结论。
            #   分片完成的前提是"该做的都做了"，不是"每一张都成功了"。
            complete = (stats.failed == before_failed
                        and stats.blacklisted == before_blacklisted
                        and stats.busy == before_busy
                        and not truncated["v"])
            excused_here = stats.excused - before_excused
            # 有条件完成：这一片里有样本被剔除、其余全做完，也算完成。
            # 标成 "excused" 版本而不是和干净完成同一行，事后能查出"哪片是
            # 剔过坏样本才完成的"——否则一片标着完成、实际少了几张，没人知道。
            conditional = (not complete and shard_mark
                           and stats.failed == before_failed
                           and stats.busy == before_busy
                           and not truncated["v"]
                           and excused_here > 0)
            if shard_mark and complete:
                mark_shard_done(name)
            elif conditional:
                mark_shard_done(name, excused=excused_here)
                _log(f"[client] {name} 有条件完成：{excused_here} 张重试后仍失败，"
                     f"已剔除出分母（明细见 excused.jsonl）")
            if conditional:
                # 条件完成的分片也算跑完了，本地缓存可以删——否则磁盘上会
                # 留着几个"完成了但永远不删"的 GB 级文件。
                complete = True
            # 本地缓存下来的片：整片跑完且零失败就删，与 tar 的"打完即删"一致。
            # 失败或截断的留着——下次直接读本地，不必重新下几个 GB。
            # --hf-keep 时一律不删（那是显式要求留档）。
            if (remove_after and uri in remove_after
                    and not getattr(args, "hf_keep", False)
                    and complete):
                try:
                    Path(uri).unlink()
                except OSError:
                    pass

    def run_parquet(repo: str, revision: str, token: str | None) -> None:
        """HF 仓库里的 parquet 分片：列文件、过滤，然后交给上面的流式入口。"""
        files = hf_source.list_parquet(repo, revision=revision, token=token,
                                       proxy=hf_proxy)
        files = _filter_shards(files, getattr(args, "hf_files", None))
        if not files:
            raise ConfigError(
                f"{repo} 里没有可处理的 .parquet（仓库为空，或请求的文件名都不存在）。")
        max_shards = int(getattr(args, "hf_shards", 0) or 0)
        if max_shards:
            files = files[:max_shards]
        # 本地缓存优先：下载页（或 `--hf-cache` 里手动放好的）提前下过的分片
        # 直接读本地文件，不再走网络。网络差的机器用硬盘换速度，网络好的
        # 不下载就还是流式拉——两条路的产物与跳过逻辑完全一样。
        with hf_source.cache_session(hf_cache, should_stop):
            entries: list[tuple[str, str]] = []
            remove_after: set[str] = set()
            hit = 0
            for name in files:
                local = hf_cache / Path(name)
                if local.is_file() and local.stat().st_size > 0:
                    entries.append((name, str(local)))
                    remove_after.add(str(local))
                    hit += 1
                else:
                    entries.append((name, f"hf://datasets/{repo}/{name}"))
            if hit:
                _log(f"[client] 本地缓存 : {hit}/{len(files)} 片命中 {hf_cache}")
            _run_parquet_entries(entries, token=token, remove_after=remove_after)

    if len(clients) > 1:
        _log(f"[client] 目标服务 : {len(clients)} 个（多卡并行）  模型名 {args.model_name}")
        for i, c in enumerate(clients):
            _log(f"           #{i} {c.base_url}")
    else:
        _log(f"[client] 目标服务 : {client.base_url}  模型名 {args.model_name}")
    spec_note: list[str] = []
    if output.wants_nl:
        spec_note.append(f"NL {output.nl_min_tokens}~{output.nl_max_tokens} token")
    if need_tags:
        spec_note.append(f"tag {output.tag_min_count}~{output.tag_max_count} 个"
                         f"（{tag_backend.name if tag_backend else 'VLM'}）")
    _log(f"[client] 产出模式 : {output.mode}  " + "  ".join(spec_note))
    if parts_want:
        # 方向冲突由 run_batch 开头统一拦（那里是执行入口，直接调用也生效）。
        # 这里只负责把"这一趟在干什么"写清楚。
        _log(f"[client] 分项补齐 : 这一片只补 {parts_want}"
             + ("" if output.mode == parts_want
                else f"（产出模式 {output.mode}，另一侧已有效的图会跳过）"))
    if variant is not None:
        note = "整段替换" if variant.prompt_file else ("追加说明" if variant.prompt_note else "默认指令")
        _log(f"[client] AB 变体  : {variant.name}（{note}）")
        if variant.note:
            _log(f"           {variant.note}")
    _log(f"[client] 产物目录 : {out_dir.resolve()}")
    _log(f"[client] 并发     : {concurrency}    "
         f"model_id {prov_base['model_id'] or '(未知)'}")
    # 两条线的设备分开显示：NL 那侧由服务端的 vLLM 决定（客户端改不了），
    # tag 这侧才是本机参数。混在一起时，用户会以为改 --tag-device 能挪动 LLM。
    _log(f"[client] NL 设备  : {fp.get('device') or '(由服务端 vLLM 决定)'}")
    if tag_backend:
        _log(f"[client] tag 设备 : {tag_device}（{tag_backend.precision}"
             f"{f'，batch={tag_backend.batch_size}' if tag_backend.batch_size > 1 else ''}）")
    else:
        _log("[client] tag 设备 : 未启用（tag 由 VLM 出）")
    _log("")

    from . import hf_source

    hf_repo = str(getattr(args, "hf_repo", "") or "")
    # 每个仓库一个子目录（下载页与这里必须同口径，否则提前下好的分片命中不了）
    hf_cache = Path(getattr(args, "hf_cache", "") or hf_source.cache_dir_for(hf_repo))
    # HF 相关的网络请求统一走这一个代理：列清单、下分片、parquet 流式。
    # 只给本进程用，不改系统设置。
    hf_proxy = str(getattr(args, "hf_proxy", "") or "")
    if hf_proxy:
        # parquet 流式走 datasets/fsspec（内部是 requests），只认环境变量。
        # 用户显式指定代理时才覆盖，且不动本机回环（那些请求都是
        # trust_env=False，本来就绕开）。
        for name in ("HTTPS_PROXY", "HTTP_PROXY",
                     "https_proxy", "http_proxy"):
            os.environ.setdefault(name, hf_proxy)
        _log(f"[client] 网络代理 : {hf_proxy}")

    code = 0
    started = time.time()
    try:
        from .storage.work import WorkIndex
        work = WorkIndex(out_dir)
        work.refresh()
        done_ids = work
        if artifact_format in ("parquet", "jsonl"):
            from . import artifacts as artifacts_mod

            try:
                artifacts_mod.ensure_available(artifact_format)
            except RuntimeError as exc:
                raise ConfigError(str(exc)) from exc
            if artifact_format == "parquet":
                sink = artifacts_mod.ParquetSink(out_dir, rows_per_file=rows_per_file,
                                                 writer=work.owner, on_commit=work.publish)
            else:
                # jsonl 按包及进程分片：几十个分包跑同一个产物目录时，回传才能
                # 一人发自己的。--force 会重做已完成的图，所以不能续写同名文件。
                sink = artifacts_mod.MultiShardSink(
                    out_dir, rows_per_file=rows_per_file, append=not args.force,
                    writer=work.owner, on_commit=work.publish)
        elif artifact_format != "json":
            raise ConfigError(
                f"--artifact-format 只能是 json / parquet / jsonl，收到 {artifact_format!r}")
        if sink is not None:
            extra = f"，已有 {len(done_ids)} 张将自动跳过" if done_ids else ""
            if artifact_format == "jsonl":
                _log(f"[client] 产物形态 : jsonl 按包分片 "
                     f"（包名 + 进程编号 + 分片序号，"
                     f"每 {sink.rows_per_file} 行落一次盘{extra}）")
            else:
                _log(f"[client] 产物形态 : {artifact_format} 分片，"
                     f"每 {sink.rows_per_file} 行一片{extra}")

        if guard is not None:
            guard.start()
            # 守护线程的首轮采样是异步的，不等它的话，开跑后前几秒读到的还是
            # 初始的 ok。真实原因是"磁盘已经不足却照样开始写"——这类问题越早
            # 拦住越便宜，所以这里同步采一次，读数还能顺手打给人看。
            pre = guard.watchdog.sample_once()
            print(f"[guard] {pre.describe()}")
            if pre.level == "stop":
                raise Stopped(pre.describe())

        if hf_repo:
            token = args.hf_token or os.environ.get("HF_TOKEN")
            revision = str(getattr(args, "hf_revision", "main") or "main")
            want_fmt = str(getattr(args, "hf_format", "auto") or "auto").lower()
            fmt = want_fmt
            if fmt == "auto":
                fmt = hf_source.detect_format(hf_repo, revision=revision, token=token)
                if not fmt:
                    raise ConfigError(
                        f"{hf_repo} 里既没有 *.parquet 也没有 *.tar。"
                        f"确认仓库类型是 dataset，且文件已经上传完。")
            _log(f"[client] HF 仓库 : {hf_repo}   格式 {fmt}")

            if fmt == "parquet":
                run_parquet(hf_repo, revision, token)
            else:
                sized = hf_source.list_shards_sized(
                    hf_repo, revision=revision,
                    pattern=str(getattr(args, "hf_pattern", "*.tar") or "*.tar"),
                    token=token, proxy=hf_proxy,
                )
                size_map = dict(sized)
                names = [n for n, _ in sized]
                names = _filter_shards(names, getattr(args, "hf_files", None))
                if not names:
                    raise ConfigError(
                        f"{hf_repo} 里没有可处理的 .tar 分片"
                        f"（仓库为空，或请求的文件名都不存在）。")
                max_shards = int(getattr(args, "hf_shards", 0) or 0)
                if max_shards:
                    names = names[:max_shards]

                # 预取窗口与磁盘余量。余量不只是给下载用的：产物也写在
                # out_dir，多半与临时目录同一块盘，不给它留位置就会
                # "分片下满了、产物写不进去"——那种失败在几千张后才能发现。
                prefetch = max(0, int(getattr(args, "hf_prefetch", 1) or 0))
                reserve_gb = float(getattr(args, "hf_reserve_gb", 0.0) or 0.0)
                if reserve_gb > 0:
                    reserve_bytes = int(reserve_gb * (1 << 30))
                else:
                    biggest = max((size_map.get(n, 0) for n in names), default=0)
                    reserve_bytes = max(2 << 30, int(biggest * 0.3))

                _log(f"[client] 分片数 : {len(names)}")
                _log(f"[client] 临时目录: {hf_cache}")
                _log(f"[client] 下载策略: 预取 {prefetch} 片，处理完即删；"
                     f"余量 {reserve_bytes / 1073741824:.1f} GB，"
                     f"空间不够就暂停下载而不是写爆盘")
                _log("")

                keep = bool(getattr(args, "hf_keep", False))
                todo: list[str] = []
                for idx, name in enumerate(names, 1):
                    if not args.force and shard_done(name) and not args.limit:
                        _log(f"[client] [{idx}/{len(names)}] {name}  已完成，整片跳过")
                        continue
                    todo.append(name)

                seen = len(names) - len(todo)
                conns = max(1, min(16, int(getattr(args, "hf_conns", 4) or 1)))
                if conns > 1:
                    _log(f"[client] 下载并发: {conns} 条连接/片"
                         f"（单条被限速时靠多段并发提速）")
                for shard in hf_source.iter_hf_shards(
                        todo, repo=hf_repo, cache_dir=hf_cache,
                        revision=revision, token=token, prefetch=prefetch,
                        reserve_bytes=reserve_bytes, sizes=size_map,
                        conns=conns, proxy=hf_proxy,
                        keep=keep, verbose=not args.quiet, on_log=_log,
                        should_stop=should_stop):
                    seen += 1
                    name = shard.filename
                    if should_stop is not None and should_stop():
                        raise Stopped("收到停止请求")
                    _log(f"[client] [{seen}/{len(names)}] {name}")
                    (done_dir / f"{Path(name).name}.done").unlink(missing_ok=True)
                    before_failed = stats.failed
                    before_blacklisted = stats.blacklisted
                    before_busy = stats.busy
                    before_excused = stats.excused
                    try:
                        _, truncated = run_group([shard.path], hf_repo_name=hf_repo)
                    finally:
                        # 处理完立刻删：这是"只占一个分片空间"的前提，
                        # 也是预取能继续提交下一片的前提（空间刚被释放）。
                        hf_source.cleanup(shard, keep=keep)
                    # 只有整片跑完且零（未解决的）失败才标记。有失败别标
                    # （坏样本不能被藏起来）；被 --limit 截断也别标（片尾还没跑）。
                    # 例外：这一片里有样本被**剔除**了、其余全做完，算有条件完成。
                    if (stats.failed == before_failed
                            and stats.blacklisted == before_blacklisted
                            and stats.busy == before_busy and not truncated):
                        mark_shard_done(name)
                    elif (stats.failed == before_failed
                          and stats.busy == before_busy and not truncated
                          and stats.excused > before_excused):
                        mark_shard_done(name, excused=stats.excused - before_excused)
                        _log(f"[client] {name} 有条件完成："
                             f"{stats.excused - before_excused} 张重试后仍失败，已剔除")
                    if remaining == 0 and getattr(args, "limit", None):
                        break
        else:
            if not args.shard:
                raise ConfigError(
                    "要么给 --shard <分片>（本地 parquet / zip / tar），"
                    "要么给 --hf-repo <org/name>（仓库）。")
            # 本地两种格式走两条路：parquet 流式读、zip/tar 解包读。
            # 判定交给 classify_local_inputs（同时把"混着装"拦下来）。
            parquet_files, shard_files = classify_local_inputs(args.shard)
            if parquet_files:
                _log(f"[client] 本地 parquet : {len(parquet_files)} 片")
                _run_parquet_entries(
                    [(p.name, str(p.resolve())) for p in parquet_files],
                    shard_mark=False,
                )
                if not stats.total:
                    raise ConfigError(
                        "这些 parquet 里没有可处理的行：确认有 image 列（图片字节）"
                        "与 image_id 列。")
            else:
                total, _ = run_group(shard_files)
                if not total:
                    print("[client] 分发包里没有任务。检查 --shard 是否指对了包。")
                    code = 1

    except hf_source.HFError as exc:
        # 必须走 _log 而不是裸 print(stderr)：任务页只消费 on_log 发出去的事件，
        # 401/404 那句"该去填 token / 该检查仓库名"的指引写进 stderr 等于没写，
        # 用户只会看到一句"打标未完整跑完"。
        _log(f"[client] HF 访问失败：{exc}")
        _log("[client] 上面这段就是原因：401/403 缺 token、404 是仓库名或类型不对、"
             "DNS/连接超时是网络或代理。修好后重跑同一条命令即可续做。")
        code = 1
    except Stopped as exc:
        print(f"\n[client] 已停止：{exc}", file=sys.stderr)
        if str(exc).startswith("连续"):
            # 配置性错误中止：指向密钥/模型名/地址，别把 tiers.yaml 的阈值
            # 抬出来误导人（这两个原因的表现都是"跑一半停了"）。
            print("[client] 这批的错误与具体图片无关（密钥 / 模型名 / 地址 "
                  "/ 权限），修好之后重跑即可。", file=sys.stderr)
        else:
            print("[client] 阈值在 configs/tiers.yaml 的 limits 段"
                  "（disk_min_free_gb / temp_cutoff_c）。", file=sys.stderr)
        print("[client] 产物已写盘，修好之后直接重跑同一条命令即可续做"
              "（已完成的会自动跳过；在途的几张没写产物，会被重做）。", file=sys.stderr)
        code = 1
    finally:
        if guard is not None:
            guard.stop()
        # sink 必须在这里最后 flush 一次：末片可能不满 rows_per_file，
        # 不 flush 就等于把最后几百张悄悄丢了（而且"没报错"）。
        if sink is not None:
            try:
                sink.close()
            except Exception as exc:      # noqa: BLE001 - 落盘失败要显式报出来
                print(f"[client][写入失败] 最后一片产物没落成: {exc}",
                      file=sys.stderr)
                code = 1
        if work is not None:
            work.close()
        for c in clients:
            try:
                c.close()
            except Exception:             # noqa: BLE001 - 关连接失败不该盖住真正的错误
                pass
        # 多卡包装器自己持有一个线程池。它不在 TaggerClient 里，
        # client.close() 关不到——漏掉的话进程退出时会挂着 N 个空转线程。
        closer = getattr(tag_backend, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:             # noqa: BLE001
                pass
        if cache is not None:
            # describe 要在 close 之前读：关掉连接之后连行数都查不到了。
            cache_info = cache.describe()
            cache.close()

    if provider:
        # 价目表只从存档里读：价格每月都变，内置一份迟早开始说谎，
        # 而算错的成本比不报成本更误导人。没有价目就只报 token。
        stats.cost = nl_api.estimate_cost(
            model_id, stats.prompt_tokens, stats.tokens,
            nl_api.load_saved().get("prices") or {})
    elapsed = time.time() - started
    _log("")
    for line in stats.summary_lines():
        _log("  " + line)
    _log(f"  总耗时     : {elapsed / 60:.1f} 分钟")
    if stats.done and elapsed > 0:
        _log(f"  吞吐       : {stats.done / elapsed:.2f} 张/秒")
    if cache_info:
        _log(f"  缓存       : 命中 {cache_info['hits']} / 未命中 "
             f"{cache_info['misses']}（库内 {cache_info['rows']} 条，"
             f"{cache_info['bytes'] / (1 << 30):.2f} GB）")
    # ---- 分段计时。默认不落盘，只在 --stats-json 时写。
    # 为什么不总是打印：这些数字对调优有用，对日常跑批只是噪音；
    # 而"顺手多打几行"的习惯会让真正的告警被淹没。
    stage_snapshot = stage.snapshot()
    if stage_snapshot:
        print()
        print("  分段计时   :")
        for name in StageTimer.STAGES:
            row = stage_snapshot.get(name)
            if row:
                print(f"    {name:<6} {row['avg_ms']:>8.2f} ms/次  "
                      f"共 {row['count']:>6} 次  累计 {row['total_ms'] / 1000:>7.2f}s")
        print("    （tag 与 http 是并行跑的，各项累计之和会超过墙钟）")

    if args.stats_json:
        stats_path = Path(args.stats_json)
        try:
            stats_path.parent.mkdir(parents=True, exist_ok=True)
            stats_path.write_text(json.dumps({
                "schema": 1,
                "ts": time.time(),
                "elapsed_s": round(elapsed, 2),
                "mode": output.mode,
                "variant": variant_name,
                "concurrency": concurrency,
                "model_id": prov_base.get("model_id"),
                "tag_backend": tag_backend.name if tag_backend else None,
                "tag_precision": tag_backend.precision if tag_backend else None,
                "tag_batch": tag_backend.batch_size if tag_backend else None,
                "tag_graph": bool(getattr(tag_backend, "_graph", None)) if tag_backend else None,
                "tag_preprocess": tag_backend.preprocess if tag_backend else None,
                "counts": {
                    "total": stats.total, "done": stats.done,
                    "failed": stats.failed, "skipped": stats.skipped,
                    "blacklisted": stats.blacklisted,
                    # 无内容图：不计入 failed，也不计入 throughput 的分母。
                    "blank_skipped": stats.blank_skipped,
                },
                "throughput_per_s": round(stats.done / elapsed, 3) if elapsed else 0.0,
                "stages": stage_snapshot,
                "note": "各阶段不互斥（tag 与 http 并行），total_ms 相加会超过墙钟；"
                        "看 avg_ms 与 count 判断该优化哪一段",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"[client] 计时写入 {stats_path}")
        except OSError as exc:
            print(f"[client][警告] 计时文件写不了 {stats_path}: {exc}", file=sys.stderr)

    if stats.failed:
        print()
        print(f"[client] {stats.failed} 张失败，明细在 {failures}")
        print("         失败行不会写产物文件，所以重跑时会被自动重做——"
              "先把原因修掉再跑，别让它们一直循环。")
    if code == 0 and (stats.failed or stats.busy or stats.blacklisted):
        code = 1

    if code == 0:
        print()
        print("[client] 下一步：把整个产物目录交回总机，总机跑 "
              "`tagger merge --roots <目录> --shard-dir <分发包目录>` 对账合并。")
    return code
