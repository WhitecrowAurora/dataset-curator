"""Load the text GGUF and its paired F16 vision projector without re-quantizing."""
from __future__ import annotations

from pathlib import Path

import gguf
import numpy as np
import torch
from torch import nn

from .config import DecoderConfig
from .decoder import CaptionDecoder
from .matrices.gguf import BLOCKS, GGUFMatrix


class GGUFWeights:
    def __init__(self, path: str | Path):
        self.reader = gguf.GGUFReader(str(path))
        self.tensors = {t.name: t for t in self.reader.tensors}

    def value(self, key):
        return self.reader.fields[key].contents()

    def tensor(self, name):
        source = self.tensors[name]
        data = gguf.dequantize(source.data, source.tensor_type)
        return torch.from_numpy(np.array(data, dtype=np.float32, copy=True).reshape(
            tuple(int(v) for v in reversed(source.shape))))

    def matrix(self, name, *, device, dtype, optimized=False, repack_kinds=()):
        source = self.tensors[name]
        width, rows = map(int, source.shape)
        if source.tensor_type.name in BLOCKS:
            packed = torch.from_numpy(np.array(source.data, dtype=np.uint8, copy=True))
            module = GGUFMatrix(packed, rows, width, source.tensor_type.name, optimized=optimized).to(device)
            return module.repack(kinds=repack_kinds) if repack_kinds else module
        if source.tensor_type not in (gguf.GGMLQuantizationType.F16, gguf.GGMLQuantizationType.F32):
            raise ValueError(f"Unsupported experimental text matrix type: {source.tensor_type.name}")
        module = nn.Linear(width, rows, bias=False, device="meta")
        module.weight = nn.Parameter(self.tensor(name).to(device=device, dtype=dtype), requires_grad=False)
        return module


def load_decoder(path: str | Path, config: DecoderConfig, *, device, dtype, optimized=False,
                 q6_repack=False, q4_repack=False, gguf_repack=()):
    import importlib.util

    can_repack = (optimized and torch.device(device).type == "cuda"
                 and torch.cuda.get_device_capability(device) >= (8, 0)
                 and importlib.util.find_spec("triton.experimental.gluon") is not None)
    kinds = set(gguf_repack)
    if q6_repack or q4_repack:
        kinds.add("Q6_K")
    if q4_repack:
        kinds.add("Q4_K")
    repack_kinds = frozenset(kinds) if can_repack else ()
    weights = GGUFWeights(path)
    if weights.value("general.architecture") != "qwen3vl":
        raise ValueError("Expected a dense Qwen3-VL GGUF")
    dimensions = {
        "block_count": config.num_hidden_layers, "embedding_length": config.hidden_size,
        "feed_forward_length": config.intermediate_size,
        "attention.head_count": config.num_attention_heads,
        "attention.head_count_kv": config.num_key_value_heads,
    }
    for key, expected in dimensions.items():
        if weights.value("qwen3vl." + key) != expected:
            raise ValueError(f"GGUF/config mismatch for {key}")
    if not np.isclose(weights.value("qwen3vl.rope.freq_base"), config.rope_theta):
        raise ValueError("GGUF/config MRoPE frequency mismatch")
    if tuple(weights.value("qwen3vl.rope.dimension_sections")[:3]) != config.mrope_section:
        raise ValueError("GGUF/config MRoPE section mismatch")
    with torch.device("meta"):
        decoder = CaptionDecoder(config, optimized=optimized)
    from .decoder import Rotary

    decoder.rotary_emb = Rotary(config).to(device)
    embedding = weights.matrix("token_embd.weight", device=device, dtype=dtype, optimized=optimized)
    if isinstance(embedding, GGUFMatrix):
        decoder.embed_tokens = embedding
    else:
        decoder.embed_tokens = nn.Embedding.from_pretrained(embedding.weight, freeze=True)
    for i, layer in enumerate(decoder.layers):
        prefix = f"blk.{i}."
        for target, source in (("q_proj", "attn_q"), ("k_proj", "attn_k"),
                               ("v_proj", "attn_v"), ("o_proj", "attn_output")):
            setattr(layer.self_attn, target, weights.matrix(prefix + source + ".weight", device=device, dtype=dtype,
                                                          optimized=optimized, repack_kinds=repack_kinds))
        for target, source in (("gate_proj", "ffn_gate"), ("up_proj", "ffn_up"), ("down_proj", "ffn_down")):
            setattr(layer.mlp, target, weights.matrix(prefix + source + ".weight", device=device, dtype=dtype,
                                                    optimized=optimized, repack_kinds=repack_kinds))
        for norm, name in ((layer.input_layernorm, "attn_norm"),
                           (layer.post_attention_layernorm, "ffn_norm"),
                           (layer.self_attn.q_norm, "attn_q_norm"), (layer.self_attn.k_norm, "attn_k_norm")):
            norm.weight = nn.Parameter(weights.tensor(prefix + name + ".weight").to(device=device, dtype=dtype), requires_grad=False)
    decoder.norm.weight = nn.Parameter(weights.tensor("output_norm.weight").to(device=device, dtype=dtype), requires_grad=False)
    output = weights.matrix("output.weight", device=device, dtype=dtype, optimized=optimized,
                            repack_kinds=repack_kinds) if "output.weight" in weights.tensors else embedding
    return decoder.eval(), output.eval()


def load_vision(path: str | Path, config, *, device, dtype):
    from transformers.models.qwen3_vl.modeling_qwen3_vl import (
        Qwen3VLVisionModel,
    )

    from .vision_rotary import load_rotary

    weights = GGUFWeights(path)
    if weights.value("clip.projector_type") != "qwen3vl_merger":
        raise ValueError("Expected the Qwen3-VL vision projector paired with the text GGUF")
    config._attn_implementation = "sdpa"
    with torch.device("meta"):
        model = Qwen3VLVisionModel(config)
    model.rotary_pos_emb = load_rotary(config, device=device)

    def name_for(key):
        if key == "pos_embed.weight":
            return "v.position_embd.weight"
        if key.startswith("patch_embed.proj."):
            return "v.patch_embd." + key.rsplit(".", 1)[1]
        if key.startswith("merger."):
            _, part, suffix = key.split(".")
            return {"norm": "v.post_ln", "linear_fc1": "mm.0", "linear_fc2": "mm.2"}[part] + "." + suffix
        if key.startswith("deepstack_merger_list."):
            _, i, part, suffix = key.split(".")
            part = {"norm": "norm", "linear_fc1": "fc1", "linear_fc2": "fc2"}[part]
            return f"v.deepstack.{config.deepstack_visual_indexes[int(i)]}.{part}.{suffix}"
        parts = key.split(".")
        _, i = parts[:2]
        part = ".".join(parts[2:-1])
        mapped = {"norm1": "ln1", "norm2": "ln2", "attn.qkv": "attn_qkv", "attn.proj": "attn_out",
                  "mlp.linear_fc1": "ffn_up", "mlp.linear_fc2": "ffn_down"}[part]
        return f"v.blk.{i}.{mapped}.{parts[-1]}"

    state = {}
    for key, expected in model.state_dict().items():
        name = name_for(key)
        tensor = (torch.stack((weights.tensor(name), weights.tensor(name + ".1")), dim=2)
                  if key == "patch_embed.proj.weight" else weights.tensor(name))
        if tensor.shape != expected.shape:
            raise ValueError(f"Vision GGUF/config mismatch for {key}: {tensor.shape} vs {expected.shape}")
        state[key] = tensor.to(device=device, dtype=dtype)
    model.load_state_dict(state, strict=True, assign=True)
    return model.eval()

