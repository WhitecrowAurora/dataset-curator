"""Continuous Torii captions with independent sampling and reusable hybrid slots."""
from __future__ import annotations

from dataclasses import dataclass

import torch
from transformers import DynamicCache

from ..qwen3vl.engine import CaptionResult
from ..qwen3vl.sampling import BatchSampler, Sampling
from .batching import prepare_batch
from .cache import SlotCache
from .decode import Decoder


@dataclass(slots=True)
class _Request:
    request: object
    history: list[int]
    prompt_tokens: int
    generator: torch.Generator
    tokens: list[int]


class CaptionSession:
    def __init__(self, engine, batch_size):
        self.engine = engine
        self.slots = [None] * batch_size
        self.cache = self.decoder = self.logits = None
        self.sampler = BatchSampler(engine.model.triton_projections)
        self.inactive_sampling = Sampling()

    @property
    def available(self):
        return [i for i, state in enumerate(self.slots) if state is None]

    @property
    def active(self):
        return any(state is not None for state in self.slots)

    @property
    def can_admit(self):
        return bool(self.available)

    @torch.inference_mode()
    def begin(self, requests):
        free = self.available[:len(requests)]
        if len(free) != len(requests):
            raise ValueError("Caption session has no free slots")
        engine = self.engine
        inputs, histories, sizes = prepare_batch(engine, requests)
        inputs = {k: v.to(device=engine.device, dtype=engine.dtype if v.is_floating_point() else v.dtype)
                  for k, v in inputs.items()}
        positions = engine.model._prepare_position_ids_for_generation(inputs["input_ids"], inputs)
        source = DynamicCache(config=engine.model.config)
        output = engine.model(**inputs, position_ids=positions, past_key_values=source,
                              use_cache=True, logits_to_keep=1)
        if self.cache is None:
            self.cache = SlotCache(engine.model.config, source, len(self.slots), engine.context_capacity, engine.device)
            self.decoder = Decoder(engine, self.cache, len(self.slots))
            self.logits = output.logits.new_zeros(len(self.slots), output.logits.shape[-1])
        self.cache.adopt(source, free, inputs["input_ids"].shape[1], sizes)
        for row, (slot, request) in enumerate(zip(free, requests, strict=True)):
            generator = torch.Generator(device=engine.device)
            generator.seed() if request.sampling.seed is None else generator.manual_seed(request.sampling.seed)
            self.logits[slot].copy_(output.logits[row, -1])
            self.decoder.positions[:, slot].copy_(positions[:, row, -1:] + 1)
            self.slots[slot] = _Request(request, histories[row], sizes[row], generator, [])
        return free

    add = begin

    def cancel(self, slot):
        self.slots[slot] = None

    @torch.inference_mode()
    def step(self):
        if not self.active:
            return []
        selected = self.sampler(self.logits, [state.history if state else [] for state in self.slots],
                               [state.request.sampling if state else self.inactive_sampling for state in self.slots],
                               [state.generator if state else None for state in self.slots],
                               [state is not None for state in self.slots]).tolist()
        completed = []
        for slot, state in enumerate(self.slots):
            if state is None:
                continue
            token = selected[slot]
            stop = token in self.engine.eos
            if not stop:
                state.tokens.append(token)
                state.history.append(token)
            if stop or len(state.tokens) >= state.request.sampling.max_tokens:
                result = CaptionResult(self.engine.processor.tokenizer.decode(state.tokens, skip_special_tokens=True),
                                       tuple(state.tokens), state.prompt_tokens, "stop" if stop else "length")
                completed.append((slot, result))
                self.cancel(slot)
        if self.active:
            self.logits.copy_(self.decoder(selected, [state is not None for state in self.slots]))
        return completed
