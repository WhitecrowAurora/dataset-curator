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
            默认后端，所以批处理与 tf32 也接了（见 PixAITagger._predict_many）。
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

from .tag_preprocess import normalize_preprocess, tensor_prep_plan as _tensor_prep_plan

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
    """把各种写法归一成 fp32 / tf32 / bf16 / fp16。

    各档的取舍（79 张真实素材、真权重、阈值 0.35 实测，详见
    devtools/perf-baseline.md §10）：
        fp32  基线，零漂移。
        tf32  fp32 权重 + tensor core，79 张 **0 个 tag 真改判**、概率最大漂移
              0.0027，前向 1.12×（b1）/ 1.47×（b4）。可以当默认。
        bf16  权重也转 bf16，前向 1.74×（b1）/ 2.36×（b4），但 3002 个选中
              tag 里有 2 个真改判（脱离阈值 ±0.01 边界带）。
              这个数字对"抓训练集"是可接受的抖动，对"以后要归因的口径"不是，
              所以默认不开，要快的人自己开。
        fp16  **只为 V100 这类没有 bf16 的卡存在**。Volta（sm_70）缺 bf16 的
              硬件支持，但 fp16 tensor core 正是它的主力；对这群用户，
              fp32 慢、bf16 跑不了，fp16 是唯一能吃到 tensor core 的选择。

    ★fp16 与 bf16 的风险性质不同，不能当作"都是半精度"混为一谈：
      fp16 只有 5 位指数（上界 65504），ViT 里的 softmax / LayerNorm 在
      极端输入下有溢出成 inf 的风险；bf16 是 8 位指数、动态范围与 fp32 相同，
      不会溢出，代价是尾数只有 7 位。所以 fp16 更要靠 tag 一致性校验兜底
      ——"V100 上没有别的选择"不等于"不用验证"。
    """
    p = (value or "fp32").strip().lower()
    if p in ("", "fp32", "float32", "off", "none", "full"):
        return "fp32"
    if p in ("tf32", "tensorfloat32"):
        return "tf32"
    if p in ("bf16", "bfloat16"):
        return "bf16"
    if p in ("fp16", "float16", "half"):
        return "fp16"
    raise ValueError(f"未知精度 {value!r}，可选：fp32 / tf32 / bf16 / fp16")


def half_dtype(precision: str):
    """precision 对应的半精度 torch dtype；fp32 / tf32 返回 None（不转权重）。

    集中在一处，是因为"把权重和输入转成半精度"这件事散在四个地方
    （pixai 的 pipeline、wd-eva02 的 _load、CUDA Graph 的静态输入、
    每次前向的输入张量），漏掉任何一处都会变成"权重 fp16 输入 fp32"
    的隐式提升，看着能跑，速度却没了一半。
    """
    import torch

    p = normalize_precision(precision)
    if p == "bf16":
        return torch.bfloat16
    if p == "fp16":
        return torch.float16
    return None


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


