"""Fused FP8/INT8/W4 weight decoding into A16 products, not native A8 math."""
from __future__ import annotations

import torch
import triton
import triton.language as tl


@triton.jit
def _weight(W, SCALE, ZERO, GROUPS, rows, cols, N: tl.constexpr, K: tl.constexpr,
            BITS: tl.constexpr, SCOLS: tl.constexpr, SCALAR: tl.constexpr,
            GROUP: tl.constexpr, BLOCK_M: tl.constexpr, BLOCK_K: tl.constexpr,
            HAS_ZERO: tl.constexpr, HAS_GROUPS: tl.constexpr):
    mask = (rows[:, None] < N) & (cols[None, :] < K)
    if BITS:
        stride = tl.cdiv(K * BITS, 32)
        word = tl.load(W + rows[:, None] * stride + cols[None, :] // (32 // BITS), mask, 0).to(tl.uint32)
        q = ((word >> (cols[None, :] % (32 // BITS) * BITS)) & ((1 << BITS) - 1)).to(tl.float32) - (1 << (BITS - 1))
    else:
        q = tl.load(W + rows[:, None] * K + cols[None, :], mask, 0.0).to(tl.float32)
    if SCALAR:
        offsets = tl.full((rows.shape[0], cols.shape[0]), 0, tl.int32)
    elif BLOCK_M:
        offsets = (rows[:, None] // BLOCK_M) * SCOLS + cols[None, :] // BLOCK_K
    else:
        if HAS_GROUPS:
            groups = tl.load(GROUPS + cols, cols < K, 0)
        elif GROUP:
            groups = cols // GROUP
        else:
            groups = tl.full((cols.shape[0],), 0, tl.int32)
        offsets = rows[:, None] * SCOLS + groups[None, :]
    scale = tl.load(SCALE + offsets, mask, 0).to(tl.float32)
    if HAS_ZERO:
        zero = tl.load(ZERO + offsets, mask, 0).to(tl.float32)
        q -= zero
    return q * scale


@triton.jit
def _product(X, W, SCALE, ZERO, GROUPS, Y, M: tl.constexpr, N: tl.constexpr, K: tl.constexpr,
             BITS: tl.constexpr, SCOLS: tl.constexpr, SCALAR: tl.constexpr,
             GROUP: tl.constexpr, BLOCK_M: tl.constexpr, BLOCK_K: tl.constexpr,
             HAS_ZERO: tl.constexpr, HAS_GROUPS: tl.constexpr,
             BM: tl.constexpr, BN: tl.constexpr, BK: tl.constexpr, VECTOR: tl.constexpr):
    outputs = tl.program_id(1) * BN + tl.arange(0, BN)
    inner = tl.arange(0, BK)
    if VECTOR:
        row = tl.program_id(0)
        acc = tl.full((BN, BK), 0, tl.float32)
    else:
        rows = tl.program_id(0) * BM + tl.arange(0, BM)
        acc = tl.full((BM, BN), 0, tl.float32)
    for offset in range(tl.cdiv(K, BK)):
        cols = offset * BK + inner
        w = _weight(W, SCALE, ZERO, GROUPS, outputs, cols, N, K, BITS, SCOLS, SCALAR,
                    GROUP, BLOCK_M, BLOCK_K, HAS_ZERO, HAS_GROUPS)
        if VECTOR:
            x = tl.load(X + row * K + cols, cols < K, 0)
            acc += w.to(x.dtype).to(tl.float32) * x[None, :].to(tl.float32)
        else:
            x = tl.load(X + rows[:, None] * K + cols[None, :], (rows[:, None] < M) & (cols[None, :] < K), 0)
            acc = tl.dot(x, tl.trans(w.to(x.dtype)), acc)
    if VECTOR:
        tl.store(Y + row * N + outputs, tl.sum(acc, 1), outputs < N)
    else:
        tl.store(Y + rows[:, None] * N + outputs[None, :], acc, (rows[:, None] < M) & (outputs[None, :] < N))


def linear(x, weight, factors, *, bits=0):
    if x.dtype != torch.float16:
        raise ValueError("The experimental scaled CUDA kernel uses FP16 activations")
    x = x.contiguous()
    m, k = x.numel() // x.shape[-1], x.shape[-1]
    n = factors.out_features
    output = torch.empty(m, n, device=x.device, dtype=x.dtype)
    vector = m <= 8
    bn = 8 if vector else 64
    bm = 1 if vector else 16
    block_m, block_k = factors.block_shape or (0, 0)
    with torch.cuda.device(x.device):
        _product[(triton.cdiv(m, bm), triton.cdiv(n, bn))](
            x, weight, factors.scale, factors.zero_point, factors.group_index, output,
            m, n, k, bits, factors.scale.shape[1], factors.scale.numel() == 1,
            factors.group_size, block_m, block_k, factors.zero_point is not None,
            factors.group_index is not None, bm, bn, 256, vector,
            num_warps=4, num_stages=1, enable_fp_fusion=False,
        )
    return output.reshape(*x.shape[:-1], n)
