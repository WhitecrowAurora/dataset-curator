"""Reuse a captured token decoder across requests sharing the same KV slots."""
from __future__ import annotations

import torch


class _GraphCache:
    def __init__(self, storage, positions, enabled, length, span):
        self.storage = storage
        self.positions, self.enabled = positions, enabled
        self.decode_length, self.span = length, span
        self.length = 0

    def prepare(self, valid):
        return self.storage.valid[:, None, None, :self.span]

    def write(self, layer, key, value):
        from .kernels.cache import write_slot

        k, v = self.storage.key[layer], self.storage.value[layer]
        write_slot(key, value, k, v, self.positions, self.enabled)
        return k[:, :, :self.span], v[:, :, :self.span]


class DecodeGraph:
    def __init__(self, engine, storage, logits):
        from .kernels.decode_inputs import DecodeInputs

        self.engine, self.storage = engine, storage
        batch = len(storage.lengths)
        self.inputs = DecodeInputs(engine.device, batch)
        self.ids, self.positions, self.valid = self.inputs.ids, self.inputs.positions, self.inputs.valid
        self.kv_positions, self.length = self.inputs.kv_positions, self.inputs.length
        self.stream = torch.cuda.Stream(device=engine.device)
        self.pool = torch.cuda.graph_pool_handle()
        self.graphs = {}
        self.destination = logits

    @staticmethod
    def supported(engine):
        return (engine.decode_graph and engine.device.type == "cuda"
                and engine.decoder.norm.optimized and engine.context_capacity <= 4096
                and engine.decoder.config.head_dim <= 256
                and all(layer.self_attn.decode_attention == "native" for layer in engine.decoder.layers)
                and torch.cuda.get_device_capability(engine.device) >= (8, 0))

    def _forward(self, cache):
        from .kernels.decode_inputs import copy_logits

        self.inputs.prepare(self.storage)
        hidden = self.engine.decoder(self.engine.decoder.embed(self.ids, self.engine.dtype),
                                     self.positions, self.valid, cache)
        logits = self.engine.output(hidden[:, -1])
        copy_logits(logits, self.destination, self.valid[:, 0])
        return self.destination

    def _capture(self, span):
        cache = _GraphCache(self.storage, self.kv_positions, self.valid[:, 0], self.length, span)
        current = torch.cuda.current_stream(self.engine.device)
        self.stream.wait_stream(current)
        with torch.cuda.stream(self.stream):
            # Compile and initialize library handles before capture. These runs
            # overwrite the same token position without advancing slot lengths.
            for _ in range(2):
                self._forward(cache)
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph, stream=self.stream, pool=self.pool):
                logits = self._forward(cache)
        current.wait_stream(self.stream)
        self.graphs[span] = graph, logits
        return graph, logits

    @torch.inference_mode()
    def __call__(self, ids, positions, active):
        storage = self.storage
        if any(length >= storage.valid.shape[1] for length, enabled in zip(storage.lengths, active) if enabled):
            raise ValueError("Caption exceeds the configured KV context capacity")
        self.inputs.upload(ids, positions, storage.lengths, active)
        storage.end = max(length + int(enabled) for length, enabled in zip(storage.lengths, active))
        storage.lengths = [length + int(enabled) for length, enabled in zip(storage.lengths, active)]
        # Match the eager softmax/PV reduction bucket, including a non-power-of-
        # two capacity. The device length prevents reading the unused tail.
        span = min(1 << (self.storage.end - 1).bit_length(), self.storage.valid.shape[1])
        graph, logits = self.graphs.get(span) or self._capture(span)
        graph.replay()
        return logits