class TagBackend(ABC):
    """统一接口：给一张 PIL 图，回来的是一串 (tag, prob)，已按概率降序。"""

    name: str = "base"

    def __init__(self, *, keep: Sequence[str] = DEFAULT_KEEP,
                 thresholds: dict[str, float] | None = None,
                 # 判别式模型的 top-N 上限，与配置的 output.tag_max_count **不是
                 # 同一个东西**（那条是 VLM 生成 tag 的 token 预算），别试图统一。
                 # 详见 build_backend 的同名参数。
                 max_tags: int = 160, verbose: bool = True,
                 precision: str = "fp32", batch_size: int = 1,
                 use_graph: bool = True, prefetch: bool = True,
                 preprocess: str = "pil"):
        self.keep = tuple(keep)
        self.thresholds = dict(thresholds or {})
        self.max_tags = max_tags
        self.verbose = verbose
        # precision / batch_size 只有能落地的后端才认（见 WDEva02Tagger）。
        # 放在基类是为了让调用方无论传哪个后端都不会 TypeError。
        self.precision = normalize_precision(precision)
        self.batch_size = max(1, int(batch_size))
        # 基类只收着，不实现：CUDA Graph 目前只有 wd-eva02 有。
        self.use_graph = bool(use_graph)
        self.preprocess = normalize_preprocess(preprocess)
        self.prefetch = bool(prefetch)
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

    def rescore_last(self, index: int,
                     thresholds: dict[str, float] | None) -> dict[str, float]:
        """用上一次前向的分数快照，换一套阈值重筛第 index 张（不重跑前向）。

        为什么不用 `predict_batch_with`：那个会真的再前向一遍。tag 与 VLM 是
        并行起的，等 `tag_future.result()` 时才拿到分数；若此时发现"这张恰好
        是被截断的那张"，为了换条线再烧一次前向毫无意义——分数就在手上。

        取不到快照（后端不支持、或 index 越界）返回空 dict，调用方自己退回
        到"直接放弃"。
        """
        probs = getattr(self, "last_probs", None)
        if not probs or not (0 <= index < len(probs)):
            return {}
        saved_thr, saved_key = self.thresholds, self._plan_key
        try:
            self.thresholds = dict(thresholds or {})
            self._plan_key = None
            return self._filter(probs[index])
        finally:
            self.thresholds = saved_thr
            self._plan_key = saved_key

    def predict_batch_with(self, images: Sequence,
                          thresholds: dict[str, float] | None
                          ) -> list[TagPrediction]:
        """临时换一套阈值再算一批，算完还原（保持顺序，与入参一一对应）。

        为什么需要它：CPU 分类头是**确定性**的，同一张图重跑拿到的分数一个
        不差。所以"tag 过少就再来一遍"必须实现成**对同一份分数换条判定线再
        筛一次**，否则只是烧一遍前向换回同一份结果。

        与 `predict_raw` 一样走 `_plan_override`，单 worker 执行器里不存在
        并发改写；区别是这里保留后端自己的类别过滤（keep）与 max_tags。
        """
        self.load()
        with self._lock:
            saved_plan = self._plan_override
            saved_thr = self.thresholds
            try:
                self.thresholds = dict(thresholds or {})
                self._plan_key = None            # 逼 _filter_plan 按新阈值重建
                self._plan_override = None
                return self.predict_batch(list(images))
            finally:
                self._plan_override = saved_plan
                self.thresholds = saved_thr
                self._plan_key = None

    def predict_batch(self, images: Sequence) -> list[TagPrediction]:
        """一次算多张。

        为什么单独开一个口子而不是让调用方循环 predict：
        GPU 上前向的固定开销（kernel 发射、CPU dispatch）跟 batch 无关，
        79 张实测 fp32 单张 57.8 ms、batch=4 时 49.1 ms，换成 bf16 后
        才是 57.8 → 17.3 ms（3.3×）。**批处理的收益只在低精度下才真正
        兑现**，所以这两个参数必须能一起给。

        不攒批的后端（当前是 onnx）走基类默认实现，等价于循环调用，
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

    def _apply_precision_flags(self) -> None:
        """tf32 的进程级开关。子类在模型上卡之后、第一次前向之前调用一次。

        为什么非显式打开不可：PyTorch 1.12 起 `cuda.matmul.allow_tf32`
        默认 False，Ampere 以上的 tensor core 全程闲置。对一个 fp32 的
        分类头来说这不是小数——GEMM 本来可以走 tf32，白白多花近一倍时间，
        而权重、输入、判定阈值一个字都没变（零漂移）。

        ★这是**进程级**开关，不属于某个后端：同一进程里建两个不同精度的
        后端时，后加载的那份决定全局值。当前每次运行只 build 一个后端
        （见 build_backend 的调用点），所以不构成问题；将来要在一张卡上
        同时挂两个后端，必须先想清楚这一条。

        `device` 只有子类存（建后端时传进来的那张卡），基类没有，所以用
        getattr 取。CPU 上恒为 False——那里没有 tensor core，开了只是自欺。
        """
        import torch

        device = str(getattr(self, "device", "cpu") or "cpu")
        on = self.precision in ("tf32", "bf16") and device != "cpu"
        torch.backends.cuda.matmul.allow_tf32 = on
        torch.backends.cudnn.allow_tf32 = on

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


# ------------------------------------------------------------------ 本地目录

# transformers 从目录加载时**必需**的几个文件。少一个就不是"一个模型目录"，
# 传进去只会得到一句 "does not appear to have a file named config.json"，
# 而这时按 HF 缓存加载反而是能跑的。
_MODEL_REQUIRED = ("config.json", "preprocessor_config.json")
# 权重：safetensors 或 pytorch_model.bin 二者其一即可（官方发的是 safetensors，
# 但量化/转换出来的目录常常只有 bin）。
_MODEL_WEIGHTS = ("*.safetensors", "pytorch_model.bin")


def _missing_model_files(root: Path) -> list[str]:
    """本地模型目录还缺哪些必需文件。空 = 够用了，可以完全离线加载。"""
    if not root.is_dir():
        return ["（目录不存在）"]
    missing = [name for name in _MODEL_REQUIRED if not (root / name).is_file()]
    if not any(next(iter(root.glob(pat)), None) for pat in _MODEL_WEIGHTS):
        missing.append("model.safetensors")
    return missing


# ------------------------------------------------------------------ pixai


class PixAITagger(TagBackend):
    """pixai-labs/pixai-tagger-v1.0。

    README 给的用法就是 transformers pipeline + trust_remote_code，
    返回值按类别分组：{"general": {"1girl": 0.98, ...}, "character": {...}, ...}。
    阈值可以按类别给，也可以给一个数字（对全部类别生效）。
    """

    name = "pixai"

    def __init__(self, model_id: str = "pixai-labs/pixai-tagger-v1.0",
                 model_dir: str | Path | None = None,
                 device: str = "cpu", **kw: Any):
        super().__init__(**kw)
        self.model_id = model_id
        self.model_dir = Path(model_dir) if model_dir else None
        self.device = device
        self._pipe = None
        # 上一次前向的全量分数（阈值 0 那份），供 rescore_last 降线重筛用。
        # 只留最新一批：重筛永远针对"刚算完的那张图"，留着历史只会吃内存。
        self.last_scores: list[dict[str, float]] | None = None
        # bf16 真的按 bf16 加载权重（不再在构造阶段静默降级）。
        #
        # 以前这里是"看到 bf16 就改写成 tf32、权重仍 fp32"：保守本身没错，但它是
        # **静默的**——配置里写 bf16、日志里只有一句"已按 tf32 跑"，实际跑的
        # 是 fp32 权重的 GEMM。1008² / batch16 下 fp32 的激活与带宽开销接近翻倍，
        # 而页面和指纹都看不出来（实测 pixai 单张 0.63s）。
        #
        # 现在改成"真加载 → 验证 → 失败才回落"：
        #   * dtype 到底兼不兼容，由 _load 里一次冒烟前向当场回答，
        #     而不是靠"没实测过"预判；
        #   * 真回落时把 _precision_downgraded 置 True、precision 改回 tf32，
        #     于是产物与指纹记录的仍是**实际执行**的精度。
        #
        # 名字不再叫 _bf16_*：现在半精度有两档（bf16 给 Ampere+，fp16 给
        # V100 这类没有 bf16 的卡），回落逻辑对两者是同一条。
        self._precision_downgraded = False
        # 是否在每批之后额外再跑一次"阈值 0"的前向、留全量分数快照。
        # 默认 False：那是**整整一倍**的前向开销，而它只服务于"降线重筛"。
        # 不存快照时 client._rescore_at 会退回"重算那一张"，语义不变。
        self.snapshot_scores = False

    def _load(self) -> None:
        try:
            from transformers import pipeline
        except ImportError as exc:
            raise TaggerUnavailable(
                "pixai tagger 需要 transformers/torch/Pillow：\n"
                "  pip install torch torchvision transformers timm"
            ) from exc

        # ★权重下好了却"一直联网载入"的根因在这里：pipeline 收的是**仓库名**
        #   时，transformers 每次加载都要先问一遍 hub（有没有新 revision），
        #   国内这一步就卡在超时上——而用户明明已经在「模型管理」里把权重下到
        #   了 models/<backend>/。所以只要那个目录真的完整，就传**绝对路径**，
        #   transformers 见到目录会完全跳过网络。
        #
        #   判据必须"够了才用"：目录里只有半截文件时传目录，报的会是
        #   "找不到 config"，而原本能靠缓存跑起来——所以没备齐就退回仓库名，
        #   让 transformers 自己按缓存/网络来。
        source: str = self.model_id
        if self.model_dir is not None:
            missing = _missing_model_files(self.model_dir)
            if not missing:
                source = str(self.model_dir.resolve())
            elif self.verbose:
                print(f"[tagger] {self.model_dir} 缺 {', '.join(missing)}，"
                      f"改按仓库名 {self.model_id} 加载（会联网）", flush=True)
        kwargs: dict[str, Any] = {
            "model": source,
            "image_processor": source,
            "trust_remote_code": True,
        }
        if self.device and self.device != "cpu":
            kwargs["device"] = self.device
        else:
            kwargs["device"] = -1
        self._pipe = self._build_pipeline(pipeline, kwargs, source)
        if self._precision_downgraded and self.verbose:
            print("[tagger] pixai 的半精度没落地（远端管线 dtype 不兼容），"
                  "已回落 tf32：权重仍是 fp32。产物与指纹按 tf32 记。",
                  flush=True)
        # device 已经定下来了（pipeline 建好），此时设进程级 tf32 才对得上。
        self._apply_precision_flags()
        if self.verbose:
            extra = f"，{self.precision}" if self.precision != "fp32" else ""
            if self.batch_size > 1:
                extra += f"，batch={self.batch_size}"
            print(f"[tagger] 已加载 {self.model_id}（{self.device}{extra}）",
                  flush=True)

    # --------------------------------------------------------- 精度是否真落地

    def _build_pipeline(self, pipeline, kwargs: dict[str, Any], source: str):
        """按 precision 把 dtype 真的传进去，并**验证它确实生效**。

        为什么必须验证而不是"传了就算"：`dtype=` 与 `torch_dtype=` 在不同
        transformers 版本里叫法不同，传错时多数版本只是忽略关键字——构造成功、
        按 fp32 加载、一声不吭。只判断"没抛异常"就会得出"半精度已生效"的错误
        结论，而这正是这次排查里最难的一层（配置写 bf16、实测却是 fp32 权重）。

        半精度失败有三条路径，都必须真回落并留痕：
          * 关键字被忽略 / 远端把权重锁在 fp32 → 查实际 dtype 发现；
          * 权重是半精度但前向炸了（某一层 dtype 不匹配）→ 冒烟前向发现；
          * 设备是 CPU → 半精度在 CPU 上没有 tensor core，只会更慢。
        """
        import torch

        want = half_dtype(self.precision)
        if want is not None and str(self.device or "").lower() == "cpu":
            # CPU 上不做转换：没有 tensor core，半精度只会更慢，而且
            # torch 的部分 CPU 算子在 half 上会退回 fp32 再转换，白折腾。
            self._note_half_fallback(f"设备是 {self.device}，半精度无收益")
            want = None
        if want is not None:
            pipe = self._open_pipeline(pipeline, kwargs, want)
            if pipe is not None:
                actual = self._pipe_dtype(pipe)
                if actual is not None and actual != want:
                    self._note_half_fallback(f"权重实际是 {actual}")
                else:
                    try:
                        self._smoke(pipe)
                        return pipe
                    except Exception as exc:      # noqa: BLE001 - 兜底回落
                        self._note_half_fallback(
                            f"前向失败 {type(exc).__name__}: {exc}")
        return self._open_pipeline(pipeline, kwargs, torch.float32, required=True)

    def _note_half_fallback(self, why: str) -> None:
        self._precision_downgraded = True
        self.precision = "tf32"
        if self.verbose:
            print(f"[tagger] pixai 半精度未生效（{why}），回落 tf32", flush=True)

    @staticmethod
    def _open_pipeline(pipeline, kwargs: dict[str, Any], dtype, *,
                       required: bool = False):
        """按 dtype 建 pipeline。`dtype` / `torch_dtype` 挨个试。

        required=True 时失败会抛 TaggerUnavailable（fp32 这条是底线，
        它建不起来就是真的装不了）；否则返回 None 让调用方走回落。
        """
        last: Exception | None = None
        for key in ("dtype", "torch_dtype"):
            try:
                return pipeline(**kwargs, **{key: dtype})
            except TypeError as exc:
                last = exc          # 该版本不认这个关键字，换下一个
                continue
            except Exception as exc:  # noqa: BLE001 - 远端代码花样太多
                last = exc
                break
        if not required:
            return None
        # 两个关键字都不被接受时，别因此判定"装不了"：不传 dtype 时
        # transformers 自己按权重卡里的 dtype 加载，对 fp32 这条底线来说
        # 等价于我们要的结果。真正装不起来才会在下面抛。
        try:
            return pipeline(**kwargs)
        except Exception as exc:      # noqa: BLE001
            raise TaggerUnavailable(
                f"加载 {kwargs.get('model')} 失败：{type(exc).__name__}: {exc}\n"
                f"若网络受限，先手动下载："
                f"huggingface-cli download pixai-labs/pixai-tagger-v1.0") from exc

    @staticmethod
    def _pipe_dtype(pipe):
        """pipeline 里那张模型实际用的 dtype。拿不到返回 None（不乱猜）。"""
        import torch

        model = getattr(pipe, "model", None)
        if model is None:
            return None
        dtype = getattr(model, "dtype", None)
        if isinstance(dtype, torch.dtype):
            return dtype
        try:
            return next(model.parameters()).dtype
        except (AttributeError, StopIteration, TypeError):
            return None

    def _smoke(self, pipe) -> None:
        """用一张极小图真跑一次前向，验 bf16 的运行时兼容性。

        构造期不报错、推理期才炸是这类 trust_remote_code 模型的典型表现
        （某一层硬写了 .float() / 某个 buffer 是 fp32）。与其让第一批图在
        半小时后出问题，不如启动时多花一次前向。
        """
        from PIL import Image

        probe = Image.new("RGB", (64, 64), (127, 127, 127))
        out = pipe(probe, batch_size=1)
        if not isinstance(out, (dict, list)):
            raise RuntimeError(f"冒烟返回了意外类型 {type(out).__name__}")

    # -------------------------------------------------------------- 阈值/摊平

    def _threshold_arg(self) -> float | dict[str, float] | None:
        """阈值参数该长什么样。pipeline 认两种形态：数字（全体生效）或
        {类别: 阈值} 的 dict（README 的口径）。

        只配了 "default" 时必须给数字——把 "default" 当类别名塞进 dict 的
        话 pipeline 认不出这一类，等于没设阈值，静默退回"全都要"。

        ★但 pipeline 的 dict 形态**表达不了 default**，所以"default + 某个
          类别"并存时，全局阈值在这里会被摘掉（只剩 {类别: 阈值}）。用户看到
          的现象是：设了 0.6 的全局阈值，只有他点名的那一类按 0.6，其余类别
          悄悄回到更宽的口径，而且换个后端（wd-eva02 走 _threshold_of）就对
          不上。这个缺口在 `_has_orphan_default` + `_flatten` 里补，不在这里
          硬塞 "default" 键。
        """
        if set(self.thresholds) == {"default"}:
            return float(self.thresholds["default"])
        return {k: v for k, v in self.thresholds.items() if k != "default"} or None

    def _has_orphan_default(self) -> bool:
        """是否存在"被 pipeline 丢掉的全局阈值"。

        只有"既给了 default、又给了别的类别"才算：只给 default 时它会以数字
        形态整个交给 pipeline，没丢；一个都没给时也没有可丢的。
        """
        return "default" in self.thresholds and len(self.thresholds) > 1

    def _flatten(self, results: dict) -> dict[str, float]:
        """{"general": {"1girl": 0.98}, ...} → {"1girl": 0.98, ...}

        跨类别取 max 而不是"后写覆盖"：同一个 tag 理论上只属于一个类别，但
        trust_remote_code 的返回值不保证不重叠；覆盖的结果会随 dict 顺序变，
        等于让产出依赖一个没写进任何文档的实现细节。

        ★pipeline 的 dict 阈值表达不了 "default"，所以"default + 类别阈值"
          并存时全局那一档会被丢掉（见 `_threshold_arg`）。这里补一道二次
          过滤：pipeline 已经砍过一轮，这一轮只可能**更严格**，不会放出
          pipeline 没给的 tag，所以产出只会守着用户设的下限，不会变脏。

          只在 `_has_orphan_default()` 为真时才启用：其他情况（没设阈值、
          只设 default、只设类别）pipeline 那边已经精确处理过，这里再过滤
          一遍等于用 0.35 覆盖 pipeline 自己的默认口径，反而会改变现有产出。
        """
        orphan = self._has_orphan_default()
        out: dict[str, float] = {}
        for category, table in (results or {}).items():
            if self.keep and category not in self.keep:
                continue
            floor = self._threshold_of(category) if orphan else 0.0
            for tag, score in table.items():
                value = float(score)
                if value < floor:
                    continue
                out[tag] = max(out.get(tag, 0.0), value)
        return out

    # -------------------------------------------------------------- 推理

    def _full_scores(self, imgs: Sequence) -> list[dict[str, float]]:
        """全量分数（阈值压到 0，pipeline 不再筛）。

        为什么要这条额外通路：pipeline 一旦拿到阈值，返回的就是**已筛**的那批，
        低分那部分在内部就被丢了，外面无从补。要"降一档再筛一次"，必须留下
        未筛的全表。

        ★传 threshold=0.0 而不是不传：不传时 pipeline 用它 `config.json` 里的
          `tags_best_threshold` / `category_best_threshold`（逐 tag 标定过的，
          不是个整数），一样是筛过的——那样"全量"是假的。

        ★阈值已经低到 0 时不再留快照：那就等于把全部 tag 的分数存一份，
          白占内存，而调用方拿它重筛也筛不出更多东西。
        """
        if not imgs or self._threshold_of("general") <= 0:
            return []
        try:
            out = self._pipe([self._open_rgb(im) for im in imgs],
                             batch_size=len(imgs), threshold=0.0)
            payloads = out if isinstance(out, list) else [out]
            if len(payloads) != len(imgs):
                return []
            return [self._flatten(p.get("results") if isinstance(p, dict) else {})
                    for p in payloads]
        except Exception:            # noqa: BLE001 - 快照是加分项，失败不拖垮产出
            return []

    def rescore_last(self, index: int,
                     thresholds: dict[str, float] | None) -> dict[str, float]:
        """用上一次前向的**全量**分数，换一套阈值重筛第 index 张。

        `predict_batch_with` 会真的再前向一遍，而 tag 与 VLM 是并行起跑的：
        等结果时才发现 tag 太少，分数其实就在手上，再算一遍纯属白烧。
        快照由 `_predict_many` 在阈值 0 的那一次留下（见那边的说明）。
        """
        probs = getattr(self, "last_scores", None)
        if not probs or not (0 <= index < len(probs)):
            return {}
        saved_thr, saved_key = self.thresholds, self._plan_key
        try:
            self.thresholds = dict(thresholds or {})
            self._plan_key = None
            return self._apply_thresholds(probs[index])
        finally:
            self.thresholds = saved_thr
            self._plan_key = saved_key

    def _apply_thresholds(self, scores: dict[str, float]) -> dict[str, float]:
        """按当前阈值表筛全量分数（= pipeline 自己那套 mask 的等价实现）。

        ★用 `>` 而不是 `>=`：pixai 的 `_forward` 里写的是 `prob_c > threshold_c`。
          差一个等号，分数恰好等于阈值的那批 tag 会多出来，于是"重筛"与
          "首轮"的结果对不上，看起来像重筛把 tag 变多了。
        """
        floor = self._threshold_of("general")
        return {tag: value for tag, value in scores.items() if value > floor}

    def _predict_one(self, image) -> dict[str, float]:
        img = self._open_rgb(image)
        thr = self._threshold_arg()
        payload = (self._pipe(img, threshold=thr) if thr else self._pipe(img))
        return self._flatten(payload["results"])

    def _predict_one(self, image) -> dict[str, float]:
        img = self._open_rgb(image)
        thr = self._threshold_arg()
        payload = (self._pipe(img, threshold=thr) if thr else self._pipe(img))
        return self._flatten(payload["results"])

    def _predict_many(self, images: Sequence) -> list[dict[str, float]]:
        """一次喂多张。pixai 是 1008² / patch 14 的 ViT，单张就是 5184 个
        token，攒批的收益比 wd-eva02（448²，1024 个 token）大得多。

        为什么非覆写不可：基类默认是逐张 `_predict_one`，pipeline 每次只见
        到一张图，前向的固定开销（kernel 发射、Python dispatch）会按张数
        重复付。

        ★必须把 `batch_size` 一起传进去：pipeline 的默认值是 1，即使喂一个
        list 它也会**在内部逐张循环**。少了这个参数，代码看起来像做了批处理
        而吞吐一点不变——"改了没效果"里最难查的那一类。

        顺序必须与入参一一对应（execute 侧按位置对位回 image_id）。
        """
        import time

        t0 = time.perf_counter()
        imgs = [self._open_rgb(im) for im in images]
        decode_ms = int((time.perf_counter() - t0) * 1000)
        if not imgs:
            self._split_ms = (0, 0)
            return []

        thr = self._threshold_arg()
        call: dict[str, Any] = {"batch_size": len(imgs)}
        if thr:
            call["threshold"] = thr
        tf0 = time.perf_counter()
        try:
            out = self._pipe(imgs, **call)
        except Exception:            # noqa: BLE001 - 逐张重试，坏图不该带走整批
            # 批里只要有一张解不开，整批都会抛。而 tag_batch 可以到 16，一次
            # 异常会让执行器把 16 个 future 全部判失败——一张坏图毁掉 15 张好图。
            # 退回逐张：坏的那张自己报错，其余照常出产。
            if len(imgs) == 1:
                raise
            self._split_ms = (decode_ms,
                              int((time.perf_counter() - tf0) * 1000))
            return [self._predict_one(im) for im in imgs]
        self._split_ms = (decode_ms, int((time.perf_counter() - tf0) * 1000))

        # 单张时 pipeline 返回 dict，多张时返回 list。统一成 list——"长度为 1
        # 的 list"两种形态都可能出现，调用方不该按长度分支。
        payloads = out if isinstance(out, list) else [out]
        if len(payloads) != len(imgs):
            # 静默 zip 会把结果整体错位一格，产物里 tag 与图对不上且看不出来。
            raise RuntimeError(
                f"pixai 返回 {len(payloads)} 条，提交了 {len(imgs)} 张")
        # 分数快照默认**不做**（见 __init__ 的 snapshot_scores）。
        #
        # 以前这里无条件调 _full_scores，等于每批图多跑一整遍 1008² ViT：
        #   1. 那是第二次前向（阈值 0 那次），是实打实的 2×，不是"观测开销"；
        #   2. 原注释说"和每张图都重跑一遍不是一个量级"——反过来了，
        #      对 batch 里的每一张，它确实就是"多跑一遍"。
        #
        # 不做快照不丢功能：client._rescore_at 拿不到快照时会退回
        # predict_batch_with 重算**那一张**，只有真的触发"降线救 tag 太少"
        # 的少数图才付这笔钱，而不是全量图每张都付。
        self.last_scores = self._full_scores(imgs) if self.snapshot_scores else None
        return [self._flatten(p.get("results") if isinstance(p, dict) else {})
                for p in payloads]


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
        self._tensor_prep = None
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
        # 上一次前向的只读快照（恰好是被截断的那张时才会用）。
        # 存的是按下标摊平的分数：重筛只需要"换条线扫一遍"，不需要 16473
        # 个名字的字典。只留最新一批，内存占用与 batch 同阶。
        self.last_probs: list[Sequence[float]] | None = None

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
                "wd-eva02 tagger 需要 torch/timm/safetensors：\n"
                "  pip install torch timm safetensors"
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
        model_args = dict(cfg.get("model_args") or {})
        img_size = int(model_args.get("img_size") or 448)
        model_args.update(num_classes=num_classes, img_size=img_size)
        if cfg.get("global_pool") is not None:
            model_args.setdefault("global_pool", cfg["global_pool"])

        # 先把词表读完并和分类头对账，再加载 1.3 GB 权重：词表坏了要立刻报，
        # 不该等到读盘读了几十秒之后。
        self._names, self._categories = _read_wd_tags(csv_path, model_id=self.model_id)
        _check_vocab_against_head(len(self._names), num_classes,
                                  csv_path=csv_path, model_id=self.model_id)

        # 权重对应的 RoPE 尺寸、裁剪和归一化都在本地配置里；只传架构名
        # 会套用 timm 原始模型的默认值，形状匹配但推理结果不正确。
        model = timm.create_model(arch, pretrained=False,
                                  pretrained_cfg=cfg.get("pretrained_cfg") or None,
                                  **model_args)
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
        half = half_dtype(self.precision)
        if half is not None:
            if self.device == "cpu":
                # CPU 上没有半精度的 tensor core，实测反而更慢；直接回落，
                # 而不是让用户以为开了加速。
                if self.verbose:
                    print(f"[tagger] CPU 上 {self.precision} 无收益，已回落 fp32",
                          flush=True)
                self.precision = "fp32"
            else:
                model = model.to(dtype=half)
        if self.device and self.device != "cpu":
            model = model.to(self.device)
        self._apply_precision_flags()
        self._model = model

        data_cfg = timm.data.resolve_data_config({}, model=model)
        self._transform = timm.data.create_transform(**data_cfg, is_training=False)
        if self.preprocess == "tensor":
            self._tensor_prep = _tensor_prep_plan(self._transform)
            if self._tensor_prep is None:
                self.preprocess = "pil"
                if self.verbose:
                    print("[tagger] 模型预处理含张量路径不支持的变换，回落 PIL", flush=True)

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
            # 静态输入必须和权重同 dtype：不一致的话图里固化的是隐式提升
            # 之后那条路径，回放出来的速度和 eager 没区别。
            dtype = half_dtype(self.precision) or torch.float32
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

    @staticmethod
    def _pixels_uint8(image):
        from torchvision.transforms.functional import pil_to_tensor

        return pil_to_tensor(image)

    def _prep_on_device(self, pixels):
        return self._tensor_prep.apply(pixels, self.device)

    def _decode_batch(self, part: Sequence) -> tuple[list, int]:
        """解码 + 预处理一小批，返回 (张量列表, 本批耗时 ms)。

        耗时在**这一层**量：预取时它跑在后台线程里，主线程去等 future
        只能量到"还要等多久"，把那个当解码耗时会让 `tag_decode` 越跑越小。
        """
        import time

        t0 = time.perf_counter()
        if self.preprocess == "tensor":
            tensors = [self._pixels_uint8(self._open_rgb(im)) for im in part]
        else:
            tensors = [self._transform(self._open_rgb(im)) for im in part]
        return tensors, int((time.perf_counter() - t0) * 1000)

    def _forward_chunk(self, tensors: Sequence) -> tuple[list[dict[str, float]],
                                                        list[dict[str, float]], int]:
        """一小批前向 → (过滤后的 tag, rating 分数, 前向耗时 ms)。"""
        import time

        import torch

        graph = self._graph
        if self.preprocess == "tensor":
            tensors = [self._prep_on_device(t) for t in tensors]
        x = torch.stack(list(tensors))
        if self.device and self.device != "cpu":
            x = x.to(self.device)
        half = half_dtype(self.precision)
        if half is not None:
            x = x.to(half)
        tf0 = time.perf_counter()
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
        # 只读快照：换了阈值要重筛同一批分数时用（见 rescore_from_last）。
        self.last_probs = probs
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


# ---------------------------------------------------------- 显存预估（给页面用）
#
# 为什么会需要这个：批大小过去是个 1/2/4/8/16 的固定下拉，用户按"大一点更快"
# 的直觉选 16，结果是 16GB 卡上 batch 8 就超出约 2 GiB（实测），而页面事先
# 什么都不说。OOM 发生在加载完权重、跑完第一批之后，报的又是一段 CUDA 内部
# 的错误，看起来像驱动坏了。
#
# ★两个"超了"的形态要说清，它们的表现完全不同：
#   * 真 OOM：进程抛 CUDA out of memory，任务直接失败；
#   * **回落到系统内存**：Windows 驱动会把溢出的部分放到主机内存里，不报错，
#     但每步都要过 PCIe，速度掉一个数量级——pixai 用 tf32（权重 fp32、激活
#     也是 4 字节）跑出 4.6 s/张、而 bf16 只要 0.145 s/张，就是这一种。
# 所以这里的判据是"估算峰值 vs 当前空闲显存"，而不是"会不会崩"。

# 批大小的上限。前端滑动条、后端夹取、显存估算三处都用这一个数，
# 免得"UI 能拖到 100、服务端 64 就截断"这种对不上的情况。
TAG_BATCH_MAX = 100

# 每个后端的显存画像：参数量（百万）、输入边长、单图视觉 token 数。
# token = (边长 / patch)²：pixai 1008/14 = 72 → 5184；wd-eva02 448/14 = 32 → 1024。
_TAG_VRAM_PROFILE: dict[str, dict[str, float]] = {
    "pixai": {"params_m": 486.0, "input": 1008, "tokens": 5184},
    "wd-eva02": {"params_m": 304.0, "input": 448, "tokens": 1024},
    "cl-tagger-v2": {"params_m": 300.0, "input": 448, "tokens": 1024},
    "onnx": {"params_m": 300.0, "input": 448, "tokens": 1024},
}

# 激活内存的两个系数：线性项（hidden 状态 / MLP）与二次项（注意力矩阵）。
# 标定依据（2026-11，pixai 1008²，bf16）：16GB 卡上 batch 4 可跑、
# batch 8 超出约 2 GiB → 每个并发单位约 2.1 GiB。二次项吃掉的正是
# 5184 token 那个 O(n²) 的注意力，这也是为什么"加倍 batch"不是加倍开销
# 那么简单——它在同一张卡上同时挤占容量和带宽。
_ACT_LINEAR_GIB = 3.26e-4      # GiB / token / 并发单位
_ACT_QUAD_GIB = 1.52e-8        # GiB / token² / 并发单位


def estimate_tag_vram_gb(backend: str, precision: str, batch_size: int) -> float | None:
    """估一个 tag 后端在给定精度/批大小下的峰值显存（GiB）。未知后端返 None。

    是**估算**而不是测量：权重那项基本准（参数量 × 每参数字节），激活那项
    由两个标定系数推出来，误差大致 ±30%。够用来回答"这个 batch 会不会
    撑爆我的卡"，不够用来做容量规划。

    ★tf32 的权重仍是 fp32（见 PixAITagger：tf32 只是打开 tensor core，
      不改 dtype），所以它按 4 字节算——这正是 pixai 选 tf32 时显存翻倍、
      进而回落到系统内存的原因。
    """
    prof = _TAG_VRAM_PROFILE.get(str(backend or "").strip().lower())
    if prof is None:
        return None
    per_param = 4.0 if normalize_precision(precision) in ("fp32", "tf32") else 2.0
    weights = prof["params_m"] * 1e6 * per_param / 1024 ** 3
    tokens = prof["tokens"]
    # 激活随精度按字节数等比缩放（fp32 的中间结果是 bf16 的两倍宽）。
    scale = per_param / 2.0
    per_unit = (_ACT_LINEAR_GIB * tokens + _ACT_QUAD_GIB * tokens * tokens) * scale
    return round(weights + per_unit * max(1, int(batch_size)), 2)


def tag_vram_budget(backend: str, precision: str, batch_size: int, *,
                    device: str = "auto") -> dict:
    """给页面的一组判断：估算峰值 / 卡上总量 / 此刻空闲 / 建议怎么做。

    返回的 level 只有三档，对应页面上三种颜色：
      ok     —— 估算值 < 空闲的 85%
      tight  —— 落在 85%~100%，能跑但别再加
      over   —— 估算值超过空闲。**不一定会崩**（见上面"回落到系统内存"），
                但那种状态下吞吐会掉一个数量级，所以照样要红着提示。
    """
    from .hardware import free_vram_gb, visible_gpus

    est = estimate_tag_vram_gb(backend, precision, batch_size)
    if est is None:
        return {"ok": False, "reason": f"没有 {backend} 的显存画像"}
    on_cpu = str(device or "").strip().lower() in ("cpu", "")
    if on_cpu:
        return {"ok": True, "estimate_gb": est, "level": "cpu",
                "advice": "跑在 CPU 上，不占显存"}
    gpus = visible_gpus()
    if not gpus:
        return {"ok": False, "estimate_gb": est, "reason": "检测不到 NVIDIA 显卡"}
    gpu = gpus[0]
    free = free_vram_gb(gpu.index)
    total = gpu.vram_gb
    # 空闲量拿不到就退回总量打八折：经验上桌面合成会先占掉一两 GB，
    # 按总量直接比会得出"还剩 16GB"这种乐观到危险的结论。
    budget = free if free is not None else round(total * 0.8, 2)
    ratio = est / budget if budget > 0 else 99.0
    if ratio <= 0.85:
        level, advice = "ok", ""
    elif ratio <= 1.0:
        level = "tight"
        advice = (f"接近上限（估算 {est} GiB / 可用 {budget} GiB），"
                  f"建议降到 {_safe_batch(backend, precision, budget)}")
    else:
        level = "over"
        advice = (f"超出可用显存（估算 {est} GiB / 可用 {budget} GiB）。"
                  f"驱动可能回落到系统内存：不报错，但会慢一个数量级。"
                  f"建议降到 {_safe_batch(backend, precision, budget)}")
    return {"ok": True, "estimate_gb": est, "total_gb": total,
            "free_gb": free, "budget_gb": budget, "level": level,
            "advice": advice, "gpu": gpu.name}


def _safe_batch(backend: str, precision: str, budget_gb: float) -> int:
    """在给定预算内最大的 batch（留 15% 余量）。找不到就返回 1。"""
    limit = max(0.5, budget_gb * 0.85)
    best = 1
    for n in range(1, TAG_BATCH_MAX + 1):
        est = estimate_tag_vram_gb(backend, precision, n)
        if est is None or est > limit:
            break
        best = n
    return best


def resolve_device(pref: str | None) -> str:
    """把 `auto` 解析成具体设备。

    只管**一个**设备。要一张卡清单用 `resolve_devices`——多卡必须由调用方
    显式点名，`auto` 不会自己铺开（理由见那个函数的注释）。
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


