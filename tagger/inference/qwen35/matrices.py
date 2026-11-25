"""保留 GGUF 量化块，在投影边界恢复 Qwen3.5 的 head 顺序。"""
from __future__ import annotations

import torch
from torch import nn

from ..qwen3vl.matrices.gguf import GGUFMatrix


class DecodeMatrix(GGUFMatrix):
    """Keep large decode batches in packed MMA; prefill retains cuBLAS."""

    def forward(self, value):
        decode = value.ndim == 2 or (value.ndim == 3 and value.shape[-2] == 1)
        if decode and self.optimized and value.is_cuda and value.dtype == torch.float16:
            if value.numel() == self.in_features and self.kernel_kind == "Q6_K_REPACKED":
                from ..qwen3vl.kernels.q6_vector import vector
                output = value.new_empty(*value.shape[:-1], self.out_features)
                with torch.cuda.device(value.device):
                    vector(value.contiguous(), self.packed, output, self.out_features, self.in_features,
                           coalesced=True)
                return output
            from ..qwen3vl.kernels.gguf import linear
            return linear(value, self.packed, self.out_features, self.kernel_kind, allow_dense=False)
        return super().forward(value)


def inverse_heads(value, dim, key_heads, value_heads, head_dim):
    shape = list(value.shape)
    value = value.reshape(*shape[:dim], value_heads // key_heads, key_heads, head_dim, *shape[dim + 1:])
    return value.transpose(dim, dim + 1).reshape(shape)


class HeadProjection(nn.Module):
    def __init__(self, matrix, order, *, input_order=False):
        super().__init__()
        self.matrix = matrix
        self.register_buffer("order", order)
        self.input_order = input_order

    def forward(self, value):
        if self.input_order:
            return self.matrix(value.index_select(-1, self.order))
        return self.matrix(value).index_select(-1, self.order)


class QuantizedEmbedding(nn.Module):
    def __init__(self, matrix, dtype):
        super().__init__()
        self.matrix, self.dtype = matrix, dtype

    def forward(self, ids):
        if (ids.numel() <= 32 or ids.shape[-1] == 1) and isinstance(self.matrix, GGUFMatrix):
            # Decode has only a few rows. Avoid unique(), whose dynamic output
            # synchronizes the GPU and cannot be captured in a CUDA graph.
            if self.matrix.optimized and ids.is_cuda and self.dtype == torch.float16:
                from ..qwen3vl.kernels.gguf import embedding
                return embedding(ids, self.matrix.packed, self.matrix.out_features,
                                 self.matrix.in_features, self.matrix.kernel_kind)
            rows = self.matrix.dequantize_rows(ids.reshape(-1), self.dtype)
            return rows.reshape(*ids.shape, self.matrix.in_features)
        return self.matrix.rows(ids, self.dtype)


def load_matrix(weights, name, *, device, dtype):
    from .memory import can_use_triton
    optimized = can_use_triton(device)
    matrix = weights.matrix(name, device=device, dtype=dtype, optimized=optimized)
    if optimized and isinstance(matrix, GGUFMatrix):
        matrix = DecodeMatrix(matrix.packed, matrix.out_features, matrix.in_features,
                              matrix.kind, optimized=True)
        matrix.repack(kinds={"Q4_K", "Q5_K", "Q6_K", "Q8_0"})
    return matrix
