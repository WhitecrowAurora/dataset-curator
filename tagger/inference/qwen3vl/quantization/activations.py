"""Symmetric W8A8 activation policies with a portable reference implementation."""
from __future__ import annotations

from dataclasses import dataclass

import torch

DTYPES = {"int8": torch.int8, "fp8": torch.float8_e4m3fn,
          "fp8_e5m2": torch.float8_e5m2}


def bounds(dtype):
    info = torch.iinfo(dtype) if dtype == torch.int8 else torch.finfo(dtype)
    return info.min, info.max


def symmetric_scale(maximum, dtype):
    lo, hi = bounds(dtype)
    scale = maximum.float() / ((hi - lo) / 2)
    return torch.where(scale == 0, torch.ones_like(scale), scale)


def encode(value, scale, dtype):
    lo, hi = bounds(dtype)
    value = (value.float() / scale).clamp(lo, hi)
    if dtype == torch.int8:
        value = value.round()
    return value.to(dtype)


@dataclass(frozen=True, slots=True)
class ActivationQuantization:
    dtype: torch.dtype
    strategy: str = "token"
    group_size: int = 0

    def __post_init__(self):
        if self.dtype not in DTYPES.values():
            raise ValueError(f"Unsupported W8A8 activation dtype: {self.dtype}")
        if self.strategy not in ("token", "tensor", "group"):
            raise ValueError(f"Unsupported W8A8 activation strategy: {self.strategy}")
        if self.strategy == "group" and self.group_size <= 0:
            raise ValueError("Activation groups require a positive group size")

    def quantize(self, x, static_scale=None):
        flat = x.reshape(-1, x.shape[-1]).float()
        if static_scale is not None:
            scale = static_scale.float().reshape(1, 1)
            return encode(flat, scale, self.dtype), scale
        if self.strategy == "tensor":
            scale = symmetric_scale(flat.abs().amax().reshape(1, 1), self.dtype)
            return encode(flat, scale, self.dtype), scale
        width = self.group_size if self.strategy == "group" else flat.shape[1]
        groups = (flat.shape[1] + width - 1) // width
        # Only pad the reference reduction, never the logical matrix width.
        padded = torch.nn.functional.pad(flat, (0, groups * width - flat.shape[1]))
        scale = symmetric_scale(padded.reshape(flat.shape[0], groups, width).abs().amax(-1), self.dtype)
        expanded = scale.repeat_interleave(width, dim=1)[:, :flat.shape[1]]
        return encode(flat, expanded, self.dtype), scale

    def expand_scale(self, scale, width):
        if scale.numel() == 1:
            return scale
        group = self.group_size if self.strategy == "group" else width
        return scale.repeat_interleave(group, dim=1)[:, :width]


def checkpoint_activation(args, weight_dtype, static_scale=None, zero_point=None):
    """Read supported compressed-tensors A8 schemes without changing their policy."""
    if args.get("type") not in ("int", "float"):
        raise ValueError("Unknown checkpoint activation quantization type")
    kind = "int8" if args["type"] == "int" else "fp8"
    if int(args.get("num_bits", 0)) != 8 or not args.get("symmetric", True):
        raise ValueError("W8A8 currently requires symmetric 8-bit activations")
    dtype = DTYPES[kind]
    if dtype != weight_dtype:
        raise ValueError("W8A8 weight and activation types must match")
    if args.get("scale_dtype") not in (None, "float32", "torch.float32") or args.get("block_structure"):
        raise ValueError("MX and block activation schemes are not implemented")
    strategy = args.get("strategy", "tensor")
    dynamic = args.get("dynamic", False)
    if dynamic not in (True, False, "local"):
        raise ValueError(f"Unsupported activation dynamic mode: {dynamic}")
    if not dynamic:
        if strategy != "tensor" or static_scale is None or static_scale.numel() != 1:
            raise ValueError("Static A8 requires a calibrated per-tensor input_scale")
        static_scale = static_scale.float()
        if not torch.isfinite(static_scale).all() or not (static_scale > 0).all():
            raise ValueError("Static activation scale must be finite and positive")
    else:
        static_scale = None
    if zero_point is not None and torch.count_nonzero(zero_point.float()):
        raise ValueError("Asymmetric activation zero points are not implemented")
    return ActivationQuantization(dtype, strategy, int(args.get("group_size") or 0)), static_scale
