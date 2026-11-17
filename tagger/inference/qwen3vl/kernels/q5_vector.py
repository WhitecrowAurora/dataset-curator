"""Coalesced Q5 reads retaining the original byte GEMV reduction layout."""
import triton
from triton.experimental import gluon as g
from triton.experimental.gluon import language as gl

from .q5_read import weight


@g.jit
def _vector(X, W, Y, N: gl.constexpr, K: gl.constexpr):
    layout: gl.constexpr = gl.BlockedLayout([1, 16], [2, 16], [4, 1], [1, 0])
    read_layout: gl.constexpr = gl.BlockedLayout([1, 2], [8, 4], [1, 4], [0, 1])
    rows = gl.program_id(0) * 8 + gl.arange(0, 8, layout=gl.SliceLayout(1, read_layout))
    read_inner = gl.arange(0, 256, layout=gl.SliceLayout(0, read_layout))
    inner = gl.arange(0, 256, layout=gl.SliceLayout(0, layout))
    acc = gl.full((8, 256), 0, gl.float32, layout)
    for offset in range(K // 256):
        decoded = weight(W, rows[:, None], (offset * 256 + read_inner)[None, :], N, K)
        w = gl.convert_layout(decoded.to(gl.float16).to(gl.float32), layout)
        x = gl.load(X + offset * 256 + inner).to(gl.float32)
        acc += w * x[None, :]
    out_rows = gl.program_id(0) * 8 + gl.arange(0, 8, layout=gl.SliceLayout(1, layout))
    gl.store(Y + out_rows, gl.sum(acc, 1), out_rows < N)


def vector(x, packed, output, rows, width, *, kind="Q5_K_REPACKED"):
    _vector[(triton.cdiv(rows, 8),)](x, packed, output, rows, width, num_warps=4, enable_fp_fusion=False)
