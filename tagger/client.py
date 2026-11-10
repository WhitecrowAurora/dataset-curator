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

from . import nl_api
from .artist import ArtistRule, ArtistTable, format_tag, inject_into_caption
from .config import ConfigError, OpsConfig, OutputSpec, parse_caption_chars
from .cpu_tagger import (TagBackend, TagPrediction, TaggerUnavailable, build_backend,
                         resolve_device)
from .distribute import read_shard_manifest
from .guard import ResourceGuard
from .naming import annotation_name, normalize_ext
from .netutil import trust_env_for
from .quality import QualityGate, QualityPolicy
from .result_cache import ResultCache
from .schema import Annotation, QC
from .tag_categories import group_tags, render_groups

# prompt 一改，版本号必须跟着改。provenance 靠它回答"这批数据是哪套指令产的"，
# 不清不楚的话，两个 prompt 混出来的数据集永远查不出差异来自哪。
# 注意：改 caption_chars、tag 数量这类配置，渲染出来的指令也变了，同样算"改了
# prompt"——只改数字不 bump 版本，会出现两批都叫 v2 但指令不同的情况。
# v2：长度与 tag 数量改为按配置生成（v1 是写死的 15-40 tag / 480-1000 字符）。
# v3：颜色/材质从顺序句里提成独立条款（v2 实测颜色词只覆盖 17~28%、材质 57~66%，
#     条款写在长句中间基本没被执行）；删掉 "Longer is fine"（与上限自相矛盾，
#     且 both 模式下贴着 max_tokens 天花板）；长度补一条句数口径（字符数模型数不准）；
#     both 模式补 "nothing else"；caption 段补身份名禁令（签名/水印会从"逐字转录"
#     那条缝里漏进去）。
# v4：按标注规范 v0.2 补三条——
#     ① caption 必须描述整体画风/媒介（v3 的 caption 段一句风格要求都没有，
#        只有 tag 段提过 art style）；
#     ② 引号内的可见文字保持原语言原字形，不翻译不罗马化（v3 只写 "English only"，
#        模型会为了满足那条把中文招牌译成英文）。样式由 _CAPTION_RULES 里新增的
#        两条承担，中英各一份；
#     ③ 新增"参考 tag"段（_ref_block）：源数据带原始 danbooru 标注时把它们作为
#        输入，而不是让模型从图里猜。没有 tag 时一个字都不加。
# v5：参考 tag 段按类别分组（general/character/copyright/artist/meta，取自
#     models/*/selected_tags.csv）。平铺时角色名与外貌词在模型眼里是一类东西，
#     实测会把 `hatsune_miku` 写成 "hatsune miku hair" 这种外貌词。词表缺失时
#     退回 v4 的平铺形态，所以同一份 prompt 版本下仍有两种渲染结果——provenance
#     里记的是版本号，具体形态看参考段有没有 `character:` 这种行首。
PROMPT_VERSION = "v5"

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
- Text visible in the image keeps its own language and script inside the quotes, even though this paragraph is in English: never translate it, never romanise or transliterate it. A quoted Chinese or Japanese sign is correct as-is.
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
- 引号里的文字保持它原本的语言和字形，不要翻译、不要音译或罗马化；正文用简体中文，引文可以不是中文。
- 这一段必须用简体中文写，不要用英文。纯散文：不要 markdown、不要分条、不要小标题、不要"描述："这类前缀。"""

# 图中文字：规范 v0.2 §6 把它定为"最高优先级的语言忠实规则"。
# 这一块只负责**抄**，不负责解释；`text_exact` 与 caption 引号里的字必须逐字一致，
# 所以它排在 caption 之前——自回归模型先写下来的东西，后面才引得住。
_VISIBLE_RULES = """<VISIBLE_TEXT> rules:
- Transcribe every word that is actually legible in the image: signs, subtitles, speech bubbles, UI text, sound effects, watermarks.
- Output one JSON array with one object per text region: [{"text_exact": "...", "language": "ja", "script": "Jpan", "legibility": "clear"}]
- text_exact keeps the original characters, case and punctuation. Never translate, romanise, or correct a typo.
- language is a short BCP-47 code (en, zh, ja, ko, ...); script is ISO 15924 (Latn, Hans, Hant, Jpan, Kore, Arab).
- legibility is clear / partial / illegible. Never guess characters you cannot read: mark the region illegible and transcribe only the part that is certain.
- If the image has no text at all, output exactly []."""
_VISIBLE_RULES_ZH = """<VISIBLE_TEXT> 规则：
- 把画面里真正能辨认的文字逐条抄下来：招牌、字幕、对话气泡、界面文字、拟声字、水印。
- 输出一个 JSON 数组，每段文字一个对象：[{"text_exact": "…", "language": "zh", "script": "Hans", "legibility": "clear"}]
- text_exact 保留原本的字形、大小写与标点，不翻译、不音译、也不替它改错别字。
- language 用 BCP-47 短码（en / zh / ja / ko …），script 用 ISO 15924（Latn / Hans / Hant / Jpan / Kore / Arab）。
- legibility 取 clear / partial / illegible。认不准的字不要猜：标成 illegible，只抄确定的那部分。
- 画面里完全没有文字时，就输出 [] 本身。"""

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
_REF_ZH = """
<参考> 这张图在源数据里的原始 tag，已按类别分组：
{tags}
- 组名按字面理解：`character`、`copyright`、`artist` 属于身份（画的是谁、出自哪部作品、谁画的）；`general` 与 `meta` 才描述画面；`unclassified_tags` 是源数据没给类别的，绝不能把其中任何一条当成画师名。
- 只有 `artist` 组里的 tag、或本提示别处给出的画师 handle 才算画师，不要猜。
- 用它们来确定角色、系列、服装与道具的名称和细节程度。
- 某个 tag 与画面明显矛盾时以画面为准，直接丢掉那一条。
- 把它们融进你自己的句子，不要原样罗列，也不要另外编名字。"""


def _ref_block(tags: Sequence[str], zh: bool = False, *,
               vocab: dict[str, str] | None = None) -> str:
    """把原始 tag 渲染成 prompt 里的一段。空/全空串返回空串。

    分类来自 `tag_categories`（`models/*/selected_tags.csv` 的 category 列）。
    词表里查不到的 tag 归 `unclassified_tags`，不冒充 general。
    `vocab` 显式传入是为了测试能钉住各条分支，不依赖开发机上有哪份词表。
    """
    items = [str(t).strip() for t in tags if str(t).strip()]
    if not items:
        return ""
    # 组名一律写出来（哪怕只剩 unclassified_tags）：规范 §7.1 要的就是
    # "没有分类就注明"，平铺回去等于把未知类别又冒充成了画面描述。
    text = render_groups(group_tags(items, vocab=vocab))
    return (_REF_ZH if zh else _REF_EN).format(tags=text)


def _sentences_for(char_lo: int, char_hi: int) -> tuple[int, int]:
    """字符区间 → 句数区间。下限至少 3 句，上限至少比下限多 1 句。"""
    lo = max(3, round(char_lo / _CHARS_PER_SENTENCE))
    return lo, max(lo + 1, round(char_hi / _CHARS_PER_SENTENCE))


def _prompt_for(output: OutputSpec, *, zh: bool = False) -> str:
    """按配置里的预算生成 prompt。

    ★这里的长度引导与质检硬线刻意不是一个数：
      硬线是 32~256 token（容忍"图简单、话少"，不因此判废），
      引导是 400~1000 字符（≈90~230 token）。
      把引导值也降到硬线，等于告诉模型"写 32 个 token 就行"，
      那一批数据会整体变贫——而贫是没法在质检里被抓到的。
    """
    if output.caption_chars:
        # 显式给了长度引导就用它，不再从 token 闸门派生 —— 两者回答的
        # 是不同问题：闸门管"合格不合格"，引导管"往多长写"。
        char_lo, char_hi = output.caption_chars
    else:
        char_lo = max(400, output.nl_min_tokens * 4)
        char_hi = max(char_lo + 200, min(1200, output.nl_max_tokens * 4))

    if zh:
        # 长度引导按中文口径重算：char 预算原本是"英文字符数"，直接拿去要求
        # 中文，模型会写出 400~1000 个汉字 ≈ 400~1000 token，超过 nl_max_tokens
        # （默认 256）被硬截断，截断的 caption 会被质检判废。
        char_lo = max(40, round(char_lo * _ZH_CHAR_SCALE))
        char_hi = max(char_lo + 60, round(char_hi * _ZH_CHAR_SCALE))
        # 再按 token 闸门夹一次。上面那步只是"换成中文口径"，没有考虑闸门里
        # 还被 tag 段吃掉一截；不夹的话上限仍会顶到 256 之上，整批中文都被打上
        # "NL 过长"扣分（不是判废，但分数系统性低于英文，混批时就是伪特征）。
        cap_hi = max(80, int(output.nl_max_tokens or 0) - _ZH_TAG_RESERVE_TOKENS)
        if char_hi > cap_hi:
            char_lo = max(40, round(char_lo * cap_hi / char_hi))
            char_hi = cap_hi
        cap_rules = _CAPTION_RULES_ZH.format(char_lo=char_lo, char_hi=char_hi)
    else:
        # 句数口径：模型数不准字符，但数得准句子。它只是"roughly"，
        # 验收仍走 token 硬线，所以取整到句就够了。
        sent_lo, sent_hi = _sentences_for(char_lo, char_hi)
        cap_rules = _CAPTION_RULES.format(char_lo=char_lo, char_hi=char_hi,
                                         sent_lo=sent_lo, sent_hi=sent_hi)

    lo, hi = output.tag_min_count, output.tag_max_count
    anchor_lo = min(max(30, lo), hi)
    anchor_hi = min(max(60, anchor_lo), hi)

    tag_rules = _TAG_RULES.format(lo=lo, hi=hi,
                                  anchor_lo=anchor_lo, anchor_hi=anchor_hi)

    mode = output.mode
    # 只有要写 NL 时才要求抄文字：tag-only 是"只要词表"的活法，多一个块
    # 只是白烧 token。
    want_visible = bool(output.visible_text) and output.wants_nl
    vis_rules = ((_VISIBLE_RULES_ZH if zh else _VISIBLE_RULES)
                 if want_visible else "")
    vis_block = ("<VISIBLE_TEXT>\n"
                 '[{"text_exact": "欢迎光临", "language": "zh", "script": "Hans", '
                 '"legibility": "clear"}]\n</VISIBLE_TEXT>\n'
                 if want_visible else "")
    if mode == "tag":
        return ("Produce one block about the image, in exactly this format:\n\n"
                "<TAGS>\ntag_one, tag_two, tag_three\n</TAGS>\n\n"
                f"{tag_rules}\n\nOutput the TAGS block and nothing else.")
    if mode == "nl" and not want_visible:
        return ("Produce one block about the image, in exactly this format:\n\n"
                "<CAPTION>\none paragraph of description\n</CAPTION>\n\n"
                f"{cap_rules}\n\nOutput the CAPTION block and nothing else.")
    if mode == "nl":
        return ("Produce two blocks about the image, in exactly this format:\n\n"
                f"{vis_block}"
                "<CAPTION>\none paragraph of description\n</CAPTION>\n\n"
                f"{vis_rules}\n\n{cap_rules}\n\n"
                "Output these two blocks, in this order, and nothing else.")
    # both 模式原来不写"nothing else"：模型加一句 "Here are the tags:" 不致命
    # （正则能抓），但 max_tokens 是按「NL 全量 + tag 全量 + 标记」抠出来的，
    # 那段前言是从 caption 的预算里偷的。
    if not want_visible:
        return ("Produce two blocks about the image, in exactly this format:\n\n"
                "<TAGS>\ntag_one, tag_two, tag_three\n</TAGS>\n"
                "<CAPTION>\none paragraph of description\n</CAPTION>\n\n"
                f"{tag_rules}\n\n{cap_rules}\n\n"
                "Output these two blocks, in this order, and nothing else.")
    return ("Produce three blocks about the image, in exactly this format:\n\n"
            "<TAGS>\ntag_one, tag_two, tag_three\n</TAGS>\n"
            f"{vis_block}"
            "<CAPTION>\none paragraph of description\n</CAPTION>\n\n"
            f"{tag_rules}\n\n{vis_rules}\n\n{cap_rules}\n\n"
            "Output these three blocks, in this order, and nothing else.")


# 三道正则都写成「闭合标签可有可无」：模型偶尔只写开标记就往下走，
# 那种输出仍然有值，硬要求闭标记会把它整条判废。
# 但"没有闭标记"不等于"一直到文末"：如果后面紧跟着另一段的开标记，
# 必须在那儿截断，否则 `<CAPTION>` 的正文会被当成 tag 洗进词表
# （实测会产出 captiona_woman_with 这种鬼东西）。★加新块时**每一道**的
# lookahead 都要补上，漏一道就等于让新块的正文流进上一个块。
_TAG_BLOCK = re.compile(
    r"<TAGS>(.*?)(?:</TAGS>|(?=<CAPTION>)|(?=<VISIBLE_TEXT>)|\Z)", re.S | re.I)
_CAPTION_BLOCK = re.compile(
    r"<CAPTION>(.*?)(?:</CAPTION>|(?=<TAGS>)|(?=<VISIBLE_TEXT>)|\Z)", re.S | re.I)
_VISIBLE_BLOCK = re.compile(
    r"<VISIBLE_TEXT>(.*?)(?:</VISIBLE_TEXT>|(?=<TAGS>)|(?=<CAPTION>)|\Z)",
    re.S | re.I)

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


_LEGIBILITY = ("clear", "partial", "illegible")


def _visible_one(item: object) -> dict | None:
    """一条可见文字 → 规范 §6 的结构。拿不到 text_exact 就丢掉这条。"""
    if isinstance(item, str):
        item = {"text_exact": item}
    if not isinstance(item, dict):
        return None
    exact = str(item.get("text_exact") or item.get("text") or "").strip()
    if not exact:
        return None
    rec: dict = {"text_exact": exact}
    lang = str(item.get("language") or "").strip().lower()
    if lang:
        # 只留语言码本体：模型常写成 "ja (Japanese)" 这种带注解的形式。
        rec["language"] = re.split(r"[\s(（,;]+", lang)[0]
    script = str(item.get("script") or "").strip()
    if script:
        rec["script"] = re.split(r"[\s(（,;]+", script)[0][:4].title()
    leg = str(item.get("legibility") or item.get("text_readable") or "").strip().lower()
    if leg in ("true", "readable", "yes"):
        leg = "clear"
    rec["legibility"] = leg if leg in _LEGIBILITY else "unknown"
    bbox = item.get("bbox") or item.get("bbox_optional")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        try:
            rec["bbox"] = [round(float(v), 4) for v in bbox]
        except (TypeError, ValueError):
            pass
    return rec


def normalize_visible(raw: str) -> list[dict]:
    """把 `<VISIBLE_TEXT>` 段洗成 `[{text_exact, language, script, legibility, bbox?}]`。

    ★解析是**降级**的，不是二元的：模型给 JSON 就用 JSON；给了逐行原文（或
    JSON 被 max_tokens 截断）就按行收，每条只带 `text_exact` + `unknown`。
    宁可少几个字段，也不该把已经抄对的原文整段丢掉——原文正是这一块最贵的
    信息，丢了这一段就白花 token 了。
    """
    text = (raw or "").strip()
    if not text:
        return []
    # 模型偶尔包 ```json 围栏；块标记里出现围栏是它的习惯，不是我们要的数据。
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text, flags=re.I | re.M).strip()
    items: object = None
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        try:
            items = json.loads(text[start:end + 1])
        except ValueError:
            items = None
    if not isinstance(items, list):
        # 截断/纯文本兜底。先按 `"text_exact": "..."` 抠——JSON 被 max_tokens
        # 切断时，前面已经写完的条目仍能一条条捞回来（按行切会把 `[`、`{`
        # 这些结构行也当成"文字"）。
        pairs = re.findall(r'"text_exact"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if pairs:
            items = []
            for raw in pairs:
                try:
                    items.append({"text_exact": json.loads(f'"{raw}"')})
                except ValueError:
                    items.append({"text_exact": raw})
        else:
            # 连键名都没有：一行一条纯文本。空行与纯结构行都不算。
            items = [re.sub(r"^\s*[-*]\s*", "", ln) for ln in text.splitlines()
                     if ln.strip() and ln.strip(" \t[]{},:")]
    out: list[dict] = []
    for item in items:
        rec = _visible_one(item)
        if rec is not None:
            out.append(rec)
    return out


def _estimate_visible_tokens(visible: Sequence[dict]) -> int:
    """估算 `<VISIBLE_TEXT>` 段吃掉了多少 completion token。

    ★为什么要扣：`nl_tokens` 是**整段回复**的 usage，而 `nl_max_tokens`
    这条闸门是给 caption 定的。不扣掉这一块，每张带字的图都会顶破 256，
    质检判 "NL 过长"（completeness 2.5）→ 反复重试 → 真正的原因是"多了个
    必填字段"，不是模型话多。这批数据会静默地整体降分。

    粗估口径：每条 20 token 的固定结构开销（键名 + 括号 + 逗号），正文按
    中日韩一字一 token、其余四字符一 token。
    """
    if not visible:
        return 0
    cjk = other = 0
    for rec in visible:
        for ch in str(rec.get("text_exact") or ""):
            o = ord(ch)
            if (0x3040 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF
                    or 0xAC00 <= o <= 0xD7AF or 0x3400 <= o <= 0x4DBF):
                cjk += 1
            else:
                other += 1
    return 20 * len(visible) + cjk + other // 4


def parse_output_full(text: str, mode: str) -> tuple[list[str], str, list[dict]]:
    """从模型输出里取出 (tags, caption, visible_text)。

    没有块标记时按模式兜底：tag/nl 单模式把整段当结果；both 模式直接判空——
    在两个块的输出里靠猜切分，等于批量制造脏数据，宁可让它进重试。
    """
    body = (text or "").strip()
    if not body:
        return [], "", []

    have_marker = bool(_TAG_BLOCK.search(body) or _CAPTION_BLOCK.search(body)
                       or _VISIBLE_BLOCK.search(body))
    if not have_marker:
        if mode == "tag":
            return normalize_tags(body), "", []
        if mode == "nl":
            return [], normalize_caption(body), []
        return [], "", []

    tags: list[str] = []
    caption = ""
    visible: list[dict] = []
    if mode in ("tag", "both"):
        m = _TAG_BLOCK.search(body)
        tags = normalize_tags(m.group(1)) if m else []
    if mode in ("nl", "both"):
        m = _CAPTION_BLOCK.search(body)
        caption = normalize_caption(m.group(1)) if m else ""
        m = _VISIBLE_BLOCK.search(body)
        visible = normalize_visible(m.group(1)) if m else []
    return tags, caption, visible


def parse_output(text: str, mode: str) -> tuple[list[str], str]:
    """(tags, caption) 二元组。保留给老调用方/测试；新代码用 parse_output_full。"""
    tags, caption, _ = parse_output_full(text, mode)
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


# 进度回调。kind ∈ {"done", "failed", "skipped", "blacklisted"}：
#   done / failed 带 outcome（failed 里含写盘失败，那种同样没有产物）；
#   skipped / blacklisted 没有 outcome——这两张根本没去推理，编一个出来只会骗人。
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

    def summary_lines(self) -> list[str]:
        lines = [
            f"总共       : {self.total}",
            f"完成       : {self.done}",
            f"其中缓存   : {self.cached}",
            f"跳过（已有）: {self.skipped}",
            f"跳过（拉黑）: {self.blacklisted}",
            f"失败       : {self.failed}",
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
        self.max_attempts = max_attempts
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
        # system 那句话同理：文件里写了就用文件的，否则用内置那份。
        self.system = system_text or SYSTEM_PROMPT
        # 中文变体：只在真要产 NL、且没被整段替换时才生成。
        # 默认 0 = 全英文，历史行为不变；要多少由上层（JobSpec）传下来。
        self.caption_zh_ratio = max(0.0, min(1.0, float(caption_zh_ratio or 0.0)))
        if prompt_zh_text:
            # 文件自带中文版：比例 > 0 才用；没写就不出中文——不能拿内置中文版
            # 去配自定义英文版，两边要求不同，出来的数据没法一起用。
            self.prompt_zh = (prompt_zh_text
                              if self.caption_zh_ratio > 0 and output.wants_nl
                              else "")
        else:
            self.prompt_zh = (
                _prompt_for(output, zh=True)
                if self.caption_zh_ratio > 0 and not prompt_text and output.wants_nl
                else ""
            )
        self.variant = variant
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
            "prompt": hashlib.sha256(self.prompt.encode("utf-8")).hexdigest(),
            # 自定义提示词可以连 system 一起换：同一条 prompt 配不同 system
            # 是两套指令，不进指纹就会跨套命中缓存。
            "system": hashlib.sha256(self.system.encode("utf-8")).hexdigest(),
            # 中文变体与比例必须进指纹：漏了它，先按全英文跑过一批、再把比例
            # 调到 30% 重跑，被抽中的那批会命中英文缓存，拿回英文结果。
            "prompt_zh": (hashlib.sha256(self.prompt_zh.encode("utf-8")).hexdigest()
                          if self.prompt_zh else ""),
            "zh_ratio": round(self.caption_zh_ratio, 4),
            "mode": self.output.mode,
            # 开了 visible_text 的回复比没开的多一整块：同一张图不能跨这两档
            # 命中缓存，否则会拿回一条没有 visible_text 的旧产物。
            "visible_text": bool(self.output.visible_text),
            "nl_budget": [self.output.nl_min_tokens, self.output.nl_max_tokens],
            "tag_count": [self.output.tag_min_count, self.output.tag_max_count],
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
            "tag_backend": ([self.tag_backend.name,
                             str(getattr(self.tag_backend, "precision", ""))]
                            if self.tag_backend is not None else None),
            "gate": self._policy_fingerprint(),
        }
        raw = json.dumps(parts, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _cache_key(self, data: bytes) -> str:
        return f"{self.cache_prefix}:{hashlib.sha256(data).hexdigest()}"

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
        tag_payload = None
        nl_payload = None
        if want_tag and tag_rec is not None:
            tags = [(str(t), float(p)) for t, p in tag_rec.get("tags") or []]
            tag_payload = self._tag_payload(
                task, tags, temperature, tokens, attempts,
                rating=tag_rec.get("rating") or None,
                escalated=escalated, cache_hit=True,
                prompt_tokens=prompt_tokens)
        if want_nl and nl_rec is not None:
            verdict = SimpleNamespace(score=float(nl_rec.get("score") or 0.0),
                                      reasons=list(nl_rec.get("reasons") or []))
            nl_payload = self._nl_payload(
                task, str(nl_rec.get("caption") or ""),
                int(nl_rec.get("tag_count") or 0), temperature, tokens,
                attempts, verdict, escalated=escalated, cache_hit=True,
                prompt_tokens=prompt_tokens,
                visible=nl_rec.get("visible_text") or ())
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
        return base + _ref_block(task.ref_tags, zh=zh)

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
            "annotation_version": f"{rev or 'unknown'}+p{PROMPT_VERSION}",
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
                top_p=self.top_p, max_tokens=self.max_tokens, seed=self.seed)
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
        base["prompt_ver"] = PROMPT_VERSION
        # prompt 指纹。AB 变体之间常常只差一句话，prompt_ver 分不出来；
        # 事后想回答"这批的 12 个 tag 是哪套指令逼出来的"，只能靠它。
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
        if self.cache is not None and data is not None:
            cache_key = self._cache_key(data)
            record = self.cache.get(cache_key)
            if record is not None:
                hit = self._cached_outcome(task, record, started)
                if hit is not None:
                    return hit

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
                             cache_key=cache_key)
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
             cache_key: str | None = None) -> Outcome:
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
        task_prompt = self._prompt_for_task(task)

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
                tags, caption, visible = parse_output_full(text, self.output.mode)

            # 显式空产出检查必须放在质检之前：质量门在 tags 为空时
            # tag_coverage 返回 1.0（"没有强 tag，视为全覆盖"），
            # 于是"模型一个 tag 都没给"会被判成合格。这个洞只能在这里补。
            reasons = []
            if want_tag and not tags:
                reasons.append("没有产出 tag")
            if want_nl and not caption:
                reasons.append("没有产出 caption")

            # tag 数量也是 prompt 里的硬约定，但质量门不管它（那是文本质量维度）。
            # 多给的按重要性顺序截断——prompt 要求降序输出，截掉的正好最不重要，
            # 比整条判废合理；少给的只能重试，那是模型敷衍。
            if want_tag and tags:
                if len(tags) < self.output.tag_min_count:
                    reasons.append(
                        f"tag 过少（{len(tags)} < {self.output.tag_min_count}）")
                elif len(tags) > self.output.tag_max_count:
                    tags = tags[: self.output.tag_max_count]

            if reasons:
                if attempts > self.max_attempts:
                    break
                continue

            head = tags[:_QC_TAG_HEAD]
            ann = Annotation(
                image_sha256="",          # 客户端不算哈希，见模块头说明
                image_path=task.file,
                tags=[(t, 1.0) for t in head],
                caption_dense=caption,
                tag_line=", ".join(head),
                # nl_tokens 扣掉可见文字块：闸门是给 caption 定的，见
                # _estimate_visible_tokens 里的说明。
                qc=QC(caption_chars=len(caption),
                      nl_tokens=max(0, tokens - _estimate_visible_tokens(visible))),
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
                verdict = self.gate.evaluate(
                    ann, attempt=attempts - 1, temperature=temperature,
                    max_pixels=budget, image=qc_image,
                )
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
                    # 分类头是确定性的：同一张图再算一次结果不变，
                    # 数量不够就重试是纯浪费。直接判失败，让人去看阈值。
                    if len(tags_with_prob) < self.output.tag_min_count:
                        return Outcome(
                            task.image_id, False,
                            error=(f"tag 过少（{len(tags_with_prob)} < "
                                   f"{self.output.tag_min_count}），"
                                   f"后端 {self.tag_backend.name if self.tag_backend else '?'}"
                                   f" 的阈值偏低"),
                            wall_ms=int((time.perf_counter() - started) * 1000),
                            attempts=attempts, tokens=tokens_total,
                        )
                    tags_with_prob = tags_with_prob[: self.output.tag_max_count]

                # 只写本模式该产的文件：tag 模式下多写一个 -nl.json，
                # 会让 merge 在 require=tag 时多找一条"本不该存在"的记录，
                # 更糟的是那份 caption 没经过任何按模式裁剪的约束。
                # 先进缓存再返回：存的是"模型产物"（tag+概率、rating、caption、
                # 质检分），**不存 provenance/ts/画师注入**——那三样在命中时
                # 必须按本次运行重新生成，否则产物会指鹿为马。
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
                                            for k, v in (rating or {}).items()}}
                                if self.need_tags else None),
                        "nl": ({"caption": caption,
                                "visible_text": visible,
                                "score": float(verdict.score),
                                "reasons": list(verdict.reasons),
                                "tag_count": len(tags_with_prob)}
                               if want_nl else None),
                    })
                return Outcome(
                    task.image_id, True,
                    tag=(self._tag_payload(task, tags_with_prob, temperature,
                                           tokens, attempts, rating=rating,
                                           escalated=escalated,
                                           prompt_tokens=prompt_total)
                         if self.need_tags else None),
                    nl=(self._nl_payload(task, caption, len(tags_with_prob),
                                         temperature, tokens, attempts, verdict,
                                         escalated=escalated,
                                         prompt_tokens=prompt_total,
                                         visible=visible)
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

        return Outcome(
            task.image_id, False,
            error="; ".join(reasons)[:400],
            wall_ms=int((time.perf_counter() - started) * 1000),
            attempts=attempts, tokens=tokens_total,
            prompt_tokens=prompt_total, rate_limited=rate_limited, fatal=fatal,
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
                     prompt_tokens: int = 0) -> dict:
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
        return payload

    def _nl_payload(self, task: TaskRow, caption: str, tag_count: int,
                    temperature: float, tokens: int, attempts: int, verdict,
                    *, escalated: bool = False, cache_hit: bool = False,
                    prompt_tokens: int = 0,
                    visible: Sequence[dict] | None = None) -> dict:
        artist = self._artist_fields(task)
        artist_tag = artist["artist_tag"]
        text = caption
        if artist_tag:
            # 画师标签只进 caption 与 json 字段，不并进 tag 串：
            # `@name` 不是 danbooru 词汇，混进 tag 会把词表撕出一个洞。
            text = inject_into_caption(text, artist_tag, self.artist_rule)
        payload = {
            "image_id": task.image_id,
            "caption": text,
            **artist,
            "tag_count": tag_count,
            # 规范 v0.2 §8：`visible_text` 是必填字段，没有文字就是空数组。
            # 空数组照写——"这张图没有字"和"这一版没跑这一项"是两回事，
            # 缺字段会让下游两种都分不出来。
            "visible_text": list(visible or ()),
            "nl_tokens": tokens,
            # 见 _tag_payload：本机模式不写，API 模式才有依据核算成本。
            "prompt_tokens": prompt_tokens or None,
            "chars": len(text),
            "score": verdict.score,
            "flags": list(verdict.reasons),
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


def _already_done(client: TaggerClient, task: TaskRow,
                  out_dir: Path, layout: str) -> bool:
    """已有有效产物就跳过图片；换模型或产出模式须强制重做才重算。"""
    tag_path, nl_path = _artifact_paths(out_dir, task, layout)
    return (_existing_ok(tag_path, "tag") or _existing_ok(nl_path, "caption"))


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
    `_already_done`）。stop 优先于 resume：暂停中收到停止请求会当场抛
    `Stopped`，不会把主循环卡在等恢复上。
    """
    # 注意：这里不再 len(tasks)。parquet 模式的任务是流式生成的，
    # 长度事先根本不知道（要先读完分片才能数清），所以 total 在迭代时累加。
    if done_ids is None:
        done_ids = set()

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
            line = json.dumps({
                "image_id": task.image_id, "file": task.file,
                "shard": Path(task.shard).name, "error": outcome.error,
                "attempts": outcome.attempts, "ts": time.time(),
            }, ensure_ascii=False)
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
            if sink is not None:
                if not force and task.image_id in done_ids:
                    stats.skipped += 1
                    if verbose:
                        print(f"  [跳过] {task.image_id}（产物里已有）", flush=True)
                    emit("skipped", task, None)
                    continue
            elif not force and _already_done(client, task, out_dir, layout):
                stats.skipped += 1
                if verbose:
                    print(f"  [跳过] {task.image_id}（产物已存在）", flush=True)
                emit("skipped", task, None)
                continue
            if work is not None:
                status = work.claim(task.image_id, force=force)
                if status != "claimed":
                    if status == "done":
                        stats.skipped += 1
                        emit("skipped", task, None)
                    else:
                        stats.busy += 1
                        emit("busy", task, None)
                    continue
                # 另一个进程可能在初次检查之后、领取之前刚刚完成逐图文件。
                if sink is None and not force and _already_done(client, task, out_dir, layout):
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
               on_log=None, guard: "ResourceGuard | None" = None) -> int:
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
                        visible_text=output.visible_text)

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
    # 设备：auto 走探测（有卡就 cuda:0），UI 上做成下拉框即可。
    tag_device = resolve_device(args.tag_device or tagging.get("device"))
    # 精度默认 tf32：79 张真实素材上实测 0 个 tag 翻面、概率最大漂移 0.0027，
    # 前向 1.33×。要逐位复现老口径就显式 --tag-precision fp32。
    tag_precision = str(getattr(args, "tag_precision", None)
                        or tagging.get("precision") or "tf32")
    tag_batch = int(getattr(args, "tag_batch", None) or tagging.get("batch") or 1)
    # 预处理：pil = 现状（CPU 缩放/归一化）；tensor = 缩放/归一化上卡。
    # 解码始终在 CPU——nvJPEG 逐张比 PIL 还慢，只有批量才赢，而这里是逐张喂。
    tag_preprocess = str(getattr(args, "tag_preprocess", None)
                         or tagging.get("preprocess") or "pil")
    tag_backend = build_backend(
        backend_name,
        model=model_for_backend,
        model_dir=args.tag_model_dir,
        device=tag_device,
        keep=tagging.get("keep_categories"),
        thresholds={str(k): float(v) for k, v in (tagging.get("thresholds") or {}).items()},
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

        # 实测（RTX 4090 主机 + 消费级 CPU）：pixai 1008²/486M 在 CPU 上
        # 约 16 s/张，wd-eva02 448²/eva02-large 约 2.3 s/张。而 GPU 上的
        # VLM 通常 1~3 s/张——用 pixai 跑 CPU，整条流水线会被它拖慢 5~8 倍，
        # "双线并行"退化成"CPU 串行 + GPU 空转"。
        if tag_backend.name == "pixai" and tag_device == "cpu":
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
                            visible_text=output.visible_text)

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
    # <VISIBLE_TEXT> 是 NL 之外的独立一段。不预留就会从 caption 的预算里偷：
    # 表现是「有字的图 caption 被截断」——而那恰好是文字类样本，损失定向且隐蔽。
    # 160 ≈ 5 条文字 × 20 token 结构开销 + 正文余量。
    if output.visible_text and output.wants_nl:
        need_tokens += 160
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
    # 跨运行累计的失败账。同一张图失败到 max_failures 次就不再碰它，
    # 否则每次重跑都要为同一批坏样本重烧 3 次推理。
    max_failures = int(getattr(args, "max_failures", 3) or 0)
    fail_counts = load_failure_counts(failures)
    if fail_counts:
        black = sum(1 for n in fail_counts.values() if max_failures and n >= max_failures)
        _log(f"[client] 历史失败 : {len(fail_counts)} 个 id，"
             f"其中 {black} 个已达上限（本次跳过）")

    # 分片级完成标记：HF 模式下重跑时整片跳过。
    # 存在单独的 .hf-done 目录而不是产物目录根下，免得被 merge 的扫描扫到。
    done_dir = out_dir / ".hf-done"

    def shard_done(name: str) -> bool:
        # 旧版在中止或 Parquet 未落盘时也会写标记，需逐图核验一次。
        try:
            marker = (done_dir / f"{Path(name).name}.done").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        return marker.startswith(f"complete-v2\t{name}\t")

    def mark_shard_done(name: str) -> None:
        # 分片 sink 会攒行；必须先成功封盘，再承诺下次可整片跳过。
        if sink is not None:
            sink.flush()
        done_dir.mkdir(parents=True, exist_ok=True)
        from .storage.atomic import write_bytes
        write_bytes(done_dir / f"{Path(name).name}.done",
                    f"complete-v2\t{name}\t{time.time():.0f}\n".encode("utf-8"))

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
            )
            if remaining:
                remaining = max(0, remaining - counter["n"])
            # 只有"整片跑完且零失败"才标记。被 --limit 截断的片不标，
            # 否则片尾没跑的样本下次被整片跳过，永远静默丢失。
            complete = (stats.failed == before_failed
                        and stats.blacklisted == before_blacklisted
                        and stats.busy == before_busy
                        and not truncated["v"])
            if shard_mark and complete:
                mark_shard_done(name)
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
                    try:
                        _, truncated = run_group([shard.path], hf_repo_name=hf_repo)
                    finally:
                        # 处理完立刻删：这是"只占一个分片空间"的前提，
                        # 也是预取能继续提交下一片的前提（空间刚被释放）。
                        hf_source.cleanup(shard, keep=keep)
                    # 只有整片跑完且零失败才标记。有失败别标（坏样本不能被
                    # 藏起来）；被 --limit 截断也别标（片尾还没跑）。
                    if (stats.failed == before_failed
                            and stats.blacklisted == before_blacklisted
                            and stats.busy == before_busy and not truncated):
                        mark_shard_done(name)
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
    if code == 0 and (stats.failed or stats.busy):
        code = 1

    if code == 0:
        print()
        print("[client] 下一步：把整个产物目录交回总机，总机跑 "
              "`tagger merge --roots <目录> --shard-dir <分发包目录>` 对账合并。")
    return code