def resolve_devices(pref) -> list[str]:
    """把设备偏好解析成一张卡清单（去重、保序）。

    接受的写法：
        None / "" / "auto"        → 有卡 ["cuda:0"]，否则 ["cpu"]（=老行为）
        "cpu"                     → ["cpu"]
        "cuda:0,cuda:1,cuda:2"    → 三张卡
        ["cuda:0", "cuda:1"]      → 同上

    ★`auto` **不铺满所有卡**。多一张卡就是多加载一份权重：显存、启动时间、
    以及"这台机器上还有什么在跑"都跟着变。而"机器有几张卡"和"我想拿几张
    跑 tag"是两件事——铺满必须由调用方（命令行 / 界面勾选）显式点名。

    ★显式写了 cuda:N 却不存在这张卡时，这里就报错：让它一路走到
    `model.to("cuda:7")` 只会得到 "Invalid device ordinal"，而那句话离
    "你把设备勾错了"很远。
    """
    if isinstance(pref, (list, tuple)):
        raw = [str(x or "").strip().lower() for x in pref]
    else:
        raw = [s.strip().lower() for s in str(pref or "").split(",")]

    items: list[str] = []
    for value in raw:
        if not value:
            continue
        if value not in items:
            items.append(value)
    if not items or items == ["auto"]:
        return [resolve_device("auto")]
    if "auto" in items:
        # auto 与具体卡混着给，含义是"能自动就自动"——按单卡处理，别猜
        return [resolve_device("auto")]

    _check_devices_exist(items)
    return items


