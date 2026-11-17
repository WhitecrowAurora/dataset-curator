"""Stage token metadata and update active logits inside the decoder graph."""
from __future__ import annotations

import torch
import triton
import triton.language as tl


@triton.jit
def _prepare(META, IDS, POSITIONS, VALID, KV_POSITIONS, LENGTH, MASK,
             BATCH: tl.constexpr, CAPACITY: tl.constexpr, BLOCK: tl.constexpr):
    slots = tl.arange(0, BLOCK)
    mask = slots < BATCH
    ids = tl.load(META + slots, mask, 0)
    positions = tl.load(META + BATCH + slots, mask, 0)
    kv = tl.load(META + 2 * BATCH + slots, mask, 0)
    active = tl.load(META + 3 * BATCH + slots, mask, 0).to(tl.int1)
    tl.store(IDS + slots, ids, mask)
    for axis in tl.static_range(3):
        tl.store(POSITIONS + axis * BATCH + slots, positions, mask)
    tl.store(VALID + slots, active, mask)
    tl.store(KV_POSITIONS + slots, tl.where(active, kv, 0), mask)
    tl.store(MASK + slots * CAPACITY + kv, True, mask & active)
    tl.store(LENGTH, tl.max(tl.where(mask, kv + active.to(tl.int64), 0), 0))


@triton.jit
def _copy_logits(X, Y, ACTIVE, WIDTH: tl.constexpr, BLOCK: tl.constexpr):
    slot = tl.program_id(0)
    cols = tl.program_id(1) * BLOCK + tl.arange(0, BLOCK)
    active = tl.load(ACTIVE + slot)
    mask = active & (cols < WIDTH)
    value = tl.load(X + slot * WIDTH + cols, mask, 0)
    tl.store(Y + slot * WIDTH + cols, value, mask)


class DecodeInputs:
    def __init__(self, device, batch):
        self.host = torch.empty(4, batch, dtype=torch.long, pin_memory=True)
        self.metadata = torch.empty(4, batch, device=device, dtype=torch.long)
        self.ids = torch.empty(batch, 1, device=device, dtype=torch.long)
        self.positions = torch.empty(3, batch, 1, device=device, dtype=torch.long)
        self.valid = torch.empty(batch, 1, device=device, dtype=torch.bool)
        self.kv_positions = torch.empty(batch, device=device, dtype=torch.long)
        self.length = torch.empty((), device=device, dtype=torch.long)

    def upload(self, ids, positions, lengths, active):
        self.host.copy_(torch.tensor([ids, positions, lengths, active], dtype=torch.long))
        # One blocking transfer also makes this staging buffer safe to reuse
        # for callers that do not immediately synchronize through sampling.
        self.metadata.copy_(self.host)

    def prepare(self, storage):
        batch = self.ids.shape[0]
        _prepare[(1,)](self.metadata, self.ids, self.positions, self.valid,
                      self.kv_positions, self.length, storage.valid, batch,
                      storage.valid.shape[1], triton.next_power_of_2(batch), num_warps=4)


def copy_logits(source, destination, active):
    batch, width = source.shape
    _copy_logits[(batch, triton.cdiv(width, 1024))](
        source, destination, active, width, 1024, num_warps=4)
