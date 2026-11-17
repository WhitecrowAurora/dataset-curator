"""W8A8 matrices: native Tensor Core products and an explicit A8 fallback."""
from __future__ import annotations

import torch

from ..quantization.activations import ActivationQuantization
from .base import TiledMatrix
from .scaled import ScaledMatrix


def native_tile(factors, activation):
    """A K tile must never cross either quantization scale boundary."""
    if factors.zero_point is not None or factors.group_index is not None:
        return None
    weight_group = (factors.block_shape[1] if factors.block_shape
                    else factors.group_size if factors.scale.numel() != 1 else 0)
    groups = [g for g in (weight_group, activation.group_size if activation.strategy == "group" else 0) if g]
    tile = min([128, *groups])
    if tile < 32 or tile & (tile - 1) or any(g % tile for g in groups):
        return None
    return tile


class NativeMatrix(ScaledMatrix):
    def __init__(self, weight, scale, *, activation: ActivationQuantization,
                 input_scale=None, **kwargs):
        zero = kwargs.get("zero_point")
        if zero is not None and not torch.count_nonzero(zero.float()):
            kwargs["zero_point"] = None
        super().__init__(weight.contiguous(), scale, **kwargs)
        if weight.dtype != activation.dtype:
            raise ValueError("Native W8A8 requires matching weight/activation dtypes")
        self.activation = activation
        self.factors.scale = self.factors.scale.contiguous()
        self.register_buffer("input_scale", input_scale.float().reshape(1, 1) if input_scale is not None else None)
        self.tile = native_tile(self.factors, activation)

    def forward(self, x):
        minimum = (8, 0) if self.weight.dtype == torch.int8 else (8, 9)
        if (self.optimized and self.tile is not None and x.is_cuda
                and x.dtype in (torch.float16, torch.bfloat16)
                and torch.cuda.get_device_capability(x.device) >= minimum):
            from ..kernels.native import linear

            return linear(x, self.weight, self.factors, self.activation, self.input_scale, self.tile)
        quantized, scale = self.activation.quantize(x, self.input_scale)
        value = quantized.float() * self.activation.expand_scale(scale, self.in_features)
        # Unsupported devices still execute the specified activation quantization.
        value = value.reshape(x.shape).to(x.dtype)
        return TiledMatrix.forward(self, value)


def can_group(x, matrices):
    if (len(matrices) not in (2, 3) or not x.is_cuda
            or x.dtype not in (torch.float16, torch.bfloat16)
            or x.numel() // x.shape[-1] >= 32
            or not all(isinstance(m, NativeMatrix) and m.optimized and m.tile is not None for m in matrices)):
        return False
    first = matrices[0]
    provided = first.input_scale
    for matrix in matrices:
        if (matrix.in_features != x.shape[-1] or matrix.weight.device != x.device
                or matrix.weight.dtype != first.weight.dtype or matrix.activation != first.activation):
            return False
        scale = matrix.input_scale
        if (scale is None) != (provided is None):
            return False
        if scale is not None and (scale.device != x.device or scale.dtype != provided.dtype
                                  or scale.data_ptr() != provided.data_ptr()):
            return False
    minimum = (8, 0) if first.weight.dtype == torch.int8 else (8, 9)
    return torch.cuda.get_device_capability(x.device) >= minimum
