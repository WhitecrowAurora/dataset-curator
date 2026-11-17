"""FP8/INT8 weight storage with explicit per-tensor/channel/group/block scales."""
from __future__ import annotations

import torch
from torch import nn

from .base import TiledMatrix


class ScaleFactors(nn.Module):
    def __init__(self, shape: tuple[int, int], scale: torch.Tensor, *,
                 group_size: int = 0, block_shape: tuple[int, int] | None = None,
                 zero_point: torch.Tensor | None = None, group_index: torch.Tensor | None = None):
        super().__init__()
        self.out_features, self.in_features = shape
        self.register_buffer("scale", scale.float().reshape(1, 1) if scale.numel() == 1 else scale.float())
        self.register_buffer("zero_point", zero_point.float() if zero_point is not None else None)
        self.register_buffer("group_index", group_index)
        self.group_size, self.block_shape = group_size, block_shape
        if block_shape:
            expected = ((self.out_features + block_shape[0] - 1) // block_shape[0],
                        (self.in_features + block_shape[1] - 1) // block_shape[1])
        else:
            expected = (self.out_features, (self.in_features + group_size - 1) // group_size if group_size else 1)
        if self.scale.numel() != 1 and tuple(self.scale.shape) != expected:
            raise ValueError(f"Scale shape {tuple(self.scale.shape)} does not match {expected}")
        if zero_point is not None and zero_point.shape != self.scale.shape:
            raise ValueError("Zero point and scale must use the same grid")
        if group_index is not None and (group_index.numel() != self.in_features
                                        or int(group_index.min()) < 0 or int(group_index.max()) >= self.scale.shape[1]):
            raise ValueError("Invalid activation-order group indices")

    def forward(self, ids):
        if self.scale.numel() == 1:
            return self.scale, self.zero_point if self.zero_point is not None else 0
        cols = torch.arange(self.in_features, device=ids.device)
        if self.block_shape:
            rows, cols = ids // self.block_shape[0], cols // self.block_shape[1]
        else:
            rows = ids
            cols = self.group_index if self.group_index is not None else (cols // self.group_size if self.group_size else torch.zeros_like(cols))
        scale = self.scale[rows[:, None], cols[None, :]]
        zero = self.zero_point[rows[:, None], cols[None, :]] if self.zero_point is not None else 0
        return scale, zero


class ScaledMatrix(TiledMatrix):
    def __init__(self, weight: torch.Tensor, scale: torch.Tensor, *,
                 group_size: int = 0, block_shape: tuple[int, int] | None = None,
                 zero_point: torch.Tensor | None = None, group_index: torch.Tensor | None = None,
                 optimized: bool = False):
        super().__init__(*weight.shape)
        self.register_buffer("weight", weight)
        self.factors = ScaleFactors(tuple(weight.shape), scale, group_size=group_size,
                                     block_shape=block_shape, zero_point=zero_point, group_index=group_index)
        self.optimized = optimized

    def dequantize_rows(self, ids, dtype):
        scale, zero = self.factors(ids)
        return ((self.weight.index_select(0, ids).float() - zero) * scale).to(dtype)

    def forward(self, x):
        minimum = (8, 9) if self.weight.dtype == torch.float8_e4m3fn else (8, 0)
        if (self.optimized and x.is_cuda and x.dtype == torch.float16
                and torch.cuda.get_device_capability(x.device) >= minimum):
            from ..kernels.scaled import linear

            return linear(x, self.weight, self.factors)
        return super().forward(x)

