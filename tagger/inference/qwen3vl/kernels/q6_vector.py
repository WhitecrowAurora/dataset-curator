"""Coalesced Q4/Q6 GEMV reads with the original FP32 reduction order."""
from __future__ import annotations

import triton
from triton.experimental import gluon as g
from triton.experimental.gluon import language as gl
from triton.experimental.gluon.language import BlockedLayout, SliceLayout

from .q4_read import weight as _q4_weight
from .q5_read import weight as _q5_weight
from .q6_read import weight as _weight
from .q8_read import weight as _q8_weight


@g.jit
def _vector(X, W, Y, N: gl.constexpr, K: gl.constexpr, BN: gl.constexpr, BK: gl.constexpr,
            KIND: gl.constexpr, COALESCED: gl.constexpr = False):
    layout: gl.constexpr = BlockedLayout([1, 2], [1, 32], [1, 4], [1, 0])
    read_layout: gl.constexpr = BlockedLayout([1, 2], [8, 4], [1, 4], [0, 1])
    rows = gl.program_id(0) * BN + gl.arange(0, BN, layout=SliceLayout(1, read_layout))
    inner = gl.arange(0, BK, layout=SliceLayout(0, layout))
    read_inner = gl.arange(0, BK, layout=SliceLayout(0, read_layout))
    accumulation: gl.constexpr = read_layout if COALESCED else layout
    acc = gl.full((BN, BK), 0, gl.float32, accumulation)
    for offset in range(gl.cdiv(K, BK)):
        cols = offset * BK + read_inner
        if KIND == "Q4_K_REPACKED":
            weight = _q4_weight(W, rows[:, None], cols[None, :], N, K).to(gl.float16).to(gl.float32)
        elif KIND == "Q5_K_REPACKED":
            weight = _q5_weight(W, rows[:, None], cols[None, :], N, K).to(gl.float16).to(gl.float32)
        elif KIND == "Q8_0_REPACKED":
            weight = _q8_weight(W, rows[:, None], cols[None, :], N, K).to(gl.float16).to(gl.float32)
        else:
            weight = _weight(W, rows[:, None], cols[None, :], N, K).to(gl.float16).to(gl.float32)
        weight = gl.convert_layout(weight, accumulation)
        cols = offset * BK + (read_inner if COALESCED else inner)
        x = gl.load(X + cols, cols < K, 0).to(gl.float32)
        acc += weight * x[None, :]
    # Moving the layout conversion out of the K loop preserves each FP32
    # accumulator and the final reduction order while avoiding repeated shuffles.
    acc = gl.convert_layout(acc, layout)
    out_rows = gl.program_id(0) * BN + gl.arange(0, BN, layout=SliceLayout(1, layout))
    gl.store(Y + out_rows, gl.sum(acc, 1), out_rows < N)


def vector(x, packed, out, rows, width, *, kind="Q6_K_REPACKED", coalesced=False):
    _vector[(triton.cdiv(rows, 8),)](x, packed, out, rows, width, 8, 256, kind, coalesced,
                                     num_warps=4, enable_fp_fusion=False)
