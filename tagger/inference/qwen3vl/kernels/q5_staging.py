"""Decode lossless Q5 fields from the shared MMA block."""
from triton.experimental import gluon as g
from triton.experimental.gluon import language as gl


@g.jit
def weight(shared, offset, BK: gl.constexpr, layout: gl.constexpr):
    cols = offset + gl.arange(0, BK, layout=gl.SliceLayout(1, layout))
    lanes = gl.arange(0, 32, layout=gl.SliceLayout(0, layout))
    group, lane = cols // 32, cols % 32
    header = shared.gather(gl.full((BK, 32), 0, gl.int32, layout), 0)
    d = header.to(gl.uint16).to(gl.float16, bitcast=True).to(gl.float32)
    dmin = (header >> 16).to(gl.uint16).to(gl.float16, bitcast=True).to(gl.float32)
    shift = group[:, None] % 4 * 8
    a = (shared.gather(gl.full((BK, 32), 1, gl.int32, layout), 0) >> shift) & 255
    b = (shared.gather(gl.full((BK, 32), 2, gl.int32, layout), 0) >> shift) & 255
    c = (shared.gather(gl.full((BK, 32), 3, gl.int32, layout), 0) >> shift) & 255
    scale = gl.where(group[:, None] < 4, a & 63, (c & 15) | ((a >> 2) & 48)).to(gl.float32)
    minimum = gl.where(group[:, None] < 4, b & 63, (c >> 4) | ((b >> 2) & 48)).to(gl.float32)
    packed = shared.gather((12 + group // 2 * 8 + lane // 4)[:, None] + lanes[None, :] * 0, 0)
    code = (packed >> (lane[:, None] % 4 * 8 + group[:, None] % 2 * 4)) & 15
    high = shared.gather((4 + lane // 4)[:, None] + lanes[None, :] * 0, 0)
    code |= ((high >> (lane[:, None] % 4 * 8 + group[:, None])) & 1) << 4
    return ((d * scale) * code.to(gl.float32) - (dmin * minimum)).to(gl.float16)
