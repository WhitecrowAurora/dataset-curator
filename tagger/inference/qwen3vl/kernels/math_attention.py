"""Math SDPA with its original GEMMs and one fused all-masked-row cleanup."""
from __future__ import annotations

import torch
import triton
import triton.language as tl
from torch.nn import functional as F


@triton.jit
def _clear_masked(SCORES, OUT, WIDTH: tl.constexpr, BLOCK: tl.constexpr):
    row = tl.program_id(0)
    columns = tl.arange(0, BLOCK)
    scores = tl.load(SCORES + row * WIDTH + columns, columns < WIDTH, -float('inf'))
    fully_masked = tl.sum((scores != -float('inf')).to(tl.int32), 0) == 0
    if fully_masked:
        tl.store(OUT + row * WIDTH + columns, 0, columns < WIDTH)


def prefill_attention(q, k, v, mask):
    # Retain the installed dispatcher's choice of Flash/efficient/cuDNN kernels.
    backend = torch.ops.aten._fused_sdp_choice(q, k, v, mask, enable_gqa=True)
    if backend != 0:
        return F.scaled_dot_product_attention(q, k, v, attn_mask=mask, enable_gqa=True)
    dtype = q.dtype
    promote = (dtype in (torch.float16, torch.bfloat16)
               and not torch.backends.cuda.fp16_bf16_reduction_math_sdp_allowed())
    accumulator = torch.float32 if promote else dtype
    scale = q.shape[-1] ** -0.25
    query = q.to(accumulator) * scale
    key, value = k.to(accumulator), v.to(accumulator)
    if q.shape[-3] != k.shape[-3]:
        key = key.repeat_interleave(q.shape[-3] // k.shape[-3], -3)
        value = value.repeat_interleave(q.shape[-3] // v.shape[-3], -3)
    scores = torch.matmul(query, key.transpose(-2, -1) * scale)
    if mask is not None:
        if mask.dtype == torch.bool:
            mask = torch.zeros_like(mask, dtype=dtype).masked_fill_(~mask, -float('inf'))
        scores.add_(mask)
    probabilities = scores.softmax(-1)
    with torch.cuda.device(q.device):
        _clear_masked[(scores.numel() // scores.shape[-1],)](
            scores, probabilities, scores.shape[-1], triton.next_power_of_2(scores.shape[-1]),
            num_warps=4)
    return torch.matmul(probabilities, value).to(dtype)
