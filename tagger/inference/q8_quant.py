"""GGML Q8_0：每 32 个权重以 fp16 scale 和 32 个 int8 存储。"""
from __future__ import annotations

import numpy as np

BLOCK_SIZE = 32
BLOCK_BYTES = 34


def quantize_q8_0(values: np.ndarray) -> bytes:
    """按 ggml 参考实现的舍入规则生成小端 block_q8_0 字节。"""
    blocks = np.asarray(values, dtype=np.float32).reshape(-1, BLOCK_SIZE)
    scale = np.abs(blocks).max(axis=1, keepdims=True) / 127
    inverse = np.divide(1, scale, out=np.zeros_like(scale), where=scale != 0)
    scaled = blocks * inverse
    codes = np.copysign(np.floor(np.abs(scaled) + 0.5), scaled).astype(np.int8)
    output = np.empty((len(blocks), BLOCK_BYTES), dtype=np.uint8)
    output[:, :2] = scale.astype('<f2').view(np.uint8)
    output[:, 2:] = codes.view(np.uint8)
    return output.tobytes()
