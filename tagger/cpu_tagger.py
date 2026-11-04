"""CPU 侧 tag 反推（分类头模型）。

为什么单独一条线
----------------
tag 和 NL 是两种性质的信号，硬塞给同一个生成模型会互相拖累：

    VLM 生成 tag                专用分类头
    词表每次可能换写法           固定词表，确定性输出
    没有置信度（只能填 1.0）     有概率，能按阈值过滤
    与 NL 共享一次 prefill       单图几十~几百毫秒，CPU 就能跑

所以默认走「GPU 跑 VLM 出 NL、CPU 跑 tagger 出 tag」两条线并行：
GPU 那边每张 1~3 秒，CPU 这边同一时间把 tag 算完，谁也等不谁。
这也是 `_tag_payload` 里 prob 不再是硬编码 1.0 的原因。

三个后端
--------
pixai       pixai-labs/pixai-tagger-v1.0，transformers pipeline（trust_remote_code），
            30877 词表、1008² 输入。效果最好，但 486M 参数在 CPU 上偏慢。
wd-eva02    ashen-sensored/wd-eva02-tagger-2026-canary，timm eva02_large 448²、
            16473 词表。CPU 上明显更快，是"CPU 跟不上就换它"的选项。
cl-tagger-v2 cella110n/cl_tagger_v2（onnxruntime，v2_01a）。仓库需要 token，
            预处理参数从 model_metadata.json 读，读不到时用保守默认值。

依赖全部是懒加载的：不用 tag 反推的人不该被迫装 torch。
"""

from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# WD 词表的 category 编号（selected_tags.csv 的 category 列）
# 9 = rating（general/sensitive/nsfw/explicit）。这个必须单列出来：
# 不加这一条，rating 会落进 default 分支被当成 general 保留下来，
# 实测里 eva02 对一张纯色图就吐了 `explicit 0.62` —— 分级标签是判图用的，
# 混进训练 tag 等于给模型喂了错误的先验。
WD_CATEGORY = {0: "general", 1: "artist", 3: "copyright", 4: "character",
               5: "meta", 9: "rating"}

# 默认只要"描述画面"的类别。artist 类必须丢掉——画师名由目录结构决定，
# 让模型猜作者等于把 100% 准确的信息换成 60% 准确的猜测。
DEFAULT_KEEP = ("general", "character", "copyright", "style")

# 阈值兜底。**不能是 0**：WD 词表有 16473 个 tag，不过滤的结果是每张图
# 都按概率排序取前 160 个——实测一张纯色图能吐出满额的 160 条，
# 全是"看起来像那么回事"的噪声。0.35 是 WD 系列社区常用的起点，
# 真实素材上还要用 sample.md 抽检再调（分类头的分数分布跟模型/素材强相关）。
DEFAULT_THRESHOLD = 0.35


class TaggerUnavailable(RuntimeError):
    """依赖没装或模型加载失败。错误信息要带"怎么装/怎么改"，不要只抛栈。"""


def normalize_precision(value: str | None) -> str:
    """把各种写法归一成 fp32 / tf32 / bf16。

    三档的取舍（79 张真实素材、真权重、阈值 0.35 实测，详见
    devtools/perf-baseline.md §10）：
        fp32  基线，零漂移。
        tf32  fp32 权重 + tensor core，79 张 **0 个 tag 真改判**、概率最大漂移
              0.0027，前向 1.12×（b1）/ 1.47×（b4）。可以当默认。
        bf16  权重也转 bf16，前向 1.74×（b1）/ 2.36×（b4），但 3002 个选中
              tag 里有 2 个真改判（脱离阈值 ±0.01 边界带）。
              这个数字对"抓训练集"是可接受的抖动，对"以后要归因的口径"不是，
              所以默认不开，要快的人自己开。
    """
    p = (value or "fp32").strip().lower()
    if p in ("", "fp32", "float32", "off", "none"):
        return "fp32"
    if p in ("tf32", "tensorfloat32"):
        return "tf32"
    if p in ("bf16", "bfloat16"):
        return "bf16"
    raise ValueError(f"未知精度 {value!r}，可选：fp32 / tf32 / bf16")


def normalize_preprocess(value: str | None) -> str:
    """预处理放在哪：pil = 现状（PIL 缩放/归一化，CPU）；tensor = 上卡。

    "解码"本身没有搬：实测 nvJPEG 单张比 PIL 还慢（15.2 vs 11.3 ms），批量
    才赢，而这里是逐张喂。真正吃时间的是缩放+归一化那一半（15.6 ms 里的
    10 ms 左右），把它留在张量上才划算——见 devtools/perf-baseline.md §12。
    """
    p = (value or "pil").strip().lower().replace("-", "_").replace(" ", "")
    if p in ("", "pil", "cpu", "pillow"):
        return "pil"
    if p in ("tensor", "gpu", "cuda", "torch", "tensor_gpu", "pil_gpu"):
        return "tensor"
    raise ValueError(f"未知预处理 {value!r}，可选：pil / tensor")


@dataclass(slots=True)
class TagPrediction:
    tags: list[tuple[str, float]] = field(default_factory=list)
    backend: str = ""
    latency_ms: int = 0
    # WD 的 rating 头（general / sensitive / questionable / explicit）。
    # 它**不属于 tag**：
    # 混进 tag 串会给训练数据喂一个错误的先验（整个词表里只有 4 个 tag，
    # 却描述的是"这张图的审核等级"）。但它也不该被扔掉——分类头每张图都
    # 算了，拿来做数据集清洗/剔除误标是白捡的。所以单列一个字段往下传。
    rating: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class _CudaGraph:
    """一次固定形状前向的图回放。

    为什么值得：这一批图要发 1480 次 kernel，每次约 3 µs 的固定发射成本，
    加上 Python 逐层 dispatch，实测占掉一半墙钟。图把这 1480 次压成一次回放。
    为什么只认满批：图形状是固化的，尾批不走图（走 eager），比补零诚实。
    """

    graph: Any
    static_in: Any
    static_out: Any
    batch: int

    def replay(self) -> None:
        self.graph.replay()


