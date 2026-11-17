"""Per-channel symmetric weight quantization; no calibration or file conversion."""
from __future__ import annotations

import torch

from ..matrices.native import NativeMatrix
from .activations import DTYPES, ActivationQuantization, encode, symmetric_scale


@torch.no_grad()
def quantize_matrix(weight, precision: str, *, optimized=False):
    if precision not in ("fp8", "int8"):
        raise ValueError("native_precision must be 'fp8' or 'int8'")
    dtype = DTYPES[precision]
    quantized = torch.empty_like(weight, dtype=dtype)
    scales = torch.empty(weight.shape[0], 1, dtype=torch.float32, device=weight.device)
    # Limit conversion workspace instead of keeping a full FP32 model copy.
    for start in range(0, weight.shape[0], 256):
        value = weight[start:start + 256].float()
        scale = symmetric_scale(value.abs().amax(-1, keepdim=True), dtype)
        quantized[start:start + 256] = encode(value, scale, dtype)
        scales[start:start + 256] = scale
    return NativeMatrix(quantized, scales, activation=ActivationQuantization(dtype), optimized=optimized)
