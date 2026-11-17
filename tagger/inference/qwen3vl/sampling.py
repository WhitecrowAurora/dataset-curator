"""Per-request generators and llama.cpp-style repetition penalty."""
from __future__ import annotations

from dataclasses import dataclass

import torch


@dataclass(frozen=True, slots=True)
class Sampling:
    max_tokens: int = 800
    temperature: float = 0.2
    top_p: float = 0.9
    repeat_penalty: float = 1.12
    repeat_last_n: int = 64
    top_k: int = 0
    min_p: float = 0.0
    seed: int | None = None

    def __post_init__(self):
        if self.max_tokens < 1 or self.temperature < 0 or not 0 < self.top_p <= 1:
            raise ValueError("Invalid caption sampling budget, temperature, or top_p")
        if self.repeat_penalty <= 0 or self.repeat_last_n < 0 or self.top_k < 0 or not 0 <= self.min_p <= 1:
            raise ValueError("Invalid repetition or candidate sampling settings")


def sample(logits, history, options: Sampling, generator):
    scores = logits.float().clone()
    if options.repeat_penalty != 1 and options.repeat_last_n and history:
        # Gather from the unmodified scores first: repeated ids get the same
        # penalized value, so they need no dynamic-size CUDA unique operation.
        ids = torch.tensor(history[-options.repeat_last_n:], device=scores.device)
        seen = scores[ids]
        scores[ids] = torch.where(seen > 0, seen / options.repeat_penalty, seen * options.repeat_penalty)
    if options.temperature == 0:
        return scores.argmax()
    scores /= options.temperature
    if options.top_k and options.top_k < scores.numel():
        scores.masked_fill_(scores < scores.topk(options.top_k).values[-1], -torch.inf)
    probs = scores.softmax(-1)
    if options.min_p:
        probs.masked_fill_(probs < probs.max() * options.min_p, 0)
    if options.top_p < 1:
        values, order = probs.sort(descending=True)
        values.masked_fill_(values.cumsum(-1) - values >= options.top_p * values.sum(), 0)
        return order[torch.multinomial(values, 1, generator=generator)[0]]
    return torch.multinomial(probs, 1, generator=generator)[0]


class BatchSampler:
    def __init__(self, optimized=False):
        self.optimized = optimized
        self.greedy = None
        self.preparation = None
        self.probabilities = None

    def __call__(self, logits, histories, options, generators, active):
        if (self.optimized and logits.is_cuda and logits.stride(-1) == 1
                and all(not enabled or option.temperature == 0 for option, enabled in zip(options, active))
                and torch.cuda.get_device_capability(logits.device) >= (8, 0)):
            from .kernels.sampling import GreedySampler

            if self.greedy is None:
                self.greedy = GreedySampler(logits)
            return self.greedy(logits, histories, options, active)
        if (self.optimized and logits.is_cuda and logits.shape[0] >= 4
                and logits.shape[1] >= 4096 and logits.shape[1] % 4 == 0
                and logits.stride(-1) == 1
                and torch.cuda.get_device_capability(logits.device) >= (8, 0)):
            from .kernels.sampling_prepare import SamplingPreparation
            from .sampling_batch import sample_batch

            if self.preparation is None:
                self.preparation = SamplingPreparation(logits)
            graph = None
            if all(option.temperature > 0 and not option.top_k and not option.min_p
                   and option.top_p < 1 for option in options):
                from .sampling_graph import ProbabilityGraph

                top_p = tuple(option.top_p for option in options)
                if (self.probabilities is None or self.probabilities.top_p != top_p
                        or self.probabilities.input.shape != logits.shape
                        or self.probabilities.input.device != logits.device):
                    self.probabilities = ProbabilityGraph(logits.float(), top_p)
                graph = self.probabilities
            return sample_batch(logits, histories, options, generators, active, self.preparation, graph)
        return torch.stack([sample(logits[i], history, option, generator) if enabled
                            else logits.new_zeros((), dtype=torch.long)
                            for i, (history, option, generator, enabled) in
                            enumerate(zip(histories, options, generators, active))])