@dataclass(slots=True)
class _TensorPrep:
    """timm 的 PIL transform 翻译成"整条留在张量上"的等价步骤。

    只认 Resize / CenterCrop / ToTensor / Normalize 这四种组合；出现别的
    算子（RandomResizedCrop、Normalize 缺失、Lanczos…）就返回 None，让调用
    方回落 PIL——宁可慢，不可悄悄换一套预处理口径。
    """

    # 拉到指定 (h, w)：timm 的 squash 模式，直接拉伸、不做等比。
    resize: tuple[int, int] | None = None
    # 短边缩到这个值：timm 的 center 模式，等比缩放后再 CenterCrop。
    short_edge: int | None = None
    crop: tuple[int, int] | None = None
    mode: str = "bicubic"
    mean: Any = None
    std: Any = None

    def target_size(self, height: int, width: int) -> tuple[int, int]:
        """按 torchvision Resize 的语义算出缩放后的 (h, w)。"""
        if self.resize is not None:
            return self.resize
        if self.short_edge is not None:
            edge = self.short_edge
            if height <= width:
                return (edge, max(1, int(edge * width / height)))
            return (max(1, int(edge * height / width)), edge)
        return (height, width)


def _interp_name(interpolation: Any) -> str | None:
    """torchvision 的插值枚举 → F.interpolate 的 mode；认不出就 None。"""
    from torchvision.transforms import InterpolationMode

    # BOX / HAMMING / LANCZOS 没有对应的 F.interpolate 实现。PIL 的 LANCZOS
    # 是这里最可能遇到的一种，而它恰好是"在 CPU 上做"的理由之一。
    return {
        InterpolationMode.NEAREST: "nearest",
        InterpolationMode.NEAREST_EXACT: "nearest",
        InterpolationMode.BILINEAR: "bilinear",
        InterpolationMode.BICUBIC: "bicubic",
    }.get(interpolation)


def _tensor_prep_plan(transform: Any) -> _TensorPrep | None:
    """从 timm 建好的 transform 里读出等价的可上卡步骤。

    为什么要读而不是写死 448/bicubic：这套参数来自模型自己的 data config，
    换一个分类头就变（wd-eva02 是 squash 拉伸；别的模型可能是短边缩放 +
    CenterCrop 0.875）。写死等于把"换个模型悄悄换口径"埋进去。
    """
    import torch
    from torchvision import transforms as T

    ops = list(getattr(transform, "transforms", [transform]))
    plan = _TensorPrep()
    seen_norm = False
    for op in ops:
        name = type(op).__name__
        if isinstance(op, T.Resize):
            size = op.size
            if isinstance(size, (tuple, list)):
                if len(size) != 2:
                    return None
                plan.resize = (int(size[0]), int(size[1]))
            else:
                plan.short_edge = int(size)
            mode = _interp_name(getattr(op, "interpolation", None))
            if mode is None:
                return None
            plan.mode = mode
        elif isinstance(op, T.CenterCrop):
            size = op.size
            plan.crop = ((int(size), int(size)) if not isinstance(size, (tuple, list))
                         else (int(size[0]), int(size[1])))
        elif isinstance(op, T.Normalize):
            plan.mean = torch.as_tensor(op.mean, dtype=torch.float32).view(-1, 1, 1)
            plan.std = torch.as_tensor(op.std, dtype=torch.float32).view(-1, 1, 1)
            seen_norm = True
        elif isinstance(op, (T.ToTensor, T.PILToTensor, T.ConvertImageDtype)) or name in (
                "MaybeToTensor", "MaybePILToTensor"):
            continue
        else:
            return None
    if not seen_norm or plan.mean is None or plan.std is None:
        return None
    return plan


