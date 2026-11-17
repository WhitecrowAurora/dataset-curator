"""Recreate nonpersistent vision buffers across Transformers constructor APIs."""
from __future__ import annotations

from inspect import signature


def load_rotary(config, *, device):
    from transformers.models.qwen3_vl.modeling_qwen3_vl import (
        Qwen3VLVisionRotaryEmbedding,
    )

    argument = (config if "config" in signature(Qwen3VLVisionRotaryEmbedding).parameters
                else config.hidden_size // config.num_heads // 2)
    return Qwen3VLVisionRotaryEmbedding(argument).to(device)
