"""Incremental caption decoding with per-request sampling and reusable KV slots."""
from __future__ import annotations

from dataclasses import dataclass

import torch

from .cache import SlotCache
from .cache_prefill import PrefillCache
from .decode_graph import DecodeGraph
from .engine import CaptionResult
from .prefill import PrefillJob
from .sampling import BatchSampler, Sampling


@dataclass(slots=True)
class _Request:
    request: object
    history: list[int]
    prompt_tokens: int
    generator: torch.Generator
    tokens: list[int]
    decode: bool = False
    position: int = 0


class CaptionSession:
    def __init__(self, engine, batch_size):
        self.engine = engine
        self.slots = [None] * batch_size
        self.cache = None
        self.logits = None
        self.offsets = torch.zeros(batch_size, device=engine.device, dtype=torch.long)
        self.prefill_job = None
        self.prefill_slots = []
        self.prefill_requests = []
        self.cancelled = set()
        self.sampler = BatchSampler(engine.decoder.norm.optimized)
        self.inactive_sampling = Sampling()
        self.decode_graph = None

    @property
    def available(self):
        return [i for i, state in enumerate(self.slots) if state is None and i not in self.prefill_slots]

    @property
    def active(self):
        return self.prefill_job is not None or any(state is not None for state in self.slots)

    @property
    def can_admit(self):
        return self.prefill_job is None and bool(self.available)

    @torch.inference_mode()
    def add(self, requests):
        slots = self.begin(requests)
        self._advance_prefill(len(self.engine.decoder.layers))
        return slots

    @torch.inference_mode()
    def begin(self, requests):
        free = self.available[:len(requests)]
        if self.prefill_job is not None or len(free) != len(requests):
            raise ValueError("Caption session has no free slots")

        def cache_factory(width):
            if self.cache is None:
                self.cache = SlotCache(self.engine.decoder.config, len(self.slots), self.engine.context_capacity,
                                       device=self.engine.device, dtype=self.engine.dtype,
                                       optimized=self.engine.decoder.norm.optimized)
            return PrefillCache(self.cache, free, width)

        job = PrefillJob(self.engine, requests, cache_factory=cache_factory)
        self.prefill_job, self.prefill_slots, self.prefill_requests = job, free, requests
        return free

    def _advance_prefill(self, layers):
        if self.prefill_job is None:
            return
        initial = self.prefill_job.advance(layers)
        if initial is None:
            return
        initial.cache.commit()
        if self.logits is None:
            self.logits = initial.logits.new_zeros(len(self.slots), initial.logits.shape[-1])
        positions = initial.offsets.tolist()
        for row, (slot, request) in enumerate(zip(self.prefill_slots, self.prefill_requests, strict=True)):
            if slot in self.cancelled:
                self.cache.release(slot)
                continue
            generator = torch.Generator(device=self.engine.device)
            generator.seed() if request.sampling.seed is None else generator.manual_seed(request.sampling.seed)
            self.logits[slot].copy_(initial.logits[row])
            self.offsets[slot].copy_(initial.offsets[row])
            self.slots[slot] = _Request(request, initial.history[row], initial.prompt_sizes[row], generator, [],
                                       position=positions[row])
        self.prefill_job, self.prefill_slots, self.prefill_requests = None, [], []
        self.cancelled.clear()

    @torch.inference_mode()
    def cancel(self, slot):
        if slot in self.prefill_slots:
            self.cancelled.add(slot)
            if len(self.cancelled) == len(self.prefill_slots):
                for pending in self.prefill_slots:
                    self.cache.release(pending)
                self.prefill_job, self.prefill_slots, self.prefill_requests = None, [], []
                self.cancelled.clear()
            return
        self.slots[slot] = None
        if self.cache is not None:
            self.cache.release(slot)

    @torch.inference_mode()
    def step(self):
        if self.prefill_job is not None:
            layers = ((self.engine.prefill_layers or len(self.engine.decoder.layers))
                      if any(self.slots) else len(self.engine.decoder.layers))
            self._advance_prefill(layers)
        states = self.slots
        if not any(states):
            return []
        decoding = [state is not None and state.decode for state in states]
        if any(decoding):
            tokens = [state.tokens[-1] if enabled else 0 for state, enabled in zip(states, decoding)]
            offsets = [len(state.tokens) - 1 if enabled else 0 for state, enabled in zip(states, decoding)]
            self.cache.active = decoding
            length = max(n + int(enabled) for n, enabled in zip(self.cache.lengths, decoding))
            if DecodeGraph.supported(self.engine, length):
                if self.decode_graph is None:
                    self.decode_graph = DecodeGraph(self.engine, self.cache, self.logits)
                positions = [offset + state.position if state is not None else 0
                             for offset, state in zip(offsets, states)]
                self.decode_graph(tokens, positions, decoding)
            else:
                ids = torch.tensor(tokens, device=self.engine.device)[:, None]
                positions = (self.offsets + torch.tensor(offsets, device=self.engine.device)).view(1, -1, 1).expand(3, -1, -1)
                valid = torch.tensor(decoding, device=self.engine.device, dtype=torch.bool)[:, None]
                hidden = self.engine.decoder(self.engine.decoder.embed(ids, self.engine.dtype), positions, valid, self.cache)
                logits = self.engine.output(hidden[:, -1])
                for i, enabled in enumerate(decoding):
                    if enabled:
                        self.logits[i].copy_(logits[i])
        selected = self.sampler(self.logits, [state.history if state else [] for state in states],
                                [state.request.sampling if state else self.inactive_sampling for state in states],
                                [state.generator if state else None for state in states],
                                [state is not None for state in states]).tolist()
        completed = []
        for slot, state in enumerate(states):
            if state is None:
                continue
            token = selected[slot]
            state.tokens.append(token)
            state.history.append(token)
            state.decode = True
            stop = token in self.engine.eos
            if stop or len(state.tokens) >= state.request.sampling.max_tokens:
                result = CaptionResult(self.engine.processor.tokenizer.decode(state.tokens, skip_special_tokens=True),
                                       tuple(state.tokens), state.prompt_tokens, "stop" if stop else "length")
                completed.append((slot, result))
                self.cancel(slot)
        return completed
