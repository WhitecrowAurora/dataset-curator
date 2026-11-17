"""Image preparation and first-pass logits shared by batch and slot generation."""
from __future__ import annotations

import time
from dataclasses import dataclass

import torch

from .cache import KVCache
from .positions import image_positions


@dataclass(slots=True)
class Prefill:
    cache: KVCache
    logits: torch.Tensor
    history: list[list[int]]
    offsets: torch.Tensor
    prompt_sizes: list[int]
    timings: dict[str, float]


class PrefillJob:
    @torch.inference_mode()
    def __init__(self, engine, requests, *, prompt_only=False, cache_factory=None):
        from transformers.vision_utils import get_vision_cu_seqlens

        self.engine = engine
        self.layer = 0
        started = time.perf_counter()
        inputs = engine.prepare(requests)
        self.timings = {"prepare_s": time.perf_counter() - started, "prefill_s": 0.0}
        ids = inputs["input_ids"].to(engine.device)
        valid = inputs["attention_mask"].to(device=engine.device, dtype=torch.bool)
        grid = inputs["image_grid_thw"]
        cpu_valid = inputs["attention_mask"].bool()
        positions, deltas = image_positions(inputs["input_ids"], cpu_valid, grid,
                                            engine.config.image_token_id,
                                            engine.config.vision_config.spatial_merge_size)
        self.prompt_sizes = cpu_valid.sum(-1).tolist()
        self.offsets = torch.tensor(self.prompt_sizes, device=engine.device) + deltas.to(engine.device)
        self.history = [inputs["input_ids"][i, cpu_valid[i]].tolist() for i in range(len(requests))]
        if ids.shape[1] + max(r.sampling.max_tokens for r in requests) - 1 > engine.context_capacity:
            raise ValueError("Prompt and requested caption exceed the configured context capacity")
        self.cache = (cache_factory(ids.shape[1]) if cache_factory else KVCache(
            engine.decoder.config, len(requests), ids.shape[1] if prompt_only else engine.context_capacity,
            device=engine.device, dtype=engine.dtype))
        self.hidden = engine.decoder.embed(ids, engine.dtype)
        started = time.perf_counter()
        # Both vision loaders use SDPA, whose chunk lengths are consumed on CPU.
        vision = engine.vision(inputs["pixel_values"].to(device=engine.device, dtype=engine.dtype),
                               grid_thw=grid.to(engine.device), cu_seqlens=get_vision_cu_seqlens(grid),
                               return_dict=True)
        engine._synchronize()
        self.timings["vision_s"] = time.perf_counter() - started
        self.visual_mask = ids == engine.config.image_token_id
        self.hidden[self.visual_mask] = vision.pooler_output.to(engine.dtype)
        self.deepstack = vision.deepstack_features
        self.mask = self.cache.prepare(valid)
        self.rotary = engine.decoder.rotary_emb(positions.to(engine.device), engine.dtype)

    @torch.inference_mode()
    def advance(self, layers):
        decoder = self.engine.decoder
        end = min(self.layer + layers, len(decoder.layers))
        started = time.perf_counter()
        for i in range(self.layer, end):
            self.hidden = decoder.layers[i](self.hidden, self.rotary, self.mask, self.cache, i)
            if i < len(self.deepstack):
                self.hidden = self.hidden.clone()
                self.hidden[self.visual_mask] += self.deepstack[i].to(self.hidden.dtype)
        self.layer = end
        done = end == len(decoder.layers)
        result = None
        if done:
            self.cache.length += self.hidden.shape[1]
            logits = self.engine.output(decoder.norm(self.hidden)[:, -1])
            result = Prefill(self.cache, logits, self.history, self.offsets, self.prompt_sizes, self.timings)
        self.engine._synchronize()
        self.timings["prefill_s"] += time.perf_counter() - started
        return result


@torch.inference_mode()
def prefill(engine, requests, *, prompt_only=False):
    job = PrefillJob(engine, requests, prompt_only=prompt_only)
    return job.advance(len(engine.decoder.layers))
