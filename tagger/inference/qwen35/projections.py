"""Reuse grouped GGUF projections and SiLU fusion in Torii's language MLP."""
from __future__ import annotations

from torch import nn


class PackedMLP(nn.Module):
    def __init__(self, source):
        super().__init__()
        self.gate_proj, self.up_proj, self.down_proj = source.gate_proj, source.up_proj, source.down_proj
        self.act_fn = source.act_fn

    def forward(self, value):
        if value.shape[-2] != 1 or not value.is_cuda:
            return self.down_proj(self.act_fn(self.gate_proj(value)) * self.up_proj(value))
        from ..qwen3vl.matrices.projections import project
        from ..qwen3vl.kernels.elementwise import silu_gate
        from ..qwen3vl.matrices.gguf import GGUFMatrix
        if value.shape[0] >= 32 and all(isinstance(matrix, GGUFMatrix) and matrix.optimized
                                        for matrix in (self.gate_proj, self.up_proj)):
            from ..qwen3vl.kernels.gguf import linear_group
            gate, up = linear_group(value, (self.gate_proj, self.up_proj))
        else:
            gate, up = project(value, self.gate_proj, self.up_proj, optimized=True)
        return self.down_proj(silu_gate(gate, up))