class TagBackend(ABC):
    """统一接口：给一张 PIL 图，回来的是一串 (tag, prob)，已按概率降序。"""

    name: str = "base"

    def __init__(self, *, keep: Sequence[str] = DEFAULT_KEEP,
                 thresholds: dict[str, float] | None = None,
                 max_tags: int = 160, verbose: bool = True,
                 precision: str = "fp32", batch_size: int = 1,
                 use_graph: bool = True, prefetch: bool = True,
                 preprocess: str = "pil"):
        # prefetch 只有 wd-eva02 真的用（它在 __init__ 里自己消费掉了）。
        # 但 build_backend 对所有后端都会传，而注释里"传进去不报错"的前提
        # 原先并不成立：pixai / cl / onnx 一路漏到基类，直接 TypeError。
        # 收在这里，调用方就不用按后端名字分枝。
        self.keep = tuple(keep)
        self.thresholds = dict(thresholds or {})
        self.max_tags = max_tags
        self.verbose = verbose
        # precision / batch_size 只有能落地的后端才认（见 WDEva02Tagger）。
        # 放在基类是为了让调用方无论传哪个后端都不会 TypeError。
        self.precision = normalize_precision(precision)
        self.batch_size = max(1, int(batch_size))
        # 预处理放哪。解码留在 CPU（逐张喂，nvJPEG 单张反而更慢），
        # 只有缩放+归一化这一段上卡；目前只有 wd-eva02 实现了。
        self.preprocess = normalize_preprocess(preprocess)
        # 基类只收着，不实现：CUDA Graph 目前只有 wd-eva02 有。
        self.use_graph = bool(use_graph)
        # 上一次 predict_batch 的耗时拆分（毫秒）。给 --stats-json 用：
        # "tag" 这一段混着解码和前向，只看合数根本判断不了该优化哪边——
        # 实测 PNG 解码 33 ms、前向 26~63 ms，比例随精度和格式翻来覆去。
        # 只有单 worker 调用（客户端的 tag 执行器就是），多线程读到的
        # 是"最近一次"，属于观测量，不参与任何正确性判断。
        self.last_timings: dict[str, int] = {"total": 0, "decode": 0, "forward": 0}
        # 子类在 _predict_many 里写 (解码 ms, 前向 ms)；基类负责清零。
        self._split_ms: tuple[int, int] = (0, 0)
        self._lock = threading.Lock()
        self._ready = False

    # -------------------------------------------------------------- 生命周期

    def load(self) -> None:
        """加载模型。子类实现 _load()。"""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self._load()
            self._ready = True

    @abstractmethod
    def _load(self) -> None: ...

    @abstractmethod
    def _predict_one(self, image) -> dict[str, float]: ...

    # -------------------------------------------------------------- 推理

    def predict(self, image) -> TagPrediction:
        """线程安全：整段上锁。

        CPU 推理本来就没有并行的收益（同一个模型多线程只会互相抢核，
        而且 tagger 的瓶颈是单张前向），串行反而更稳。
        """
        return self.predict_batch([image])[0]

    def predict_batch(self, images: Sequence) -> list[TagPrediction]:
        """一次算多张。

        为什么单独开一个口子而不是让调用方循环 predict：
        GPU 上前向的固定开销（kernel 发射、CPU dispatch）跟 batch 无关，
        79 张实测 fp32 单张 57.8 ms、batch=4 时 49.1 ms，换成 bf16 后
        才是 57.8 → 17.3 ms（3.3×）。**批处理的收益只在低精度下才真正
        兑现**，所以这两个参数必须能一起给。

        不攒批的后端（pixai / onnx）走基类默认实现，等价于循环调用，
        数值与 predict 逐张跑完全一致。
        """
        import time

        imgs = list(images)
        if not imgs:
            return []
        self.load()
        self._split_ms = (0, 0)
        t0 = time.perf_counter()
        with self._lock:
            raws = self._predict_many(imgs)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        # 拆分耗时。decode 是子类自己报的；没报（pixai / onnx）就把全部算进
        # forward——宁可粗，也不要凭空编一个解码占比出来。
        dec, fwd = self._split_ms
        self.last_timings = {
            "total": elapsed_ms,
            "decode": dec,
            "forward": fwd or max(0, elapsed_ms - dec),
        }
        # 单张时把整段耗时记在那一张上；多张时按张均摊，免得 latency_ms
        # 看着像"每张花了 N 倍时间"。吞吐看的是总时长，这里只是不让它误导。
        per = max(1, elapsed_ms // len(imgs))
        # 子类若额外产出了按张对齐的信号（当前只有 rating），这里顺手带上。
        ratings = list(getattr(self, "_last_ratings", ()) or ())
        out = []
        for i, raw in enumerate(raws):
            pairs = sorted(raw.items(), key=lambda kv: -kv[1])[: self.max_tags]
            out.append(TagPrediction(
                tags=[(t, round(float(p), 4)) for t, p in pairs],
                backend=self.name,
                latency_ms=per,
                rating=ratings[i] if i < len(ratings) else {},
            ))
        return out

    def _predict_many(self, images: Sequence) -> list[dict[str, float]]:
        """默认逐张跑。能真正攒批的后端覆写这个方法。"""
        return [self._predict_one(image) for image in images]

    def predict_raw(self, images: Sequence) -> list[dict[str, float]]:
        """原始分数：不筛类别、不过阈值。标定阈值要用它。

        只有能绕过"阈值过滤"这一层的后端才实现得了（目前是 wd-eva02）：
        pixai 的阈值在 transformers pipeline 里面，onnx 那套也是照配置裁剪，
        在这里假装支持只会给出误导性的结果。所以基类直接拒绝。
        """
        raise NotImplementedError(
            f"{self.name} 后端不支持原始分数（阈值在它自己的推理管线里）；"
            f"当前可用的是 wd-eva02")

    # -------------------------------------------------------------- 工具

    def _threshold_of(self, category: str) -> float:
        """类别阈值 → 通用阈值 → DEFAULT_THRESHOLD，逐级回退。"""
        if category in self.thresholds:
            return float(self.thresholds[category])
        return float(self.thresholds.get("default", DEFAULT_THRESHOLD))

    @staticmethod
    def _open_rgb(image):
        """接受 路径 / 文件对象 / bytes / PIL 图，统一回到 RGB 的 PIL 图。

        bytes 这一条是给客户端用的：客户端手里本来就是 zip 里的原始字节，
        让它先解一次、后端再 convert 一次，等于每张图多付一次全尺寸拷贝
        （1024² 上是几毫秒，白丢的）。**只在需要时才解码**——已经是 RGB
        的图不再 convert，PIL 的 convert 即使在 RGB 上也会整幅复制一遍。
        """
        from io import BytesIO

        from PIL import Image

        if isinstance(image, (bytes, bytearray, memoryview)):
            image = BytesIO(bytes(image))
        if not isinstance(image, Image.Image):
            image = Image.open(image)
            image.load()  # 文件句柄可能在调用方 with 块里，先读完再放
        if image.mode == "RGB":
            return image
        # 非 RGB 一律走 PIL 自己的 convert（与改动前逐像素一致）：
        # 这里只省掉"本来就已经是 RGB 还要复制一遍"的那次拷贝。
        return image.convert("RGB")


# ------------------------------------------------------------------ pixai


class PixAITagger(TagBackend):
    """pixai-labs/pixai-tagger-v1.0。

    README 给的用法就是 transformers pipeline + trust_remote_code，
    返回值按类别分组：{"general": {"1girl": 0.98, ...}, "character": {...}, ...}。
    阈值可以按类别给，也可以给一个数字（对全部类别生效）。
    """

    name = "pixai"

    def __init__(self, model_id: str = "pixai-labs/pixai-tagger-v1.0",
                 device: str = "cpu", **kw: Any):
        super().__init__(**kw)
        self.model_id = model_id
        self.device = device
        self._pipe = None

    def _load(self) -> None:
        try:
            from transformers import pipeline
        except ImportError as exc:
            raise TaggerUnavailable(
                "pixai tagger 需要 transformers/torch/Pillow。\n"
                "  一键补装：重跑 install.bat（Windows）/ ./install.sh（Linux），\n"
                "  它们默认会装 requirements-tag.txt；或手动：\n"
                "    python -m pip install -r requirements-tag.txt"
            ) from exc

        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "image_processor": self.model_id,
            "trust_remote_code": True,
        }
        if self.device and self.device != "cpu":
            kwargs["device"] = self.device
        else:
            kwargs["device"] = -1
        try:
            self._pipe = pipeline(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 远端代码出错的花样太多
            raise TaggerUnavailable(
                f"加载 {self.model_id} 失败：{type(exc).__name__}: {exc}\n"
                f"若网络受限，先手动下载：huggingface-cli download {self.model_id}"
            ) from exc
        if self.verbose:
            print(f"[tagger] 已加载 {self.model_id}（{self.device}）", flush=True)

    def _predict_one(self, image) -> dict[str, float]:
        img = self._open_rgb(image)
        # README 说 threshold 既可以是数字（全体生效），也可以是 {类别: 阈值} 的 dict。
        # 只配了 "default" 时给数字——把 "default" 当类别名传进 dict 的话，
        # pipeline 认不出这一类，等于没设阈值。
        thr: float | dict[str, float] | None
        if set(self.thresholds) == {"default"}:
            thr = float(self.thresholds["default"])
        else:
            thr = ({k: v for k, v in self.thresholds.items() if k != "default"} or None)
        results = self._pipe(img, threshold=thr)["results"] if thr \
            else self._pipe(img)["results"]
        out: dict[str, float] = {}
        for category, table in results.items():
            if self.keep and category not in self.keep:
                continue
            for tag, score in table.items():
                out[tag] = max(out.get(tag, 0.0), float(score))
        return out


# ------------------------------------------------------------------ wd-eva02


class WDEva02Tagger(TagBackend):
    """ashen-sensored/wd-eva02-tagger-2026-canary（timm，WD 布局）。

    仓库结构与 SmilingWolf 的 WD 系列一致：
        config.json          architecture / num_classes / pretrained_cfg
        model.safetensors    权重
        selected_tags.csv    tag_id,name,category,count

    预处理不手写：用 timm 的 resolve_data_config + create_transform 按
    config.json 里的 pretrained_cfg 生成（mean/std 0.5、crop_pct 1.0、
    center crop），手抄这些参数是最容易抄错的地方。
    """

    name = "wd-eva02"

    def __init__(self, model_id: str = "ashen-sensored/wd-eva02-tagger-2026-canary",
                 model_dir: str | Path | None = None, device: str = "cpu",
                 use_graph: bool = True, prefetch: bool = True, **kw: Any):
        super().__init__(**kw)
        self.model_id = model_id
        self.model_dir = Path(model_dir) if model_dir else None
        self.device = device
        self.use_graph = use_graph
        # 解码预取：CUDA 上前向时后台线程解下一批（见 _predict_many）
        self.prefetch = prefetch
        self._model = None
        self._transform = None
        self._graph: _CudaGraph | None = None
        self._names: list[str] = []
        self._categories: list[str] = []
        # (tag 下标, tag 名, 阈值) 的预摊平表，见 _filter_plan()
        self._plan: list[tuple[int, str, float]] = []
        self._plan_key: tuple | None = None
        # rating 的 (下标, 名字) 表，和最后一次预测里每张图的 rating 分数
        self._rating: list[tuple[int, str]] = []
        self._rating_key: tuple | None = None
        self._last_ratings: list[dict[str, float]] = []
        # 非 None 时 _filter 改用这张表（predict_raw 用）
        self._plan_override: list[tuple[int, str, float]] | None = None
        # 预处理上卡的计划（见 _tensor_prep_plan）；None = 走原 PIL 路径
        self._tensor_prep: _TensorPrep | None = None

    def _resolve_dir(self) -> Path:
        if self.model_dir is not None:
            return self.model_dir
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise TaggerUnavailable(
                "需要 huggingface_hub 才能按仓库名下载：pip install huggingface_hub"
            ) from exc
        return Path(snapshot_download(self.model_id))

    def _load(self) -> None:
        try:
            import timm
            import torch  # timm 需要，同时也是 precision/device 的实现依赖
            from safetensors.torch import load_file
        except ImportError as exc:
            raise TaggerUnavailable(
                "wd-eva02 tagger 需要 torch/timm/safetensors。\n"
                "  一键补装：重跑 install.bat（Windows）/ ./install.sh（Linux），\n"
                "  或手动：python -m pip install -r requirements-tag.txt"
            ) from exc

        root = self._resolve_dir()
        cfg_path = root / "config.json"
        csv_path = root / "selected_tags.csv"
        if not cfg_path.is_file() or not csv_path.is_file():
            raise TaggerUnavailable(
                f"{root} 不像 WD tagger 仓库（缺 config.json / selected_tags.csv）")

        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        arch = cfg.get("architecture") or "eva02_large_patch14_448"
        num_classes = int(cfg.get("num_classes") or 0)
        img_size = int((cfg.get("model_args") or {}).get("img_size") or 448)

        # 先把词表读完并和分类头对账，再加载 1.3 GB 权重：词表坏了要立刻报，
        # 不该等到读盘读了几十秒之后。
        self._names, self._categories = _read_wd_tags(csv_path, model_id=self.model_id)
        _check_vocab_against_head(len(self._names), num_classes,
                                  csv_path=csv_path, model_id=self.model_id)

        model = timm.create_model(arch, pretrained=False,
                                  num_classes=num_classes, img_size=img_size)
        weights = load_file(str(root / "model.safetensors"))
        missing, unexpected = model.load_state_dict(weights, strict=False)
        # 按键的**位置**判断，不按个数：分类头（head.*）允许缺——那是
        # num_classes 对齐时的正常差异；骨干键缺一个就说明权重整体对不上
        # （多半是下错了仓库）。之前"缺超过 8 个才报"的写法，碰上恰好缺
        # 6 个骨干键的错权重就放行了，产物看起来正常、内容全是随机头。
        bad_missing = [k for k in missing if not k.startswith("head.")]
        if bad_missing:
            raise TaggerUnavailable(
                f"权重与结构不匹配（骨干缺 {len(bad_missing)} 个键，"
                f"如 {bad_missing[0]}；多 {len(unexpected)} 个）。"
                f"确认 config.json 与 model.safetensors 来自同一次导出。")
        head_missing = [k for k in missing if k.startswith("head.")]
        if head_missing and not any(k.startswith("head.") for k in weights):
            # 骨干全对上了但分类头没加载：跑起来是随机初始化的头，
            # 产物"看着正常、内容全错"——这比加载失败更贵，必须拦。
            raise TaggerUnavailable(
                f"权重文件里没有分类头（缺 {', '.join(head_missing)}），"
                f"这样跑出来的 tag 全部来自随机初始化。权重导出不完整（{self.model_id}）。")
        model.eval()

        # 精度与设备必须在这里定死，不能留到 _predict_one 里再转换：
        #   · 之前这里漏了 model.to(device)，`--tag-device cuda:0` 对 wd-eva02
        #     是彻底的空转（模型和输入都留在 CPU），日志却照旧打印 "cuda:0"；
        #   · bf16 权重转换必须在 eval() 之后、加载完 transform 之前做完，
        #     放到前向里每张图转一遍等于白付一次 300M 元素的转换。
        if self.precision == "bf16":
            if self.device == "cpu":
                # CPU 上 bf16 没有 tensor core，实测反而更慢；直接回落，
                # 而不是让用户以为开了加速。
                if self.verbose:
                    print("[tagger] CPU 上 bf16 无收益，已回落 fp32", flush=True)
                self.precision = "fp32"
            else:
                model = model.to(dtype=torch.bfloat16)
        if self.device and self.device != "cpu":
            model = model.to(self.device)
        self._apply_precision_flags()
        self._model = model

        data_cfg = timm.data.resolve_data_config({}, model=model)
        self._transform = timm.data.create_transform(**data_cfg, is_training=False)
        # 预处理上卡：读出 timm transform 的等价步骤。读不出来（换了算子、
        # 或者本来就没装 torchvision）就回落 PIL，绝不自己写死一套参数。
        self._tensor_prep = None
        if self.preprocess == "tensor":
            if not str(self.device).startswith("cuda"):
                if self.verbose:
                    print(f"[tagger] 预处理 tensor 需要 CUDA（当前 {self.device}），"
                          "已回落 PIL", flush=True)
            else:
                try:
                    self._tensor_prep = _tensor_prep_plan(self._transform)
                except Exception as exc:  # noqa: BLE001 - 认不出来就回落
                    self._tensor_prep = None
                    if self.verbose:
                        print(f"[tagger] 解析 timm transform 失败，回落 PIL："
                              f"{type(exc).__name__}: {exc}", flush=True)
                if self._tensor_prep is None and self.verbose:
                    print("[tagger] 这套 transform 不在支持列表（Resize/CenterCrop/"
                          "Normalize），已回落 PIL", flush=True)

        self._maybe_capture_graph(img_size)
        if self.verbose:
            extra = f"，{self.precision}" if self.precision != "fp32" else ""
            if self.batch_size > 1:
                extra += f"，batch={self.batch_size}"
            print(f"[tagger] 已加载 {arch}（{len(self._names)} 个 tag，"
                  f"{self.device}{extra}）", flush=True)

    def _maybe_capture_graph(self, img_size: int) -> None:
        """batch>1 且在 CUDA 上时，抓一张固定形状的图。

        只在满批时用（见 _predict_many）。这里是"尽量上，上不了就算"：
        抓图会因为驱动/显存/自定义算子失败，而失败不该让整个打标崩掉——
        eager 路径本来就对。
        """
        self._graph = None
        if not self.use_graph or self.batch_size <= 1:
            return
        if not str(self.device).startswith("cuda"):
            return

        import torch

        if not torch.cuda.is_available():
            return
        try:
            dtype = torch.bfloat16 if self.precision == "bf16" else torch.float32
            static_in = torch.zeros(self.batch_size, 3, img_size, img_size,
                                    device=self.device, dtype=dtype)
            # 抓图前必须在旁路 stream 上热身：图会把第一次走到的分支和算法
            # 固化下来，热身不足的话固化的是一次没走完的路径。
            side = torch.cuda.Stream()
            side.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(side), torch.no_grad():
                for _ in range(3):
                    self._model(static_in)
            torch.cuda.current_stream().wait_stream(side)
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph), torch.no_grad():
                static_out = self._model(static_in)
            self._graph = _CudaGraph(graph, static_in, static_out, self.batch_size)
            if self.verbose:
                print(f"[tagger] CUDA Graph 已就绪（batch={self.batch_size}）", flush=True)
        except Exception as exc:  # noqa: BLE001 - 抓图失败就回落，不能拖垮打标
            self._graph = None
            if self.verbose:
                print(f"[tagger] CUDA Graph 抓不了，回落 eager："
                      f"{type(exc).__name__}: {exc}", flush=True)

    def _apply_precision_flags(self) -> None:
        """tf32 是进程级开关，只能在加载时设一次。

        PyTorch 1.12 起 allow_tf32 默认 False，等于白白丢掉 tensor core；
        wd-eva02 是卷积分类头，吃满这个开关没有副作用（实测 0 翻面）。
        """
        import torch

        on = self.precision in ("tf32", "bf16") and self.device != "cpu"
        torch.backends.cuda.matmul.allow_tf32 = on
        torch.backends.cudnn.allow_tf32 = on

    def _filter_plan(self) -> list[tuple[int, str, float]]:
        """把"要哪个 tag、下标多少、阈值多少"摊平成一张表并缓存。

        原来每张图要在 16473 个 tag 上逐个走 `category not in keep` +
        `_threshold_of(category)`（每张两次查表 + 一次函数调用）。bf16 之后
        前向只要 17 ms，这个循环却能吃掉十几毫秒——"前向快 3.3×、端到端
        只有 1.33×"里丢掉的收益，一半在这里。
        """
        key = (self.keep, tuple(sorted(self.thresholds.items())))
        if self._plan_key != key:
            plan: list[tuple[int, str, float]] = []
            for idx, (name, category) in enumerate(zip(self._names, self._categories)):
                if self.keep and category not in self.keep:
                    continue
                plan.append((idx, name, self._threshold_of(category)))
            self._plan, self._plan_key = plan, key
        return self._plan

    def _rating_plan(self) -> list[tuple[int, str]]:
        """rating 那几行单独一张表：不过阈值，四个分数全要。

        判定分级靠的是四个分数的相对大小（argmax），不是"过没过 0.35"。
        按阈值过滤会把 0.31 / 0.28 / 0.25 / 0.16 这种真实的分布削成空，
        而这个分布正是数据集清洗要用的东西。
        """
        key = ("rating", tuple(self._names))
        if self._rating_key != key:
            self._rating = [(i, n) for i, (n, c) in
                            enumerate(zip(self._names, self._categories))
                            if c == "rating"]
            self._rating_key = key
        return self._rating

    def _filter(self, probs: Sequence[float]) -> dict[str, float]:
        # predict_raw 会临时把 plan 换成全量表（见 predict_raw 的说明）
        plan = self._plan_override if self._plan_override is not None else self._filter_plan()
        out: dict[str, float] = {}
        for idx, name, thr in plan:
            prob = probs[idx]
            if prob >= thr:
                out[name] = float(prob)
        return out

    def _decode_batch(self, part: Sequence) -> tuple[list, int]:
        """解码（+ PIL 预处理）一小批，返回 (张量列表, 本批耗时 ms)。

        耗时在**这一层**量：预取时它跑在后台线程里，主线程去等 future
        只能量到"还要等多久"，把那个当解码耗时会让 `tag_decode` 越跑越小。

        tensor 预处理模式下这里只做到"uint8 CHW 张量"：解码本来就是 CPU
        活（nvJPEG 逐张比 PIL 还慢），而缩放/归一化挪到 _forward_chunk 里
        上卡——这样预取仍然能拿 CPU 解码去盖 GPU 前向，不会白丢重叠。
        """
        import time

        t0 = time.perf_counter()
        if self._tensor_prep is not None:
            tensors = [self._pixels_uint8(im) for im in part]
        else:
            tensors = [self._transform(self._open_rgb(im)) for im in part]
        return tensors, int((time.perf_counter() - t0) * 1000)

    def _pixels_uint8(self, image):
        """解码到 uint8 CHW 张量（仍在 CPU），给 tensor 预处理的入口用。

        为什么先转张量再上卡：H2D 一次 3 MB 左右，比在 CPU 上做 bicubic
        缩放（1024² 约 7 ms）便宜一个数量级。
        """
        import numpy as np
        import torch

        rgb = self._open_rgb(image)
        # np.array（不是 asarray）：PIL 的数组视图是只读的，torch.from_numpy
        # 对只读数组会告警，而且我们后面的 permute/切片都要求可写。
        return torch.from_numpy(np.array(rgb)).permute(2, 0, 1)

    def _prep_on_device(self, pixels):
        """uint8 CHW（CPU）→ 归一化后的 float CHW（GPU），等价于 timm 的 transform。

        数值口径：缩放与归一化的**算式**与 timm 一致（/255 → (x-mean)/std），
        但重采样实现换了——PIL 的 Lanczos/bicubic 与 F.interpolate 的
        bicubic+antialias 不是同一套核，像素会有 ±0.3（归一化后）的差，
        tag 集合约 1% 会变。所以这是 opt-in，默认仍走 PIL。
        """
        import torch
        import torch.nn.functional as F

        plan = self._tensor_prep
        x = pixels.to(self.device, non_blocking=True).unsqueeze(0).float()
        height, width = x.shape[-2:]
        target = plan.target_size(height, width)
        if target != (height, width):
            if plan.mode == "nearest":
                x = F.interpolate(x, size=target, mode="nearest")
            else:
                x = F.interpolate(x, size=target, mode=plan.mode,
                                  antialias=True, align_corners=False)
        if plan.crop is not None:
            crop_h, crop_w = plan.crop
            cur_h, cur_w = x.shape[-2:]
            if crop_h < cur_h or crop_w < cur_w:
                top = int(round((cur_h - crop_h) / 2.0))
                left = int(round((cur_w - crop_w) / 2.0))
                x = x[:, :, top:top + crop_h, left:left + crop_w]
        x = x.div_(255.0)
        x = x.sub_(plan.mean.to(x.device)).div_(plan.std.to(x.device))
        return x[0]

    def _forward_chunk(self, tensors: Sequence) -> tuple[list[dict[str, float]],
                                                        list[dict[str, float]], int]:
        """一小批前向 → (过滤后的 tag, rating 分数, 前向耗时 ms)。"""
        import time

        import torch

        graph = self._graph
        # 计时从前向这一段的入口就开始：tensor 模式下这里包着 H2D + 缩放 +
        # 归一化，全是 GPU 上的实打实时间（占的也是前向那条流）；PIL 模式下
        # 包着 stack 与 H2D。两种都属于 forward，算进 decode 会让 tag_decode
        # 凭空变小、看着像解码变快了。
        tf0 = time.perf_counter()
        if self._tensor_prep is not None:
            x = torch.stack([self._prep_on_device(t) for t in tensors])
        else:
            x = torch.stack(list(tensors))
            if self.device and self.device != "cpu":
                x = x.to(self.device)
        if self.precision == "bf16":
            x = x.to(torch.bfloat16)
        with torch.no_grad():
            if graph is not None and x.shape[0] == graph.batch:
                # CUDA Graph：把这一批的 1480 次 kernel 发射压成一次回放。
                # 只认满批——图形状是固定的，尾批（不足一批）走 eager，
                # 别为了用图去补零，补出来的假像素本身就不该进模型。
                graph.static_in.copy_(x)
                graph.replay()
                logits = graph.static_out
            else:
                logits = self._model(x)
            # sigmoid 在 fp32 做：bf16 的 sigmoid 会把 0.35 附近的分数
            # 压得更粗，而阈值判定恰恰只在这一段敏感。
            probs = torch.sigmoid(logits.clone().float()).cpu().tolist()
        fwd_ms = int((time.perf_counter() - tf0) * 1000)

        rating_plan = self._rating_plan()
        tags: list[dict[str, float]] = []
        ratings: list[dict[str, float]] = []
        for row in probs:
            tags.append(self._filter(row))
            if rating_plan:
                ratings.append({name: round(float(row[idx]), 4)
                                for idx, name in rating_plan})
        return tags, ratings, fwd_ms

    def _predict_many(self, images: Sequence) -> list[dict[str, float]]:
        """按批跑；CUDA 上前向时用后台线程预取下一批的解码。

        为什么要预取：解码+预处理是纯 CPU（PNG 一张 33 ms），前向在 GPU 上
        只要 18~26 ms。串行做是两者相加，重叠之后取 max——省下来的正好是
        较小那一项。像素**逐位不变**：还是同一条 PIL 路径，只是挪了时间位置。
        CPU 上不预取：两边抢同一批核，重叠只会互相拖。

        preprocess="tensor" 时预取照样有效：解码仍在 CPU（这里只做到 uint8
        张量），缩放/归一化在前向那条流上。代价是像素不再逐位相同——见
        _prep_on_device 的说明。
        """
        chunk = max(1, self.batch_size)
        parts = [list(images[i:i + chunk]) for i in range(0, len(images), chunk)]
        self._last_ratings = []

        use_prefetch = (self.prefetch and len(parts) > 1
                        and str(self.device).startswith("cuda"))
        out: list[dict[str, float]] = []
        decode_ms = 0
        fwd_ms = 0

        if not use_prefetch:
            for part in parts:
                tensors, dms = self._decode_batch(part)
                tags, ratings, fms = self._forward_chunk(tensors)
                decode_ms += dms
                fwd_ms += fms
                out.extend(tags)
                self._last_ratings.extend(ratings)
        else:
            from concurrent.futures import ThreadPoolExecutor

            # 单 worker、最多两批在途（当前在算 + 下一批在解）：
            # 一次把整批都提交出去会把 79 张解好的张量全留在内存里（约 190 MB）。
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="tag-prefetch") as pool:
                cur = pool.submit(self._decode_batch, parts[0])
                for i, _ in enumerate(parts):
                    nxt = (pool.submit(self._decode_batch, parts[i + 1])
                           if i + 1 < len(parts) else None)
                    tensors, dms = cur.result()
                    decode_ms += dms
                    tags, ratings, fms = self._forward_chunk(tensors)
                    fwd_ms += fms
                    out.extend(tags)
                    self._last_ratings.extend(ratings)
                    cur = nxt

        # 注意：预取生效时 decode 与 forward **互相重叠**，两者相加会大于
        # 墙钟 total。这不是统计 bug，是重叠的直接体现——判断是否还在串行，
        # 看 decode+forward 是否明显大于 total 即可。
        self._split_ms = (decode_ms, fwd_ms)
        return out

    def predict_raw(self, images: Sequence) -> list[dict[str, float]]:
        """原始分数：不筛类别、不过阈值（保持顺序，与入参一一对应）。

        实现上给 `_filter` 临时塞一张全量 plan（keep 为空、阈值 0），
        算完在 finally 里还原；tag 执行器是单 worker，不存在并发改写。
        """
        self.load()
        plan = [(i, name, 0.0) for i, name in enumerate(self._names)]
        with self._lock:
            saved = self._plan_override
            self._plan_override = plan
            try:
                return self._predict_many(list(images))
            finally:
                self._plan_override = saved

    def _predict_one(self, image) -> dict[str, float]:
        return self._predict_many([image])[0]


