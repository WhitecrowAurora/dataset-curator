"""The dense Qwen3-VL text configuration used by the caption decoder."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DecoderConfig:
    vocab_size: int
    hidden_size: int
    intermediate_size: int
    num_hidden_layers: int
    num_attention_heads: int
    num_key_value_heads: int
    head_dim: int
    rms_norm_eps: float
    rope_theta: float
    mrope_section: tuple[int, int, int]

    @classmethod
    def from_hf(cls, config) -> DecoderConfig:
        rope = getattr(config, "rope_parameters", None) or config.rope_scaling
        if rope.get("rope_type", "default") != "default":
            raise ValueError("The caption decoder currently supports default MRoPE only")
        return cls(
            config.vocab_size, config.hidden_size, config.intermediate_size,
            config.num_hidden_layers, config.num_attention_heads,
            config.num_key_value_heads, config.head_dim, config.rms_norm_eps,
            rope.get("rope_theta", getattr(config, "rope_theta", 5000000.0)),
            tuple(rope["mrope_section"]),
        )

