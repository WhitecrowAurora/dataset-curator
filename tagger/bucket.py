"""分辨率分桶：在一个固定的像素预算里，为若干宽高比各求一个对齐尺寸。

为什么要有这一层
----------------
主创定的口径是"768×768 总预算，可以不同比例，直接上桶"。这句话里有两个约束
必须拆开看，否则会写错：

1. **预算是面积，不是边长。** 589824 是 768×768 的乘积。一张 16:9 的图在同样
   预算下应该是 1024×576，而不是 1365×768——后者面积 1048320，接近预算的 1.8 倍。
   把"768"理解成短边，会让宽幅图的成本凭空涨一大截。

2. **对齐粒度是 32。** Qwen3-VL 是 patch=16、merge=2，即 32×32 像素才凑成一个
   视觉 token。尺寸不是 32 的倍数时，processor 会自己向上取整，产生你算不出来的
   零头。提前对齐，账才对得上。

两个约束一夹，非方形桶就注定填不满预算——这是几何决定的，不是实现缺陷。
以 4:3 为例，32 对齐下的可行尺寸只有 768×576（442368）和 864×640（552960），
后者占预算 93.8% 已经是能做到的最好。

分桶到底解决什么
----------------
对 VLM 打标本身，它不改变模型行为——processor 反正会按 max_pixels 自己缩一次。
真正被解决的是**下游**：

- 同桶内图片尺寸完全一致，训练时一个 batch 不需要 padding，显存曲线是平的；
- 归档 meta 里写死 bucket 名，训练侧按桶开 dataloader，不用每次重新统计分布；
- 极宽/极窄的图会被归到最接近的桶，不会因为长边爆掉而产生异常大的 token 数。

换句话说是"给下游一个稳定的输入契约"，不是"让 VLM 看得更清楚"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

# Qwen3-VL: patch_size=16, merge_size=2 → 一个视觉 token 覆盖 32×32 像素
DEFAULT_ALIGN = 32

# 比例误差上限。超过这个值说明桶的形状已经和原图明显不符，
# 强行塞进去会裁掉或多出内容，不如让调用方按原比例处理。
MAX_RATIO_ERROR = 0.08

# 常见的一组比例：方形、横三档、竖一档。
# 选这五个是因为绝大多数图库的宽高比分布集中在这里，
# 再细分下去每个桶的样本量会太小，失去分桶的意义。
DEFAULT_RATIOS: tuple[float, ...] = (
    1.0,        # 1:1
    4 / 3,      # 4:3
    3 / 2,      # 3:2
    16 / 9,     # 16:9
    9 / 16,     # 9:16（竖图）
)


@dataclass(slots=True, frozen=True)
class Bucket:
    """一个分桶：名字、目标尺寸、以及它实际用掉了多少预算。"""

    name: str
    ratio: float
    width: int
    height: int

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def tokens(self) -> int:
        """视觉 token 数。对齐之后必然整除，不会有余数。"""
        return self.pixels // 1024

    def fit(self, max_pixels: int) -> float:
        return self.pixels / max_pixels if max_pixels else 0.0

    def describe(self, max_pixels: int) -> str:
        return (
            f"{self.name:<8} {self.width:>5}×{self.height:<5} "
            f"{self.pixels:>8} px  {self.tokens:>5} tok  "
            f"{self.fit(max_pixels) * 100:5.1f}%"
        )


def _snap(value: float, align: int) -> int:
    """四舍五入到 align 的倍数。避免 Python 的银行家舍入在 .5 处出偏差。"""
    return int(math.floor(value / align + 0.5)) * align


def _ratio_name(ratio: float) -> str:
    """给比例起个可读名字，用于 meta 与文件名。

    容差放到 0.5% 是故意的：配置里人是照着 1.3333 / 1.7778 这种写法敲的，
    不会去写 4/3 的精确浮点。用 1e-6 匹配的话这几个桶会退化成 "1.33"，
    meta 里的名字就失去可读性了——而 meta 是要被下游按名字分组读的。
    """
    for known, name in (
        (1.0, "1x1"),
        (4 / 3, "4x3"),
        (3 / 2, "3x2"),
        (16 / 9, "16x9"),
        (9 / 16, "9x16"),
        (2 / 1, "2x1"),
        (1 / 2, "1x2"),
    ):
        if abs(ratio - known) / known < 0.005:
            return name
    return f"{ratio:.2f}"


def solve_bucket(
    ratio: float,
    max_pixels: int,
    *,
    align: int = DEFAULT_ALIGN,
) -> Bucket:
    """在预算内为单个比例求最大可行尺寸。

    策略是"面积优先"：先把高度按理想值取整，再在其邻域里枚举几个候选，
    取不超预算且面积最大的那个。面积优先而不是比例优先，是因为这里的稀缺资源
    是 token 预算——为了凑一个漂亮的 4:3 而丢掉 7% 的像素，等于白花钱。
    """
    if ratio <= 0:
        raise ValueError(f"宽高比必须为正: {ratio}")
    if max_pixels < align * align:
        raise ValueError(f"像素预算 {max_pixels} 小于一个对齐单元 {align}²")

    ideal_h = math.sqrt(max_pixels / ratio)
    base = int(ideal_h // align) * align

    best: tuple[int, int] | None = None
    # 向上多试两档是给"宽度富余、高度不足"的比例留空间；
    # 向下试四档覆盖窄桶。再远就偏离原比例太久了。
    for delta in (2 * align, align, 0, -align, -2 * align, -3 * align, -4 * align):
        h = base + delta
        if h < align:
            continue
        w = _snap(ratio * h, align)
        if w < align or w * h > max_pixels:
            continue
        if best is None or w * h > best[0] * best[1]:
            best = (w, h)

    if best is None:
        raise ValueError(f"比例 {ratio} 在预算 {max_pixels} 内找不到可行尺寸")

    w, h = best
    return Bucket(name=_ratio_name(ratio), ratio=ratio, width=w, height=h)


def compute_buckets(
    max_pixels: int,
    ratios: Sequence[float] | None = None,
    *,
    align: int = DEFAULT_ALIGN,
) -> list[Bucket]:
    """为一组比例构建分桶表。默认 5 档，与主创说的"一种分辨率范围 5 个"一致。"""
    ratios = tuple(ratios) if ratios else DEFAULT_RATIOS
    return [solve_bucket(r, max_pixels, align=align) for r in ratios]


def assign_bucket(
    width: int,
    height: int,
    buckets: Iterable[Bucket],
    *,
    max_pixels: int,
    align: int = DEFAULT_ALIGN,
) -> Bucket | None:
    """把一张图分配到最接近的桶。

    判断用的是"比例的对数距离"，不是绝对差：1:1 与 3:2 的差距，和 3:2 与 2:1
    的差距，在绝对差上都是 0.5，但视觉上前者大得多。取对数才符合直觉。

    超过 MAX_RATIO_ERROR 返回 None，表示这张图的比例超出设计范围（比如全景图、
    长截图），调用方应当按原比例处理而不是硬塞进某个桶。
    """
    if width <= 0 or height <= 0:
        return None

    actual = width / height
    log_actual = math.log(actual)

    best: tuple[float, Bucket] | None = None
    for b in buckets:
        err = abs(math.log(b.ratio) - log_actual)
        if best is None or err < best[0]:
            best = (err, b)

    if best is None:
        return None

    err, bucket = best
    # 对数误差换算回相对误差：(e^err - 1)，err 很小时约等于 err
    relative = math.exp(err) - 1
    if relative > MAX_RATIO_ERROR:
        return None

    # 桶本身可能因为对齐而略超/略欠预算，这里再确认一次
    if bucket.pixels > max_pixels:
        return None
    return bucket


def tokens_of(width: int, height: int, *, align: int = DEFAULT_ALIGN) -> int:
    """给定尺寸的视觉 token 数（向上对齐到 32 后计算）。"""
    w = math.ceil(width / align) * align
    h = math.ceil(height / align) * align
    return (w * h) // 1024