def _read_wd_tags(path: Path, *, model_id: str) -> tuple[list[str], list[str]]:
    """读 selected_tags.csv → (名字, 类别名)。

    只依赖标准库：csv 三列就够，没必要为此拉 pandas。
    """
    import csv

    names: list[str] = []
    cats: list[str] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tag_id = row.get("tag_id")
            name = (row.get("name") or "").strip()
            if not name:
                # 名字为空的行如果直接跳过，后面每个 tag 的下标都会往前挪一格，
                # 而分类头的输出是按行序对齐的 —— 那等于把整张词表错位。
                raise TaggerUnavailable(
                    f"{path} 第 {len(names) + 2} 行（tag_id={tag_id}）没有 name。"
                    f"空名字会让词表与分类头错位，必须先修好 csv（{model_id}）。")
            try:
                idx = int(tag_id) if tag_id not in (None, "") else len(names)
            except ValueError as exc:
                raise TaggerUnavailable(
                    f"{path} 第 {len(names) + 2} 行的 tag_id 不是整数：{tag_id!r}"
                    f"（{model_id}）") from exc
            category = WD_CATEGORY.get(int(row.get("category") or 0), "general")
            if idx != len(names):
                # 这里以前是"按 id 落位、中间补空名字"，补出来的空 tag 会一路
                # 传到产物里（`_filter` 不过滤空名）。行序与 tag_id 不一致本身
                # 就是导出坏了的信号，直接报。
                raise TaggerUnavailable(
                    f"{path} 的 tag_id 不连续：第 {len(names) + 2} 行的 tag_id="
                    f"{idx}，但前面只有 {len(names)} 行。词表与分类头的顺序对不上，"
                    f"不能用来打标（{model_id}）。")
            names.append(name)
            cats.append(category)
    if not names:
        raise TaggerUnavailable(f"{path} 里没有读到任何 tag（{model_id}）")
    return names, cats


