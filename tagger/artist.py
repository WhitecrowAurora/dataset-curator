"""画师标签：从素材目录结构提取作者名，并注入标注。

为什么要有这一层
----------------
素材的第一层目录名就是画师名，这是人工整理时留下的现成信息，比让模型猜
可靠得多。所以这里只做一件事：把这个事实**无损**地搬到标注里。

三个必须坚持的原则：

1. **画师名是标识符，不是描述文本。**
   它的用途是让训练侧能把同一作者的图归到一起、让模型学到风格绑定。
   它必须全局唯一且跨批次一致。既然是标识符，就不能"翻译得通顺"—— 
   "水月"翻成 Shuiyue / Water Moon / Suiyue 都算对，但不一致就等于废掉。

2. **不让模型生成这个字段。**
   哪怕把画师名写进 prompt，模型也可能改写、简写或干脆拼错。一旦某批次的
   标签漂了，整批数据都要重打，而这个字段本来是可以 100% 准确的。
   模型最多被告知"这张图的作者是 X"用于影响描述，标签本身由系统拼接。

3. **提取失败时要显式留空，不要兜底成文件夹以外的猜测。**
   根目录散图、多层嵌套、路径异常，都可能拿不到作者。这时宁可为空，
   也不要拿文件名或上级目录去凑——错的标签比没有标签伤害大得多。

关于非英文名
------------
默认**保留原拼写**。需要英文时走别名表（artist_aliases.json），由人确认一次
后长期复用；这条路径可审计、可 diff、可回滚。自动音译（pypinyin 之类）会引入
隐性不一致，而且同一份数据里混用两种转写方式比全用原文更难排查。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# 标签里不能出现的字符：逗号会和 tag 列表分隔冲突，换行会破坏 jsonl 行边界，
# @ 前缀重复会导致 "@@@name" 这种下游解析歧义。
_UNSAFE = re.compile(r"[,;\n\r\t@#]+")
_SPACES = re.compile(r"\s+")

# 视为"非英文"的判据：只要出现非 ASCII 字母，就认为需要外部映射才能英文化。
_NON_ASCII = re.compile(r"[^\x00-\x7F]")


@dataclass(slots=True)
class ArtistRule:
    """画师标签的启用规则。全部来自配置，代码里不留魔法值。"""

    enabled: bool = False
    # 从路径的第几层取（0 = 第一层目录）
    depth: int = 0
    # 注入前缀。扩散模型社区惯例是 @
    prefix: str = "@"
    # 写进 caption 开头，还是只进 JSON 字段
    inject_caption: bool = True
    # 翻译表路径，为空则不查、也不自动累积
    aliases_path: str | None = None
    # 允许出现在标签里的最大长度，超了视为异常路径而不是作者名
    max_len: int = 64
    # 名字里有空格时替换成下划线。
    # 原因：caption 是按空格分词的，`@sakimi chan` 会被切成 `@sakimi` 和 `chan`
    # 两个 token，画师绑定就散了一半。合并成一个词才能整体激活。
    # 没有空格的名字原样保留，不做任何改写。
    space_to_underscore: bool = True

    @classmethod
    def from_dict(cls, d: dict | None) -> "ArtistRule":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            depth=int(d.get("depth", 0)),
            prefix=str(d.get("prefix", "@")),
            inject_caption=bool(d.get("inject_caption", True)),
            aliases_path=d.get("aliases_path") or None,
            max_len=int(d.get("max_len", 64)),
            space_to_underscore=bool(d.get("space_to_underscore", True)),
        )


@dataclass(slots=True)
class ArtistTag:
    """一次提取的结果。raw 是原始拼写，tag 是最终注入用的形式。"""

    raw: str = ""
    tag: str = ""
    # 是否来自别名表。用于统计"有多少名字还没有英文映射"
    mapped: bool = False
    # 拿不到作者时的原因，便于批量排查而不是静默丢
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.tag)


def _clean(name: str, max_len: int, *, space_to_underscore: bool = True) -> str:
    """清洗成可以当标签用的字符串。

    规则很克制：只做「必须做」的转换，其余一律原样保留。
    - 连续空白压成一个空格
    - 去掉会破坏 tag 语法的字符（逗号/换行/@/#）
    - 有空格才换成下划线（caption 按空格分词，带空格的 tag 会被拆开）
    - 没有空格的名字一个字都不改 —— 忠实还原是默认行为
    """
    name = _SPACES.sub(" ", name.strip())
    name = _UNSAFE.sub("", name).strip()
    # 去掉空名字与 "."、".." 这类路径伪目录
    if not name or name in (".", ".."):
        return ""
    if space_to_underscore and " " in name:
        name = name.replace(" ", "_")
    return name[:max_len].strip()


def load_aliases(path: str | Path | None) -> dict[str, str]:
    """加载别名表：{原始名: 英文名}。

    文件不存在或格式不对时返回空表而不是报错——别名表是可选增强，
    缺了它画师标签本身仍然可用，不该因此让整条流水线起不来。
    """
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v).strip()}


class AliasTable:
    """画师名 → 英文名 的本地翻译表，持久化、可累积。

    为什么要做成能"累积"的：
    素材是一批批来的，画师名也是一个个冒出来的。如果每次都要人工重新整理
    一份表，很快就没人维护了，最后退化成"随便翻翻"。所以设计成：
    遇到新名字先写进表（值为空），人只要把空格填上；下次再遇到同一个名字，
    直接命中，不需要再判断一次。

    空值有明确语义：**已登记但尚未翻译**。统计口径靠它区分
    "从没见过的名字"和"见过但还没翻的名字"，两种的处理优先级完全不同。
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._map: dict[str, str] = {}
        self._dirty = False
        # 读取失败必须记账：文件坏了还照常 save()，等于把人手工翻译好的
        # 别名表用一张空表覆盖掉——那是最贵的静默丢数据。
        self._load_failed = False
        if self.path is not None:
            # 这里不用 load_aliases：那个函数会把空值条目过滤掉，
            # 而空值恰恰是「已登记待翻译」的状态，必须保留。
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._map = {str(k): str(v) for k, v in raw.items()}
                else:
                    self._load_failed = True
            except (OSError, ValueError) as exc:
                self._load_failed = True
                print(f"[artist][警告] 别名表 {self.path} 读不出来（{exc}），"
                      f"按空表运行；本次不会写回，防止覆盖人工翻译。")

    # ---------------------------------------------------------------- 查询

    def as_dict(self) -> dict[str, str]:
        """只返回有译文的条目，可直接喂给 extract_artist。"""
        return {k: v for k, v in self._map.items() if v.strip()}

    def get(self, raw: str) -> str | None:
        v = self._map.get(raw, "").strip()
        return v or None

    def __contains__(self, raw: object) -> bool:
        return raw in self._map

    def __len__(self) -> int:
        return len(self._map)

    @property
    def missing(self) -> list[str]:
        """已登记但还空着译文的条目。"""
        return sorted(k for k, v in self._map.items() if not v.strip())

    @property
    def dirty(self) -> bool:
        return self._dirty

    # ---------------------------------------------------------------- 写入

    def put(self, raw: str, english: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        if self._map.get(raw, "") != english:
            self._map[raw] = english
            self._dirty = True

    def add_pending(self, names) -> list[str]:
        """把没见过的新名字登记进表（值为空），返回真正新增的那些。

        只登记不翻译——自动翻译正是我们要避免的事。这里做的只是
        "下次别再重新判断一遍"。
        """
        added: list[str] = []
        for n in names:
            n = n.strip()
            if n and n not in self._map:
                self._map[n] = ""
                added.append(n)
                self._dirty = True
        return added

    def save(self, *, force: bool = False) -> bool:
        """落盘。没有变化就不写，避免无意义地刷新文件时间戳。

        tmp + replace 原子写：别名表是人工翻译的累积成果，写一半崩溃
        留下半截 json，下次启动读不出来（触发 _load_failed），就再也没人
        能把丢掉的译文找回来。加载失败时拒绝写回——那张空表覆盖下去，
        丢的是整份手工翻译。
        """
        if self.path is None:
            return False
        if self._load_failed:
            raise RuntimeError(
                f"别名表 {self.path} 加载失败（格式坏或读不动），拒绝写回："
                f"现在覆盖会用空表抹掉人工翻译。请先手工修复该文件。")
        if not self._dirty and not force:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(dict(sorted(self._map.items())),
                          ensure_ascii=False, indent=2)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        self._dirty = False
        return True

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return (f"AliasTable(path={self.path}, total={len(self._map)}, "
                f"missing={len(self.missing)})")


def extract_artist(
    rel_path: str,
    rule: ArtistRule,
    aliases: dict[str, str] | None = None,
) -> ArtistTag:
    """从图片的相对路径提取画师名。

    只看目录层级，不看文件名：文件名通常是编号或哈希，混进来只会污染标签。
    取不到就返回空 tag 并带上原因，由调用方决定是否记录。

    两级口径要分清：
        raw  原始目录名（只去首尾空白）—— 追溯依据，也是翻译表的 key
        tag  最终注入形式 —— 查表翻译 + 清洗 + 空格转下划线后的结果
    """
    if not rule.enabled:
        return ArtistTag(reason="未启用")

    parts = [p for p in rel_path.replace("\\", "/").split("/") if p and p != "."]
    # 最后一段是文件名，目录层级要排除它
    dirs = parts[:-1] if len(parts) > 1 else []

    if len(dirs) <= rule.depth:
        return ArtistTag(reason="路径中没有足够目录层级（散图或直接在根目录）")

    raw = dirs[rule.depth].strip()
    if not raw or raw in (".", ".."):
        return ArtistTag(reason=f"第 {rule.depth + 1} 层目录名无效")

    # 先查翻译表。key 用原始目录名，因为表是人照着目录名填的，
    # 不能拿已经转过下划线的形式去查——那样人工填的条目永远命中不了。
    if aliases:
        mapped_source = aliases.get(raw)
        if mapped_source:
            mapped = _clean(mapped_source, rule.max_len,
                            space_to_underscore=rule.space_to_underscore)
            if mapped:
                return ArtistTag(raw=raw, tag=mapped, mapped=True)
        # 表里存在但值为空 = 待翻译，仍按原名走，并在统计里算作缺口

    tag = _clean(raw, rule.max_len,
                 space_to_underscore=rule.space_to_underscore)
    if not tag:
        return ArtistTag(raw=raw, reason=f"第 {rule.depth + 1} 层目录名清洗后为空")

    return ArtistTag(raw=raw, tag=tag, mapped=False)


def needs_translation(name: str) -> bool:
    """名字是否含非 ASCII 字符。用于统计别名表还缺多少条。"""
    return bool(_NON_ASCII.search(name))


def format_tag(artist: str, rule: ArtistRule) -> str:
    """拼成最终标签形式，例如 `@sakimichan`。"""
    if not artist:
        return ""
    return f"{rule.prefix}{artist}"


def prepend_tag(text: str, tag: str) -> str:
    """把 tag 放到文本最前面，幂等。

    放开头而不是结尾，是因为扩散模型的文本编码器对前缀更敏感，
    且下游按逗号切分做 tag 抽取时，首项位置是稳定的。

    caption 与 tag 串共用这一份拼接规则：两处各写一遍迟早会出现
    "caption 里带了画师、tag 串里没带"这种对不上的产物。
    """
    if not tag:
        return text
    text = text.lstrip()
    if not text:
        return tag
    # 已经带过就不重复加，保证幂等（重打标时不会越加越多）
    if text.startswith(tag):
        return text
    sep = "" if text.startswith((",", "，")) else ", "
    return f"{tag}{sep}{text}"


def inject_into_caption(caption: str, tag: str, rule: ArtistRule) -> str:
    """把画师标签放到 caption 最前面（受规则开关约束）。"""
    if not tag or not rule.inject_caption:
        return caption
    return prepend_tag(caption, tag)


def collect_missing_translations(
    tags: list[ArtistTag],
    rule: ArtistRule,
) -> list[str]:
    """列出还没英文映射的非英文画师名，供人工补别名表。

    这是"不自动音译"策略的配套工具：缺口是显式的、可枚举的，
    而不是静默产生一批拼写各异的标签。
    """
    missing: list[str] = []
    seen: set[str] = set()
    for t in tags:
        if not t.raw or t.mapped or t.raw in seen:
            continue
        seen.add(t.raw)
        if needs_translation(t.raw):
            missing.append(t.raw)
    return sorted(missing)
