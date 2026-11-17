"""Fused activation quantization and native FP8 / INT8 Tensor Core products."""
from __future__ import annotations

import torch
import triton
import triton.language as tl
from triton.language.extra.cuda import libdevice

from ..quantization.activations import bounds, symmetric_scale


@triton.jit
def _quantize(X, Q, S, PROVIDED, K: tl.constexpr, GROUP: tl.constexpr,
              SGROUPS: tl.constexpr, SCALAR: tl.constexpr,
              LO: tl.constexpr, HI: tl.constexpr, INTEGER: tl.constexpr,
              B: tl.constexpr):
    row, group = tl.program_id(0), tl.program_id(1)
    lane = tl.arange(0, B)
    cols = group * GROUP + lane
    mask = (lane < GROUP) & (cols < K)
    value = tl.load(X + row * K + cols, mask, 0).to(tl.float32)
    if SCALAR:
        scale = tl.load(PROVIDED)
    else:
        scale = tl.max(tl.abs(value), 0) / ((HI - LO) / 2)
        scale = tl.where(scale == 0, 1.0, scale)
        tl.store(S + row * SGROUPS + group, scale)
    # Approximate reciprocal division can move half-integer INT8 values across
    # the round-to-even boundary. Match the reference's FP32 division.
    value = tl.minimum(tl.maximum(tl.div_rn(value, scale), LO), HI)
    if INTEGER:
        value = libdevice.nearbyint(value).to(tl.int8)
    tl.store(Q + row * K + cols, value, mask)


