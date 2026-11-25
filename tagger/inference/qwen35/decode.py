"""Bounded CUDA graphs for the complete single-token language forward."""
from __future__ import annotations

import torch


class Decoder:
    def __init__(self, engine, cache, batch_size):
        self.engine, self.cache = engine, cache
        self.ids = torch.zeros((batch_size, 1), device=engine.device, dtype=torch.long)
        self.positions = torch.zeros((4, batch_size, 1), device=engine.device, dtype=torch.long)
        self.graphs = {}
        self.enabled = engine.decode_graph and engine.device.type == "cuda"
        if self.enabled:
            self.stream = torch.cuda.Stream(device=engine.device)
            self.pool = torch.cuda.graph_pool_handle()

    def forward(self, span):
        self.cache.set_span(span)
        model = self.engine.model
        hidden = model.model.language_model(
            input_ids=self.ids, position_ids=self.positions, past_key_values=self.cache, use_cache=True,
            attention_mask={"full_attention": self.cache.mask(span), "linear_attention": None})[0]
        return model.lm_head(hidden[:, -1])

    def capture(self, span):
        # Warmup advances recurrent states; restore them before the real token.
        # KV warmup overwrites the same position, so it needs no full-cache copy.
        snapshots = [(value, value.clone()) for value in self.cache.recurrent_tensors()]
        def restore():
            for value, saved in snapshots:
                value.copy_(saved)
        current = torch.cuda.current_stream(self.engine.device)
        self.stream.wait_stream(current)
        with torch.cuda.stream(self.stream):
            for _ in range(2):
                self.forward(span)
                restore()
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph, stream=self.stream, pool=self.pool):
                logits = self.forward(span)
            restore()
        current.wait_stream(self.stream)
        self.graphs[span] = graph, logits
        return graph, logits

    def __call__(self, tokens, active):
        lengths = self.cache.lengths
        positions = [length if enabled else 0 for length, enabled in zip(lengths, active)]
        self.ids.copy_(torch.tensor(tokens, device=self.engine.device)[:, None])
        self.cache.positions.copy_(torch.tensor(positions, device=self.engine.device))
        # Inactive rows still execute, but are overwritten completely on admission.
        self.cache.starts.masked_fill_(~torch.tensor(active, device=self.engine.device), 0)
        length = max(positions) + 1
        if length > self.engine.context_capacity:
            raise ValueError("图片和描述超出上下文容量")
        span = min(max(512, 1 << (length - 1).bit_length()), self.engine.context_capacity)
        if self.enabled:
            graph, logits = self.graphs.get(span) or self.capture(span)
            graph.replay()
        else:
            logits = self.forward(span)
        self.cache.lengths = [length + int(enabled) for length, enabled in zip(lengths, active)]
        self.positions.add_(1)
        return logits
