"""Shared Gluon reader for lossless Q6 code and original-scale word planes."""
from triton.experimental import gluon as g
from triton.experimental.gluon import language as gl


@g.jit
def weight(W, rows, cols, N: gl.constexpr, K: gl.constexpr):
    words = W.to(gl.pointer_type(gl.uint32))
    base = (rows // 32 * (K // 256) + cols // 256) * 1696 + rows % 32
    group = cols % 256 // 16
    bit = cols % 16 * 6
    codes_base = base + group * 96
    mask = (rows < N) & (cols < K)
    low = gl.load(words + codes_base + bit // 32 * 32, mask, 0)
    shift = bit % 32
    high = gl.load(words + codes_base + (bit // 32 + 1) * 32, mask & (shift > 26), 0)
    code = ((low >> shift) | (high << (32 - shift))) & 63
    scales = gl.load(words + base + (48 + group // 4) * 32, mask, 0)
    scale = (scales >> (group % 4 * 8)).to(gl.int8).to(gl.float32)
    d = gl.load(words + base + 52 * 32, mask, 0).to(gl.uint16).to(gl.float16, bitcast=True).to(gl.float32)
    return (d * scale) * (code.to(gl.int32) - 32).to(gl.float32)
