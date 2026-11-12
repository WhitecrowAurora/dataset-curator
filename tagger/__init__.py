"""tagger —— 分布式 NL+tag 打标框架。

模块划分：
    hardware      硬件体检（显卡型号 / 显存 / 算力等级）
    config        分档表解析，硬件 + 配置 → 可启动的 RunProfile
    model_loader  vLLM 服务启动与就绪等待
    schema        标注记录数据模型（主键含 model_id，支持多模型并存）

设计前提：所有"待决策"的参数都放在 configs/tiers.yaml，代码里不留魔法数字。
主创定了口径之后改配置即可，不用改代码。
"""

__version__ = "0.1.0"

from .config import ConfigError, ModelSpec, RunProfile, TierSpec, load_tier_table, resolve_profile
from .hardware import GPUInfo, describe, query_gpus, visible_gpus
from .schema import Annotation, Provenance, QC, WorkItem

__all__ = [
    "Annotation",
    "ConfigError",
    "GPUInfo",
    "ModelSpec",
    "Provenance",
    "QC",
    "RunProfile",
    "TierSpec",
    "WorkItem",
    "__version__",
    "describe",
    "load_tier_table",
    "query_gpus",
    "resolve_profile",
    "visible_gpus",
]
