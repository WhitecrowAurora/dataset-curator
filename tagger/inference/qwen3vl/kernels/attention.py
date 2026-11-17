"""Head normalization and rotary embedding with the eager activation rounding order."""
from __future__ import annotations

import torch
import triton
import triton.language as tl


@triton.jit
def _head(X, W, COS, SIN, Y, ROW, HEADS: tl.constexpr, SEQ: tl.constexpr,
          DIM: tl.constexpr, EPS: tl.constexpr, BLOCK: tl.constexpr):
    cols = tl.arange(0, BLOCK)
    value = tl.load(X + ROW * DIM + cols, cols < DIM, 0)
    xf = value.to(tl.float32)
    variance = tl.sum(xf * xf, 0) / DIM
    normalized = (xf * tl.rsqrt(variance + EPS)).to(value.dtype)
    weight = tl.load(W + cols, cols < DIM, 0)
    normalized = (normalized * weight).to(value.dtype)
    partner = (cols + DIM // 2) % DIM
    rotated = tl.gather(normalized, partner, 0) * tl.where(cols < DIM // 2, -1, 1)
    position = ROW // HEADS
    cos = tl.load(COS + position * DIM + cols, cols < DIM, 0)
    sin = tl.load(SIN + position * DIM + cols, cols < DIM, 0)
    first = (normalized * cos).to(value.dtype)
    second = (rotated * sin).to(value.dtype)
    batch, token, head = ROW // (SEQ * HEADS), position % SEQ, ROW % HEADS
    output_row = (batch * HEADS + head) * SEQ + token
    tl.store(Y + output_row * DIM + cols, first + second, cols < DIM)


@triton.jit
def _qk(Q, K, QW, KW, COS, SIN, QY, KY, BATCH: tl.constexpr, SEQ: tl.constexpr,
        QHEADS: tl.constexpr, KHEADS: tl.constexpr, DIM: tl.constexpr,
        EPS: tl.constexpr, BLOCK: tl.constexpr):
    row = tl.program_id(0)
    qrows: tl.constexpr = BATCH * SEQ * QHEADS
    if row < qrows:
        _head(Q, QW, COS, SIN, QY, row, QHEADS, SEQ, DIM, EPS, BLOCK)
    else:
        _head(K, KW, COS, SIN, KY, row - qrows, KHEADS, SEQ, DIM, EPS, BLOCK)


def normalize_rotate(q, k, q_weight, k_weight, cos, sin, head_dim, eps):
    batch, seq = q.shape[:2]
    qheads, kheads = q.shape[-1] // head_dim, k.shape[-1] // head_dim
    qout = torch.empty(batch, qheads, seq, head_dim, device=q.device, dtype=q.dtype)
    kout = torch.empty(batch, kheads, seq, head_dim, device=k.device, dtype=k.dtype)
    with torch.cuda.device(q.device):
        _qk[(batch * seq * (qheads + kheads),)](
            q.contiguous(), k.contiguous(), q_weight, k_weight,
            cos.contiguous(), sin.contiguous(), qout, kout, batch, seq,
            qheads, kheads, head_dim, eps, triton.next_power_of_2(head_dim),
            num_warps=4, enable_fp_fusion=False)
    return qout, kout
