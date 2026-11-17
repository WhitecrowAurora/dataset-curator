"""Stage original Q6 word planes in shared memory for ordered MMA."""
from triton.experimental import gluon as g
from triton.experimental.gluon import language as gl
from triton.experimental.gluon.language.nvidia.ampere import async_copy as cp


@g.jit
def copy_block(W, shared, block, output_tile, N: gl.constexpr, K: gl.constexpr, KIND: gl.constexpr = "Q6_K_REPACKED"):
    layout: gl.constexpr = gl.BlockedLayout([1, 4], [1, 32], [4, 1], [1, 0])
    planes = gl.arange(0, 64, layout=gl.SliceLayout(1, layout))
    lanes = gl.arange(0, 32, layout=gl.SliceLayout(0, layout))
    count: gl.constexpr = 36 if KIND == "Q4_K_REPACKED" else (44 if KIND == "Q5_K_REPACKED" else 53)
    base = (output_tile * (K // 256) + block) * (count * 32)
    pointers = W.to(gl.pointer_type(gl.uint32)) + base + planes[:, None] * 32 + lanes[None, :]
    mask = (planes[:, None] < count) & (output_tile * 32 + lanes[None, :] < N) & (block < K // 256)
    cp.async_copy_global_to_shared(shared, pointers, mask)
    cp.commit_group()


@g.jit
def weight(shared, offset, BK: gl.constexpr, layout: gl.constexpr):
    cols = offset + gl.arange(0, BK, layout=gl.SliceLayout(1, layout))
    lanes = gl.arange(0, 32, layout=gl.SliceLayout(0, layout))
    group, bit = cols // 16, cols % 16 * 6
    word = group * 3 + bit // 32
    # Shared gather expands only the words consumed by the original MMA operand.
    low = shared.gather(word[:, None] + lanes[None, :] * 0, 0)
    high = shared.gather((word + 1)[:, None] + lanes[None, :] * 0, 0)
    shift = bit % 32
    code = ((low >> shift[:, None]) | gl.where(shift[:, None] > 26, high << (32 - shift[:, None]), 0)) & 63
    scales = shared.gather((48 + group // 4)[:, None] + lanes[None, :] * 0, 0)
    scale = (scales >> (group[:, None] % 4 * 8)).to(gl.int8).to(gl.float32)
    d = shared.gather(gl.full((BK, 32), 52, gl.int32, layout), 0)
    d = d.to(gl.uint16).to(gl.float16, bitcast=True).to(gl.float32)
    return ((d * scale) * (code.to(gl.int32) - 32).to(gl.float32)).to(gl.float16)