def _check_devices_exist(items: Sequence[str]) -> None:
    """cuda:N 越界时提前给一句能查的话。不知道有几张卡就跳过检查。"""
    wanted = []
    for it in items:
        if it.startswith("cuda:"):
            try:
                wanted.append(int(it.split(":", 1)[1]))
            except ValueError:
                raise ValueError(f"设备写法不对：{it!r}（要 cuda:0 这种）") from None
    if not wanted:
        return
    try:
        import torch

        if not torch.cuda.is_available():
            raise ValueError(
                f"选了 {', '.join(items)}，但这台机器上 torch 看不到 CUDA。"
                f"装的是 CPU 版 torch，或者驱动没装好。")
        total = torch.cuda.device_count()
    except ValueError:
        raise
    except Exception:      # noqa: BLE001 - 探测失败就不拦，让 torch 自己报
        return
    bad = sorted({n for n in wanted if n >= total})
    if bad:
        raise ValueError(
            f"设备 {', '.join(f'cuda:{n}' for n in bad)} 不存在："
            f"这台机器只有 {total} 张卡（cuda:0 ~ cuda:{total - 1}）。")


def _split_even(items: Sequence, n: int) -> list[list]:
    """连续等分成 n 段，前 rem 段各多一张。

    为什么是连续而不是轮转（i % n）：同一张卡拿到的图在内存里连续，
    wd-eva02 的解码预取才有机会跨块生效；轮转会让每张卡都是"跳着"的，
    预取退化成每张单独解码。拼回来也简单——按段序接上就是原顺序。
    """
    total = len(items)
    base, rem = divmod(total, n)
    out: list[list] = []
    at = 0
    for i in range(n):
        size = base + (1 if i < rem else 0)
        out.append(list(items[at:at + size]))
        at += size
    return out


