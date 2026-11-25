"""GGUF 反量化至官方 Qwen3.5 模块，恢复导出时的参数变换。"""
from __future__ import annotations

import torch
from torch import nn

from ..qwen3vl.weights import GGUFWeights
from ..qwen3vl.matrices.gguf import GGUFMatrix
from .matrices import HeadProjection, QuantizedEmbedding, inverse_heads, load_matrix


def load_model(metadata, gguf, mmproj, *, device="cpu", dtype=torch.float32, optimized="auto",
               linear_backend="auto"):
    from transformers import Qwen3_5Config
    from transformers.models.qwen3_5.modeling_qwen3_5 import (
        Qwen3_5ForConditionalGeneration, Qwen3_5TextRotaryEmbedding, Qwen3_5VisionRotaryEmbedding,
    )

    config = Qwen3_5Config.from_pretrained(str(metadata), local_files_only=True)
    config._attn_implementation = "sdpa"
    text, vision = GGUFWeights(gguf), GGUFWeights(mmproj)
    if text.value("general.architecture") != "qwen35" or vision.value("clip.projector_type") != "qwen3vl_merger":
        raise ValueError("需要 Qwen3.5 GGUF 及对应视觉编码器")
    from .memory import choose_packed
    optimized = choose_packed(text, device, dtype, optimized)
    with torch.device("meta"):
        model = Qwen3_5ForConditionalGeneration(config)
        from .linear import configure
        model.linear_backend = configure(model, device, linear_backend)
        model.packed_weights = optimized

    def assign(module, name, tensor):
        expected = module.get_parameter(name)
        if tensor.shape != expected.shape:
            raise ValueError(f"GGUF/config 参数形状不匹配：{name}: {tensor.shape} / {expected.shape}")
        parent, _, leaf = name.rpartition(".")
        setattr(module.get_submodule(parent) if parent else module, leaf,
                nn.Parameter(tensor.to(device=device, dtype=dtype), requires_grad=False))

    language = model.model.language_model
    def dense_matrix(name):
        if torch.device(device).type == "cuda" and dtype == torch.float16:
            matrix = text.matrix(name, device=device, dtype=dtype)
            if isinstance(matrix, GGUFMatrix):
                from ..qwen3vl.kernels.cuda.gguf import dequantize
                value = dequantize(matrix.packed, matrix.in_features, matrix.kind)
                if value is not None:
                    return value
            else:
                return matrix.weight
        return text.tensor(name)

    if optimized:
        language.embed_tokens = QuantizedEmbedding(load_matrix(text, "token_embd.weight", device=device, dtype=dtype), dtype)
        model.lm_head = load_matrix(text, "output.weight", device=device, dtype=dtype)
    else:
        assign(language, "embed_tokens.weight", dense_matrix("token_embd.weight"))
        assign(model, "lm_head.weight", dense_matrix("output.weight"))
    assign(language, "norm.weight", text.tensor("output_norm.weight") - 1)

    def projection(module, target, source, order=None, *, input_order=False):
        if optimized:
            matrix = load_matrix(text, source, device=device, dtype=dtype)
            setattr(module, target, HeadProjection(matrix, order, input_order=input_order) if order is not None else matrix)
        else:
            value = dense_matrix(source)
            if order is not None:
                indices = order.argsort() if input_order else order
                value = value.index_select(1 if input_order else 0, indices.to(value.device))
            assign(module, target + ".weight", value)
    for i, layer in enumerate(language.layers):
        prefix = f"blk.{i}."
        for target, source in (("input_layernorm", "attn_norm"), ("post_attention_layernorm", "post_attention_norm")):
            assign(layer, target + ".weight", text.tensor(prefix + source + ".weight") - 1)
        for target, source in (("gate_proj", "ffn_gate"), ("up_proj", "ffn_up"), ("down_proj", "ffn_down")):
            projection(layer.mlp, target, prefix + source + ".weight")
        if config.text_config.layer_types[i] == "full_attention":
            for target, source in (("q_proj", "attn_q"), ("k_proj", "attn_k"), ("v_proj", "attn_v"), ("o_proj", "attn_output")):
                projection(layer.self_attn, target, prefix + source + ".weight")
            for target, source in (("q_norm", "attn_q_norm"), ("k_norm", "attn_k_norm")):
                assign(layer.self_attn, target + ".weight", text.tensor(prefix + source + ".weight") - 1)
        else:
            cfg = config.text_config
            nk, nv = cfg.linear_num_key_heads, cfg.linear_num_value_heads
            hd = cfg.linear_value_head_dim
            def inverse(value, dim, head_dim):
                return inverse_heads(value, dim, nk, nv, head_dim)
            order_device = device
            order = inverse(torch.arange(nv * hd, device=order_device), 0, hd)
            for target, source in (("in_proj_qkv", "attn_qkv"), ("in_proj_z", "attn_gate"),
                                   ("in_proj_a", "ssm_alpha"), ("in_proj_b", "ssm_beta"), ("out_proj", "ssm_out")):
                if target == "in_proj_qkv":
                    split = cfg.linear_key_head_dim * nk * 2
                    permutation = torch.cat((torch.arange(split, device=order_device), order + split))
                elif target == "out_proj":
                    permutation = order.argsort()
                else:
                    permutation = order if target == "in_proj_z" else inverse(torch.arange(nv, device=order_device), 0, 1)
                projection(layer.linear_attn, target, prefix + source + ".weight", permutation,
                           input_order=target == "out_proj")
            for target, source in (("dt_bias", "ssm_dt.bias"), ("A_log", "ssm_a"),
                                   ("norm.weight", "ssm_norm.weight"), ("conv1d.weight", "ssm_conv1d.weight")):
                value = text.tensor(prefix + source)
                if target == "A_log":
                    value = inverse((-value).log(), 0, 1)
                elif target == "dt_bias":
                    value = inverse(value, 0, 1)
                elif target == "conv1d.weight":
                    split = cfg.linear_key_head_dim * nk * 2
                    value = torch.cat((value[:split], inverse(value[split:], 0, hd)), dim=0).unsqueeze(1)
                assign(layer.linear_attn, target, value)

    def vision_name(key):
        if key == "pos_embed.weight":
            return "v.position_embd.weight"
        if key.startswith("patch_embed.proj."):
            return "v.patch_embd." + key.rsplit(".", 1)[1]
        if key.startswith("merger."):
            _, part, suffix = key.split(".")
            return {"norm": "v.post_ln", "linear_fc1": "mm.0", "linear_fc2": "mm.2"}[part] + "." + suffix
        _, i, *parts, suffix = key.split(".")
        mapped = {"norm1": "ln1", "norm2": "ln2", "attn.qkv": "attn_qkv", "attn.proj": "attn_out",
                  "mlp.linear_fc1": "ffn_up", "mlp.linear_fc2": "ffn_down"}[".".join(parts)]
        return f"v.blk.{i}.{mapped}.{suffix}"

    for key, _ in list(model.model.visual.named_parameters()):
        name = vision_name(key)
        value = (torch.stack((vision.tensor(name), vision.tensor(name + ".1")), dim=2)
                 if key == "patch_embed.proj.weight" else vision.tensor(name))
        assign(model.model.visual, key, value)
    language.rotary_emb = Qwen3_5TextRotaryEmbedding(config.text_config, device=device)
    model.model.visual.rotary_pos_emb = Qwen3_5VisionRotaryEmbedding(
        config.vision_config.hidden_size // config.vision_config.num_heads // 2).to(device)
    return model.eval()
