"""Stable hybrid attention slots shared by prefill and captured decoding."""
from __future__ import annotations

import torch
from transformers.cache_utils import StaticCache, StaticLayer


class _AttentionSlot(StaticLayer):
    def __init__(self, capacity, positions):
        super().__init__(capacity)
        self.positions = positions
        self.span = capacity

    def update(self, key_states, value_states, *args, **kwargs):
        indices = self.positions[:, None, None, None].expand_as(key_states)
        self.keys.scatter_(2, indices, key_states)
        self.values.scatter_(2, indices, value_states)
        return self.keys[:, :, :self.span], self.values[:, :, :self.span]


class SlotCache(StaticCache):
    def __init__(self, config, source, batch_size, capacity, device):
        super().__init__(config=config, max_cache_len=capacity)
        self.positions = torch.zeros(batch_size, device=device, dtype=torch.long)
        self.starts = torch.zeros_like(self.positions)
        self.columns = torch.arange(capacity, device=device)
        self.lengths = [0] * batch_size
        for index, initial in enumerate(source.layers):
            if hasattr(initial, "keys"):
                layer = _AttentionSlot(capacity, self.positions)
                shape = (batch_size, initial.keys.shape[1], 1, initial.keys.shape[-1])
                layer.lazy_initialization(initial.keys.new_empty(shape), initial.values.new_empty(shape))
                self.layers[index] = layer
            else:
                layer = self.layers[index]
                for state in initial.conv_states:
                    conv, recurrent = initial.conv_states[state], initial.recurrent_states[state]
                    layer.lazy_initialization(conv_states=conv.new_empty(batch_size, *conv.shape[1:]),
                                              recurrent_states=recurrent.new_empty(batch_size, *recurrent.shape[1:]),
                                              state_idx=state)
                    layer.has_previous_state[state] = True

    def adopt(self, source, slots, width, sizes):
        for layer, initial in zip(self.layers, source.layers, strict=True):
            for row, slot in enumerate(slots):
                if isinstance(layer, _AttentionSlot):
                    layer.keys[slot, :, :width].copy_(initial.keys[row])
                    layer.values[slot, :, :width].copy_(initial.values[row])
                else:
                    for state in initial.conv_states:
                        layer.conv_states[state][slot].copy_(initial.conv_states[state][row])
                        layer.recurrent_states[state][slot].copy_(initial.recurrent_states[state][row])
        for slot, size in zip(slots, sizes, strict=True):
            self.starts[slot] = width - size
            self.lengths[slot] = width

    def mask(self, span):
        columns = self.columns[:span][None, :]
        return ((columns >= self.starts[:, None]) & (columns <= self.positions[:, None]))[:, None, None, :]

    def set_span(self, span):
        for layer in self.layers:
            if isinstance(layer, _AttentionSlot):
                layer.span = span

    def recurrent_tensors(self):
        for layer in self.layers:
            if not isinstance(layer, _AttentionSlot):
                yield from layer.conv_states.values()
                yield from layer.recurrent_states.values()