def _check_vocab_against_head(count: int, num_classes: int, *,
                              csv_path: Path, model_id: str) -> None:
    """词表行数必须等于分类头宽度。

    为什么必须挡：`_filter` 是 `zip(names, probs, categories)`，两边长度不一致
    时 zip 只会**静默截断**——若 csv 比头多，末尾的 tag 永远不出；若头比 csv 多，
    截断点之前的对应关系可能整体错位一格。产物看起来完全正常（tag 都认识），
    只有人工比对才能发现，30M 张的规模上等于永久污染。
    """
    if num_classes <= 0:
        raise TaggerUnavailable(
            f"config.json 里没有可用的 num_classes（读到 {num_classes}）。"
            f"缺了它 timm 会按默认 1000 类建头，权重必然对不上（{model_id}）。")
    if count != num_classes:
        raise TaggerUnavailable(
            f"词表与分类头对不上：selected_tags.csv 有 {count} 行，"
            f"config.json 的 num_classes 是 {num_classes}（{csv_path}）。\n"
            f"  任一方向的差异都会让 tag 与概率错位，且不会报错。\n"
            f"  请确认 csv 与 model.safetensors 来自同一次导出（{model_id}）。")


# ------------------------------------------------------------------ onnx


class OnnxTagger(TagBackend):
    """cella110n/cl_tagger_v2（onnxruntime）。

    这个仓库的预处理参数写在 model_metadata.json 里，字段名可能随版本变，
    所以这里全部从文件读、读不到就用最保守的默认（448 / 0.5 / 0.5），
    并把实际用到的一组参数打出来——跑偏了能立刻看出是哪一项不对。
    """

    name = "cl-tagger-v2"

    def __init__(self, model_id: str = "cella110n/cl_tagger_v2",
                 subdir: str = "v2_01a", model_dir: str | Path | None = None,
                 token: str | None = None, device: str = "cpu", **kw: Any):
        super().__init__(**kw)
        self.model_id = model_id
        self.subdir = subdir
        self.model_dir = Path(model_dir) if model_dir else None
        self.token = token
        self.device = device
        self._session = None
        self._input_name = ""
        self._names: list[str] = []
        # 与 _names 对齐的类别名。词表没带类别信息时全部记 "general"，
        # keep 过滤与按类阈值自然退化成旧行为（等于不过滤）。
        self._cats: list[str] = []
        self._size = 448
        self._mean = (0.5, 0.5, 0.5)
        self._std = (0.5, 0.5, 0.5)

    def _resolve_dir(self) -> Path:
        root = self.model_dir
        if root is None:
            try:
                from huggingface_hub import snapshot_download
            except ImportError as exc:
                raise TaggerUnavailable(
                    "需要 huggingface_hub：pip install huggingface_hub") from exc
            root = Path(snapshot_download(
                self.model_id, allow_patterns=[f"{self.subdir}/*"], token=self.token))
        target = root / self.subdir if (root / self.subdir).is_dir() else root
        return target

    def _load(self) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise TaggerUnavailable(
                "onnx 后端需要 onnxruntime：pip install onnxruntime") from exc

        import numpy as np

        root = self._resolve_dir()
        onnx_path = root / "model.onnx"
        vocab_path = root / "model_vocabulary.json"
        meta_path = root / "model_metadata.json"
        if not onnx_path.is_file():
            raise TaggerUnavailable(f"{root} 下没有 model.onnx（子目录选错了？）")

        providers = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                     if self.device and self.device != "cpu"
                     else ["CPUExecutionProvider"])
        self._session = ort.InferenceSession(str(onnx_path), providers=providers)
        self._input_name = self._session.get_inputs()[0].name

        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self._size = int(meta.get("input_size") or meta.get("image_size") or self._size)
            mean = meta.get("mean") or meta.get("pixel_mean")
            std = meta.get("std") or meta.get("pixel_std")
            if mean:
                self._mean = tuple(float(x) for x in mean)[:3]
            if std:
                self._std = tuple(float(x) for x in std)[:3]

        if not vocab_path.is_file():
            raise TaggerUnavailable(f"{root} 下没有 model_vocabulary.json")
        self._names, self._cats = _read_vocab(vocab_path)
        if len(self._names) != len(self._cats):
            # 理论上到不了这里（_read_vocab 保证等长），但词表与输出 logits
            # 的对齐是这个后端的生命线，多一道便宜的断言值得。
            raise TaggerUnavailable(
                f"词表解析异常：{len(self._names)} 个名字 vs {len(self._cats)} 个类别"
                f"（{vocab_path}）")
        if self.verbose:
            kept = sum(1 for c in self._cats if not self.keep or c in self.keep)
            print(f"[tagger] 已加载 onnx（{len(self._names)} 个 tag，"
                  f"{kept} 个在 keep 类别内，"
                  f"输入 {self._size}，mean={self._mean}）", flush=True)

    def _predict_one(self, image) -> dict[str, float]:
        import numpy as np

        img = self._open_rgb(image).resize((self._size, self._size))
        arr = np.asarray(img, dtype="float32") / 255.0
        arr = (arr - np.array(self._mean, dtype="float32")) / np.array(self._std, dtype="float32")
        arr = arr.transpose(2, 0, 1)[None, ...]           # NCHW

        logits = self._session.run(None, {self._input_name: arr})[0]
        probs = 1.0 / (1.0 + np.exp(-np.asarray(logits).reshape(-1)))

        out: dict[str, float] = {}
        for name, cat, prob in zip(self._names, self._cats, probs):
            if not name:
                continue    # 词表里的空位：跳过会错位，占位但不出 tag
            if self.keep and cat not in self.keep:
                # 与 wd-eva02 同一条规则：rating/artist 类不进 tag 串。
                # 之前这个后端没做类别过滤，纯色图会吐出 `explicit 0.62`。
                continue
            p = float(prob)
            if p < self._threshold_of(cat):
                continue
            out[name] = p
        return out


