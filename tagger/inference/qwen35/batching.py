"""Left padding and per-request sampling for official Qwen3.5 generation."""
from __future__ import annotations

import torch
from transformers import LogitsProcessor, StoppingCriteria

from ..qwen3vl.input_batch import collate
from ..qwen3vl.sampling import sample


def prepare_batch(engine, requests):
    prepared = [(r if r.prepared is not None else engine.prepare_request(r)).prepared for r in requests]
    sizes = [p["input_ids"].shape[1] for p in prepared]
    if max(sizes) + max(r.sampling.max_tokens for r in requests) > engine.context_capacity:
        raise ValueError("图片和描述超出上下文容量，请降低像素预算或描述长度")
    inputs = collate(prepared, engine.processor.tokenizer.pad_token_id)
    if "mm_token_type_ids" in prepared[0]:
        types = torch.zeros_like(inputs["input_ids"])
        for row, (p, size) in enumerate(zip(prepared, sizes)):
            types[row, -size:] = p["mm_token_type_ids"][0]
        inputs["mm_token_type_ids"] = types
    histories = [p["input_ids"][0].tolist() for p in prepared]
    return inputs, histories, sizes


class RequestSampler(LogitsProcessor):
    def __init__(self, requests, histories, device, eos, pad):
        self.options = [r.sampling for r in requests]
        self.histories, self.eos, self.pad = histories, eos, pad
        self.active = [True] * len(requests)
        self.step = 0
        self.generators = []
        for option in self.options:
            generator = torch.Generator(device=device)
            generator.seed() if option.seed is None else generator.manual_seed(option.seed)
            self.generators.append(generator)

    def __call__(self, ids, scores):
        selected = torch.stack([
            sample(scores[i], history, option, generator) if active
            else scores.new_full((), self.pad, dtype=torch.long)
            for i, (history, option, generator, active) in enumerate(
                zip(self.histories, self.options, self.generators, self.active))])
        self.step += 1
        for i, token in enumerate(selected.tolist()):
            if self.active[i]:
                self.histories[i].append(token)
                self.active[i] = token not in self.eos and self.step < self.options[i].max_tokens
        return torch.full_like(scores, -torch.inf).scatter_(1, selected[:, None], 0)


class RequestLengths(StoppingCriteria):
    def __init__(self, width, requests, device):
        self.width = width
        self.limits = torch.tensor([r.sampling.max_tokens for r in requests], device=device)

    def __call__(self, ids, scores, **kwargs):
        return ids.shape[1] - self.width >= self.limits
