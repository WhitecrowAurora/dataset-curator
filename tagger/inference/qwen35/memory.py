"""在支持量化算子的 GPU 上保留 GGUF 权重，兼容旧设备。"""
from __future__ import annotations

import importlib.util

import torch


def can_use_triton(device):
    device = torch.device(device)
    if device.type == "cuda" and torch.cuda.get_device_capability(device) >= (8, 0):
        try:
            return importlib.util.find_spec("triton.experimental.gluon") is not None
        except ModuleNotFoundError:
            pass
    return False


def choose_packed(weights, device, dtype, requested="auto"):
    if requested not in ("auto", True, False):
        raise ValueError("矩阵模式需要 auto、True 或 False")
    if requested is False or torch.device(device).type != "cuda":
        return False
    if can_use_triton(device):
        return True
    from ..qwen3vl.kernels.cuda.runtime import get_runtime
    return get_runtime() is not None
