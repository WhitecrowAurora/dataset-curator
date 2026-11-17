"""Lossless Q5_K word planes retaining all 176 original bytes per block."""
import torch
import triton
import triton.language as tl


@triton.jit
def _repack(W, Y, N: tl.constexpr, K: tl.constexpr, WORDS: tl.constexpr):
    row = tl.program_id(0)
    index = tl.program_id(1) * WORDS + tl.arange(0, WORDS)
    count: tl.constexpr = K // 256 * 44
    source = W.to(tl.pointer_type(tl.int32))
    target = (row // 32 * (K // 256) + index // 44) * 1408 + index % 44 * 32 + row % 32
    value = tl.load(source + row * count + index, index < count, 0)
    tl.store(Y + target, value, index < count)


@triton.jit
def weight(W, rows, cols, N: tl.constexpr, K: tl.constexpr):
    words = W.to(tl.pointer_type(tl.uint32))
    base = (rows[:, None] // 32 * (K // 256) + cols[None, :] // 256) * 1408 + rows[:, None] % 32
    within = cols % 256
    group, lane = within // 32, within % 32
    mask = (rows[:, None] < N) & (cols[None, :] < K)
    header = tl.load(words + base, mask, 0)
    d = header.to(tl.uint16).to(tl.float16, bitcast=True).to(tl.float32)
    dmin = (header >> 16).to(tl.uint16).to(tl.float16, bitcast=True).to(tl.float32)
    shift = group[None, :] % 4 * 8
    a = (tl.load(words + base + 32, mask, 0) >> shift) & 255
    b = (tl.load(words + base + 64, mask, 0) >> shift) & 255
    c = (tl.load(words + base + 96, mask, 0) >> shift) & 255
    scale = tl.where(group[None, :] < 4, a & 63, (c & 15) | ((a >> 2) & 48)).to(tl.float32)
    minimum = tl.where(group[None, :] < 4, b & 63, (c >> 4) | ((b >> 2) & 48)).to(tl.float32)
    packed = tl.load(words + base + (12 + group[None, :] // 2 * 8 + lane[None, :] // 4) * 32, mask, 0)
    code = (packed >> (lane[None, :] % 4 * 8 + group[None, :] % 2 * 4)) & 15
    high = tl.load(words + base + (4 + lane[None, :] // 4) * 32, mask, 0)
    code |= ((high >> (lane[None, :] % 4 * 8 + group[None, :])) & 1) << 4
    return (d * scale) * code.to(tl.float32) - (dmin * minimum)


def repack(packed, rows, width):
    result = torch.empty(triton.cdiv(rows, 32), width // 256, 44, 32,
                         device=packed.device, dtype=torch.int32)
    with torch.cuda.device(packed.device):
        _repack[(rows, triton.cdiv(width // 256 * 44, 128))](
            packed, result, rows, width, 128, num_warps=4, enable_fp_fusion=False)
    return result


