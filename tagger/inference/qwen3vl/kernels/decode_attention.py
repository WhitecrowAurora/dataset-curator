"""Single-token GQA using FP32 scores without expanding the shared KV heads."""
from __future__ import annotations

import torch
import triton
import triton.language as tl


@triton.jit(do_not_specialize=["LENGTH", "STRIDE"])
def _scores(Q, K, VALID, SCORES, QHEADS: tl.constexpr, KHEADS: tl.constexpr,
            LENGTH, DIM: tl.constexpr, SCALE: tl.constexpr,
            QB: tl.constexpr, QH: tl.constexpr, QD: tl.constexpr,
            KB: tl.constexpr, KH: tl.constexpr, KT: tl.constexpr, KD: tl.constexpr,
            VB: tl.constexpr, TOKENS: tl.constexpr, WIDTH: tl.constexpr,
            STRIDE, DEVICE_LENGTH: tl.constexpr):
    if DEVICE_LENGTH:
        LENGTH = tl.load(LENGTH)
    row = tl.program_id(0)
    batch, head = row // QHEADS, row % QHEADS
    tokens = tl.program_id(1) * TOKENS + tl.arange(0, TOKENS)
    dims = tl.arange(0, WIDTH)
    valid = tl.load(VALID + batch * VB + tokens, tokens < LENGTH, 0)
    query = tl.load(Q + batch * QB + head * QH + dims * QD, dims < DIM, 0).to(tl.float32) * SCALE
    key = tl.load(K + batch * KB + (head // (QHEADS // KHEADS)) * KH
                  + tokens[:, None] * KT + dims[None, :] * KD,
                  (tokens[:, None] < LENGTH) & (dims[None, :] < DIM), 0).to(tl.float32) * SCALE
    scores = tl.sum(key * query[None, :], 1)
    tl.store(SCORES + row * STRIDE + tokens, tl.where(valid, scores, -float("inf")), tokens < LENGTH)


@triton.jit(do_not_specialize=["LENGTH", "STRIDE"])
def _probabilities(SCORES, PROBS, LENGTH, BLOCK: tl.constexpr,
                   STRIDE, DEVICE_LENGTH: tl.constexpr):
    if DEVICE_LENGTH:
        LENGTH = tl.load(LENGTH)
    row = tl.program_id(0)
    cols = tl.arange(0, BLOCK)
    scores = tl.load(SCORES + row * STRIDE + cols, cols < LENGTH, -float("inf"))
    maximum = tl.max(scores, 0)
    # Inactive slots have an entirely masked row, whose output must be zero.
    shifted = tl.where(maximum == -float("inf"), -float("inf"), scores - maximum)
    numerator = tl.exp(shifted)
    denominator = tl.sum(numerator, 0)
    probs = numerator / tl.where(denominator > 0, denominator, 1)
    tl.store(PROBS + row * STRIDE + cols, probs, cols < LENGTH)


@triton.jit(do_not_specialize=["LENGTH", "STRIDE"])
def _values(PROBS, V, Y, QHEADS: tl.constexpr, KHEADS: tl.constexpr,
            LENGTH, DIM: tl.constexpr,
            VB: tl.constexpr, VH: tl.constexpr, VT: tl.constexpr, VD: tl.constexpr,
            TOKENS: tl.constexpr, WIDTH: tl.constexpr,
            STRIDE, DEVICE_LENGTH: tl.constexpr):
    if DEVICE_LENGTH:
        LENGTH = tl.load(LENGTH)
    row = tl.program_id(0)
    batch, head = row // QHEADS, row % QHEADS
    tokens = tl.arange(0, TOKENS)
    dims = tl.program_id(1) * WIDTH + tl.arange(0, WIDTH)
    probs = tl.load(PROBS + row * STRIDE + tokens, tokens < LENGTH, 0)
    values = tl.load(V + batch * VB + (head // (QHEADS // KHEADS)) * VH
                     + tokens[:, None] * VT + dims[None, :] * VD,
                     (tokens[:, None] < LENGTH) & (dims[None, :] < DIM), 0).to(tl.float32)
    # Masked cache entries may contain NaNs after slot reuse. Do not multiply
    # those entries by zero before removing them.
    values = tl.where(probs[:, None] > 0, values, 0)
    result = tl.sum(probs[:, None] * values, 0)
    tl.store(Y + row * DIM + dims, result, dims < DIM)


def decode_attention(q, k, v, mask, *, length=None):
    batch, heads, _, dim = q.shape
    span = k.shape[2]
    device_length = length is not None
    length = span if length is None else length
    scores = torch.empty(batch * heads, span, device=q.device, dtype=torch.float32)
    probs = torch.empty_like(scores)
    out = torch.empty_like(q, memory_format=torch.contiguous_format)
    with torch.cuda.device(q.device):
        _scores[(batch * heads, triton.cdiv(span, 32))](
            q, k, mask, scores, heads, k.shape[1], length, dim, dim ** -0.25,
            q.stride(0), q.stride(1), q.stride(3), *k.stride(), mask.stride(0),
            32, triton.next_power_of_2(dim), span, device_length,
            num_warps=4, enable_fp_fusion=False)
        _probabilities[(batch * heads,)](
            scores, probs, length, triton.next_power_of_2(span), span, device_length,
            num_warps=4, enable_fp_fusion=False)
        _values[(batch * heads, triton.cdiv(dim, 16))](
            probs, v, out, heads, k.shape[1], length, dim, *v.stride(),
            triton.next_power_of_2(span), 16, span, device_length,
            num_warps=8, enable_fp_fusion=False)
    return out
