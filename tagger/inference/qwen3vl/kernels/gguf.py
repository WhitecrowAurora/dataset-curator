"""Common GGUF block readers fused into the shared GEMV/GEMM loops."""
from __future__ import annotations

import triton
import triton.language as tl

from .q4_layout import weight as _q4_layout_weight
from .q5_layout import weight as _q5_layout_weight
from .q6 import _weight as _q6_weight
from .q6_layout import weight as _q6_layout_weight
from .q8_layout import weight as _q8_layout_weight


@triton.jit
def _half(W, base, offset: tl.constexpr, mask):
    lo = tl.load(W + base + offset, mask, 0).to(tl.uint16)
    hi = tl.load(W + base + offset + 1, mask, 0).to(tl.uint16)
    return (lo | (hi << 8)).to(tl.float16, bitcast=True).to(tl.float32)


@triton.jit
def weight(W, rows, cols, N: tl.constexpr, K: tl.constexpr, KIND: tl.constexpr):
    mask = (rows[:, None] < N) & (cols[None, :] < K)
    if KIND == "Q4_K_REPACKED":
        return _q4_layout_weight(W, rows, cols, N, K)
    if KIND == "Q5_K_REPACKED":
        return _q5_layout_weight(W, rows, cols, N, K)
    if KIND == "Q8_0_REPACKED":
        return _q8_layout_weight(W, rows, cols, N, K)
    if KIND == "Q6_K_REPACKED":
        return _q6_layout_weight(W, rows, cols, N, K)
    if KIND == "Q6_K":
        return _q6_weight(W, rows, cols, K // 256 * 210, rows < N, cols < K)
    if KIND == "Q4_0" or KIND == "Q4_1" or KIND == "Q5_0" or KIND == "Q8_0":
        size: tl.constexpr = 18 if KIND == "Q4_0" else (20 if KIND == "Q4_1" else (22 if KIND == "Q5_0" else 34))
        within = cols % 32
        base = rows[:, None] * (K // 32 * size) + cols[None, :] // 32 * size
        if KIND == "Q8_0":
            q = tl.load(W + base + 2 + within[None, :], mask, 0).to(tl.int8).to(tl.float32)
        else:
            start: tl.constexpr = 2 if KIND == "Q4_0" else (4 if KIND == "Q4_1" else 6)
            lo = tl.load(W + base + start + within[None, :] % 16, mask, 0).to(tl.int32)
            q = (lo >> (within[None, :] // 16 * 4)) & 15
            if KIND == "Q5_0":
                hi = tl.load(W + base + 2 + within[None, :] // 8, mask, 0).to(tl.int32)
                q |= ((hi >> (within[None, :] % 8)) & 1) << 4
            q = q.to(tl.float32) - (8 if KIND == "Q4_0" else (0 if KIND == "Q4_1" else 16))
        value = _half(W, base, 0, mask) * q
        return value + _half(W, base, 2, mask) if KIND == "Q4_1" else value
    k_size: tl.constexpr = 144 if KIND == "Q4_K" else 176
    within = cols % 256
    group, lane = within // 32, within % 32
    base = rows[:, None] * (K // 256 * k_size) + cols[None, :] // 256 * k_size
    a = tl.load(W + base + 4 + group[None, :] % 4, mask, 0).to(tl.int32)
    b = tl.load(W + base + 8 + group[None, :] % 4, mask, 0).to(tl.int32)
    c = tl.load(W + base + 12 + group[None, :] % 4, mask, 0).to(tl.int32)
    scale = tl.where(group[None, :] < 4, a & 63, (c & 15) | ((a >> 2) & 48)).to(tl.float32)
    minimum = tl.where(group[None, :] < 4, b & 63, (c >> 4) | ((b >> 2) & 48)).to(tl.float32)
    k_start: tl.constexpr = 16 if KIND == "Q4_K" else 48
    lo = tl.load(W + base + k_start + group[None, :] // 2 * 32 + lane[None, :], mask, 0).to(tl.int32)
    q = (lo >> (group[None, :] % 2 * 4)) & 15
    if KIND == "Q5_K":
        hi = tl.load(W + base + 16 + lane[None, :], mask, 0).to(tl.int32)
        q |= ((hi >> group[None, :]) & 1) << 4
    return (_half(W, base, 0, mask) * scale) * q.to(tl.float32) - (_half(W, base, 2, mask) * minimum)


@triton.jit
def _dequantize(W, Y, N: tl.constexpr, K: tl.constexpr, KIND: tl.constexpr,
                BN: tl.constexpr, BK: tl.constexpr):
    rows = tl.program_id(0) * BN + tl.arange(0, BN)
    cols = tl.program_id(1) * BK + tl.arange(0, BK)
    values = weight(W, rows, cols, N, K, KIND)
    tl.store(Y + rows[:, None] * K + cols[None, :], values,
             (rows[:, None] < N) & (cols[None, :] < K))


@triton.jit
def _embedding(W, IDS, Y, COUNT: tl.constexpr, N: tl.constexpr, K: tl.constexpr,
               KIND: tl.constexpr, BK: tl.constexpr):
    row = tl.program_id(0)
    token = tl.load(IDS + row + tl.arange(0, 1))
    cols = tl.program_id(1) * BK + tl.arange(0, BK)
    values = weight(W, token, cols, N, K, KIND)
    tl.store(Y + row * K + cols[None, :], values, cols[None, :] < K)


def embedding(ids, packed, rows, width, kind):
    import torch

    ids = ids.contiguous()
    out = torch.empty(*ids.shape, width, device=ids.device, dtype=torch.float16)
    with torch.cuda.device(ids.device):
        _embedding[(ids.numel(), triton.cdiv(width, 256))](
            packed, ids, out, ids.numel(), rows, width, kind, 256,
            num_warps=4, enable_fp_fusion=False)
    return out


@triton.jit
def _product(X, W, Y, M: tl.constexpr, N: tl.constexpr, K: tl.constexpr,
             KIND: tl.constexpr, BM: tl.constexpr, BN: tl.constexpr,
             BK: tl.constexpr, VECTOR: tl.constexpr):
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
        w = weight(W, outputs, cols, N, K, KIND)
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


@triton.jit
def _grouped(X, W0, W1, W2, Y0, Y1, Y2, M: tl.constexpr,
             N0: tl.constexpr, N1: tl.constexpr, N2: tl.constexpr, K: tl.constexpr,
             KIND0: tl.constexpr, KIND1: tl.constexpr, KIND2: tl.constexpr,
             BM: tl.constexpr, BN: tl.constexpr, BK: tl.constexpr, VECTOR: tl.constexpr):
    group = tl.program_id(2)
    if group == 0:
        if tl.program_id(1) * BN < N0:
            _product(X, W0, Y0, M, N0, K, KIND0, BM, BN, BK, VECTOR)
    elif group == 1:
        if tl.program_id(1) * BN < N1:
            _product(X, W1, Y1, M, N1, K, KIND1, BM, BN, BK, VECTOR)
    else:
        if tl.program_id(1) * BN < N2:
            _product(X, W2, Y2, M, N2, K, KIND2, BM, BN, BK, VECTOR)


def linear_group(x, matrices):
    import torch

    x = x.contiguous()
    m, k = x.numel() // x.shape[-1], x.shape[-1]
    kinds = [matrix.kernel_kind for matrix in matrices]
    repacked = [kind.endswith("_REPACKED") for kind in kinds]
    # Different stored formats have different readers and MMA orders.
    # Preserve each matrix's explicit numerical path for mixed groups.
    if any(repacked) and (m == 1 or not all(repacked) or len(set(kinds)) != 1):
        return tuple(matrix(x) for matrix in matrices)
    outputs = [torch.empty(m, matrix.out_features, device=x.device, dtype=x.dtype) for matrix in matrices]
    if all(repacked):
        from .q6_mma import linear_group as q6_group

        with torch.cuda.device(x.device):
            q6_group(x, matrices, outputs)
        return tuple(output.reshape(*x.shape[:-1], matrix.out_features)
                     for output, matrix in zip(outputs, matrices))
    vector = m == 1
    bm, bn, bk = (1, 8, 256) if vector else (16, 32, 64)
    sizes = [matrix.out_features for matrix in matrices]
    weights = [matrix.packed for matrix in matrices]
    kinds = [matrix.kernel_kind for matrix in matrices]
    # The unused third branch of a two-matrix group gets valid placeholder pointers.
    if len(matrices) == 2:
        weights.append(weights[-1])
        outputs.append(outputs[-1])
        sizes.append(sizes[-1])
        kinds.append(kinds[-1])
    with torch.cuda.device(x.device):
        _grouped[(triton.cdiv(m, bm), triton.cdiv(max(sizes), bn), len(matrices))](
            x, *weights, *outputs, m, *sizes, k, *kinds, bm, bn, bk, vector,
            num_warps=4, num_stages=1, enable_fp_fusion=False)
    return tuple(output.reshape(*x.shape[:-1], size) for output, size in zip(outputs[:len(matrices)], sizes))


def linear(x, packed, rows, kind, *, allow_dense=True):
    import torch

    if x.dtype != torch.float16:
        raise ValueError("The experimental GGUF CUDA kernel uses FP16 activations")
    x = x.contiguous()
    m, k = x.numel() // x.shape[-1], x.shape[-1]
    # Prefill has many input rows: unpack this matrix once, then use cuBLAS,
    # instead of decoding the same packed block in every input-row tile.
    # The temporary holds one matrix, never a full dequantized model.
    if m >= 32 and allow_dense:
        dense = torch.empty(rows, k, device=x.device, dtype=x.dtype)
        with torch.cuda.device(x.device):
            _dequantize[(triton.cdiv(rows, 8), triton.cdiv(k, 256))](
                packed, dense, rows, k, kind, 8, 256, num_warps=4, enable_fp_fusion=False)
        return torch.nn.functional.linear(x, dense)
    if m == 1 and kind.endswith("_REPACKED"):
        if kind == "Q5_K_REPACKED":
            from .q5_vector import vector
        else:
            from .q6_vector import vector

        out = torch.empty(m, rows, device=x.device, dtype=x.dtype)
        with torch.cuda.device(x.device):
            vector(x, packed, out, rows, k, kind=kind)
        return out.reshape(*x.shape[:-1], rows)
    if kind.endswith("_REPACKED"):
        from .q6_mma import linear as q6_linear

        out = torch.empty(m, rows, device=x.device, dtype=x.dtype)
        with torch.cuda.device(x.device):
            q6_linear(x, packed, out, rows, k, kind=kind)
        return out.reshape(*x.shape[:-1], rows)
    # Sharing weight reads through MMA is faster than one GEMV per request.
    vector = m == 1
    bm, bn, bk = (1, 8, 256) if vector else (16, 32, 64)
    out = torch.empty(m, rows, device=x.device, dtype=x.dtype)
    with torch.cuda.device(x.device):
        _product[(triton.cdiv(m, bm), triton.cdiv(rows, bn))](
            x, packed, out, m, rows, k, kind, bm, bn, bk, vector,
            num_warps=4, num_stages=1, enable_fp_fusion=False)
    return out.reshape(*x.shape[:-1], rows)
