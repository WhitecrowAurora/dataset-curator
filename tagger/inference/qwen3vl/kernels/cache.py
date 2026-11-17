"""Write one token's key and value into independently positioned active slots."""
from __future__ import annotations

import torch
import triton
import triton.language as tl


@triton.jit
def _write(K, V, CK, CV, POS, ACTIVE, HEADS: tl.constexpr, DIM: tl.constexpr,
           CAPACITY: tl.constexpr, KSTRIDE_B: tl.constexpr, KSTRIDE_H: tl.constexpr,
           VSTRIDE_B: tl.constexpr, VSTRIDE_H: tl.constexpr, BLOCK: tl.constexpr):
    slot = tl.program_id(0)
    cols = tl.program_id(1) * BLOCK + tl.arange(0, BLOCK)
    enabled = tl.load(ACTIVE + slot)
    position = tl.load(POS + slot)
    head, lane = cols // DIM, cols % DIM
    mask = enabled & (cols < HEADS * DIM)
    key = tl.load(K + slot * KSTRIDE_B + head * KSTRIDE_H + lane, mask, 0)
    value = tl.load(V + slot * VSTRIDE_B + head * VSTRIDE_H + lane, mask, 0)
    target = ((slot * HEADS + head) * CAPACITY + position) * DIM + lane
    tl.store(CK + target, key, mask)
    tl.store(CV + target, value, mask)


def write_slot(key, value, cached_key, cached_value, positions, active):
    batch, heads, _, dim = key.shape
    capacity = cached_key.shape[2]
    with torch.cuda.device(key.device):
        _write[(batch, triton.cdiv(heads * dim, 256))](
            key, value, cached_key, cached_value, positions, active, heads, dim, capacity,
            key.stride(0), key.stride(1), value.stride(0), value.stride(1), 256, num_warps=4)
