"""Batch caption generation from the same text GGUF and vision projector.

Image preparation and vision encoding use the official processor/model; text
decode, KV allocation, sampling and quantized execution belong to this package.
The existing NL service lifecycle manages the optional HTTP worker.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import torch

from .config import DecoderConfig
from .prefill import prefill
from .sampling import BatchSampler, Sampling
from .weights import load_decoder, load_vision


@dataclass(slots=True)
class CaptionRequest:
    image: object
    prompt: str
    system: str
    sampling: Sampling = field(default_factory=Sampling)
    prepared: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CaptionResult:
    text: str
    token_ids: tuple[int, ...]
    prompt_tokens: int
    finish_reason: str


class CaptionEngine:
    prefill_layers = 0
    decode_graph = True

    def __init__(self, model_dir: str | Path, gguf: str | Path | None = None,
                 mmproj: str | Path | None = None, *,
                 device: str = "cpu", dtype: torch.dtype | None = None,
                 max_pixels: int = 589824, context_capacity: int = 3072,
                 optimized: bool = False, native_precision: str | None = None,
                 prefill_layers: int = 0, decode_attention: str = "sdpa", decode_graph: bool = True,
                 q6_repack: bool = False, q4_repack: bool = False, prefill_attention: str = "sdpa",
                 gguf_repack: tuple[str, ...] = ()):
        from transformers import AutoProcessor, Qwen3VLConfig

        self.device = torch.device(device)
        self.dtype = dtype or (torch.float16 if self.device.type == "cuda" else torch.float32)
        self.max_pixels, self.context_capacity = max_pixels, context_capacity
        if prefill_layers < 0:
            raise ValueError("prefill_layers must be nonnegative")
        self.prefill_layers = prefill_layers
        self.decode_graph = decode_graph
        if decode_attention not in ("sdpa", "native"):
            raise ValueError("decode_attention must be 'sdpa' or 'native'")
        if prefill_attention not in ("sdpa", "cudnn"):
            raise ValueError("prefill_attention must be 'sdpa' or 'cudnn'")
        if native_precision not in (None, "fp8", "int8"):
            raise ValueError("native_precision must be 'fp8', 'int8', or None")
        if gguf is not None and native_precision is not None:
            raise ValueError("GGUF uses its stored quantization; native_precision applies to safetensors")
        self.config = Qwen3VLConfig.from_pretrained(str(model_dir), local_files_only=True)
        self.processor = AutoProcessor.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False)
        self.processor.tokenizer.padding_side = "left"
        decoder_config = DecoderConfig.from_hf(self.config.text_config)
        if gguf is not None:
            if mmproj is None:
                raise ValueError("GGUF inference requires its paired vision projector")
            self.decoder, self.output = load_decoder(gguf, decoder_config, device=self.device,
                                                      dtype=self.dtype, optimized=optimized, q6_repack=q6_repack,
                                                      q4_repack=q4_repack, gguf_repack=gguf_repack)
            self.vision = load_vision(mmproj, self.config.vision_config, device=self.device, dtype=self.dtype)
        else:
            from .hf_weights import load_hf_decoder, load_hf_vision

            self.decoder, self.output = load_hf_decoder(model_dir, decoder_config, device=self.device,
                                                         dtype=self.dtype, optimized=optimized,
                                                         native_precision=native_precision)
            self.vision = load_hf_vision(model_dir, self.config.vision_config, device=self.device, dtype=self.dtype)
        for layer in self.decoder.layers:
            layer.self_attn.decode_attention = decode_attention
            layer.self_attn.prefill_attention = prefill_attention
        eos = self.config.text_config.eos_token_id
        self.eos = {eos} if isinstance(eos, int) else set(eos)
        self.last_timings = {}

    def prepare(self, requests: list[CaptionRequest]):
        if any(request.prepared is not None for request in requests):
            from .input_batch import collate

            inputs = [request.prepared if request.prepared is not None else self.prepare([request])
                      for request in requests]
            return collate(inputs, self.processor.tokenizer.pad_token_id)
        texts = []
        for request in requests:
            messages = [{"role": "system", "content": request.system},
                        {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": request.prompt}]}]
            texts.append(self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True))
        return self.processor(text=texts, images=[r.image for r in requests], padding=True,
                              return_tensors="pt", images_kwargs={"max_pixels": self.max_pixels,
                              "min_pixels": min(65536, self.max_pixels), "device": "cpu"})

    def prepare_request(self, request: CaptionRequest):
        return replace(request, prepared=self.prepare([request]))

    @torch.inference_mode()
    def generate_batch(self, requests: list[CaptionRequest]) -> list[CaptionResult]:
        if not requests:
            return []
        total_started = time.perf_counter()
        initial = prefill(self, requests)
        cache, logits = initial.cache, initial.logits
        decode_offsets, prompt_sizes = initial.offsets, initial.prompt_sizes
        timings = initial.timings
        started = time.perf_counter()
        tokens, finish = [[] for _ in requests], ["length"] * len(requests)
        history = initial.history
        generators = []
        for request in requests:
            gen = torch.Generator(device=self.device)
            gen.seed() if request.sampling.seed is None else gen.manual_seed(request.sampling.seed)
            generators.append(gen)
        active = [True] * len(requests)
        sampler = BatchSampler(self.decoder.norm.optimized)
        options = [request.sampling for request in requests]
        for step in range(max(r.sampling.max_tokens for r in requests)):
            selected = sampler(logits, history, options, generators, active)
            # One device-to-host wait for the batch, rather than a wait and a
            # host-to-device scalar assignment for every active request.
            selected_tokens = selected.tolist()
            for i, request in enumerate(requests):
                if not active[i]:
                    continue
                token = selected_tokens[i]
                tokens[i].append(token)
                history[i].append(token)
                if token in self.eos:
                    active[i], finish[i] = False, "stop"
                elif len(tokens[i]) >= request.sampling.max_tokens:
                    active[i] = False
            if not any(active):
                break
            decode_valid = torch.tensor(active, device=self.device, dtype=torch.bool).unsqueeze(1)
            decode_positions = (decode_offsets + step).view(1, -1, 1).expand(3, -1, -1)
            hidden = self.decoder(self.decoder.embed(selected[:, None], self.dtype),
                                  decode_positions, decode_valid, cache)
            logits = self.output(hidden[:, -1])
        self._synchronize()
        timings["decode_s"] = time.perf_counter() - started
        timings["total_s"] = time.perf_counter() - total_started
        self.last_timings = timings
        return [CaptionResult(self.processor.tokenizer.decode(ids, skip_special_tokens=True),
                              tuple(ids), int(prompt_sizes[i]), finish[i]) for i, ids in enumerate(tokens)]

    def _synchronize(self):
        if self.device.type == "cuda":
            torch.cuda.synchronize(self.device)

    def create_session(self, batch_size):
        from .continuous import CaptionSession

        return CaptionSession(self, batch_size)
