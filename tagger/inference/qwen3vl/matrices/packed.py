"""compressed-tensors row-packed W4A16, including asymmetric groups."""
from __future__ import annotations

import torch

from .base import TiledMatrix
from .scaled import ScaleFactors


def unpack_int32(packed: torch.Tensor, width: int, bits: int = 4):
    if bits not in (4, 8):
        raise ValueError("The prototype supports 4-bit and 8-bit int32 packing")
    index = torch.arange(width, device=packed.device)
    return ((packed[:, index // (32 // bits)].to(torch.int64) >> (index % (32 // bits) * bits))
            & ((1 << bits) - 1)).float() - (1 << (bits - 1))


class PackedMatrix(TiledMatrix):
    def __init__(self, packed: torch.Tensor, shape: tuple[int, int], scale: torch.Tensor, *,
                 group_size: int = 128, zero_point: torch.Tensor | None = None,
                 group_index: torch.Tensor | None = None, bits: int = 4, optimized=False):
        super().__init__(*shape)
        if packed.dtype != torch.int32 or tuple(packed.shape) != (shape[0], (shape[1] * bits + 31) // 32):
            raise ValueError("Unsupported compressed-tensors packed weight layout")
        self.bits, self.optimized = bits, optimized
        self.register_buffer("packed", packed)
        self.factors = ScaleFactors(shape, scale, group_size=group_size,
                                    zero_point=zero_point, group_index=group_index)

    def dequantize_rows(self, ids, dtype):
        values = unpack_int32(self.packed.index_select(0, ids), self.in_features, self.bits)
        scale, zero = self.factors(ids)
        return ((values - zero) * scale).to(dtype)

    def forward(self, x):
        if (self.optimized and x.is_cuda and x.dtype == torch.float16
                and torch.cuda.get_device_capability(x.device) >= (8, 0)):
            from ..kernels.scaled import linear

            return linear(x, self.packed, self.factors, bits=self.bits)
        return super().forward(x)
