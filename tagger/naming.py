"""图片标识与归档布局。

为什么 image_id 要由分发环节决定，而不是本地生成
------------------------------------------------
10 个人各跑各的，最后要把结果合并成一张总表。如果每人从 0 开始编号，
合并时必然撞号——而撞号的后果不是报错，是**两条不同图片的标注被悄悄覆盖**，
等到训练时才发现某个画师的数据少了一半，已经查不出是谁覆盖的。

所以规则定死：**全局单调序号，由分发清单一次性分配**。
分发包里带着 image_id，本地只负责照着填，不重新生成。
这样"谁处理了哪些图"在分发那一刻就确定了，合并时只需要对账。

两种布局
--------
flat     画师名-序号.jpg，全部拍平。下游按 basename 组样本，最省事。
         注意这不是"丢掉画师信息"——画师已经在名字里，同时还会写进 meta。
mirror   照搬原目录结构，画师名/原文件名。适合需要保留溯源路径的场景，
         代价是不同来源的目录名容易撞车（多个 shard 都有 0001/）。

序号永远是全局的，与布局无关。局部序号在多人合并时一定会出问题。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

# Windows 文件名禁用字符 + 路径分隔符。Linux 上虽允许，但产出的数据
# 最终要在各成员的机器间流转，按最严的标准清洗才不会在别人那边炸。
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACES = re.compile(r"\s+")

# 保留扩展名白名单。表内的统一成规范拼写（.jpeg→.jpg、.tiff→.tif），
# 避免混杂导致下游按扩展名选解码器时漏掉。
_EXT_MAP = {
    ".jpg": ".jpg", ".jpeg": ".jpg", ".jpe": ".jpg",
    ".png": ".png", ".webp": ".webp", ".bmp": ".bmp",
    ".gif": ".gif", ".tif": ".tif", ".tiff": ".tif",
}

# 全链路认的图片扩展名（发现/打包/校验共用这一份）。
# 之前 archive 与 local_tag/hf_source 各维护一份，archive 那份缺 gif/tif，
# 结果是打包阶段静默丢图——所以必须只有一处定义。
IMAGE_SUFFIXES: tuple[str, ...] = (
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff",
)

# normalize_ext 只对没见过的扩展名警告一次，几万张同格式图不该刷几万行
_WARNED_EXTS: set[str] = set()


@dataclass(slots=True)
class Naming:
    """命名规则。全部来自配置。"""

    # flat | mirror
    layout: str = "flat"
    # 序号宽度。6 位支持 999999 张，够用且排序稳定
    seq_width: int = 6
    # 画师名为空时用什么占位。空串表示不加前缀（纯序号命名）
    artist_fallback: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> "Naming":
        d = d or {}
        layout = str(d.get("layout", "flat")).lower()
        if layout not in ("flat", "mirror"):
            raise ValueError(f"archive.layout 只能是 flat / mirror，收到 {layout!r}")
        return cls(
            layout=layout,
            seq_width=int(d.get("seq_width", 6)),
            artist_fallback=str(d.get("artist_fallback", "")),
        )

    @property
    def flatten(self) -> bool:
        return self.layout == "flat"


def sanitize(name: str, *, max_len: int = 64) -> str:
    """清洗成安全的文件名字段。

    空格转下划线（同一套理由：下游按空格分词），非法字符去掉，
    连续空白压成一个。不做音译、不做大小写变换。
    """
    name = _SPACES.sub(" ", name.strip())
    name = _ILLEGAL.sub("", name)
    name = name.replace(" ", "_").strip("._")
    return name[:max_len]


def normalize_ext(path: str) -> str:
    """归一扩展名。

    表内的映射到规范拼写；没见过的**保留原样**并警告一次。以前一律兜成
    .jpg——把 .avif 之类的图改名为 .jpg，包内出现"叫 .jpg 的非 JPEG 字节"，
    下游按扩展名选解码器时整个解不出来，而打包日志看不出任何异常。
    """
    ext = PurePosixPath(path.replace("\\", "/")).suffix.lower()
    known = _EXT_MAP.get(ext)
    if known:
        return known
    if ext and ext not in _WARNED_EXTS:
        _WARNED_EXTS.add(ext)
        print(f"[naming][警告] 未识别的扩展名 {ext!r}，保留原名"
              f"（同格式不重复提示；下游解码器必须认识它）")
    return ext or ".jpg"


def make_image_id(artist: str, seq: int, *, width: int = 6) -> str:
    """生成全局唯一的 image_id。

    `<画师>-<序号>` 这种形式的两个好处：人工看清单时一眼知道归属；
    排序后同一画师的图天然聚在一起，抽检时容易挑到有代表性的样本。
    """
    tag = sanitize(artist) if artist else ""
    stem = f"{seq:0{width}d}"
    return f"{tag}-{stem}" if tag else stem


def archive_name(
    image_id: str,
    *,
    ext: str,
    layout: str,
    rel_path: str,
) -> str:
    """算出图片在归档 / 输出里的文件名。"""
    if layout == "mirror":
        # 保留原路径，但扩展名统一，且去掉可能存在的 .. 之类越界段
        rel = rel_path.replace("\\", "/").strip("/")
        parts = [p for p in rel.split("/") if p and p not in (".", "..")]
        if parts:
            parts[-1] = PurePosixPath(parts[-1]).stem + normalize_ext(parts[-1])
            return "/".join(parts)
    return f"{image_id}{ext}"


def annotation_name(image_id: str, kind: str, *, layout: str,
                    rel_path: str, style: str = "suffix") -> str:
    """标注文件的落点。

    style="suffix"  与图片同 basename，只换后缀（flat 下是 xxx-000001.nl）
    style="dash"    `<名字>-<kind>.json`，即 xxx-000001-nl.json
                    这是成员本地交接时用的形态，肉眼可辨、复制粘贴不易错
    """
    if layout == "mirror":
        rel = rel_path.replace("\\", "/").strip("/")
        parts = [p for p in rel.split("/") if p and p not in (".", "..")]
        parent = "/".join(parts[:-1])
        base = f"{image_id}-{kind}.json" if style == "dash" else f"{image_id}.{kind}"
        return f"{parent}/{base}" if parent else base
    return f"{image_id}-{kind}.json" if style == "dash" else f"{image_id}.{kind}"


def group_by_artist(image_ids: list[str]) -> dict[str, list[str]]:
    """按 image_id 里的画师前缀分组。

    画师名本身可能带连字符（"K-9"），所以从右边切最后一个 "-数字"，
    而不是从左切第一个连字符。
    """
    groups: dict[str, list[str]] = {}
    for iid in image_ids:
        stem, dash, tail = iid.rpartition("-")
        artist = stem if dash and tail.isdigit() else ""
        groups.setdefault(artist, []).append(iid)
    return groups
