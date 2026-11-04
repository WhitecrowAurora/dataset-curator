"""配置层：把「硬件 + 分档表」解析成一个可直接启动的运行 profile。

设计原则：
- 硬件的限制是硬的（没有 FP8 tensor core 就是没有），配置写了也会被拒。
  宁可启动时明确报错，也不要跑起来之后发现走了反量化路径、慢一倍还没人知道。
- 模型是可换的（4B / 8B 都支持），分档表决定哪张卡跑哪个。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .bucket import Bucket, compute_buckets
from .hardware import GPUInfo


class ConfigError(Exception):
    """配置或硬件不满足要求。启动前就要抛出，不要带病运行。"""


# 分档表的默认位置。web/UI 与 CLI 必须指向同一份，否则界面上填的默认值
# 和命令行读到的配置分叉。之前 web 侧传了 None，Path(None) 抛 TypeError
# 被 except 吞掉，界面永远显示代码里的兜底值。
DEFAULT_TIERS = "configs/tiers.yaml"

# 「模型管理」页写的那份覆盖清单。单独一个文件，不回写 tiers.yaml：
# 那份是手写配置，注释和排版都是口径的一部分，程序化改写迟早把说明吃掉。
# 结构见 load_model_overlay()；文件不存在 = 没有任何覆盖。
MODEL_OVERLAY = "configs/models.json"


@dataclass(slots=True)
class ModelSpec:
    key: str
    path: str
    revision: str | None
    family: str
    quantized_path: str | None
    precision: str
    # GGUF 路径（相对仓库根）。只有 engine=llamacpp 才用；
    # 与 quantized_path 是两套东西：前者是给 llama.cpp 的成品权重，
    # 后者是给 vLLM 的 compressed-tensors 目录，互相不能替代。
    gguf: str = ""
    mmproj: str = ""

    @property
    def resolved_path(self) -> str:
        """优先用量化后的本地/远端路径，没有则回退到原始模型。"""
        return self.quantized_path or self.path

    @property
    def revision_tag(self) -> str:
        rev = (self.revision or "unpinned")[:8]
        return f"{self.key}@{rev}"

    @property
    def model_id(self) -> str:
        """写进 provenance 的模型指纹。precision 也算在内，因为换精度等于换一代数据。"""
        return f"{self.revision_tag}/{self.precision}"


@dataclass(slots=True)
class TierSpec:
    name: str
    vram_min: float
    vram_max: float
    model: str
    precision: str | None = None
    max_model_len: int = 8192
    max_num_seqs: int = 8
    # 单步最多处理多少 token（prefill + decode 合计）。0 = 用 vLLM 默认。
    max_num_batched_tokens: int = 0
    cc_min: tuple[int, int] | None = None
    note: str = ""


# 对外只暴露三档。它们不是"同一件事的三个数字"，落到 vLLM 上是三条不同的路：
#   bf16  —— 不传 --quantization，按权重自带 dtype 加载（最稳，显存最贵）
#   fp8   —— 传 --quantization fp8，由 vLLM 在线把权重转成 FP8（Ada 起）
#   w4a16 —— 权重以 4bit 存、激活仍 16bit；必须已有 AWQ/GPTQ 量化权重
QUANT_PRECISIONS = ("bf16", "fp8", "w4a16")


def normalize_quant_precision(value: str | None) -> str | None:
    """把各种写法归一成 bf16 / fp8 / w4a16。None 或空串表示"没指定"。"""
    if value is None:
        return None
    key = str(value).strip().lower()
    if not key:
        return None
    if key in ("bf16", "bfloat16"):
        return "bf16"
    if key in ("fp8", "fp8_e4m3", "fp8_dynamic"):
        return "fp8"
    if key in ("w4a16", "int4", "awq", "gptq"):
        return "w4a16"
    raise ConfigError(f"未知精度档位: {value!r}。可选：bf16 / fp8 / w4a16。")


def detect_quant_method(model: "ModelSpec") -> str | None:
    """推断 4bit 权重是哪种格式，供 vLLM 的 --quantization 使用。

    返回 None = 交给 vLLM 读权重里的 quantization_config 自己判断。只在证据
    明确时才返回名字：把 AWQ 权重标成 gptq 会让服务启动直接失败，而那句报错
    里只有形状/键名，离真正原因很远。
    """
    marks = f"{model.precision or ''} {model.quantized_path or ''}".lower()
    if "gptq" in marks:
        return "gptq"
    if "awq" in marks:
        return "awq"
    return None


@dataclass(slots=True)
class RunProfile:
    """一个可以直接用来起服务的完整配置。"""

    tier: str
    model: ModelSpec
    precision: str
    max_pixels: int
    max_model_len: int
    max_num_seqs: int
    # 0 = 不传 --max-num-batched-tokens，沿用 vLLM 默认（v1 是 8192）
    max_num_batched_tokens: int
    gpu_memory_utilization: float
    temperature: float
    top_p: float
    max_tokens: int
    request_timeout_s: int
    device_name: str
    vram_gb: float
    # 分桶表。max_pixels 是总预算，buckets 是这预算在五种宽高比下的具体落点。
    # 空列表表示只用单一尺寸，不分桶。
    buckets: list[Bucket] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def model_id(self) -> str:
        return f"{self.model.revision_tag}/{self.precision}"

    @property
    def quantization(self) -> str | None:
        """vLLM 启动时的 --quantization 值；None = 不传。

        这是一个**能力**而不是意图：precision=w4a16 却没有 quantized_path 时
        返回 None，服务实际按 bf16 加载。不做静默降级——resolve_profile 会在
        warnings 里写清，页面上看得见。
        """
        p = normalize_quant_precision(self.precision) or "bf16"
        if p == "fp8":
            return "fp8"
        if p == "w4a16" and self.model.quantized_path:
            return detect_quant_method(self.model)
        return None

    @property
    def weights_path(self) -> str:
        """这次实际加载哪套权重。

        bf16 / fp8 必须用**原始**权重：quantized_path 只表示"另有一份 4bit
        版本"，不代表这次就要用它。否则页面上选 BF16、实际却加载了 4bit，
        等于换了一代数据还看不出来。只有 W4A16 档（含分档表写的 int4/awq/
        gptq）且量化权重存在时才切过去。
        """
        p = normalize_quant_precision(self.precision) or "bf16"
        if p == "w4a16" and self.model.quantized_path:
            return self.model.quantized_path
        return self.model.path

    @property
    def weight_quantized(self) -> bool:
        """W4A16 档是否真的有 4bit 权重可用。

        与 quantization 分开：compressed-tensors 的产物不需要显式
        --quantization（vLLM 读 config.json 自己认），但权重确实已经压过。
        页面要靠这个区分"真 4bit"和"选了 W4A16 却回退 BF16"。
        """
        if normalize_quant_precision(self.precision) != "w4a16":
            return True
        return bool(self.model.quantized_path)

    @property
    def bucket_spec(self) -> str:
        """写进 provenance 的分桶指纹。换桶等于换一套数据分布。"""
        if not self.buckets:
            return "none"
        return "+".join(f"{b.name}:{b.width}x{b.height}" for b in self.buckets)

    def summary(self) -> str:
        lines = [
            f"tier          : {self.tier}",
            f"device        : {self.device_name} ({self.vram_gb} GiB)",
            f"model         : {self.weights_path}",
            f"model_id      : {self.model_id}",
            f"precision     : {self.precision}",
            f"max_pixels    : {self.max_pixels}",
            f"max_model_len : {self.max_model_len}",
            f"max_num_seqs  : {self.max_num_seqs}",
        ]
        if self.max_num_batched_tokens:
            lines.append(f"max_batched   : {self.max_num_batched_tokens}")
        if self.buckets:
            lines.append("buckets       :")
            lines.extend(f"  {b.describe(self.max_pixels)}" for b in self.buckets)
        if self.warnings:
            lines.append("warnings      :")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def _parse_cc(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return (int(value[0]), int(value[1]))
    text = str(value).strip()
    if not text:
        return None
    major, _, minor = text.partition(".")
    return (int(major), int(minor or 0))


def _check_precision_supported(precision: str, gpu: GPUInfo,
                               model_key: str = "") -> None:
    """精度与硬件的硬性校验。不兼容就抛错，不做静默降级。"""
    p = precision.lower()
    if p in ("fp8", "fp8_e4m3", "fp8_dynamic"):
        if not gpu.supports_fp8:
            raise ConfigError(
                f"{gpu.name} (compute capability {gpu.cc_str}) 不支持 FP8 tensor core。"
                f"FP8 需要 CC >= 8.9（Ada / Hopper / Blackwell 起）。"
                f"请改用 int4 或 bf16。"
            )
    elif p in ("fp4", "nvfp4"):
        if not gpu.supports_fp4:
            raise ConfigError(
                f"{gpu.name} (CC {gpu.cc_str}) 不支持 FP4。NVFP4 需要 Blackwell (CC >= 12.0)。"
            )
    elif p in ("int4", "awq", "gptq", "w4a16"):
        if not gpu.supports_int4:
            raise ConfigError(f"{gpu.name} 太旧，不支持 int4 反量化推理。")
    elif p in ("bf16", "bfloat16", "fp16"):
        # 只检查装得下，不检查算力。装不装得下由**模型**决定（8B 的 bf16
        # 权重就要 16GB），之前错查显卡型号里有没有 "8b"——显卡名里永远
        # 没有，于是任何卡都按 16GB 那档放行。
        need = 20.0 if "8b" in model_key.lower() else 16.0
        if gpu.vram_gb < need * 0.6:
            raise ConfigError(
                f"{gpu.name} 只有 {gpu.vram_gb} GiB，跑 {model_key or 'bf16 模型'} "
                f"的 bf16 很可能 OOM。建议改用 int4。"
            )
    else:
        raise ConfigError(f"未知精度档位: {precision}")


def _estimate_vram_headroom_check(profile: RunProfile, gpu: GPUInfo) -> list[str]:
    """粗估显存，给一条警告而不是硬拦截（估算不准，实测为准）。"""
    warns: list[str] = []
    small = "4b" in profile.model.key.lower()
    tb = 0.5 if small else 1.0  # 每 B 参数 int4 约 0.5GB
    if profile.precision.lower() in ("bf16", "bfloat16"):
        tb = 2.2 if small else 4.4
    elif "fp8" in profile.precision.lower():
        tb = 1.1 if small else 2.2
    elif (profile.precision.lower() in ("int4", "awq", "gptq", "w4a16")
          and not profile.model.quantized_path):
        # 4bit 档但没量化权重 = 实际按 bf16 加载。按 int4 估算会乐观一倍，
        # 正好把最该提醒的 OOM 风险藏起来。
        tb = 2.2 if small else 4.4

    approx = tb * 4 + 2.5  # 权重 + KV + 激活 + 框架余量
    if approx > gpu.vram_gb * 0.95:
        warns.append(
            f"粗估需要约 {approx:.1f} GiB 显存，本卡 {gpu.vram_gb} GiB，可能 OOM。"
            f"建议降低 max_model_len 或改用更小模型。"
        )
    return warns


def parse_caption_chars(value) -> tuple[int, int] | None:
    """把各种写法归一成 (下限, 上限)，空值 → None。

    接受 `"250-500"`、`[250, 500]`、`(250, 500)`、`None`、`0`。
    越界（<100 或 >3000）一律报错：那已经不是"调长度"，而是把任务改成了
    别的形态 —— 100 字符塞不下 prompt 要求的六段要素。
    """
    if value is None or value == 0 or value == "" or value == []:
        return None
    if isinstance(value, str):
        parts = value.replace("，", ",").replace("~", "-").replace(",", "-").split("-")
        if len(parts) != 2:
            raise ConfigError(f"caption_chars 要写成 '下限-上限'，收到 {value!r}")
        try:
            lo, hi = int(parts[0]), int(parts[1])
        except ValueError:
            raise ConfigError(
                f"caption_chars 两边必须是整数，收到 {value!r}") from None
    else:
        try:
            lo, hi = int(value[0]), int(value[1])
        except (TypeError, ValueError, IndexError):
            raise ConfigError(
                f"caption_chars 要是一对数，收到 {value!r}") from None
    if not 100 <= lo < hi <= 3000:
        raise ConfigError(
            f"caption_chars 要求 100 ≤ 下限 < 上限 ≤ 3000，收到 {lo}-{hi}。\n"
            f"低于 100 塞不下 prompt 要求的六段要素（主体/外观/环境/构图/光线/文字）；"
            f"高于 3000 会让单张耗时翻几倍。")
    return lo, hi


@dataclass(slots=True)
class OutputSpec:
    """产出什么、多长。决定一次打标要生成哪些字段。

    两个区间都是**硬约束**（越界判不合格），不是目标值：
        NL  : 32 ~ 256 token。下限压这么低是为了不因"图简单、话少"就判废，
              但 prompt 仍会要求写满中段（见 client._prompt_for），
              硬线与引导值刻意分开——把引导值降到 32 等于教模型写短句。
        tag : 10 ~ 160 个。上限给到 160 是因为高密度图（群像、复杂场景）
              真的能出上百个有效 tag，卡在 40 会白扔信息。
    """

    # both | nl | tag
    mode: str = "both"
    nl_min_tokens: int = 32
    nl_max_tokens: int = 256
    tag_min_count: int = 12
    tag_max_count: int = 160
    # 描述长度的**引导值**（字符区间），与上面两个**硬约束**（token 区间）分开。
    # 这两个数字回答的问题不同：
    #   nl_min/max_tokens —— 写出来的东西算不算合格（越界判废）
    #   caption_chars     —— 让模型往多长写
    # 混在一起改不了的坑：把一个调小会同时把闸门调松，把另一个调大会把闸门调紧。
    # None = 按 token 区间派生（400 ~ min(1200, max_tokens*4)），即今天的默认行为。
    # ★改它等于改产出分布，必须进 provenance/fingerprint，否则两批数据无法判断同源。
    #   实测（Q4 8B，126 张）：400-1024 → 250-500 时字数 −33%、延迟 −36%，
    #   而要素覆盖只掉 2~3pp（见 devtools/nl-quant-ab.md §5.2）。
    caption_chars: tuple[int, int] | None = None

    @property
    def wants_nl(self) -> bool:
        return self.mode in ("nl", "both")

    @property
    def wants_tag(self) -> bool:
        return self.mode in ("tag", "both")

    @classmethod
    def from_dict(cls, d: dict | None) -> "OutputSpec":
        d = d or {}
        mode = str(d.get("mode", "both")).lower()
        if mode not in ("nl", "tag", "both"):
            raise ConfigError(
                f"output.mode 只能是 nl / tag / both，收到 {mode!r}"
            )
        spec = cls(
            mode=mode,
            nl_min_tokens=int(d.get("nl_min_tokens", 32)),
            nl_max_tokens=int(d.get("nl_max_tokens", 256)),
            tag_min_count=int(d.get("tag_min_count", 12)),
            tag_max_count=int(d.get("tag_max_count", 160)),
            caption_chars=parse_caption_chars(d.get("caption_chars")),
        )
        if spec.nl_max_tokens and spec.nl_min_tokens > spec.nl_max_tokens:
            raise ConfigError(
                f"output.nl_min_tokens({spec.nl_min_tokens}) 大于 "
                f"nl_max_tokens({spec.nl_max_tokens})，区间为空"
            )
        if spec.tag_max_count and spec.tag_min_count > spec.tag_max_count:
            raise ConfigError(
                f"output.tag_min_count({spec.tag_min_count}) 大于 "
                f"tag_max_count({spec.tag_max_count})，区间为空"
            )
        return spec


@dataclass(slots=True)
class OpsConfig:
    """非模型相关的运行参数：硬件保护、分包、质量、上传、画师标签。"""

    limits: dict[str, Any] = field(default_factory=dict)
    archive: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    upload: dict[str, Any] = field(default_factory=dict)
    artist: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    distribute: dict[str, Any] = field(default_factory=dict)
    merge: dict[str, Any] = field(default_factory=dict)
    tagging: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)
    variants: dict[str, Any] = field(default_factory=dict)

    def report_batch_size(self) -> int:
        """报告的批次大小。配置只放默认值，命令行可覆盖。

        批次大小是尺度问题，不是对错问题：格越小越灵敏也越吵。
        所以它必须是配置项，而不是代码里的常量。
        """
        try:
            size = int((self.report or {}).get("batch_size", 1000))
        except (TypeError, ValueError):
            return 1000
        return size if size > 0 else 1000

    def output_spec(self) -> OutputSpec:
        return OutputSpec.from_dict(self.output)

    def artist_rule(self):
        from .artist import ArtistRule

        return ArtistRule.from_dict(self.artist)

    def guard_policy(self):
        """延迟导入，避免 config 依赖 guard。"""
        from .guard import GuardPolicy

        return GuardPolicy.from_dict(self.limits or {})

    def quality_policy(self):
        from .quality import QualityPolicy

        p = QualityPolicy.from_dict(self.quality or {})
        # NL 预算只有一个真值源（output 段），质检这边跟着走。
        # 两处各写一份迟早会对不上，到时候"生成了 256 却按 128 判定不合格"。
        spec = self.output_spec()
        if spec.nl_min_tokens:
            p.nl_min_tokens = spec.nl_min_tokens
        if spec.nl_max_tokens:
            p.nl_max_tokens = spec.nl_max_tokens
        return p

    def pack_options(self):
        from .pack import PackOptions

        return PackOptions.from_dict(self.archive or {})

    def ftp_config(self):
        from .upload import FtpConfig

        u = self.upload or {}
        # 密码优先读环境变量，避免凭据进版本库
        import os

        password = os.environ.get("TAGGER_FTP_PASSWORD") or str(u.get("password") or "")
        return FtpConfig(
            host=str(u.get("host") or ""),
            user=str(u.get("user") or ""),
            password=password,
            remote_dir=str(u.get("remote_dir") or "/"),
            port=int(u.get("port", 21)),
            passive=bool(u.get("passive", True)),
            timeout_s=int(u.get("timeout_s", 120)),
            use_tls=str(u.get("protocol", "ftp")).lower() in ("ftps", "sftp-tls"),
        )


def load_ops(table: dict[str, Any]) -> OpsConfig:
    return OpsConfig(
        limits=table.get("limits") or {},
        archive=table.get("archive") or {},
        quality=table.get("quality") or {},
        upload=table.get("upload") or {},
        artist=table.get("artist") or {},
        output=table.get("output") or {},
        distribute=table.get("distribute") or {},
        merge=table.get("merge") or {},
        tagging=table.get("tagging") or {},
        report=table.get("report") or {},
        variants=table.get("variants") or {},
    )


def load_tier_table(path: str | Path = DEFAULT_TIERS, *, overlay: bool = True) -> dict[str, Any]:
    """读分档表，并把「模型管理」写的覆盖清单叠上去。

    覆盖只作用于 `models` 与 `tagging.models` 两段——分档（tiers）是硬件口径，
    不该被模型清单连带改掉。overlay=False 用于"我要看原始配置"的场合（测试、
    诊断），默认开着，否则页面上加的模型对 CLI 不生效，等于两个世界。
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"分档表不存在: {p}")
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"分档表格式不对: {p}")
    return apply_model_overlay(data, load_model_overlay()) if overlay else data


