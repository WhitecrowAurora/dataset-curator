"""GGUF small-batch MMA with coalesced original fields and ordered FP32 sums."""
from __future__ import annotations

import triton
from triton.experimental import gluon as g
from triton.experimental.gluon import language as gl
from triton.experimental.gluon.language import (
    BlockedLayout,
    DotOperandLayout,
    NVMMADistributedLayout,
    SliceLayout,
)
from triton.experimental.gluon.language.nvidia.ampere import mma_v2

from .q4_read import weight as _q4_weight
from .q5_read import weight as _q5_weight
from .q6_read import weight as _weight
from .q8_read import weight as _q8_weight


@g.jit
def _product(X, W, Y, M: gl.constexpr, N: gl.constexpr, K: gl.constexpr,
             BN: gl.constexpr, BK: gl.constexpr, KIND: gl.constexpr, output_tile):
    mma: gl.constexpr = NVMMADistributedLayout(version=[2, 0], warps_per_cta=[1, 4], instr_shape=[16, 8])
    a_layout: gl.constexpr = BlockedLayout([1, 4], [4, 8], [4, 1], [1, 0])
    b_layout: gl.constexpr = DotOperandLayout(1, mma, 4)
    rows = gl.program_id(0) * 16 + gl.arange(0, 16, layout=SliceLayout(1, a_layout))
    inner_a = gl.arange(0, BK, layout=SliceLayout(0, a_layout))
    outputs = output_tile * BN + gl.arange(0, BN, layout=SliceLayout(0, b_layout))
    inner_b = gl.arange(0, BK, layout=SliceLayout(1, b_layout))
    acc = gl.full((16, BN), 0, gl.float32, mma)
    for offset in range(gl.cdiv(K, BK)):
        cols_a, cols_b = offset * BK + inner_a, offset * BK + inner_b
        x = gl.load(X + rows[:, None] * K + cols_a[None, :],
                    (rows[:, None] < M) & (cols_a[None, :] < K), 0)
        if KIND == "Q4_K_REPACKED":
            weight = _q4_weight(W, outputs[None, :], cols_b[:, None], N, K).to(gl.float16)
        elif KIND == "Q5_K_REPACKED":
            weight = _q5_weight(W, outputs[None, :], cols_b[:, None], N, K).to(gl.float16)
        elif KIND == "Q8_0_REPACKED":
            weight = _q8_weight(W, outputs[None, :], cols_b[:, None], N, K).to(gl.float16)
        else:
            weight = _weight(W, outputs[None, :], cols_b[:, None], N, K).to(gl.float16)
        # Keep k_width=4 and the prior FP32 MMA order; automatic dot layouts
        # can change rounding even when the decoded weights are identical.
        a = gl.convert_layout(x, DotOperandLayout(0, mma, 4))
        acc = mma_v2(a, weight, acc)
    out_layout: gl.constexpr = BlockedLayout([1, 4], [4, 8], [1, 4], [1, 0])
    result = gl.convert_layout(acc, out_layout)
    out_rows = gl.program_id(0) * 16 + gl.arange(0, 16, layout=SliceLayout(1, out_layout))
    out_cols = output_tile * BN + gl.arange(0, BN, layout=SliceLayout(0, out_layout))
    gl.store(Y + out_rows[:, None] * N + out_cols[None, :], result,
             (out_rows[:, None] < M) & (out_cols[None, :] < N))


@g.jit
def _grouped(X, W0, W1, W2, Y0, Y1, Y2, M: gl.constexpr,
             N0: gl.constexpr, N1: gl.constexpr, N2: gl.constexpr,
             K: gl.constexpr, BN: gl.constexpr, BK: gl.constexpr, KIND: gl.constexpr):
    tile = gl.program_id(1)
    first: gl.constexpr = gl.cdiv(N0, BN)
    second: gl.constexpr = gl.cdiv(N1, BN)
    if tile < first:
        _product(X, W0, Y0, M, N0, K, BN, BK, KIND, tile)
    elif tile < first + second:
        _product(X, W1, Y1, M, N1, K, BN, BK, KIND, tile - first)
    else:
        _product(X, W2, Y2, M, N2, K, BN, BK, KIND, tile - first - second)


@g.jit
def _equal_grouped(X, W0, W1, W2, Y0, Y1, Y2, M: gl.constexpr,
                   N0: gl.constexpr, N1: gl.constexpr, N2: gl.constexpr,
                   K: gl.constexpr, BN: gl.constexpr, BK: gl.constexpr, KIND: gl.constexpr):
    group, tile = gl.program_id(2), gl.program_id(1)
    if group == 0:
        _product(X, W0, Y0, M, N0, K, BN, BK, KIND, tile)
    elif group == 1:
        _product(X, W1, Y1, M, N1, K, BN, BK, KIND, tile)
    else:
        _product(X, W2, Y2, M, N2, K, BN, BK, KIND, tile)


@g.jit
def _linear(X, W, Y, M: gl.constexpr, N: gl.constexpr, K: gl.constexpr,
            BN: gl.constexpr, BK: gl.constexpr, KIND: gl.constexpr):
    _product(X, W, Y, M, N, K, BN, BK, KIND, gl.program_id(1))


def linear(x, packed, out, rows, width, *, kind="Q6_K_REPACKED"):
    import torch

    m = x.numel() // width
    ada = torch.cuda.get_device_capability(x.device) == (8, 9)
    if kind in ("Q4_K_REPACKED", "Q5_K_REPACKED", "Q6_K_REPACKED") and m in (4, 8) and ada:
        from .q6_pipeline import linear as staged_linear

        return staged_linear(x, packed, out, rows, width, kind=kind)
    if kind in ("Q4_K_REPACKED", "Q5_K_REPACKED", "Q8_0_REPACKED"):
        bk = 256 if ada else 64
    elif kind == "Q6_K_REPACKED":
        bk = 256 if width >= 8192 else 128
    else:
        bk = 64
    _linear[(triton.cdiv(m, 16), triton.cdiv(rows, 32))](
        x, packed, out, m, rows, width, 32, bk, kind,
        num_warps=4, enable_fp_fusion=False)


def linear_group(x, matrices, outputs):
    import torch

    m, width = x.numel() // x.shape[-1], x.shape[-1]
    kind = matrices[0].kernel_kind
    ada = torch.cuda.get_device_capability(x.device) == (8, 9)
    if kind in ("Q4_K_REPACKED", "Q5_K_REPACKED", "Q6_K_REPACKED") and m in (4, 8) and ada:
        from .q6_pipeline import linear_group as staged_group

        return staged_group(x, matrices, outputs)
    sizes = [matrix.out_features for matrix in matrices]
    weights = [matrix.packed for matrix in matrices]
    destinations = list(outputs)
    if len(matrices) == 2:
        weights.append(weights[-1])
        destinations.append(destinations[-1])
        sizes.append(sizes[-1])
    counts = [triton.cdiv(size, 32) for size in sizes[:len(matrices)]]
    equal = all(count == counts[0] for count in counts)
    kernel = _equal_grouped if equal else _grouped
    grid = ((triton.cdiv(m, 16), counts[0], len(matrices)) if equal
            else (triton.cdiv(m, 16), sum(counts)))
    if kind in ("Q4_K_REPACKED", "Q5_K_REPACKED", "Q8_0_REPACKED"):
        bk = 256 if ada else 64
    elif kind == "Q6_K_REPACKED":
        bk = 128
    else:
        bk = 64
    kernel[grid](
        x, *weights, *destinations, m, *sizes, width, 32, bk, kind,
        num_warps=4, enable_fp_fusion=False)
