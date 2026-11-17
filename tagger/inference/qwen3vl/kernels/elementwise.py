"""FP32 reductions and fused pointwise operations; explicit experimental opt-in."""
from __future__ import annotations

import torch
import triton
import triton.language as tl


@triton.jit
def _rms(X, W, Y, WIDTH: tl.constexpr, EPS: tl.constexpr, BLOCK: tl.constexpr):
    row = tl.program_id(0)
    cols = tl.arange(0, BLOCK)
    x = tl.load(X + row * WIDTH + cols, cols < WIDTH, 0)
    xf = x.to(tl.float32)
    variance = tl.sum(xf * xf, 0) / WIDTH
    normalized = (xf * tl.rsqrt(variance + EPS)).to(x.dtype)
    weight = tl.load(W + cols, cols < WIDTH, 0)
    tl.store(Y + row * WIDTH + cols, normalized * weight, cols < WIDTH)


@triton.jit
def _residual_rms(X, ADD, W, RESIDUAL, Y, WIDTH: tl.constexpr,
                  EPS: tl.constexpr, BLOCK: tl.constexpr):
    row = tl.program_id(0)
    cols = tl.arange(0, BLOCK)
    index = row * WIDTH + cols
    x = tl.load(X + index, cols < WIDTH, 0)
    add = tl.load(ADD + index, cols < WIDTH, 0)
    # Preserve the eager add's output rounding before reducing in FP32.
    residual = (x + add).to(x.dtype)
    value = residual.to(tl.float32)
    variance = tl.sum(value * value, 0) / WIDTH
    normalized = (value * tl.rsqrt(variance + EPS)).to(x.dtype)
    weight = tl.load(W + cols, cols < WIDTH, 0)
    tl.store(RESIDUAL + index, residual, cols < WIDTH)
    tl.store(Y + index, normalized * weight, cols < WIDTH)


@triton.jit
def _silu_gate(GATE, UP, Y, N: tl.constexpr, BLOCK: tl.constexpr):
    index = tl.program_id(0) * BLOCK + tl.arange(0, BLOCK)
    gate = tl.load(GATE + index, index < N, 0)
    up = tl.load(UP + index, index < N, 0)
    value = (gate.to(tl.float32) * tl.sigmoid(gate.to(tl.float32))).to(gate.dtype)
    tl.store(Y + index, value * up, index < N)


def rms_norm(x, weight, eps):
    x = x.contiguous()
    out = torch.empty_like(x)
    with torch.cuda.device(x.device):
        _rms[(x.numel() // x.shape[-1],)](x, weight, out, x.shape[-1], eps,
                                        triton.next_power_of_2(x.shape[-1]), enable_fp_fusion=False)
    return out


def residual_rms_norm(x, add, weight, eps):
    x, add = x.contiguous(), add.contiguous()
    residual, out = torch.empty_like(x), torch.empty_like(x)
    with torch.cuda.device(x.device):
        _residual_rms[(x.numel() // x.shape[-1],)](
            x, add, weight, residual, out, x.shape[-1], eps,
            triton.next_power_of_2(x.shape[-1]), enable_fp_fusion=False)
    return residual, out


def silu_gate(gate, up):
    gate, up = gate.contiguous(), up.contiguous()
    out = torch.empty_like(gate)
    with torch.cuda.device(gate.device):
        _silu_gate[(triton.cdiv(gate.numel(), 256),)](gate, up, out, gate.numel(), 256, enable_fp_fusion=False)
    return out
