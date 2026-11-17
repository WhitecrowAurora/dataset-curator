"""Write a new prompt directly into its reserved slots, without a second KV."""
from __future__ import annotations

import torch

from .cache import KVCache


class PrefillCache(KVCache):
    def __init__(self, storage, slots, width):
        self.storage, self.slots = storage, slots
        contiguous = slots == list(range(slots[0], slots[0] + len(slots)))
        self.span = slice(slots[0], slots[-1] + 1) if contiguous else None
        self.indices = torch.tensor(slots, device=storage.key.device, dtype=torch.long)
        self.valid = torch.zeros(len(slots), width, device=storage.key.device, dtype=torch.bool)
        self.length = 0
        for slot in slots:
            storage.release(slot)

    def write(self, layer, key, value):
        start, end = self.length, self.length + key.shape[2]
        k, v = self.storage.key[layer], self.storage.value[layer]
        if self.span is not None:
            k[self.span, :, start:end].copy_(key)
            v[self.span, :, start:end].copy_(value)
            return k[self.span, :, :end], v[self.span, :, :end]
        k[self.indices, :, start:end] = key
        v[self.indices, :, start:end] = value
        # Only the current layer's used prefix is gathered for fragmented slots.
        return k[:, :, :end].index_select(0, self.indices), v[:, :, :end].index_select(0, self.indices)

    def commit(self):
        self.storage.valid[self.indices, :self.length] = self.valid[:, :self.length]
        for slot in self.slots:
            self.storage.lengths[slot] = self.length