def overlay_path() -> Path:
    """覆盖清单的位置。运行时读常量，测试才好把它指到临时目录。"""
    return Path(MODEL_OVERLAY)


def load_model_overlay() -> dict[str, Any]:
    """读覆盖清单。文件不在 / 坏了都返回空覆盖——模型清单坏了不该让打标开不了机。

    结构（缺项一律当空）：
        vlm       {key: {path, precision, family, revision, quantized_path}}
        tag       {backend: repo}
        notes     {"vlm:<key>": 说明, "tag:<backend>": 说明}
        hidden    ["<vlm key>"]      ← 从 tiers.yaml 里删掉的
        tag_hidden["<backend>"]
    """
    empty: dict[str, Any] = {"vlm": {}, "tag": {}, "notes": {}, "dirs": {},
                             "proxy": "", "hidden": [], "tag_hidden": []}
    p = overlay_path()
    if not p.is_file():
        return empty
    try:
        import json

        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(raw, dict):
        return empty
    out = {
        "vlm": dict(raw.get("vlm") or {}),
        "tag": dict(raw.get("tag") or {}),
        "notes": {str(k): str(v) for k, v in (raw.get("notes") or {}).items()},
        # 按模型记的下载目录（"vlm:<key>" / "tag:<backend>"）。不属于分档表，
        # 只有 WebUI 的下载/删除会读，所以不进 apply_model_overlay。
        "dirs": {str(k): str(v) for k, v in (raw.get("dirs") or {}).items()},
        # 下载代理（http:// / socks5://），空 = 直连。同上，不进分档表。
        "proxy": str(raw.get("proxy") or ""),
        "hidden": [str(k) for k in (raw.get("hidden") or [])],
        "tag_hidden": [str(k) for k in (raw.get("tag_hidden") or [])],
    }
    return out


