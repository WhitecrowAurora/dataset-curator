"""Lossless Q8_0 word planes, preserving all 272 bytes per 256 values."""
import torch
import triton
import triton.language as tl


@triton.jit
def _repack(W, Y, N: tl.constexpr, K: tl.constexpr, B: tl.constexpr):
    row = tl.program_id(0)
    index = tl.program_id(1) * B + tl.arange(0, B)
    count: tl.constexpr = K // 256 * 68
    plane = index % 68
    block = index // 68
    base = row * (K // 32 * 34) + block * 272
    scale_pair = plane * 68
    code = plane - 4
    code_base = code // 8 * 34 + 2 + code % 8 * 4
    b0 = tl.where(plane < 4, scale_pair, code_base)
    b1 = b0 + 1
    b2 = tl.where(plane < 4, b0 + 34, b0 + 2)
    b3 = tl.where(plane < 4, b0 + 35, b0 + 3)
    mask = index < count
    word = tl.load(W + base + b0, mask, 0).to(tl.uint32)
    word |= tl.load(W + base + b1, mask, 0).to(tl.uint32) << 8
    word |= tl.load(W + base + b2, mask, 0).to(tl.uint32) << 16
    word |= tl.load(W + base + b3, mask, 0).to(tl.uint32) << 24
    target = (row // 32 * (K // 256) + block) * 2176 + plane * 32 + row % 32
    tl.store(Y + target, word, mask)


@triton.jit
def weight(W, rows, cols, N: tl.constexpr, K: tl.constexpr):
    words = W.to(tl.pointer_type(tl.uint32))
    base = (rows[:, None] // 32 * (K // 256) + cols[None, :] // 256) * 2176 + rows[:, None] % 32
    within = cols % 256
    group = within // 32
    mask = (rows[:, None] < N) & (cols[None, :] < K)
    scales = tl.load(words + base + group[None, :] // 2 * 32, mask, 0)
    d = (scales >> (group[None, :] % 2 * 16)).to(tl.uint16).to(tl.float16, bitcast=True).to(tl.float32)
    packed = tl.load(words + base + (4 + within[None, :] // 4) * 32, mask, 0)
    code = (packed >> (within[None, :] % 4 * 8)).to(tl.int8).to(tl.float32)
    return d * code


def repack(packed, rows, width):
    result = torch.empty(triton.cdiv(rows, 32), width // 256, 68, 32,
                         device=packed.device, dtype=torch.int32)
    with torch.cuda.device(packed.device):
        _repack[(rows, triton.cdiv(width // 256 * 68, 128))](
            packed, result, rows, width, 128, num_warps=4, enable_fp_fusion=False)
    return result
