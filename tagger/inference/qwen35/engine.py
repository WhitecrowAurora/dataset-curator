"""官方混合注意力前向与缓存，复用现有 caption 请求/采样契约。"""
from __future__ import annotations

import time
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

import torch

from ..qwen3vl.engine import CaptionResult
from .batching import RequestLengths, RequestSampler, prepare_batch
from .weights import load_model


class CaptionEngine:
    def __init__(self, metadata, gguf, mmproj, *, device="cpu", max_pixels=1_000_000, context_capacity=8192,
                 optimized="auto", linear_backend="auto", decode_graph=True):
        from transformers import AutoProcessor
        self.device = torch.device(device)
        self.dtype = torch.float16 if self.device.type == "cuda" else torch.float32
        self.max_pixels, self.context_capacity = max_pixels, context_capacity
        self.decode_graph = decode_graph
        self.supports_continuous = self.device.type == "cuda"
        self._batch_session = None
        self.processor = AutoProcessor.from_pretrained(str(metadata), local_files_only=True, trust_remote_code=False)
        self.processor.chat_template = (Path(__file__).parents[2] / "hair_color/torii_chat_template.jinja").read_text(encoding="utf-8")
        self.model = load_model(metadata, gguf, mmproj, device=self.device, dtype=self.dtype, optimized=optimized,
                                linear_backend=linear_backend)
        self.optimized = self.model.packed_weights
        self.runtime_info = {"device": str(self.device), "precision": "gguf_packed_fp16" if self.optimized else str(self.dtype),
                             "linear_attention": self.model.linear_backend, "max_pixels": self.max_pixels}
        self.runtime_info["libraries"] = {"torch": torch.__version__, "transformers": version("transformers")}
        if self.model.linear_backend == "fla":
            self.runtime_info["libraries"]["flash-linear-attention"] = version("flash-linear-attention")
        if self.model.triton_projections or self.model.linear_backend == "fla":
            import triton
            self.runtime_info["libraries"]["triton"] = triton.__version__
        self.eos = {self.model.config.text_config.eos_token_id}
        self.eos.update(self.processor.tokenizer.convert_tokens_to_ids(v) for v in ("<|im_end|>", "<|endoftext|>"))
        self.last_timings = {}

    def create_session(self, batch_size):
        from .session import CaptionSession
        return CaptionSession(self, batch_size)

    def prepare_request(self, request):
        messages = [{"role": "system", "content": request.system}, {"role": "user", "content": [
            {"type": "image"}, {"type": "text", "text": request.prompt}]}]
        text = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.processor(text=[text], images=[request.image], return_tensors="pt",
                                images_kwargs={"max_pixels": self.max_pixels, "min_pixels": min(65536, self.max_pixels)})
        return replace(request, prepared=inputs)

    @torch.inference_mode()
    def generate_batch(self, requests):
        if not requests:
            return []
        if not self.supports_continuous:
            return self._generate_reference(requests)
        started = time.perf_counter()
        if self._batch_session is None or len(self._batch_session.slots) != len(requests):
            self._batch_session = None
            self._batch_session = self.create_session(len(requests))
        session = self._batch_session
        results = [None] * len(requests)
        try:
            slots = session.begin(requests)
            prefill = time.perf_counter() - started
            while session.active:
                for slot, result in session.step():
                    results[slots.index(slot)] = result
        finally:
            for slot in range(len(session.slots)):
                session.cancel(slot)
        self.last_timings = {"prefill_s": prefill, "decode_s": time.perf_counter() - started - prefill,
                             "total_s": time.perf_counter() - started, "batch_size": len(requests),
                             "tokens": sum(len(r.token_ids) for r in results),
                             "prompt_tokens": sum(r.prompt_tokens for r in results)}
        return results

    @torch.inference_mode()
    def _generate_reference(self, requests):
        from transformers import GenerationConfig, LogitsProcessorList, StoppingCriteriaList

        if not requests:
            return []
        started = time.perf_counter()
        inputs, histories, sizes = prepare_batch(self, requests)
        sampler = RequestSampler(requests, histories, self.device, self.eos, self.processor.tokenizer.pad_token_id)
        inputs = {k: v.to(device=self.device, dtype=self.dtype if v.is_floating_point() else v.dtype)
                  for k, v in inputs.items()}
        size = inputs["input_ids"].shape[1]
        timing = {}
        events = [torch.cuda.Event(enable_timing=True) for _ in range(2)] if self.device.type == "cuda" else None
        def forward_started(module, args, kwargs):
            if "prefill_started" not in timing:
                if events:
                    events[0].record(torch.cuda.current_stream(self.device))
                timing["prefill_started"] = time.perf_counter()
        def forward_finished(module, args, output):
            if "prefill_finished" not in timing:
                if events:
                    events[1].record(torch.cuda.current_stream(self.device))
                timing["prefill_finished"] = time.perf_counter()
        before = self.model.register_forward_pre_hook(forward_started, with_kwargs=True)
        after = self.model.register_forward_hook(forward_finished)
        try:
            generated = self.model.generate(**inputs, generation_config=GenerationConfig(
                max_new_tokens=max(r.sampling.max_tokens for r in requests), do_sample=False, use_cache=True,
                eos_token_id=sorted(self.eos), pad_token_id=self.processor.tokenizer.pad_token_id),
                logits_processor=LogitsProcessorList([sampler]), logits_to_keep=1,
                stopping_criteria=StoppingCriteriaList([RequestLengths(size, requests, self.device)]))
        finally:
            before.remove()
            after.remove()
        results = []
        for row, request, prompt_size in zip(generated[:, size:].tolist(), requests, sizes):
            tokens = row[:request.sampling.max_tokens]
            end = next((i for i, token in enumerate(tokens) if token in self.eos), None)
            finish = "length" if end is None else "stop"
            if end is not None:
                tokens = tokens[:end]
            results.append(CaptionResult(self.processor.tokenizer.decode(tokens, skip_special_tokens=True),
                                         tuple(tokens), prompt_size, finish))
        prefill = events[0].elapsed_time(events[1]) / 1000 if events else timing["prefill_finished"] - timing["prefill_started"]
        self.last_timings = {"prefill_s": prefill,
                             "decode_s": time.perf_counter() - timing["prefill_started"] - prefill,
                             "total_s": time.perf_counter() - started, "prompt_tokens": sum(sizes),
                             "tokens": sum(len(r.token_ids) for r in results), "batch_size": len(requests)}
        return results