def apply_model_overlay(table: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """把覆盖清单合并进分档表（返回新 dict，不改入参）。"""
    if not overlay:
        return table
    models = dict(table.get("models") or {})
    tagging = dict(table.get("tagging") or {})
    tag_models = dict(tagging.get("models") or {})
    for key in overlay.get("hidden") or []:
        models.pop(str(key), None)
    for key, spec in (overlay.get("vlm") or {}).items():
        if isinstance(spec, dict) and str(spec.get("path") or "").strip():
            models[str(key)] = dict(spec)
    for key in overlay.get("tag_hidden") or []:
        tag_models.pop(str(key), None)
    for key, repo in (overlay.get("tag") or {}).items():
        if str(repo or "").strip():
            tag_models[str(key)] = str(repo)
    if not overlay.get("vlm") and not overlay.get("tag") \
            and not overlay.get("hidden") and not overlay.get("tag_hidden"):
        return table
    out = dict(table)
    out["models"] = models
    out["tagging"] = {**tagging, "models": tag_models}
    return out


def resolve_buckets(raw: Any, max_pixels: int) -> list[Bucket]:
    """解析 defaults.buckets，生成分桶表。

    支持三种写法：
        buckets: false              → 不分桶，只按 max_pixels 等比缩放
        buckets: [1.0, 1.5, 1.78]   → 只给比例，用默认对齐
        buckets: {ratios: [...], align: 32}
    """
    if raw is False:
        return []

    ratios: list[float] | None = None
    align = 32

    if isinstance(raw, dict):
        if raw.get("enabled") is False:
            return []
        if raw.get("ratios"):
            ratios = [float(r) for r in raw["ratios"]]
        align = int(raw.get("align", align))
    elif isinstance(raw, (list, tuple)):
        ratios = [float(r) for r in raw]
    elif isinstance(raw, str) and raw.lower() in ("off", "none", "false"):
        return []

    if ratios is None:
        from .bucket import DEFAULT_RATIOS

        ratios = list(DEFAULT_RATIOS)

    try:
        return compute_buckets(max_pixels, ratios, align=align)
    except ValueError as exc:
        raise ConfigError(
            f"分桶配置有误（max_pixels={max_pixels}, align={align}）：{exc}"
        ) from exc


def resolve_profile(
    table: dict[str, Any],
    gpu: GPUInfo,
    *,
    model_override: str | None = None,
    tier_override: str | None = None,
    precision_override: str | None = None,
    max_pixels_override: int | None = None,
    max_num_seqs_override: int | None = None,
    max_num_batched_tokens_override: int | None = None,
    gpu_mem_override: float | None = None,
) -> RunProfile:
    """把分档表和硬件合成为一个可直接启动的 profile。"""

    defaults: dict[str, Any] = table.get("defaults") or {}
    raw_models: dict[str, Any] = table.get("models") or {}
    raw_tiers: list[dict[str, Any]] = table.get("tiers") or []

    if not raw_models:
        raise ConfigError("分档表里没有定义任何模型（models 段为空）")
    if not raw_tiers:
        raise ConfigError("分档表里没有定义任何分档（tiers 段为空）")

    models: dict[str, ModelSpec] = {}
    for key, m in raw_models.items():
        models[key] = ModelSpec(
            key=key,
            path=m["path"],
            revision=m.get("revision"),
            family=m.get("family", "unknown"),
            quantized_path=m.get("quantized_path"),
            precision=m.get("precision", "int4"),
            # GGUF 只在 engine=llamacpp 时用；没配就由 nl_engine 报一句
            # 带下载指引的人话，而不是在这里静默用 vLLM 的路径去启动。
            gguf=str(m.get("gguf") or ""),
            mmproj=str(m.get("mmproj") or ""),
        )

    # 选分档
    tier: TierSpec | None = None
    for t in raw_tiers:
        spec = TierSpec(
            name=t["name"],
            vram_min=float(t.get("vram_min", 0)),
            vram_max=float(t.get("vram_max", 999)),
            model=t["model"],
            precision=t.get("precision"),
            max_model_len=int(t.get("max_model_len", 8192)),
            max_num_seqs=int(t.get("max_num_seqs", 8)),
            max_num_batched_tokens=int(t.get("max_num_batched_tokens", 0) or 0),
            cc_min=_parse_cc(t.get("cc_min")),
            note=t.get("note", ""),
        )
        if tier_override:
            if spec.name == tier_override:
                tier = spec
                break
            continue
        if not (spec.vram_min <= gpu.vram_gb < spec.vram_max):
            continue
        if spec.cc_min and gpu.compute_cap and gpu.compute_cap < spec.cc_min:
            continue
        tier = spec
        break

    if tier is None:
        available = ", ".join(f"{t['name']}({t.get('vram_min', 0)}-{t.get('vram_max', 999)}G)"
                              for t in raw_tiers)
        if tier_override:
            # 指定了档位却没匹配上，原因只有一个：名字写错了。这时候说
            # "没有匹配到分档：显存 16 GiB" 会把人引到显存上排查。
            raise ConfigError(
                f"分档表里没有 {tier_override!r} 这一档。可用分档：{available}。"
            )
        raise ConfigError(
            f"没有匹配到分档：显存 {gpu.vram_gb} GiB。可用分档：{available}。"
            f"可以用 --tier 强制指定。"
        )

    model_key = model_override or tier.model
    if model_key not in models:
        raise ConfigError(f"分档 {tier.name} 引用了不存在的模型 {model_key}，"
                          f"可用：{', '.join(models)}")
    model = models[model_key]

    # 精度优先级：界面/命令行显式指定 > 分档表 > 模型自带。显式指定的值先
    # 归一化（bf16/fp8/w4a16），否则同一个 fp8 会以 fp8_e4m3、fp8_dynamic
    # 等几种写法进指纹，事后没法按精度对账。未指定时保持分档表原样，避免
    # 把已有的 int4 口径改成新名字。
    wanted = normalize_quant_precision(precision_override)
    precision = wanted or (tier.precision or model.precision or "int4").lower()
    _check_precision_supported(precision, gpu, model_key=model.key)

    max_pixels = int(max_pixels_override or defaults.get("max_pixels", 589824))
    buckets = resolve_buckets(defaults.get("buckets", True), max_pixels)

    profile = RunProfile(
        tier=tier.name,
        model=model,
        precision=precision,
        max_pixels=max_pixels,
        max_model_len=tier.max_model_len,
        max_num_seqs=int(max_num_seqs_override or tier.max_num_seqs),
        max_num_batched_tokens=int(max_num_batched_tokens_override
                                   or tier.max_num_batched_tokens),
        gpu_memory_utilization=float(
            gpu_mem_override or defaults.get("gpu_memory_utilization", 0.88)
        ),
        temperature=float(defaults.get("temperature", 0.2)),
        top_p=float(defaults.get("top_p", 0.9)),
        max_tokens=int(defaults.get("max_tokens", 512)),
        request_timeout_s=int(defaults.get("request_timeout_s", 90)),
        device_name=gpu.name,
        vram_gb=gpu.vram_gb,
        buckets=buckets,
    )

    if not model.revision:
        profile.warnings.append(
            "模型 revision 未锁定（null）。换版本会让数据集出现代际差异，"
            "正式跑全量前必须 pin 到 commit hash。"
        )
    if not model.quantized_path and precision in ("int4", "awq", "gptq", "w4a16"):
        profile.warnings.append(
            f"precision={precision} 但 quantized_path 为空，将回退到原始权重 "
            f"({model.path})，实际会以 bf16 加载。请先生成或指定量化权重。"
        )
    profile.warnings.extend(_estimate_vram_headroom_check(profile, gpu))
    profile.warnings.extend(_bucket_warnings(profile))

    return profile


def _bucket_warnings(profile: RunProfile) -> list[str]:
    """分桶的坑要提前说，不要等数据出来了才发现某档桶天生填不满。"""
    warns: list[str] = []
    if not profile.buckets:
        return warns

    tok = [b.tokens for b in profile.buckets]
    spread = max(tok) / min(tok) if min(tok) else 0
    if spread > 1.15:
        thin = min(profile.buckets, key=lambda b: b.tokens)
        warns.append(
            f"分桶的 token 数不齐（{min(tok)}～{max(tok)}，差 {spread:.2f} 倍）。"
            f"最省的 {thin.name} 只有 {thin.pixels} px，是 32 对齐的几何限制，"
            f"不是配置写错。若在意均匀性，可把 max_pixels 上调到能整除的档位。"
        )
    return warns
