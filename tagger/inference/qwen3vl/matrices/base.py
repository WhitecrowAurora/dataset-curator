"""Tile quantized weights so compatibility execution needs no full FP16 copy."""
from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F


class TiledMatrix(nn.Module):
    def __init__(self, rows: int, width: int, tile_rows: int = 256):
        super().__init__()
        self.out_features, self.in_features, self.tile_rows = rows, width, tile_rows

    def dequantize_rows(self, ids: torch.Tensor, dtype: torch.dtype) -> torch.Tensor:
        raise NotImplementedError

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        flat = x.reshape(-1, self.in_features)
        output = x.new_empty(flat.shape[0], self.out_features)
        for start in range(0, self.out_features, self.tile_rows):
            end = min(start + self.tile_rows, self.out_features)
            ids = torch.arange(start, end, device=x.device)
            weight = self.dequantize_rows(ids, x.dtype)
            output[:, start:end] = F.linear(flat, weight)
        return output.reshape(*x.shape[:-1], self.out_features)

    def rows(self, ids: torch.Tensor, dtype: torch.dtype) -> torch.Tensor:
        # Large prefills often contain hundreds of identical image tokens.
        unique, inverse = ids.reshape(-1).unique(return_inverse=True)
        rows = self.dequantize_rows(unique, dtype)
        return rows[inverse].reshape(*ids.shape, self.in_features)