class MultiDeviceTagBackend:
    """同一份权重在 N 张卡上各放一份，按批派活。

    ★**不是** TagBackend 的子类：它不加载权重、不碰 torch，只做切分与对位。
      调用方要的属性（name / model_id / precision / preprocess / thresholds /
      keep / max_tags）全部转发给第一份实例，因为多卡之间这些**必须完全
      一致**——任何一项不同，同一张图的 tag 就会随"落在哪张卡"而变，
      那是产物层面无法归因的污染。

    为什么用线程而不是进程：每个实例有自己的模型和自己的卡，torch 的前向在
    C++ 侧执行时释放 GIL，线程之间不互相抢占；而进程方案要求模型对象可
    pickle（transformers pipeline 不是），Windows 上还只能 spawn，每张卡
    要多付一次 import torch + 读权重。

    ★`batch_size` 报的是**整批**容量（每卡 batch_size × 卡数）：
      `local_tag._chunk_size` 拿它算一次取多少图，报单卡值会让每张卡只分到
      1/N 的批，把攒批的收益折掉。
    """

    def __init__(self, backends: Sequence["TagBackend"],
                 devices: Sequence[str] | None = None):
        subs = [b for b in backends if b is not None]
        if not subs:
            raise ValueError("多设备 tag 后端至少要一份实例")
        self._subs = list(subs)
        self.devices = list(devices or [str(getattr(b, "device", "") or "")
                                        for b in self._subs])
        first = self._subs[0]
        self.name = str(getattr(first, "name", "") or "")
        self.model_id = str(getattr(first, "model_id", "") or "")
        self.precision = str(getattr(first, "precision", "") or "")
        self.preprocess = str(getattr(first, "preprocess", "pil") or "pil")
        self.thresholds = dict(getattr(first, "thresholds", None) or {})
        self.keep = tuple(getattr(first, "keep", None) or ())
        self.max_tags = int(getattr(first, "max_tags", 0) or 0)
        self.batch_size = (max(1, int(getattr(first, "batch_size", 1) or 1))
                           * len(self._subs))
        self.last_timings: dict[str, int] = {"total": 0, "decode": 0, "forward": 0}
        self._pool = None
        self._pool_lock = threading.Lock()

    # -------------------------------------------------------------- 生命周期

    def load(self) -> None:
        """**顺序**加载。并行加载只会让 N 份权重抢同一块盘/同一条网络，
        启动反而更慢，还把峰值内存叠起来。"""
        for b in self._subs:
            b.load()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None

    # -------------------------------------------------------------- 推理

    def _executor(self):
        if self._pool is None:
            from concurrent.futures import ThreadPoolExecutor

            with self._pool_lock:
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(
                        max_workers=len(self._subs),
                        thread_name_prefix="tag-card")
        return self._pool

    def predict_batch_with(self, images: Sequence,
                           thresholds: dict[str, float] | None) -> list:
        """换一套阈值再算一批。逐张卡转发，语义与单卡版一致。

        ★切分必须与 `predict_batch` 完全一致：换阈值只在"这张图的分数"上
          重筛，而分数属于哪张图是靠位置对回 image_id 的。两次切法不同，
          tag 就会跟错图，且产物层面看不出来。
        """
        imgs = list(images)
        if not imgs:
            return []
        if len(self._subs) == 1:
            return self._subs[0].predict_batch_with(imgs, thresholds)
        # 多卡：每张卡各自换阈值，保持一致（否则同一张图落在哪张卡结果不同）。
        for sub in self._subs:
            if not hasattr(sub, "predict_batch_with"):
                raise NotImplementedError(
                    f"{getattr(sub, 'name', sub)} 不支持换阈值重筛")
        parts = _split_even(imgs, len(self._subs))
        pool = self._executor()
        futures = [pool.submit(sub.predict_batch_with, part, thresholds)
                   for sub, part in zip(self._subs, parts)]
        merged: list = []
        for fut in futures:
            merged.extend(fut.result())
        return merged

    def predict_batch(self, images: Sequence) -> list:
        """切分 → 各卡并行 → 按原顺序拼回。

        拼回顺序就是入参顺序：切分是连续的，每段内部又保持原序，所以
        "按段序接上"天然对齐。调用方（client / local_tag）靠位置把结果对回
        image_id，错一格就会让产物里的 tag 与图对不上，而且看不出来。
        """
        import time

        imgs = list(images)
        if not imgs:
            return []
        if len(self._subs) == 1:
            out = self._subs[0].predict_batch(imgs)
            self.last_timings = dict(getattr(self._subs[0], "last_timings", {}) or {})
            return out

        parts = _split_even(imgs, len(self._subs))
        pool = self._executor()
        t0 = time.perf_counter()
        futures = [pool.submit(b.predict_batch, part)
                   for b, part in zip(self._subs, parts) if part]
        try:
            chunks = [f.result() for f in futures]
        except BaseException:
            # 一张卡炸了就取消其余：让它们继续算只是白烧 GPU，而这一批反正要重试
            for f in futures:
                f.cancel()
            raise
        total_ms = int((time.perf_counter() - t0) * 1000)

        out: list = []
        for chunk in chunks:
            out.extend(chunk)
        self.last_timings = {
            "total": total_ms,
            # 各卡是并行跑的，两个和是"卡·毫秒"，大于墙钟是正常的
            # （与单卡口径一致：decode 与 forward 本来就重叠）。
            "decode": sum(int((getattr(b, "last_timings", {}) or {}).get("decode") or 0)
                          for b, part in zip(self._subs, parts) if part),
            "forward": sum(int((getattr(b, "last_timings", {}) or {}).get("forward") or 0)
                           for b, part in zip(self._subs, parts) if part),
        }
        return out

    def __repr__(self) -> str:      # 日志里要能一眼看出用了几张卡
        return (f"MultiDeviceTagBackend({self.name}, "
                f"{len(self._subs)} 张卡: {', '.join(self.devices)})")


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
    """按名字建后端。`none` / 空串返回 None（表示 tag 走 VLM）。

    ★`max_tags`（默认 160）**不要和 `configs/tiers.yaml` 的
      `output.tag_max_count`（64）统一**。这两个数字看着像同一个东西，实际
      语义完全不同，强行合并会改变现有产出：

      * 这里：判别式分类头取 top-N。`_filter` 是**先按阈值筛、再取前 N**，
        所以 N 只是个天花板。0.35 阈值下多数图落在 30~60 个，根本到不了 160；
        只有"文字密集"的图（包装袋上的品牌/规格/日期成串）实测能到 125~152，
        那时它才真正起作用。
      * `output.tag_max_count`：VLM **生成** tag 的预算，和 `max_tokens` 绑死
        （见该段注释"160 会把 max_tokens 吃干、caption 一个字都不出"）。
        判别式模型不产 token，那条约束对它没有意义。

      现状是两条入口口径不一：CLI 走 `client.py` 传 `output.tag_max_count`，
      web 用这里的默认值。这是**刻意的取舍**——统一到 64 会截断上面那类密集
      图的 tag，而那是当前产出里正常的一部分。要改先想清楚改的是产出。
    """
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
    # 能"按目录加载"的后端才认这个参数（其余传进去会漏进基类构造器变 TypeError）。
    # ★pixai 现在也在名单里：它以前被排除，结果是**权重明明下好了还每次联网**——
    #   pipeline 只收到仓库名，transformers 每回都要先问 hub 有没有新 revision，
    #   国内这一步就是长时间"正在载入"然后超时。见 PixAITagger._load。
    if model_dir and key in ("pixai", "wd-eva02", "cl-tagger-v2", "onnx"):
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
    if key != "wd-eva02":
        obj.preprocess = "pil"
    if verbose:
        print(f"[tagger] tag 后端 {obj.name}（模型 {model or '默认'}，{device}）", flush=True)
    return obj


