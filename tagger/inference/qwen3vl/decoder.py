"""Dense Qwen3-VL decoding with preallocated KV storage and PyTorch SDPA."""
from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F

from .cache import KVCache
from .config import DecoderConfig
from .matrices.projections import project


class RMSNorm(nn.Module):
    def __init__(self, size: int, eps: float, optimized=False):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(size))
        self.eps = eps
        self.optimized = optimized

    def forward(self, x):
        # BF16 reduction ties differ in the fused kernel; retain eager norms.
        if (self.optimized and x.is_cuda and x.dtype != torch.bfloat16
                and torch.cuda.get_device_capability(x.device) >= (8, 0)):
            from .kernels.elementwise import rms_norm

            return rms_norm(x, self.weight, self.eps)
        value = x.float()
        return (value * torch.rsqrt(value.square().mean(-1, keepdim=True) + self.eps)).to(x.dtype) * self.weight

    def add(self, residual, value):
        if (self.optimized and residual.is_cuda and residual.dtype != torch.bfloat16
                and torch.cuda.get_device_capability(residual.device) >= (8, 0)):
            from .kernels.elementwise import residual_rms_norm

            return residual_rms_norm(residual, value, self.weight, self.eps)
        residual = residual + value
        return residual, self(residual)


class Rotary(nn.Module):
    def __init__(self, config: DecoderConfig):
        super().__init__()
        dim = config.head_dim
        self.register_buffer("inv_freq", 1.0 / (config.rope_theta ** (
            torch.arange(0, dim, 2, dtype=torch.float32) / dim)), persistent=False)
        self.section = config.mrope_section

    def forward(self, positions, dtype):
        freq = positions.float().unsqueeze(-1) * self.inv_freq.float()
        merged = freq[0].clone()
        for axis in (1, 2):
            merged[..., axis:self.section[axis] * 3:3] = freq[axis, ..., axis:self.section[axis] * 3:3]
        emb = torch.cat((merged, merged), dim=-1)
        return emb.cos().to(dtype).unsqueeze(1), emb.sin().to(dtype).unsqueeze(1)


def rotate(x, cos, sin):
    a, b = x.chunk(2, dim=-1)
    return x * cos + torch.cat((-b, a), dim=-1) * sin


class Attention(nn.Module):
    prefill_attention = "sdpa"

    def __init__(self, config: DecoderConfig, optimized=False, decode_attention="sdpa"):
        super().__init__()
        self.heads, self.kv_heads, self.dim = (
            config.num_attention_heads, config.num_key_value_heads, config.head_dim)
        self.q_proj = nn.Linear(config.hidden_size, self.heads * self.dim, bias=False)
        self.k_proj = nn.Linear(config.hidden_size, self.kv_heads * self.dim, bias=False)
        self.v_proj = nn.Linear(config.hidden_size, self.kv_heads * self.dim, bias=False)
        self.o_proj = nn.Linear(self.heads * self.dim, config.hidden_size, bias=False)
        self.q_norm = RMSNorm(self.dim, config.rms_norm_eps, optimized)
        self.k_norm = RMSNorm(self.dim, config.rms_norm_eps, optimized)
        self.optimized = optimized
        self.decode_attention = decode_attention

    def forward(self, x, rotary, mask, cache: KVCache, layer: int):
        batch, seq, _ = x.shape
        accelerated = self.optimized and x.is_cuda and torch.cuda.get_device_capability(x.device) >= (8, 0)
        q, k, v = project(x, self.q_proj, self.k_proj, self.v_proj, optimized=self.optimized)
        v = v.reshape(batch, seq, self.kv_heads, self.dim).transpose(1, 2)
        if accelerated and x.dtype != torch.bfloat16:
            from .kernels.attention import normalize_rotate

            q, k = normalize_rotate(q, k, self.q_norm.weight, self.k_norm.weight,
                                    *rotary, self.dim, self.q_norm.eps)
        else:
            q = self.q_norm(q.reshape(batch, seq, self.heads, self.dim)).transpose(1, 2)
            k = self.k_norm(k.reshape(batch, seq, self.kv_heads, self.dim)).transpose(1, 2)
            q, k = rotate(q, *rotary), rotate(k, *rotary)
        k, v = cache.write(layer, k, v)
        if (accelerated and self.decode_attention == "native" and seq == 1
                and k.shape[2] <= 4096 and self.dim <= 256):
            from .kernels.decode_attention import decode_attention

            value = decode_attention(q, k, v, mask, length=getattr(cache, "decode_length", None))
        elif seq > 1 and q.is_cuda and self.prefill_attention == "cudnn":
            from .kernels.prefill_attention import cudnn_attention

            value = cudnn_attention(q, k, v, mask)
        elif seq > 1 and accelerated and k.shape[2] <= 4096:
            from .kernels.math_attention import prefill_attention

            value = prefill_attention(q, k, v, mask)
        else:
            value = F.scaled_dot_product_attention(q, k, v, attn_mask=mask, enable_gqa=True)
        return self.o_proj(value.transpose(1, 2).reshape(batch, seq, -1))


