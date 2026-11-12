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

from .artist_templates import starts_with_artist_sentence

# 标签里不能出现的字符：逗号会和 tag 列表分隔冲突，换行会破坏 jsonl 行边界，
# @ 前缀重复会导致 "@@@name" 这种下游解析歧义。
_UNSAFE = re.compile(r"[,;\n\r\t@#]+")
_SPACES = re.compile(r"\s+")

# 视为"非英文"的判据：只要出现非 ASCII 字母，就认为需要外部映射才能英文化。
_NON_ASCII = re.compile(r"[^\x00-\x7F]")

# 分片包的几种后缀。剥掉它们才和画师表文件名（`<stem>.artist.parquet`）对得上。
_PACK_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")

# NL 注入句式的取值归一化。认不出的值一律按 `template`——新行为是默认，
# 只有显式写 bare/legacy 才回到"裸 @名 接正文"。
_NL_STYLES = {
    "template": "template", "sentence": "template", "style": "template",
    "bare": "bare", "prefix": "bare", "legacy": "bare", "old": "bare",
}


def _nl_style(value) -> str:
    raw = str(value if value is not None else "template").strip().lower()
    return _NL_STYLES.get(raw, "template")


@dataclass(slots=True)
class ArtistRule:
    """画师标签的启用规则。全部来自配置，代码里不留魔法值。"""

    # 默认开着：画师是这批数据最贵的标注（人工整理 + 原始 tag 校对），
    # 默认关掉的话别人拿到产物才发现没有 @name，只能整批重打。
    enabled: bool = True
    # 从路径的第几层取（0 = 第一层目录）
    depth: int = 0
    # 注入前缀。扩散模型社区惯例是 @
    prefix: str = "@"
    # 写进 caption 开头（NL 侧）。
    inject_caption: bool = True
    # 写进 tag 串开头（TAG 侧）。与上一项分开：有人只要 NL 里带画师、
    # 或者只要 tag 条件里带，硬绑成一个开关就没法表达。
    inject_tags: bool = True
    # 翻译表路径，为空则不查、也不自动累积
    aliases_path: str | None = None
    # 画师对照表：单个 parquet（全表）或一个目录（里面是 shard-XXXX.artist.parquet）。
    # 给了就优先查表，查不到再退回目录名提取——目录名是人工整理的，表是
    # danbooru 原始标注，两边都可能缺，谁有不矛盾。
    table: str | None = None
    # 允许出现在标签里的最大长度，超了视为异常路径而不是作者名
    max_len: int = 64
    # 查不到画师时是否**跳过这张图**（默认否：照常产出，只是没有 @ 前缀）。
    # 关掉的取舍是"宁可少一个前缀也不丢数据"——真实数据集里约 4.6% 的图
    # 在 danbooru 上没有作者标注，整批跳过等于凭空砍掉这部分样本；开着的
    # 取舍是"只要带画师绑定的干净数据"，由使用方按训练目标决定。
    skip_when_missing: bool = False
    # 名字里有空格时替换成下划线。
    # 原因：caption 是按空格分词的，`@sakimi chan` 会被切成 `@sakimi` 和 `chan`
    # 两个 token，画师绑定就散了一半。合并成一个词才能整体激活。
    # 没有空格的名字原样保留，不做任何改写。
    # ⚠ 与 `verbatim_names` 冲突：后者打开时（默认）本项对画师名**失效**。
    space_to_underscore: bool = True
    # 名字逐字原样优先。画师名是绑定键，大小写、空格、假名都是键的一部分；
    # 注入形态必须与来源 tag 逐字相等，否则训练侧绑不上同一个人。
    # 与 `space_to_underscore` 冲突时以本项为准（历史清洗规则让位）；
    # 置 False 才回到"空格转下划线"的旧行为。
    verbatim_names: bool = True
    # NL 注入句式。`template`（默认）：自然句式、位置固定开头；
    # `bare`：旧行为，裸 `@名字` 直接接正文，仅用于回退对比。
    nl_style: str = "template"

    @classmethod
    def from_dict(cls, d: dict | None) -> "ArtistRule":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            depth=int(d.get("depth", 0)),
            prefix=str(d.get("prefix", "@")),
            inject_caption=bool(d.get("inject_caption", True)),
            inject_tags=bool(d.get("inject_tags", True)),
            aliases_path=d.get("aliases_path") or None,
            table=d.get("table") or None,
            max_len=int(d.get("max_len", 64)),
            space_to_underscore=bool(d.get("space_to_underscore", True)),
            verbatim_names=bool(d.get("verbatim_names", True)),
            nl_style=_nl_style(d.get("nl_style")),
            skip_when_missing=bool(d.get("skip_when_missing", False)),
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


