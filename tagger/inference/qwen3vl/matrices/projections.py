"""Group small quantized projections without copying stored weights."""
from __future__ import annotations

import torch

from .gguf import GGUFMatrix


def project(x, *matrices, optimized=False):
    if (optimized and x.is_cuda and x.dtype == torch.float16
            and x.numel() // x.shape[-1] < 32
            and all(isinstance(matrix, GGUFMatrix) and matrix.optimized for matrix in matrices)
            and torch.cuda.get_device_capability(x.device) >= (8, 0)):
        from ..kernels.gguf import linear_group

        return linear_group(x, matrices)
    if optimized and x.is_cuda:
        from .native import can_group

        if can_group(x, matrices):
            from ..kernels.native import linear_group

            return linear_group(x, matrices)
    return tuple(matrix(x) for matrix in matrices)
