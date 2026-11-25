"""One-token depthwise convolution and in-place state shift in one launch."""
from __future__ import annotations

import torch
import triton
import triton.language as tl
from triton.language.extra.cuda.libdevice import exp


@triton.jit
def _update(X, STATE, WEIGHT, Y, CHANNELS: tl.constexpr, WIDTH: tl.constexpr,
            XB: tl.constexpr, XC: tl.constexpr, SB: tl.constexpr, SC: tl.constexpr,
            ST: tl.constexpr, WC: tl.constexpr, WT: tl.constexpr,
            BLOCK: tl.constexpr):
    batch = tl.program_id(0)
    channel = tl.program_id(1) * BLOCK + tl.arange(0, BLOCK)
    valid = channel < CHANNELS
    state = STATE + batch * SB + channel * SC
    # Every program owns complete channel states. Reads precede the corresponding
    # earlier-position writes, so slots never race with neighbouring programs.
    total = tl.full((BLOCK,), 0, tl.float32)
    for offset in tl.static_range(WIDTH - 1):
        value = tl.load(state + (offset + 1) * ST, valid, 0)
        weight = tl.load(WEIGHT + channel * WC + offset * WT, valid, 0)
        total = tl.fma(value.to(tl.float32), weight.to(tl.float32), total)
        tl.store(state + offset * ST, value, valid)
    value = tl.load(X + batch * XB + channel * XC, valid, 0)
    weight = tl.load(WEIGHT + channel * WC + (WIDTH - 1) * WT, valid, 0)
    total = tl.fma(value.to(tl.float32), weight.to(tl.float32), total)
    tl.store(state + (WIDTH - 1) * ST, value, valid)
    # conv1d rounds before SiLU, which evaluates in FP32 and then rounds again.
    activated = total.to(value.dtype).to(tl.float32)
    activated = tl.div_rn(activated, 1.0 + exp(-activated))
    tl.store(Y + batch * CHANNELS + channel, activated, valid)


def update(hidden_states, conv_state, weight, bias=None, activation=None):
    from transformers.models.qwen3_5.modeling_qwen3_5 import torch_causal_conv1d_update

    if (not hidden_states.is_cuda or hidden_states.dtype != torch.float16
            or hidden_states.shape[-1] != 1 or weight.dtype != hidden_states.dtype
            or conv_state.dtype != hidden_states.dtype
            or conv_state.shape[-1] != weight.shape[-1] or bias is not None):
        return torch_causal_conv1d_update(hidden_states, conv_state, weight, bias, activation)
    batch, channels, _ = hidden_states.shape
    out = torch.empty_like(hidden_states, memory_format=torch.contiguous_format)
    with torch.cuda.device(hidden_states.device):
        _update[(batch, triton.cdiv(channels, 128))](
            hidden_states, conv_state, weight, out, channels, weight.shape[-1],
            *hidden_states.stride()[:2], *conv_state.stride(), *weight.stride(), 128,
            num_warps=4, enable_fp_fusion=False)
    return out
