"""Annotation record schema.

设计要点：
- 主键是 (image_sha256, model_id)，不是单独的 image_sha256。
  这是为了支持同一张图被多个模型分别标注、并存比较；
  训练时再按需要挑一版或做配比，而不是打标时就把另一版覆盖掉。
- provenance 里每个字段都要记。没有版本号的标注数据等于不可复现的实验，
  将来无法回答"为什么这一版模型变差了"。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any


SCHEMA_VERSION = 1


@dataclass(slots=True)
class Provenance:
    """标注来源指纹。任何一个字段变了，就是新一代数据。

    字段清单必须与 client._prov_base / client._provenance 实际写进产物的
    键一一对应——本类是 Annotation.from_jsonl 读回产物的构造器，清单漏一
    个键，读回就在 Provenance(**prov) 上炸；多一个键就是文档撒谎。
    tests/test_fixes.py 里有键位一致性测试锁着，两边改动必须同步。
    """

    model_id: str            # 模型标识，形如 "qwen3vl-8b@a1b2c3d"
    model_path: str
    model_revision: str | None
    precision: str           # bf16 / fp8 / int4
    max_pixels: int
    prompt_ver: str
    temperature: float
    seed: int | None
    vllm_version: str | None
    tier: str                # 分档名，如 "mid-24g"
    device: str              # 设备名，用于定位异常批次
    # ---- 以下字段实际产物里早就有，清单以前漏了。没有默认值的话，
    # 读回真实产物会在多余键上报 TypeError。
    prompt_sha: str = ""     # 完整指令的 sha256 前 12 位；AB 变体常只差一句话，
                             # prompt_ver 分不出来，靠它回答"这句 tag 是哪套指令逼出来的"
    variant: str = ""        # AB 实验变体名，未启用时为空
    tag_backend: str = ""    # tag 由谁出（wd-eva02 / onnx / vlm），只进 tag 产物
    tag_precision: str = ""  # tag 后端的精度，与上面的 precision（VLM 的）分开记
    # 预处理放在哪（pil / tensor）。重采样实现不同，tag 集合会有差异，
    # 事后比两批 tag 数量时得能分辨是口径变了还是模型变了。
    tag_preprocess: str = ""
    # 描述长度引导（字符区间）。它是产出分布的一部分：400-1024 → 250-500
    # 会少三成字数、快三成半，事后混批时只能靠它分辨。
    caption_chars: list[int] | None = None
    # 推理来源：local（本机/自建 vLLM·llama.cpp）/ openai / anthropic。
    # 同一个 model_id 在两处是两套输出（量化、版本、上下文都不同），混批之后
    # 只能靠这一项分辨。给默认值是为了让**旧产物**（没有这个键）仍能读回——
    # 新增必填键会让历史数据在读回时直接 TypeError。
    provider: str = "local"
    # 这条是结果缓存还原的（没经历本次推理）。命中才写，未命中时产物里没有这个键。
    cache_hit: bool = False


@dataclass(slots=True)
class QC:
    """质检结果。只记录，不阻断写入。"""

    tag_coverage: float = 0.0        # 高置信 tag 被 caption 覆盖的比例
    caption_chars: int = 0
    flags: list[str] = field(default_factory=list)   # e.g. ["low_coverage", "refused"]
    bucket: str = ""                 # visual / text / nsfw / unknown
    # NL 的实际 token 数，取自 API 的 usage.completion_tokens。
    # 用 token 而不是字符数做长度预算，是因为不同语言和标点密度下字符数
    # 的换算浮动太大，128/256 这种预算只有 token 口径才说得准。
    nl_tokens: int = 0


@dataclass(slots=True)
class Annotation:
    """一条标注记录 = 一张图 × 一个模型。"""

    image_sha256: str
    image_path: str                  # 相对数据集根目录的路径，不含绝对路径
    tags: list[tuple[str, float]]    # [(tag, prob), ...] 降序
    caption_dense: str = ""          # 长描述，主体→环境→构图→光影→文字
    caption_medium: str = ""
    caption_short: str = ""
    tag_line: str = ""               # 短档 caption 的 tag 形态，用于短 prompt 分布
    # 画师标签。artist 是原始拼写（权威、可追溯到目录），artist_tag 是注入
    # caption 的形式（含 @ 前缀，可能是别名表映射后的英文）。两者分开存，
    # 是为了改名时能追溯原文，也为了让下游能选择只按原始名分组。
    artist: str = ""
    artist_tag: str = ""
    provenance: Provenance | None = None
    qc: QC = field(default_factory=QC)
    ok: bool = True
    error: str = ""
    wall_ms: int = 0
    ts: float = field(default_factory=time.time)
    schema_version: int = SCHEMA_VERSION

    @property
    def key(self) -> tuple[str, str]:
        """幂等主键。重复写入同一 key 视为覆盖，不是重复数据。"""
        model_id = self.provenance.model_id if self.provenance else ""
        return (self.image_sha256, model_id)

    def to_jsonl(self) -> str:
        d = asdict(self)
        # tuple 会被转成 list，这里保持 tags 为 [[tag, prob], ...]
        return json.dumps(d, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_jsonl(cls, line: str) -> "Annotation":
        d: dict[str, Any] = json.loads(line)
        prov = d.pop("provenance", None)
        qc = d.pop("qc", None)
        d["tags"] = [(t, p) for t, p in d.get("tags", [])]
        ann = cls(**{k: v for k, v in d.items() if k in cls.__slots__})
        if prov:
            ann.provenance = Provenance(**prov)
        if qc:
            ann.qc = QC(**qc)
        return ann


@dataclass(slots=True)
class WorkItem:
    """一个待打标样本。"""

    image_sha256: str
    image_path: str
    width: int = 0
    height: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
