"""发色名称与复核请求；具体浅色不合并成某一种发色。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

COLORS = {
    "aqua": ("青色", "#70d7cf"), "blue": ("蓝色", "#537fc7"),
    "green": ("绿色", "#72a66a"), "brown": ("棕色", "#8b634b"),
    "orange": ("橙色", "#de934e"), "pink": ("粉色", "#e2a0be"),
    "purple": ("紫色", "#9b7bb6"), "red": ("红色", "#b95c62"),
    "light": ("浅色组", "#e3e1d8"), "black": ("黑色", "#343440"),
    "white": ("白色", "#eeeef2"), "silver": ("银色", "#c8cdd6"),
    "grey": ("灰色", "#92949c"), "blonde": ("金色", "#e7cb82"),
}
LABELS = ("aqua", "blue", "green", "brown", "orange", "pink", "purple", "red", "light", "black")


def color_name(value: str) -> str:
    name = value.strip().lower().replace("_", " ")
    if name.endswith(" hair"):
        name = name[:-5].strip()
    name = {"gray": "grey", "blond": "blonde", "cyan": "aqua"}.get(name, name)
    if name not in COLORS:
        raise ValueError(f"不支持的发色：{value}")
    return name


def candidate(color: str, score: float) -> dict:
    title, swatch = COLORS[color]
    return {"color": color, "label": title, "probability": float(score), "swatch": swatch}


class HairReviewRequest(BaseModel):
    image_base64: str = Field(min_length=1)
    image_name: str = "图片"
    # 留空调用 PixAI；可复用已有打标结果，不重复运行主分类器。
    pixai_color: str = ""
    crop: tuple[int, int, int, int] | None = None
    qwen_url: str = "http://127.0.0.1:8000"
    qwen_model: str = "tagger"
    media: Literal["anime", "photo", "unknown"] = "unknown"
    visible_hair: Literal["yes", "no", "unknown"] = "unknown"
    pixai_scores: dict[str, float] | None = None
    parent_id: str = ""


class HairWorkflowSettings(BaseModel):
    enabled: bool = False
    media: Literal["anime", "photo", "unknown"] = "unknown"
    min_confidence: float = Field(default=.7, ge=0, le=1)
    cache_enabled: bool = True
    cache_max_gb: float = Field(default=0, ge=0, le=1024)
    memory_reserve_gb: float = Field(default=0, ge=0, le=1024)
    torii_dir: str = "models/nl/torii-gate-0.5"
    torii_engine: Literal["llamacpp", "native"] = "llamacpp"
    qwen_dir: str = "models/nl/qwen3vl-8b-abliterated/qwen3vl-8b-abliterated-Q6_K.gguf"
    torii_url: str = ""
    torii_model: str = "ToriiGate-0.5"
    qwen_url: str = ""
    qwen_model: str = "tagger"


class HairDecisionRequest(BaseModel):
    colors: list[str] = Field(default_factory=list, max_length=14)
    no_hair: bool = False