def clean_artist_name(name: str, max_len: int = 64) -> str:
    """对外暴露的画师名清洗。

    整流器（artist_import）必须和注入侧用**同一个**清洗口径，否则
    "整流完查得到、注入时又变成另一个名字"，别名表也会因为 key 不一致失效。
    """
    return _clean(name, max_len)


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
    # 名字逐字原样优先：verbatim_names 打开（默认）时，空格转下划线对画师名
    # 失效，否则 `sakimi chan` 会变成 `sakimi_chan`，注入形态与来源 tag 不再
    # 逐字相等，训练侧绑不上同一个人。
    fold_spaces = rule.space_to_underscore and not rule.verbatim_names
    if aliases:
        mapped_source = aliases.get(raw)
        if mapped_source:
            mapped = _clean(mapped_source, rule.max_len,
                            space_to_underscore=fold_spaces)
            if mapped:
                return ArtistTag(raw=raw, tag=mapped, mapped=True)
        # 表里存在但值为空 = 待翻译，仍按原名走，并在统计里算作缺口

    tag = _clean(raw, rule.max_len, space_to_underscore=fold_spaces)
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
    if starts_with_artist_sentence(text, tag):
        # 上游已按句式模板把画师句写在开头（`使用 @名 的风格` /
        # `In the style of @名`）。这里再拼一次裸名前缀，同一个名字会在
        # 正文里出现两遍——句式那次带语义，裸名这次才是多余的，所以跳过。
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


