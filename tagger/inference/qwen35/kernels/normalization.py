"""Fuse normalization's pointwise tail, retaining PyTorch's reduction order."""
from __future__ import annotations

import torch
import triton
import triton.language as tl
from triton.language.extra.cuda.libdevice import exp


@triton.jit
def _tail(X, W, GATE, VARIANCE, Y, WIDTH: tl.constexpr, EPS: tl.constexpr,
          GATED: tl.constexpr, BLOCK: tl.constexpr):
    row = tl.program_id(0)
    columns = tl.arange(0, BLOCK)
    offset = row * WIDTH + columns
    value = tl.load(X + offset, columns < WIDTH, 0).to(tl.float32)
    variance = tl.load(VARIANCE + row)
    value = value * tl.rsqrt(variance + EPS)
    weight = tl.load(W + columns, columns < WIDTH, 0).to(tl.float32)
    if GATED:
        # The official gated norm rounds both normalization and weighting to
        # FP16 before multiplying by the FP32 SiLU gate.
        value = value.to(tl.float16).to(tl.float32)
        value = (value * weight).to(tl.float16).to(tl.float32)
        gate = tl.load(GATE + offset, columns < WIDTH, 0).to(tl.float32)
        value = value * tl.div_rn(gate, 1.0 + exp(-gate))
    else:
        # Qwen3.5 uses (1 + weight), applied in FP32 before the final cast.
        value = value * (1.0 + weight)
    tl.store(Y + offset, value, columns < WIDTH)


def normalize(value, weight, eps, gate=None):
    # Replacing this reduction with a Triton tree changes some FP16 outputs.
    variance = value.float().pow(2).mean(-1, keepdim=True)
    value, weight = value.contiguous(), weight.contiguous()
    gate = gate.contiguous() if gate is not None else None
    output = torch.empty_like(value)
    width = value.shape[-1]
    with torch.cuda.device(value.device):
        _tail[(value.numel() // width,)](
            value, weight, gate, variance, output, width, eps, gate is not None,
            triton.next_power_of_2(width), num_warps=4, enable_fp_fusion=False)
    return output
