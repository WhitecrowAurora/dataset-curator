"""Single-token grouped attention without duplicating the shared KV cache."""
from __future__ import annotations

import inspect
from types import FunctionType, MethodType

import torch


def decode_attention(module, query, key, value, attention_mask, dropout=0.0, scaling=None, **kwargs):
    from transformers.integrations.sdpa_attention import sdpa_attention_forward

    if (query.is_cuda and query.shape[2] == 1 and attention_mask is not None
            and attention_mask.dtype == torch.bool and dropout == 0
            and attention_mask.shape[1:3] == (1, 1)
            and (scaling is None or scaling == query.shape[-1] ** -0.5)):
        batch, heads, _, width = query.shape
        # At one token, the query heads sharing each KV head can occupy the
        # query-length axis. SDPA then reads each KV head once, with no repeat_kv.
        grouped = query.reshape(batch, key.shape[1], heads // key.shape[1], width)
        output = torch.nn.functional.scaled_dot_product_attention(
            grouped, key, value, attn_mask=attention_mask, scale=scaling, is_causal=False)
        return output.reshape(batch, heads, 1, width).transpose(1, 2).contiguous(), None
    return sdpa_attention_forward(module, query, key, value, attention_mask,
                                  dropout=dropout, scaling=scaling, **kwargs)


def configure(model):
    from transformers.modeling_utils import AttentionInterface
    from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5Attention

    # Keep Transformers' rotary, gating and cache logic. Only this model's
    # attention dispatch changes; other model instances retain their defaults.
    interfaces = AttentionInterface()
    interfaces['sdpa'] = decode_attention
    original = inspect.unwrap(Qwen3_5Attention.forward)
    forward = FunctionType(original.__code__, dict(original.__globals__, ALL_ATTENTION_FUNCTIONS=interfaces),
                           original.__name__, original.__defaults__, original.__closure__)
    forward.__kwdefaults__ = original.__kwdefaults__
    for layer in model.model.language_model.layers:
        if hasattr(layer, 'self_attn'):
            layer.self_attn.forward = MethodType(forward, layer.self_attn)
