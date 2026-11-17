"""Replay probability preparation and each request's independent random draw."""
from __future__ import annotations

import torch


class ProbabilityGraph:
    def __init__(self, scores, top_p):
        self.input = torch.empty_like(scores)
        self.top_p = top_p
        self.input.zero_()
        self.stream = torch.cuda.Stream(device=scores.device)
        current = torch.cuda.current_stream(scores.device)
        self.stream.wait_stream(current)
        with torch.cuda.stream(self.stream):
            for _ in range(2):
                self._prepare()
            self.graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(self.graph, stream=self.stream):
                self.values, self.order = self._prepare()
        current.wait_stream(self.stream)
        self.draws = [None] * len(top_p)

    def _prepare(self):
        values, order = self.input.softmax(-1).sort(descending=True)
        for row, top_p in zip(values, self.top_p):
            row.masked_fill_(row.cumsum(-1) - row >= top_p * row.sum(), 0)
        return values, order

    def __call__(self, scores):
        self.input.copy_(scores)
        self.graph.replay()
        return self.values, self.order

    def sample(self, generators, active):
        result = []
        for slot, (generator, enabled) in enumerate(zip(generators, active)):
            if not enabled:
                result.append(self.order.new_zeros(()))
                continue
            cached = self.draws[slot]
            if cached is None or cached[0] is not generator:
                cached = self._capture_draw(slot, generator)
                self.draws[slot] = cached
            _, graph, selected = cached
            graph.replay()
            result.append(selected)
        return torch.stack(result)

    def _capture_draw(self, slot, generator):
        state = generator.get_state()
        current = torch.cuda.current_stream(self.input.device)
        self.stream.wait_stream(current)
        with torch.cuda.stream(self.stream):
            for _ in range(2):
                torch.multinomial(self.values[slot], 1, generator=generator)
            graph = torch.cuda.CUDAGraph()
            graph.register_generator_state(generator)
            with torch.cuda.graph(graph, stream=self.stream):
                index = torch.multinomial(self.values[slot], 1, generator=generator)
                selected = self.order[slot].gather(0, index).squeeze(0)
        current.wait_stream(self.stream)
        # Warmup and capture must not consume this request's random sequence.
        generator.set_state(state)
        return generator, graph, selected