@triton.jit
def _product(X, W, XS, WS, Y, M: tl.constexpr, N: tl.constexpr, K: tl.constexpr,
             XSCALAR: tl.constexpr, XGROUP: tl.constexpr, XSGROUPS: tl.constexpr,
             WSCALAR: tl.constexpr, WGROUP: tl.constexpr, WBLOCK_M: tl.constexpr,
             WSCOLS: tl.constexpr, INTEGER: tl.constexpr,
             BM: tl.constexpr, BN: tl.constexpr, BK: tl.constexpr):
    rows = tl.program_id(0) * BM + tl.arange(0, BM)
    outputs = tl.program_id(1) * BN + tl.arange(0, BN)
    inner = tl.arange(0, BK)
    acc = tl.full((BM, BN), 0, tl.float32)
    for offset in range(tl.cdiv(K, BK)):
        cols = offset * BK + inner
        x = tl.load(X + rows[:, None] * K + cols[None, :], (rows[:, None] < M) & (cols[None, :] < K), 0.0)
        w = tl.load(W + outputs[None, :] * K + cols[:, None], (outputs[None, :] < N) & (cols[:, None] < K), 0.0)
        if XSCALAR:
            xs = tl.load(XS)
        else:
            xs = tl.load(XS + rows * XSGROUPS + offset * BK // XGROUP, rows < M, 0)
        if WSCALAR:
            ws = tl.load(WS)
        else:
            scale_rows = outputs // WBLOCK_M if WBLOCK_M else outputs
            scale_col = offset * BK // WGROUP if WGROUP else 0
            ws = tl.load(WS + scale_rows * WSCOLS + scale_col, outputs < N, 0)
        if INTEGER:
            product = tl.dot(x, w, out_dtype=tl.int32).to(tl.float32)
        else:
            product = tl.dot(x, w, out_dtype=tl.float32, max_num_imprecise_acc=32)
        if XSCALAR and WSCALAR:
            acc += product * (xs * ws)
        elif XSCALAR:
            acc += product * (xs * ws[None, :])
        elif WSCALAR:
            acc += product * (xs[:, None] * ws)
        else:
            acc += product * (xs[:, None] * ws[None, :])
    tl.store(Y + rows[:, None] * N + outputs[None, :], acc,
             (rows[:, None] < M) & (outputs[None, :] < N))


def _prepare(x, activation, input_scale):
    flat = x.reshape(-1, x.shape[-1]).contiguous()
    m, k = flat.shape
    width = activation.group_size if activation.strategy == "group" else k
    groups = triton.cdiv(k, width)
    provided = input_scale
    if activation.strategy == "tensor" and provided is None:
        provided = symmetric_scale(flat.float().abs().amax().reshape(1, 1), activation.dtype)
    scalar = provided is not None
    scales = provided if scalar else torch.empty(m, groups, device=x.device, dtype=torch.float32)
    quantized = torch.empty(m, k, device=x.device, dtype=activation.dtype)
    lo, hi = bounds(activation.dtype)
    with torch.cuda.device(x.device):
        _quantize[(m, groups)](flat, quantized, scales, provided, k, width, groups,
                              scalar, lo, hi, activation.dtype == torch.int8,
                              triton.next_power_of_2(width), num_warps=8 if width > 4096 else 4)
    return quantized, scales, scalar, width, groups


@triton.jit
def _grouped(X, XS, W0, W1, W2, S0, S1, S2, Y0, Y1, Y2,
             M: tl.constexpr, N0: tl.constexpr, N1: tl.constexpr, N2: tl.constexpr, K: tl.constexpr,
             XSCALAR: tl.constexpr, XGROUP: tl.constexpr, XSGROUPS: tl.constexpr,
             WS0: tl.constexpr, WG0: tl.constexpr, WB0: tl.constexpr, WC0: tl.constexpr, BK0: tl.constexpr,
             WS1: tl.constexpr, WG1: tl.constexpr, WB1: tl.constexpr, WC1: tl.constexpr, BK1: tl.constexpr,
             WS2: tl.constexpr, WG2: tl.constexpr, WB2: tl.constexpr, WC2: tl.constexpr, BK2: tl.constexpr,
             INTEGER: tl.constexpr, BM: tl.constexpr):
    group = tl.program_id(2)
    if group == 0:
        if tl.program_id(1) * 64 < N0:
            _product(X, W0, XS, S0, Y0, M, N0, K, XSCALAR, XGROUP, XSGROUPS,
                     WS0, WG0, WB0, WC0, INTEGER, BM, 64, BK0)
    elif group == 1:
        if tl.program_id(1) * 64 < N1:
            _product(X, W1, XS, S1, Y1, M, N1, K, XSCALAR, XGROUP, XSGROUPS,
                     WS1, WG1, WB1, WC1, INTEGER, BM, 64, BK1)
    else:
        if tl.program_id(1) * 64 < N2:
            _product(X, W2, XS, S2, Y2, M, N2, K, XSCALAR, XGROUP, XSGROUPS,
                     WS2, WG2, WB2, WC2, INTEGER, BM, 64, BK2)


def linear_group(x, matrices):
    first = matrices[0]
    quantized, scales, scalar, width, groups = _prepare(x, first.activation, first.input_scale)
    m, k = quantized.shape
    capability = torch.cuda.get_device_capability(x.device)
    integer = matrices[0].weight.dtype == torch.int8
    bm = 64 if not integer and capability[0] in (9, 10) else 16
    outputs = [torch.empty(m, v.out_features, device=x.device, dtype=x.dtype) for v in matrices]
    options = []
    for matrix in matrices:
        block_m, block_k = matrix.factors.block_shape or (0, matrix.factors.group_size)
        options.append((matrix.factors.scale.numel() == 1, block_k, block_m,
                        matrix.factors.scale.shape[1], matrix.tile))
    with torch.cuda.device(x.device):
        copies = list(matrices)
        results = list(outputs)
        count = len(copies)
        if count == 2:
            copies.append(copies[-1])
            results.append(results[-1])
            options.append(options[-1])
        _grouped[(triton.cdiv(m, bm), triton.cdiv(max(v.out_features for v in copies), 64), count)](
            quantized, scales, *(v.weight for v in copies), *(v.factors.scale for v in copies),
            *results, m, *(v.out_features for v in copies), k, scalar, width, groups,
            *(value for option in options for value in option), integer, bm,
            num_warps=4, enable_fp_fusion=False)
    return tuple(output.reshape(*x.shape[:-1], matrix.out_features) for matrix, output in zip(matrices, outputs))


def linear(x, weight, factors, activation, input_scale, tile):
    quantized, scales, scalar, width, groups = _prepare(x, activation, input_scale)
    m, k = quantized.shape
    n = factors.out_features
    output = torch.empty(m, n, device=x.device, dtype=x.dtype)
    block_m, block_k = factors.block_shape or (0, factors.group_size)
    capability = torch.cuda.get_device_capability(x.device)
    # Hopper requires warp-group MMA for native FP8; a 16-row tile becomes F16 MMA.
    bm = 64 if weight.dtype != torch.int8 and capability[0] in (9, 10) else 16
    with torch.cuda.device(x.device):
        _product[(triton.cdiv(m, bm), triton.cdiv(n, 64))](
            quantized, weight, scales, factors.scale, output, m, n, k,
            scalar, width, groups, factors.scale.numel() == 1, block_k,
            block_m, factors.scale.shape[1], weight.dtype == torch.int8,
            bm, 64, tile, num_warps=4, enable_fp_fusion=False)
    return output.reshape(*x.shape[:-1], n)
