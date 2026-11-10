"""参考 tag 的类别表（general / character / copyright / artist / meta）。

源数据里的原始 danbooru tag 是一锅混的：角色名、作品名、画师 handle 与画面
描述挤在同一行。平铺喂给 VLM 有两个已知后果——`hatsune_miku` 会被当成
"一个长发少女"的同义词，画师 handle 会被写进画面描述。分成组之后 prompt
里才能明说"character/copyright 是身份、artist 是画师、其余才是画面"。

数据源只有一个：`models/<tagger>/selected_tags.csv` 的 category 列
（wd-eva02 那份 16473 行，跟着权重一起下）。找不到就退回平铺——
拿不到分类是常态（tar 分片根本没有 tag 列），不该因此打不了标。
"""

from __future__ import annotations

import csv
import os
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

# WD 词表的 category 编号。与 cpu_tagger.WD_CATEGORY 同源，这里不 import 它：
# 那个模块顶层拖着 torch/timm 的导入链，而组装 prompt 不该为此付一次 import。
WD_CATEGORY = {0: "general", 1: "artist", 3: "copyright", 4: "character",
               5: "meta", 9: "rating"}

# 渲染顺序：先说"画的是谁/出自哪"（模型据此定细节），再说画师，最后是画面。
# rating 不在其中——见 group_tags。
# unclassified 必须单列：规范 §7.1 明确"来源没有分类就注明 unclassified_tags，
# 不可把它们任意冒充 artist"。早先的实现把词表外的 tag 一律归 general，
# 那等于告诉模型"这是画面描述"——`vocaloid` 这种作品名就会被写成场景词。
GROUP_ORDER = ("character", "copyright", "artist", "general", "meta", "unclassified")

# 默认找这一份。后端可以换成 pixai/cl-tagger-v2，但那两个的词表没落盘，
# 所以才要在 find_vocab 里扫 models/*/ 而不只是写死一个目录。
DEFAULT_VOCAB = Path("models/wd-eva02/selected_tags.csv")

# 环境变量覆盖：模型目录整体被挪走（外置盘、打包版）时用它指路。
VOCAB_ENV = "TAGGER_TAG_VOCAB"


def find_vocab() -> Path | None:
    """能找到的唯一一份分类词表，找不到返回 None。

    顺序：环境变量 → 默认 wd-eva02 → models/*/selected_tags.csv 的第一个。
    扫描排序是刻意的：同一台机器上多份词表时，结果必须可复现，
    不能由文件系统枚举顺序决定（那会让两台机器拼出的 prompt 不一样）。
    """
    env = os.environ.get(VOCAB_ENV, "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    if DEFAULT_VOCAB.is_file():
        return DEFAULT_VOCAB
    root = Path("models")
    if root.is_dir():
        for cand in sorted(root.glob("*/selected_tags.csv")):
            if cand.is_file():
                return cand
    return None


@lru_cache(maxsize=8)
def _read_vocab(path: str, mtime_ns: int) -> dict[str, str]:
    """csv → {tag 名: 类别}。mtime 参与缓存键：换了模型不用重启进程。"""
    table: dict[str, str] = {}
    try:
        with Path(path).open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                try:
                    code = int(row.get("category") or 0)
                except (TypeError, ValueError):
                    code = 0
                table[name] = WD_CATEGORY.get(code, "general")
    except OSError:
        # 读不了就当没有词表：平铺渲染，打标照常。
        return {}
    return table


def load_vocab(path: Path | str | None = None) -> dict[str, str]:
    """读分类词表。path 为 None 时用 find_vocab()。失败一律返回空表。"""
    p = Path(path) if path is not None else find_vocab()
    if p is None or not p.is_file():
        return {}
    try:
        stat = p.stat()
    except OSError:
        return {}
    return _read_vocab(str(p), stat.st_mtime_ns)


def category_of(name: str, vocab: dict[str, str] | None = None) -> str:
    """一个 tag 的类别。词表里没有的算 `unclassified`。

    ★宁可说"不知道"也不猜：把作品名/画师名错归成 general，等于告诉模型
    "这是画面描述"，它会照着写进 caption。规范 §7.1 也是这个口径。
    """
    table = vocab if vocab is not None else load_vocab()
    return table.get(name.strip(), "unclassified")


def group_tags(tags: Iterable[str], *,
               vocab: dict[str, str] | None = None) -> list[tuple[str, list[str]]]:
    """按类别分组，保持组内原有顺序。空组不返回。

    rating 类（general/sensitive/explicit 这几个词本身）直接丢掉：它们是
    danbooru 的审核等级，不是画面事实。喂给模型只会换来一段 NSFW 措辞，
    而词表里这四个词在 selected_tags.csv 里恰好也叫 general / sensitive /
    questionable / explicit，混进 general 组特别隐蔽。
    """
    table = vocab if vocab is not None else load_vocab()
    buckets: dict[str, list[str]] = {}
    for raw in tags:
        name = (raw or "").strip()
        if not name:
            continue
        cat = table.get(name, "unclassified")
        if cat == "rating":
            continue
        buckets.setdefault(cat, []).append(name)
    return [(cat, buckets[cat]) for cat in GROUP_ORDER if buckets.get(cat)]


def render_groups(groups: Sequence[tuple[str, Sequence[str]]]) -> str:
    """分组成 "xxx_tags: tag tag" 的多行文本。空输入返回空串。

    字段名带 `_tags` 后缀是规范 §7.1 的写法（`general_tags` /
    `character_tags` / …），prompt 里直接用它，模型看到的词与文档一致。
    """
    return "\n".join(f"{cat}_tags: {' '.join(tags)}"
                     for cat, tags in groups if tags)
