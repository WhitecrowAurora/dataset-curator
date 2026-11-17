"""Read original Q5_K scale/min fields and five-bit codes in MMA layout."""
from triton.experimental import gluon as g
from triton.experimental.gluon import language as gl


@g.jit
def weight(W, rows, cols, N: gl.constexpr, K: gl.constexpr):
    words = W.to(gl.pointer_type(gl.uint32))
    base = (rows // 32 * (K // 256) + cols // 256) * 1408 + rows % 32
    within = cols % 256
    group, lane = within // 32, within % 32
    mask = (rows < N) & (cols < K)
    header = gl.load(words + base, mask, 0)
    d = header.to(gl.uint16).to(gl.float16, bitcast=True).to(gl.float32)
    dmin = (header >> 16).to(gl.uint16).to(gl.float16, bitcast=True).to(gl.float32)
    shift = group % 4 * 8
    a = (gl.load(words + base + 32, mask, 0) >> shift) & 255
    b = (gl.load(words + base + 64, mask, 0) >> shift) & 255
    c = (gl.load(words + base + 96, mask, 0) >> shift) & 255
    scale = gl.where(group < 4, a & 63, (c & 15) | ((a >> 2) & 48)).to(gl.float32)
    minimum = gl.where(group < 4, b & 63, (c >> 4) | ((b >> 2) & 48)).to(gl.float32)
    packed = gl.load(words + base + (12 + group // 2 * 8 + lane // 4) * 32, mask, 0)
    code = (packed >> (lane % 4 * 8 + group % 2 * 4)) & 15
    high = gl.load(words + base + (4 + lane // 4) * 32, mask, 0)
    code |= ((high >> (lane % 4 * 8 + group)) & 1) << 4
    return (d * scale) * code.to(gl.float32) - (dmin * minimum)
