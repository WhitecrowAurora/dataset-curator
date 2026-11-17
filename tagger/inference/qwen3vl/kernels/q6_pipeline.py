"""Double-buffer original Q4/Q6 word planes while executing ordered FP32 MMA."""
from __future__ import annotations

import triton
from triton.experimental import gluon as g
from triton.experimental.gluon import language as gl
from triton.experimental.gluon.language.nvidia.ampere import async_copy as cp
from triton.experimental.gluon.language.nvidia.ampere import mma_v2

from .q4_staging import weight as q4_weight
from .q5_staging import weight as q5_weight
from .q6_staging import copy_block, weight


@g.jit
def _product(X, W, Y, M: gl.constexpr, N: gl.constexpr, K: gl.constexpr,
             BM: gl.constexpr, BK: gl.constexpr, KIND: gl.constexpr, output_tile):
    mma: gl.constexpr = gl.NVMMADistributedLayout(version=[2, 0], warps_per_cta=[1, 4], instr_shape=[16, 8])
    a_layout: gl.constexpr = gl.BlockedLayout([1, 4], [4, 8], [4, 1], [1, 0])
    b_layout: gl.constexpr = gl.DotOperandLayout(1, mma, 4)
    shared_layout: gl.constexpr = gl.SwizzledSharedLayout(vec=4, per_phase=1, max_phase=1, order=[1, 0])
    shared = gl.allocate_shared_memory(gl.uint32, [2, 64, 32], shared_layout)
    rows = gl.program_id(0) * BM + gl.arange(0, BM, layout=gl.SliceLayout(1, a_layout))
    inner = gl.arange(0, BK, layout=gl.SliceLayout(0, a_layout))
    acc = gl.full((BM, 32), 0, gl.float32, mma)
    copy_block(W, shared.index(0), 0, output_tile, N, K, KIND)
    cp.wait_group(0)
    gl.barrier()
    for block in range(K // 256):
        if block + 1 < K // 256:
            copy_block(W, shared.index((block + 1) % 2), block + 1, output_tile, N, K, KIND)
        for part in gl.static_range(256 // BK):
            cols = block * 256 + part * BK + inner
            x = gl.load(X + rows[:, None] * K + cols[None, :], rows[:, None] < M, 0)
            a = gl.convert_layout(x, gl.DotOperandLayout(0, mma, 4))
            if KIND == "Q4_K_REPACKED":
                b = q4_weight(shared.index(block % 2), part * BK, BK, b_layout)
            elif KIND == "Q5_K_REPACKED":
                b = q5_weight(shared.index(block % 2), part * BK, BK, b_layout)
            else:
                b = weight(shared.index(block % 2), part * BK, BK, b_layout)
            acc = mma_v2(a, b, acc)
        # All shared reads finish before this buffer can be reused two blocks later.
        gl.barrier()
        cp.wait_group(0)
        gl.barrier()
    out_layout: gl.constexpr = gl.BlockedLayout([1, 4], [4, 8], [1, 4], [1, 0])
    result = gl.convert_layout(acc, out_layout)
    out_rows = gl.program_id(0) * BM + gl.arange(0, BM, layout=gl.SliceLayout(1, out_layout))
    out_cols = output_tile * 32 + gl.arange(0, 32, layout=gl.SliceLayout(0, out_layout))
    gl.store(Y + out_rows[:, None] * N + out_cols[None, :], result,
             (out_rows[:, None] < M) & (out_cols[None, :] < N))


@g.jit
def _linear(X, W, Y, M: gl.constexpr, N: gl.constexpr, K: gl.constexpr,
            BM: gl.constexpr, BK: gl.constexpr, KIND: gl.constexpr):
    _product(X, W, Y, M, N, K, BM, BK, KIND, gl.program_id(1))


@g.jit
def _grouped(X, W0, W1, W2, Y0, Y1, Y2, M: gl.constexpr,
             N0: gl.constexpr, N1: gl.constexpr, N2: gl.constexpr, K: gl.constexpr, BK: gl.constexpr, KIND: gl.constexpr):
    tile = gl.program_id(1)
    first: gl.constexpr = gl.cdiv(N0, 32)
    second: gl.constexpr = gl.cdiv(N1, 32)
    if tile < first:
        _product(X, W0, Y0, M, N0, K, 16, BK, KIND, tile)
    elif tile < first + second:
        _product(X, W1, Y1, M, N1, K, 16, BK, KIND, tile - first)
    else:
        _product(X, W2, Y2, M, N2, K, 16, BK, KIND, tile - first - second)


def linear(x, packed, out, rows, width, *, block_rows=16, block_k=None, kind="Q6_K_REPACKED"):
    m = x.numel() // width
    bk = block_k or (256 if kind in ("Q4_K_REPACKED", "Q5_K_REPACKED") or width >= 8192 else 128)
    _linear[(triton.cdiv(m, block_rows), triton.cdiv(rows, 32))](
        x, packed, out, m, rows, width, block_rows, bk, kind,
        num_warps=4, num_stages=1, enable_fp_fusion=False)


def linear_group(x, matrices, outputs):
    count = len(matrices)
    kind = matrices[0].kernel_kind
    sizes = [matrix.out_features for matrix in matrices]
    weights = [matrix.packed for matrix in matrices]
    destinations = list(outputs)
    if count == 2:
        sizes.append(0)
        weights.append(weights[-1])
        destinations.append(destinations[-1])
    width = x.shape[-1]
    grid = (triton.cdiv(x.numel() // width, 16), sum(triton.cdiv(size, 32) for size in sizes))
    _grouped[grid](x, *weights, *destinations, x.numel() // width, *sizes, width,
                   256 if kind in ("Q4_K_REPACKED", "Q5_K_REPACKED") else 128, kind,
                   num_warps=4, num_stages=1, enable_fp_fusion=False)
