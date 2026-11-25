"""在 Torii 实例内选择线性注意力内核，保持官方归一化与 CPU 路径。"""
from __future__ import annotations

import importlib.util
import logging

import torch


def configure(model, device, backend="auto"):
    from transformers.models.qwen3_5 import modeling_qwen3_5 as reference

    if backend not in ("auto", "torch", "fla"):
        raise ValueError(f"未知线性注意力实现：{backend}")
    chunk, recurrent = reference.torch_chunk_gated_delta_rule, reference.torch_recurrent_gated_delta_rule
    selected = "torch"
    supported = (torch.device(device).type == "cuda" and torch.cuda.get_device_capability(device) >= (8, 0)
                 and importlib.util.find_spec("fla") is not None)
    if backend != "torch" and supported:
        try:
            from fla.ops.gated_delta_rule import chunk_gated_delta_rule, fused_recurrent_gated_delta_rule
        except (ImportError, OSError) as error:
            if backend == "fla":
                raise
            logging.getLogger(__name__).warning("FLA 未就绪，Torii 使用 PyTorch 线性注意力：%s", error)
        else:
            chunk, recurrent = chunk_gated_delta_rule, fused_recurrent_gated_delta_rule
            selected = "fla"
    elif backend == "fla":
        raise ValueError("FLA 需要 Ampere 或更新的 CUDA GPU 及 flash-linear-attention 依赖")

    for layer in model.model.language_model.layers:
        attention = getattr(layer, "linear_attn", None)
        if attention is None:
            continue
        # Transformers 的可选 FLA 导入还会替换 norm；这里固定原 norm，避免环境改变模型语义。
        attention.norm = reference.Qwen3_5RMSNormGated(attention.head_v_dim, eps=attention.layer_norm_epsilon)
        attention.causal_conv1d_fn = None
        attention.causal_conv1d_update = reference.torch_causal_conv1d_update
        attention.chunk_gated_delta_rule = chunk
        attention.recurrent_gated_delta_rule = recurrent
    return selected
