"""Read unchanged Q8_0 scales and codes in the MMA operand layout."""
from triton.experimental import gluon as g
from triton.experimental.gluon import language as gl


@g.jit
def weight(W, rows, cols, N: gl.constexpr, K: gl.constexpr):
    words = W.to(gl.pointer_type(gl.uint32))
    base = (rows // 32 * (K // 256) + cols // 256) * 2176 + rows % 32
    within = cols % 256
    group = within // 32
    mask = (rows < N) & (cols < K)
    scales = gl.load(words + base + group // 2 * 32, mask, 0)
    d = (scales >> (group % 2 * 16)).to(gl.uint16).to(gl.float16, bitcast=True).to(gl.float32)
    packed = gl.load(words + base + (4 + within // 4) * 32, mask, 0)
    code = (packed >> (within % 4 * 8)).to(gl.int8).to(gl.float32)
    return d * code
