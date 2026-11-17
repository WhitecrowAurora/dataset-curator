"""Torch decoding of common GGUF blocks on CPU and CUDA, including Volta."""
from __future__ import annotations

import torch

from .base import TiledMatrix

BLOCKS = {"Q4_0": (32, 18), "Q4_1": (32, 20), "Q5_0": (32, 22), "Q8_0": (32, 34),
          "Q4_K": (256, 144), "Q5_K": (256, 176), "Q6_K": (256, 210)}


def _half(blocks, offset):
    return blocks[:, offset:offset + 2].contiguous().view(torch.float16).float()


def unpack_blocks(packed: torch.Tensor, kind: str) -> torch.Tensor:
    count, size = BLOCKS[kind]
    blocks = packed.reshape(-1, size)
    index = torch.arange(count, device=packed.device)
    if kind == "Q6_K":
        half, group, lane = index // 128, (index % 128) // 32, index % 32
        low = blocks[:, half * 64 + group.remainder(2) * 32 + lane].int()
        high = blocks[:, 128 + half * 32 + lane].int()
        q = ((low >> (group // 2 * 4)) & 15) | (((high >> (group * 2)) & 3) << 4)
        scales = blocks[:, 192:208].contiguous().view(torch.int8).float()
        return (_half(blocks, 208) * scales[:, index // 16]) * (q - 32).float()
    if kind == "Q8_0":
        return _half(blocks, 0) * blocks[:, 2:].contiguous().view(torch.int8).float()
    if kind in ("Q4_0", "Q4_1", "Q5_0"):
        start, bias = {"Q4_0": (2, 8), "Q4_1": (4, 0), "Q5_0": (6, 16)}[kind]
        low = blocks[:, start + index % 16].int()
        q = (low >> (index // 16 * 4)) & 15
        if kind == "Q5_0":
            high = blocks[:, 2 + index // 8].int()
            q |= ((high >> (index % 8)) & 1) << 4
        value = _half(blocks, 0) * (q - bias).float()
        return value + _half(blocks, 2) if kind == "Q4_1" else value
    group, lane = index // 32, index % 32
    scale_data = blocks[:, 4:16].int()
    scale_group = torch.arange(8, device=packed.device)
    # Four low groups use six-bit fields, the high four combine two fragments.
    scales = torch.where(scale_group[None, :] < 4, scale_data[:, scale_group % 4] & 63,
                         (scale_data[:, 8 + scale_group % 4] & 15) | ((scale_data[:, scale_group % 4] >> 2) & 48))
    minima = torch.where(scale_group[None, :] < 4, scale_data[:, 4 + scale_group % 4] & 63,
                         (scale_data[:, 8 + scale_group % 4] >> 4) | ((scale_data[:, 4 + scale_group % 4] >> 2) & 48))
    start = 16 if kind == "Q4_K" else 48
    low = blocks[:, start + group // 2 * 32 + lane].int()
    q = (low >> (group % 2 * 4)) & 15
    if kind == "Q5_K":
        q |= ((blocks[:, 16 + lane].int() >> group) & 1) << 4
    return (_half(blocks, 0) * scales[:, group].float()) * q.float() - (_half(blocks, 2) * minima[:, group].float())


class GGUFMatrix(TiledMatrix):
    def __init__(self, packed: torch.Tensor, rows: int, width: int, kind: str, *,
                 tile_rows: int = 256, optimized: bool = False):
        super().__init__(rows, width, tile_rows)
        count, size = BLOCKS[kind]
        if width % count:
            raise ValueError(f"{kind} rows must contain complete {count}-value blocks")
        self.kind, self.optimized = kind, optimized
        self.kernel_kind = kind
        self.register_buffer("packed", packed.reshape(rows, width // count * size))

    def dequantize_rows(self, ids, dtype):
        if self.kernel_kind in ("Q4_K_REPACKED", "Q5_K_REPACKED"):
            blocks = self.packed[ids // 32, :, :, ids % 32].contiguous().view(torch.uint8)
            return unpack_blocks(blocks, self.kind).reshape(ids.numel(), self.in_features).to(dtype)
        if self.kernel_kind == "Q8_0_REPACKED":
            words = self.packed[ids // 32, :, :, ids % 32].contiguous()
            scales = words[:, :, :4].contiguous().view(torch.uint8).reshape(ids.numel(), -1, 8, 2)
            codes = words[:, :, 4:].contiguous().view(torch.uint8).reshape(ids.numel(), -1, 8, 32)
            blocks = torch.cat((scales, codes), dim=-1)
            return unpack_blocks(blocks, self.kind).reshape(ids.numel(), self.in_features).to(dtype)
        if self.kernel_kind == "Q6_K_REPACKED":
            blocks = self.packed[ids // 32, :, :, ids % 32]
            index = torch.arange(256, device=ids.device)
            group, bit = index // 16, index % 16 * 6
            word = group * 3 + bit // 32
            words = blocks.long() & 0xffffffff
            codes = words[:, :, word] >> (bit % 32)
            codes |= torch.where(bit % 32 > 26, words[:, :, word + 1] << (32 - bit % 32), 0)
            scale = (words[:, :, 48 + group // 4] >> (group % 4 * 8)).to(torch.int8).float()
            d = blocks[:, :, 52].to(torch.int16).contiguous().view(torch.float16).float()
            return ((d[:, :, None] * scale) * ((codes & 63) - 32).float()).reshape(ids.numel(), self.in_features).to(dtype)
        selected = self.packed.index_select(0, ids)
        if selected.is_cuda and dtype == torch.float16:
            from ..kernels.cuda.gguf import dequantize

            output = dequantize(selected, self.in_features, self.kind)
            if output is not None:
                return output
        return unpack_blocks(selected, self.kind).reshape(ids.numel(), self.in_features).to(dtype)

    def repack(self, *, q4=False, kinds=None):
        selected = {"Q6_K", "Q4_K"} if q4 else {"Q6_K"}
        if kinds is not None:
            selected = kinds
        if self.kernel_kind != self.kind or self.kind not in selected or self.in_features % 256:
            return self
        from importlib import import_module

        layout = {"Q4_K": "q4_layout", "Q5_K": "q5_layout", "Q6_K": "q6_layout", "Q8_0": "q8_layout"}
        module = layout.get(self.kind)
        if module is not None:
            repack = import_module("..kernels." + module, __package__).repack
            self.packed = repack(self.packed, self.out_features, self.in_features)
            self.kernel_kind = self.kind + "_REPACKED"
        return self

    def rows(self, ids, dtype):
        if (self.optimized and ids.is_cuda and dtype == torch.float16
                and ids.numel() <= 32 and torch.cuda.get_device_capability(ids.device) >= (8, 0)):
            from ..kernels.gguf import embedding

            return embedding(ids, self.packed, self.out_features, self.in_features, self.kernel_kind)
        return super().rows(ids, dtype)

    def forward(self, x):
        if (self.optimized and x.is_cuda and x.dtype == torch.float16
                and torch.cuda.get_device_capability(x.device) >= (8, 0)):
            from ..kernels.gguf import linear

            return linear(x, self.packed, self.out_features, self.kernel_kind)
        if x.is_cuda and x.dtype == torch.float16 and self.kernel_kind == self.kind:
            from ..kernels.cuda.gguf import linear

            output = linear(x, self.packed, self.out_features, self.kind, self.tile_rows)
            if output is not None:
                return output
        return super().forward(x)