def build_tag_backends(backend: str, *, devices: Sequence[str] | None = None,
                       **kw: Any):
    """按设备清单建后端。

    单卡（含 `devices` 为空）返回的就是 `build_backend` 的原实例，调用方
    拿到的对象与以前一字不差——多卡是"多出来的一层"，不是一个新口径。

    多卡时每张卡各建一份（各自加载权重），再用 MultiDeviceTagBackend 包起来。
    `kwargs` 里的 `device` 会被忽略：设备由 `devices` 决定，两份来源同时存在
    只会让"到底用了哪张卡"变成猜。
    """
    kw.pop("device", None)
    keys = [d for d in (devices or []) if d]
    if len(keys) <= 1:
        single = kw | ({"device": keys[0]} if keys else {})
        return build_backend(backend, **single)

    objs = [build_backend(backend, device=d, **kw) for d in keys]
    objs = [o for o in objs if o is not None]
    if not objs:
        return None            # backend=none：一张卡都不用建
    if len(objs) == 1:         # 只建成一份（理论上到不了这里）就别包装
        return objs[0]
    return MultiDeviceTagBackend(objs, keys)


def build_tag_backend_lanes(backend: str, *, devices: Sequence[str] | None = None,
                            limit: int = 0, **kw: Any) -> list:
    """每张卡各建一份实例，**不包装**——包级并行用：一条 lane 独占一张卡。

    为什么不复用 `build_tag_backends`：那个返回的是
    `MultiDeviceTagBackend`，它会把**每一批**摊到所有卡上。包级并行的前提
    正好相反——一条 lane 只认自己那张卡，否则 N 条 lane 会互相抢同一批卡，
    等于两套并行叠在一起，既快不起来也说不清谁在算。

    `limit` 用来把 lane 数压到包数以下：包比卡少时，多出来的实例只有加载
    权重的时间成本（pixai 一份 fp32 约 2 GB），一点算力都用不上。
    """
    kw.pop("device", None)
    keys = [d for d in (devices or []) if d][:limit or None]
    if not keys:
        keys = ["cpu"]
    return [build_backend(backend, device=d, **kw) for d in keys]
