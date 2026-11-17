"""Fused GGML Q6_K dequantization and matrix multiplication.

Tensor-core products use FP16 values and FP32 accumulation. The original
packed reader also provides the numerical reference for runtime repacking.
"""
from __future__ import annotations

import triton
import triton.language as tl


@triton.jit
def _weight(packed, rows, cols, stride: tl.constexpr, row_mask, col_mask):
    within = cols % 256
    half = within // 128
    group = (within % 128) // 32
    lane = within % 32
    base = rows[:, None] * stride + (cols[None, :] // 256) * 210
    mask = row_mask[:, None] & col_mask[None, :]
    lo = tl.load(packed + base + half[None, :] * 64 + (group[None, :] % 2) * 32 + lane[None, :], mask, 0)
    hi = tl.load(packed + base + 128 + half[None, :] * 32 + lane[None, :], mask, 0)
    q = (((lo >> ((group[None, :] // 2) * 4)) & 15)
         | (((hi >> (group[None, :] * 2)) & 3) << 4)).to(tl.int32) - 32
    scale = tl.load(packed + base + 192 + within[None, :] // 16, mask, 0).to(tl.int8).to(tl.float32)
    d0 = tl.load(packed + base + 208, mask, 0).to(tl.uint16)
    d1 = tl.load(packed + base + 209, mask, 0).to(tl.uint16)
    d = (d0 | (d1 << 8)).to(tl.float16, bitcast=True).to(tl.float32)
    return (d * scale) * q.to(tl.float32)