class ArtistTable:
    """从 parquet 里查画师：`post id -> [名字]`。

    两种形态：

    - **单个 parquet**（全表，约 70 MB）：第一次查询时整份读进内存；
    - **一个目录**（里面是 `shard-XXXXXX.artist.parquet`）：只读当前分片那一份。

    为什么按分片读：一个分片约 200 KB，一次打标只跑一两个分片；把 845 份
    全读进来要 130 MB 和好几秒，而按分片读是"用哪片读哪片"，还能让下载侧
    只下自己需要的那一份。

    分片必须**配对**才敢用：每片 parquet 的文件级 metadata 记着它是按哪个
    tar 切的（`shard`）和切出多少行（`rows`），对不上就拒答。理由见
    `_pairing_problem`——错片的答案看起来和正确答案一模一样，只是编号是旧的。

    三种形态（前两种在本地，第三种在 HF）：

    - `D:\\x\\all.parquet`
    - `D:\\x\\shards`（里面是 `shard-XXXXXX.artist.parquet`）
    - `hf://datasets/<repo>/<文件或目录>`：按需下载，不整目录预拉

    在线形态存在的理由：画师表是 104 MB/844 片，成员机器上未必有，
    每次 scp 一份不现实；而"用哪片拉哪片"正好是分片形态天生支持的。

    读不到就返回空列表——调用方会退回目录名提取，再没有就留空。
    宁可没有标签，也不要猜（见模块开头的第 3 条）。
    """

    HF_SCHEME = "hf://"
    # 与 hf_source.HF_CACHE_ROOT 同口径。不直接 import 那个模块：它为了下载
    # 拖了 httpx 与分段计划，而查表只需要一个文件落点。
    HF_CACHE_ROOT = Path("run") / "hf-cache"

    def __init__(self, spec: str | None):
        raw = (spec or "").strip()
        self.path: Path | None = None
        self.remote_repo = ""
        self.remote_file = ""       # 在线**单表**形态
        self.remote_dir = ""        # 在线**分片目录**形态
        self.error: str = ""
        self._single: dict[int, list[str]] | None = None
        self._shards: dict[str, dict[int, list[str]]] = {}
        # 分片级问题：读不动、配对不上、拉不下来。只记账 + 报几次，
        # **不**把整张表掐掉。
        self.problems: list[str] = []
        self._reported: set[str] = set()
        # 拉不到/读不动的分片。远程形态下必须记住，否则每张图都会重试下载。
        self._failed: set[str] = set()
        if raw.startswith(self.HF_SCHEME):
            self._parse_hf(raw)
        elif raw:
            self.path = Path(raw)
            if not self.path.exists():
                self.error = f"画师表不存在：{self.path}"

    def _parse_hf(self, raw: str) -> None:
        """`hf://datasets/<repo>/<文件或目录>` → (repo, file|dir)。

        不猜"是文件还是目录"：带 `.parquet` 就是单表，其余一律当分片目录。
        猜错的代价是一整批静默无画师，不如让写法有唯一解。
        """
        rest = raw[len(self.HF_SCHEME):].strip("/")
        if rest.startswith("datasets/"):
            rest = rest[len("datasets/"):]
        parts = [p for p in rest.split("/") if p]
        if len(parts) < 3:
            self.error = (f"在线画师表要写成 hf://datasets/<repo>/<文件或目录>：{raw}")
            return
        self.remote_repo = "/".join(parts[:2])
        tail = "/".join(parts[2:])
        if tail.lower().endswith(".parquet"):
            self.remote_file = tail
        else:
            self.remote_dir = tail.rstrip("/")

    @property
    def active(self) -> bool:
        if self.error:
            return False
        return self.path is not None or bool(self.remote_repo)

    @property
    def online(self) -> bool:
        return bool(self.remote_repo)

    @staticmethod
    def id_of(image_id: str) -> int | None:
        """从 `8349000` / `8349000.jpg` / `artist-8349000` 里取 post id。

        取**末段里最长的那串数字**：`8349000_1.jpg`（重名后缀）按"最后一串"
        会拿到 1，`sub2/8349000.jpg` 按"第一串"会拿到 2。最长串两头都对。
        """
        text = str(image_id or "").strip()
        if text.isdigit():
            return int(text)
        seg = re.split(r"[\\/]", text)[-1]
        runs = re.findall(r"\d+", seg) or re.findall(r"\d+", text)
        if not runs:
            return None
        return int(max(runs, key=len))

    @staticmethod
    def shard_stem(shard: str) -> str:
        """把各种写法的分片名归一到 `shard-000012`。

        实际见过的四种：本地路径 `D:\\...\\shard-000012.tar`、HF 直读的
        `hf://datasets/x/y/shard-000012.tar#member`、manifest 里的
        `dist-0003.zip::artist/0001.jpg`、以及已经是 parquet 名的。
        用 `Path(...).stem` 只对第一种成立：`.tar.gz` 会留下 `.tar`，
        `#member` / `::member` 会整段粘在名字后面，于是文件名对不上，
        查询静默返回空。
        """
        text = str(shard or "").strip()
        text = text.split("#", 1)[0]              # hf://.../#member
        text = text.split("::", 1)[0]             # 包名::包内成员
        name = text.replace("\\", "/").rsplit("/", 1)[-1]
        low = name.lower()
        if low.endswith(".artist.parquet"):
            return name[: -len(".artist.parquet")]
        for suf in _PACK_SUFFIXES:
            if low.endswith(suf):
                return name[: -len(suf)]
        return Path(name).stem if "." in name else name

    def _complain(self, key: str, msg: str) -> None:
        """分片级问题：记账并报一次，但不动 `active`。

        以前这类错误直接写 `self.error`，而 `active` 就看它——于是某一分钟
        读不动一片，从那一刻起**每**一片都失效，退回目录名/旧字段，全程
        没有一句提示。一片的问题必须止于一片。
        """
        if key in self._reported:
            return
        self._reported.add(key)
        self.problems.append(msg)
        # 在线形态下 844 片全拉不到会刷屏 844 行：前 20 条报出来，其余只记账。
        if len(self.problems) <= 20:
            print(f"[artist][警告] {msg}", flush=True)

    def _fetch(self, filename: str) -> Path | None:
        """从 HF 拉一个文件到本地缓存，返回本地路径；失败返回 None。

        token 走 `HF_TOKEN` 环境变量（huggingface_hub 的标准口径）——画师表
        可能在 gated 仓库里，但把 token 塞进 ArtistRule 意味着它会进任务快照
        和历史记录，那是密钥泄露的常见路子。

        不预取整个目录：844 片全拉是 104 MB + 844 次请求，而一次打标只用
        一两片。
        """
        try:
            from huggingface_hub import hf_hub_download
        except ImportError:
            self.error = "在线画师表需要 huggingface_hub：pip install huggingface_hub"
            return None
        cache_dir = self.HF_CACHE_ROOT / (
            self.remote_repo.replace("/", "--") + "--artist")
        try:
            local = hf_hub_download(
                repo_id=self.remote_repo, filename=filename, repo_type="dataset",
                cache_dir=str(cache_dir))
        except Exception as exc:                       # noqa: BLE001
            self._complain(
                f"{self.remote_repo}/{filename}",
                f"拉不到在线画师表 {self.remote_repo}/{filename}："
                f"{type(exc).__name__} {exc}")
            return None
        return Path(local)

    def _pairing_problem(self, tbl, path: Path, expect: str) -> str:
        """分片 parquet 自带记号与请求对不上时的错因，对得上返回空串。

        `work/shard_artists.py` 切表时把 tar 名和行数写进文件级 metadata，
        就是为了让这一层能发现"拿错片"。没有这道校验，一片按旧数据切出来的
        parquet 会被当成新片照用：行数、列名、类型全对，只有编号是旧的，
        查出来的画师张冠李戴且一句日志都没有（错位是静默的）。
        """
        meta = tbl.schema.metadata or {}
        mark = meta.get(b"shard")
        if expect and mark:
            got = self.shard_stem(mark.decode("utf-8", "replace"))
            if got and got != expect:
                return (f"{path.name} 记的是「{got}」，要查的是「{expect}」："
                        f"两片对不上，照它回答等于给错画师")
        rows = meta.get(b"rows")
        if rows:
            try:
                want = int(rows.decode("utf-8", "replace"))
            except ValueError:
                want = -1
            if want >= 0 and want != tbl.num_rows:
                return (f"{path.name} 自称 {want} 行、实有 {tbl.num_rows} 行："
                        f"这份表写了一半或换过版本")
        return ""

    def _read(self, path: Path, *, expect: str = "") -> dict[int, list[str]] | None:
        """读一份 parquet。返回 None 表示读不了（错因已记账/写进 error）。"""
        try:
            import pyarrow.parquet as pq
        except ImportError:
            self.error = "查画师表需要 pyarrow：pip install pyarrow"
            return None
        try:
            tbl = pq.read_table(path, columns=["id", "artists"])
        except Exception as exc:                       # noqa: BLE001
            why = f"读不了画师表 {path.name}：{type(exc).__name__} {exc}"
            if expect:
                self._complain(expect, why)
            else:
                self.error = why
            return None
        bad = self._pairing_problem(tbl, path, expect)
        if bad:
            self._complain(expect or path.name, bad)
            return None
        return self._normalize(
            dict(zip(tbl.column("id").to_pylist(),
                     tbl.column("artists").to_pylist())), path, expect)

    def _normalize(self, raw: dict, path: Path, expect: str) -> dict[int, list[str]]:
        """把表里的 id 与画师名整成查表侧的唯一口径。

        两件事，都是实测踩出来的：

        - **id 可能是字符串**（danbooru 的导出常见）。不转 int 的话 lookup 一律
          查不到，而且**不报错**——表现成"整批没有画师"，再叠上
          skip_when_missing 就是整片静默跳过。
        - **名字可能是空串或纯空格**。原样返回会让上层以为"有画师"，于是跳过
          开关不生效、caption 里又不会出现前缀，两头不一致。空名一律当没有。
        """
        out: dict[int, list[str]] = {}
        bad_id = 0
        for key, names in raw.items():
            try:
                pid = int(key)
            except (TypeError, ValueError):
                bad_id += 1
                continue
            cleaned = [str(n).strip() for n in (names or []) if str(n).strip()]
            if cleaned:
                out[pid] = cleaned
        if bad_id:
            self._complain(expect or path.name,
                           f"{path.name} 有 {bad_id} 行的 id 不是数字，已跳过"
                           f"（那些图会被当成没有画师）")
        return out

    def _single_dict(self) -> dict[int, list[str]]:
        """单表形态（本地文件或远程 parquet）整份读进内存。"""
        if self._single is not None:
            return self._single
        if self.remote_file:
            local = self._fetch(self.remote_file)
            got = self._read(local) if local is not None else None
        else:
            assert self.path is not None
            got = self._read(self.path)
        # 单表失败也缓存空表：否则每张图都会重新下一遍 70 MB。
        # 错因已经进 error/problems，不会变成无声失败。
        self._single = got or {}
        return self._single

    def _shard_dict(self, shard: str) -> dict[int, list[str]]:
        stem = self.shard_stem(shard)
        if not stem:
            return {}
        hit = self._shards.get(stem)
        if hit is not None:
            return hit
        if self.remote_dir:
            # 远程失败要记住：仓库里没有这片就是没有，每张图重试一次下载
            # 会把 844 片变成几十万次 404。本地相反——文件可能是边跑边拷的。
            if stem in self._failed:
                return {}
            local = self._fetch(f"{self.remote_dir}/{stem}.artist.parquet")
            if local is None:
                self._failed.add(stem)
                return {}
            got = self._read(local, expect=stem)
        else:
            assert self.path is not None
            f = self.path / f"{stem}.artist.parquet"
            if not f.is_file():
                # 不缓存"没有"：分片表可能是边跑边拷的，钉死成空表等于
                # 这一片从此再也查不到，退回目录名兜底且毫无提示。
                return {}
            got = self._read(f, expect=stem)
        if got is None:
            self._failed.add(stem)
            return {}
        self._shards[stem] = got
        return got

    def lookup(self, image_id: str, shard: str | None = None) -> list[str]:
        if not self.active:
            return []
        pid = self.id_of(image_id)
        if pid is None:
            return []
        # 单表：本地文件，或在线以 `.parquet` 结尾的地址
        if self.remote_file or (self.path is not None and self.path.is_file()):
            return self._single_dict().get(pid, [])
        if not shard:
            return []
        return self._shard_dict(shard).get(pid, [])


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
