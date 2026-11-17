"""Contiguous KV storage for fixed batches and independently reusable slots."""
from __future__ import annotations

import torch

from .config import DecoderConfig


class KVCache:
    def __init__(self, config: DecoderConfig, batch: int, capacity: int, *, device, dtype):
        shape = (config.num_hidden_layers, batch, config.num_key_value_heads,
                 capacity, config.head_dim)
        self.key = torch.empty(shape, device=device, dtype=dtype)
        self.value = torch.empty_like(self.key)
        self.valid = torch.zeros(batch, capacity, device=device, dtype=torch.bool)
        self.length = 0

    def prepare(self, valid: torch.Tensor) -> torch.Tensor:
        end = self.length + valid.shape[1]
        if end > self.valid.shape[1]:
            raise ValueError("Caption exceeds the configured KV context capacity")
        self.valid[:, self.length:end] = valid
        if valid.shape[1] == 1:
            return self.valid[:, None, None, :end]
        queries = torch.arange(self.length, end, device=valid.device)
        keys = torch.arange(end, device=valid.device)
        return (keys[None, None, None, :] <= queries[None, None, :, None]) & self.valid[:, None, None, :end]

    def write(self, layer: int, key: torch.Tensor, value: torch.Tensor):
        end = self.length + key.shape[2]
        self.key[layer, :, :, self.length:end] = key
        self.value[layer, :, :, self.length:end] = value
        return self.key[layer, :, :, :end], self.value[layer, :, :, :end]


class SlotCache:
    def __init__(self, config: DecoderConfig, batch: int, capacity: int, *, device, dtype, optimized=False):
        shape = (config.num_hidden_layers, batch, config.num_key_value_heads,
                 capacity, config.head_dim)
        # SDPA may load a masked tail before applying the mask. Keep it finite.
        self.key = torch.zeros(shape, device=device, dtype=dtype)
        self.value = torch.zeros_like(self.key)
        self.valid = torch.zeros(batch, capacity, device=device, dtype=torch.bool)
        self.lengths = [0] * batch
        self.rows = None
        self.indices = None
        self.end = 0
        self.active = [False] * batch
        self.optimized = optimized
        self.positions = None
        self.enabled = None
        self.length = 0  # CaptionDecoder's batch counter; slot writes use lengths.

    def install(self, slot: int, source: KVCache, row: int):
        length = source.length
        self.key[:, slot, :, :length].copy_(source.key[:, row, :, :length])
        self.value[:, slot, :, :length].copy_(source.value[:, row, :, :length])
        self.valid[slot].zero_()
        self.valid[slot, :length].copy_(source.valid[row, :length])
        self.lengths[slot] = length

    def release(self, slot: int):
        self.lengths[slot] = 0
        self.valid[slot].zero_()

    def prepare(self, valid: torch.Tensor) -> torch.Tensor:
        active = self.active
        if any(length >= self.valid.shape[1] for length, enabled in zip(self.lengths, active) if enabled):
            raise ValueError("Caption exceeds the configured KV context capacity")
        self.rows = torch.tensor([i for i, enabled in enumerate(active) if enabled], device=valid.device)
        self.indices = torch.tensor([length for length, enabled in zip(self.lengths, active) if enabled], device=valid.device)
        self.valid[self.rows, self.indices] = True
        if self.optimized and valid.is_cuda:
            self.positions = torch.tensor([length if enabled else 0 for length, enabled in zip(self.lengths, active)], device=valid.device)
            self.enabled = valid[:, 0].contiguous()
        self.end = max(length + int(enabled) for length, enabled in zip(self.lengths, active))
        self.lengths = [length + int(enabled) for length, enabled in zip(self.lengths, active)]
        return self.valid[:, None, None, :self.end]

    def write(self, layer: int, key: torch.Tensor, value: torch.Tensor):
        if self.optimized and key.is_cuda and torch.cuda.get_device_capability(key.device) >= (8, 0):
            from .kernels.cache import write_slot

            write_slot(key, value, self.key[layer], self.value[layer], self.positions, self.enabled)
        else:
            self.key[layer, self.rows, :, self.indices, :] = key[self.rows, :, 0, :]
            self.value[layer, self.rows, :, self.indices, :] = value[self.rows, :, 0, :]
        return self.key[layer, :, :, :self.end], self.value[layer, :, :, :self.end]