class MLP(nn.Module):
    def __init__(self, config: DecoderConfig, optimized=False):
        super().__init__()
        self.gate_proj = nn.Linear(config.hidden_size, config.intermediate_size, bias=False)
        self.up_proj = nn.Linear(config.hidden_size, config.intermediate_size, bias=False)
        self.down_proj = nn.Linear(config.intermediate_size, config.hidden_size, bias=False)
        self.optimized = optimized

    def forward(self, x):
        gate, up = project(x, self.gate_proj, self.up_proj, optimized=self.optimized)
        if self.optimized and x.is_cuda and torch.cuda.get_device_capability(x.device) >= (8, 0):
            from .kernels.elementwise import silu_gate

            return self.down_proj(silu_gate(gate, up))
        return self.down_proj(F.silu(gate) * up)


class DecoderLayer(nn.Module):
    def __init__(self, config: DecoderConfig, optimized=False, decode_attention="sdpa"):
        super().__init__()
        self.self_attn = Attention(config, optimized, decode_attention)
        self.mlp = MLP(config, optimized)
        self.input_layernorm = RMSNorm(config.hidden_size, config.rms_norm_eps, optimized)
        self.post_attention_layernorm = RMSNorm(config.hidden_size, config.rms_norm_eps, optimized)

    def forward(self, x, rotary, mask, cache, layer):
        attention = self.self_attn(self.input_layernorm(x), rotary, mask, cache, layer)
        x, normalized = self.post_attention_layernorm.add(x, attention)
        return x + self.mlp(normalized)


class CaptionDecoder(nn.Module):
    def __init__(self, config: DecoderConfig, optimized=False, decode_attention="sdpa"):
        super().__init__()
        self.config = config
        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size)
        self.layers = nn.ModuleList(DecoderLayer(config, optimized, decode_attention) for _ in range(config.num_hidden_layers))
        self.norm = RMSNorm(config.hidden_size, config.rms_norm_eps, optimized)
        self.rotary_emb = Rotary(config)

    def embed(self, ids: torch.Tensor, dtype: torch.dtype) -> torch.Tensor:
        if hasattr(self.embed_tokens, "rows"):
            return self.embed_tokens.rows(ids, dtype)
        return self.embed_tokens(ids)

    def forward(self, inputs_embeds, positions, valid, cache, *, visual_mask=None, deepstack=()):
        mask = cache.prepare(valid)
        rotary = self.rotary_emb(positions, inputs_embeds.dtype)
        x = inputs_embeds
        for i, layer in enumerate(self.layers):
            x = layer(x, rotary, mask, cache, i)
            if i < len(deepstack):
                x = x.clone()
                x[visual_mask] += deepstack[i].to(x.dtype)
        cache.length += inputs_embeds.shape[1]
        return self.norm(x)

