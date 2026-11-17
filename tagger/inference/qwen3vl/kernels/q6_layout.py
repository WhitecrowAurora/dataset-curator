"""Lossless Q6_K word planes retaining the original FP16 and INT8 scales."""
from __future__ import annotations

import torch
import triton
import triton.language as tl


@triton.jit
def _repack(W, Y, N: tl.constexpr, K: tl.constexpr, GROUPS: tl.constexpr):
    row = tl.program_id(0)
    groups = tl.program_id(1) * GROUPS + tl.arange(0, GROUPS)
    lanes = tl.arange(0, 16)
    cols = groups[:, None] * 16 + lanes[None, :]
    within = cols % 256
    half, section, lane = within // 128, (within % 128) // 32, within % 32
    base = row * (K // 256 * 210) + cols // 256 * 210
    mask = cols < K
    low = tl.load(W + base + half * 64 + section % 2 * 32 + lane, mask, 0).to(tl.uint32)
    high = tl.load(W + base + 128 + half * 32 + lane, mask, 0).to(tl.uint32)
    codes = ((low >> (section // 2 * 4)) & 15) | (((high >> (section * 2)) & 3) << 4)
    target = (row // 32 * (K // 256) + groups // 16) * 1696 + groups % 16 * 96 + row % 32
    for word in tl.static_range(3):
        shift = lanes * 6 - word * 32
        fragment = tl.where(shift[None, :] >= 0, codes << tl.maximum(shift[None, :], 0),
                            codes >> tl.maximum(-shift[None, :], 0))
        fragment = tl.where((shift[None, :] > -6) & (shift[None, :] < 32), fragment, 0)
        tl.store(Y + target + word * 32, tl.sum(fragment, 1), groups < K // 16)


@triton.jit
def _scales(W, Y, N: tl.constexpr, K: tl.constexpr, BLOCKS: tl.constexpr):
    row = tl.program_id(0)
    blocks = tl.program_id(1) * BLOCKS + tl.arange(0, BLOCKS)
    base = row * (K // 256 * 210) + blocks * 210
    target = (row // 32 * (K // 256) + blocks) * 1696 + row % 32
    mask = blocks < K // 256
    for group in tl.static_range(4):
        value = tl.full((BLOCKS,), 0, tl.uint32)
        for byte in tl.static_range(4):
            value |= tl.load(W + base + 192 + group * 4 + byte, mask, 0).to(tl.uint32) << (byte * 8)
        tl.store(Y + target + (48 + group) * 32, value, mask)
    lo = tl.load(W + base + 208, mask, 0).to(tl.uint32)
    hi = tl.load(W + base + 209, mask, 0).to(tl.uint32)
    tl.store(Y + target + 52 * 32, lo | (hi << 8), mask)


@triton.jit
def weight(W, rows, cols, N: tl.constexpr, K: tl.constexpr):
    words = W.to(tl.pointer_type(tl.uint32))
    bit = cols % 16 * 6
    base = (rows[:, None] // 32 * (K // 256) + cols[None, :] // 256) * 1696 + rows[:, None] % 32
    group = cols % 256 // 16
    codes_base = base + group[None, :] * 96
    mask = (rows[:, None] < N) & (cols[None, :] < K)
    low = tl.load(words + codes_base + bit[None, :] // 32 * 32, mask, 0)
    shift = bit % 32
    codes = low >> shift[None, :]
    high = tl.load(words + codes_base + (bit[None, :] // 32 + 1) * 32, mask & (shift[None, :] > 26), 0)
    codes |= high << (32 - shift[None, :])
    scales = tl.load(words + base + (48 + group[None, :] // 4) * 32, mask, 0)
    scale = (scales >> (group[None, :] % 4 * 8)).to(tl.int8).to(tl.float32)
    d = tl.load(words + base + 52 * 32, mask, 0).to(tl.uint16).to(tl.float16, bitcast=True).to(tl.float32)
    scale = d * scale
    return scale * ((codes & 63).to(tl.int32) - 32).to(tl.float32)


def repack(packed, rows, width):
    # 48 code words, four packed INT8-scale words and the original FP16 d.
    # Only the final output-row tile is padded; no scale is re-quantized.
    result = torch.empty(triton.cdiv(rows, 32), width // 256, 53, 32, device=packed.device, dtype=torch.int32)
    with torch.cuda.device(packed.device):
        _repack[(rows, triton.cdiv(width // 16, 32))](
            packed, result, rows, width, 32, num_warps=4, enable_fp_fusion=False)
        _scales[(rows, triton.cdiv(width // 256, 32))](
            packed, result, rows, width, 32, num_warps=4, enable_fp_fusion=False)
    return result
