"""Install fused FP16 normalization only on this Torii language model."""
from __future__ import annotations

from types import MethodType

import torch


def _rms_forward(self, x):
    from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5RMSNorm

    if not x.is_cuda or x.dtype != torch.float16 or self.weight.dtype != torch.float16:
        return Qwen3_5RMSNorm.forward(self, x)
    from .kernels.normalization import normalize

    return normalize(x, self.weight, self.eps)


def _gated_forward(self, hidden_states, gate=None):
    from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5RMSNormGated

    if (not hidden_states.is_cuda or hidden_states.dtype != torch.float16 or self.weight.dtype != torch.float16
            or gate is None or gate.dtype != torch.float16):
        return Qwen3_5RMSNormGated.forward(self, hidden_states, gate)
    from .kernels.normalization import normalize

    return normalize(hidden_states, self.weight, self.variance_epsilon, gate)


def configure(model):
    from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5RMSNorm, Qwen3_5RMSNormGated

    for module in model.model.language_model.modules():
        if isinstance(module, Qwen3_5RMSNorm):
            module.forward = MethodType(_rms_forward, module)
        elif isinstance(module, Qwen3_5RMSNormGated):
            module.forward = MethodType(_gated_forward, module)
