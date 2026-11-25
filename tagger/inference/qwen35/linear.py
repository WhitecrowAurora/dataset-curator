"""在 Torii 实例内选择线性注意力内核，保持官方归一化与 CPU 路径。"""
from __future__ import annotations

import importlib.util
import inspect
import logging
from types import FunctionType, MethodType

import torch


def _torch_function(function):
    # kernels 会把函数包装成 Module，其 forward 闭包仍保留原函数。
    if isinstance(function, torch.nn.Module):
        function = inspect.getclosurevars(function.forward).nonlocals["func"]
    return inspect.unwrap(function)


def _instance_forward(reference, chunk, recurrent):
    from transformers.integrations.accelerate import force_accelerate_hooks

    # 新版 forward 改用模块函数；复制命名空间，仍执行官方缓存与前向逻辑。
    # 不修改共享 Transformers，也不让可选 CUDA 扩展接管 CPU / torch 路径。
    original = inspect.unwrap(reference.Qwen3_5GatedDeltaNet.forward)
    namespace = dict(original.__globals__,
                     causal_conv1d_fn=_torch_function(reference.causal_conv1d_fn),
                     causal_conv1d_update=_torch_function(reference.causal_conv1d_update),
                     torch_chunk_gated_delta_rule=chunk,
                     torch_recurrent_gated_delta_rule=recurrent)
    forward = FunctionType(original.__code__, namespace, original.__name__,
                           original.__defaults__, original.__closure__)
    forward.__kwdefaults__ = original.__kwdefaults__
    forward.__annotations__ = original.__annotations__
    return force_accelerate_hooks("conv1d")(forward)


def configure(model, device, backend="auto"):
    from transformers.models.qwen3_5 import modeling_qwen3_5 as reference

    if backend not in ("auto", "torch", "fla"):
        raise ValueError(f"未知线性注意力实现：{backend}")
    chunk = _torch_function(reference.torch_chunk_gated_delta_rule)
    recurrent = _torch_function(reference.torch_recurrent_gated_delta_rule)
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

    conv_update = getattr(reference, "torch_causal_conv1d_update", None)
    forward = _instance_forward(reference, chunk, recurrent) if conv_update is None else None
    for layer in model.model.language_model.layers:
        attention = getattr(layer, "linear_attn", None)
        if attention is None:
            continue
        # Transformers 的可选 FLA 导入还会替换 norm；这里固定原 norm，避免环境改变模型语义。
        attention.norm = reference.Qwen3_5RMSNormGated(attention.head_v_dim, eps=attention.layer_norm_epsilon)
        if forward is None:
            attention.causal_conv1d_fn = None
            attention.causal_conv1d_update = conv_update
            attention.chunk_gated_delta_rule = chunk
            attention.recurrent_gated_delta_rule = recurrent
        else:
            attention.forward = MethodType(forward, attention)
    return selected