def _read_vocab(path: Path) -> tuple[list[str], list[str]]:
    """词表 JSON 的形态各仓库不一，统一读成 (名字, 类别) 两条等长序列。

    支持：list[str] / list[{"name":...}] / {"tags":[...]} / {"0": "1girl", ...}。
    条目带 category 字段时按 WD_CATEGORY 的整数编号或字符串名归一；
    没有类别信息的一律记 "general"，让 keep 过滤退化为不过滤。

    空名字**占位不丢弃**：这里丢一个，后面所有 tag 的下标就往前挪一格，
    与分类头的输出整体错位——错位的词表比空 tag 贵得多。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_items: list = []
    if isinstance(data, dict):
        for key in ("tags", "names", "vocabulary", "labels"):
            if isinstance(data.get(key), list):
                raw_items = data[key]
                break
        else:
            # {"0": "1girl", ...} 这种以 id 为键的写法
            items = sorted(data.items(),
                           key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0)
            raw_items = [v for _, v in items]
    elif isinstance(data, list):
        raw_items = data

    names: list[str] = []
    cats: list[str] = []
    for item in raw_items:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("tag")
                       or item.get("label") or "").strip()
            cat = _norm_category(item.get("category") or item.get("cat"))
        else:
            name = str(item).strip()
            cat = ""
        names.append(name)
        cats.append(cat or "general")
    return names, cats


def _norm_category(value) -> str:
    """词表里的类别可能是 0/4/9 这样的整数编号，也可能是字符串名。"""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return WD_CATEGORY.get(int(value), "")
    return str(value).strip().lower()


# ------------------------------------------------------------------ 工厂


BACKENDS = {
    "pixai": lambda **kw: PixAITagger(**kw),
    "wd-eva02": lambda **kw: WDEva02Tagger(**kw),
    "cl-tagger-v2": lambda **kw: OnnxTagger(**kw),
    "onnx": lambda **kw: OnnxTagger(**kw),
}


def resolve_device(pref: str | None) -> str:
    """把 `auto` 解析成具体设备。

    UI 上给一个下拉框（auto / cpu / cuda:0 / cuda:1）就够了，不用让用户
    去猜自己有没有卡——auto 走探测，有人插了两张卡也能直接写 cuda:1。
    """
    p = (pref or "auto").strip().lower()
    if p and p != "auto":
        return p
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda:0"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001 - 没装 torch 就走 cpu，不用报错
        pass
    return "cpu"


def build_backend(
    backend: str,
    *,
    model: str | None = None,
    model_dir: str | Path | None = None,
    device: str = "cpu",
    keep: Sequence[str] | None = None,
    thresholds: dict[str, float] | None = None,
    max_tags: int = 160,
    token: str | None = None,
    verbose: bool = True,
    precision: str = "fp32",
    batch_size: int = 1,
    use_graph: bool = True,
    prefetch: bool = True,
    preprocess: str = "pil",
) -> TagBackend | None:
    """按名字建后端。`none` / 空串返回 None（表示 tag 走 VLM）。"""
    key = (backend or "").strip().lower()
    if key in ("", "none", "vlm"):
        return None
    if key not in BACKENDS:
        raise ValueError(
            f"未知 tag 后端 {backend!r}，可选：{', '.join(sorted(BACKENDS))}, none")

    kw: dict[str, Any] = {
        "keep": tuple(keep) if keep else DEFAULT_KEEP,
        "thresholds": thresholds,
        "max_tags": max_tags,
        "verbose": verbose,
        # 基类统一收下：不认识这两个参数的后端（这里指没覆写 _predict_many 的）
        # 会退化成逐张跑，行为与加参数之前一字不差。
        "precision": precision,
        "batch_size": batch_size,
        "preprocess": preprocess,
    }
    if model:
        kw["model_id"] = model
    # model_dir 只有能"按目录加载"的后端才认（pixai 走 transformers 的缓存，
    # 传进来会一路漏进基类构造器变成 TypeError）。
    if model_dir and key in ("wd-eva02", "cl-tagger-v2", "onnx"):
        kw["model_dir"] = model_dir
    if device:
        kw["device"] = device
    if token and key.startswith(("cl", "onnx")):
        kw["token"] = token
    # CUDA Graph / 解码预取只有 wd-eva02 实现了，别的后端收了也没用；但传进去
    # 不报错比按名字分支更稳（将来 onnx 想接也不用改这里）。
    kw["use_graph"] = use_graph
    kw["prefetch"] = prefetch

    obj = BACKENDS[key](**kw)
    if verbose:
        print(f"[tagger] tag 后端 {obj.name}（模型 {model or '默认'}，{device}）", flush=True)
    return obj
