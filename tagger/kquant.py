"""K-quant 量化的纯 numpy 实现（Q4_K / Q6_K）。

为什么自己写：llama-quantize 是 llama.cpp 的 C++ 可执行文件，用户得先装一个
200MB 的 runtime 才能压模型。而 K-quant 本身只是"每 256 个权重一组、算 scale
和 min、把量化值塞进固定字节布局"，我们手上就有 numpy，没必要为此引入外部程序。

布局按 ggml 的 block 结构（**一字不能差，差了就加载不了**）：

    block_q4_K  144 字节 / 256 权重
        fp16 d, fp16 dmin, uint8 scales[12], uint8 qs[128]
    block_q6_K  210 字节 / 256 权重
        uint8 ql[128], uint8 qh[64], int8 scales[16], fp16 d

注意这里实现的是**参考实现（ref）的近似**：ggml 的 make_qkx2_quants 会为每组
权重迭代搜最优 scale，我们只做一次 min-max 划分。代价是同样 bit 数下误差略大，
换来的是 numpy 向量化后能跑得动（8B 模型几十秒级，而不是几小时）。每个函数
都配了 dequantize，产物质量靠"量化→反量化"的 RMS 误差自检来兜。
"""

from __future__ import annotations

import numpy as np

QK_K = 256          # 一个 super-block 的权重数，Q4_K/Q6_K 都是这个粒度
QK_K_SCALE = 32     # Q4_K 里一个 sub-block 的权重数
Q6K_BLOCK_BYTES = 210
Q4K_BLOCK_BYTES = 144


# ---------------------------------------------------------------- 位打包工具

def _f16(values: np.ndarray) -> np.ndarray:
    """转成 fp16 的字节视图（GGUF 里的 d / dmin 都是 fp16）。"""
    return np.asarray(values, dtype=np.float16).reshape(-1)


def _pack_q6k(codes: np.ndarray, scales: np.ndarray, d: np.ndarray) -> bytes:
    """把 (nb, 256) 的 6-bit 码字（0..63）拼成 block_q6_K 字节流。

    布局（与 dequantize_row_q6_K 对偶）：每 128 权重一组，
      ql[0:32]  低 4 位 → 权重 0..31   的高/低半字节分别是 0..31 与 64..95
      ql[32:64] 低 4 位 → 权重 32..63  同上，32..63 与 96..127
      qh 每字节 4 个 2-bit 高位，分别属于上述四段
    """
    nb = codes.shape[0]
    out = np.empty(nb * Q6K_BLOCK_BYTES, dtype=np.uint8)
    view = out.reshape(nb, Q6K_BLOCK_BYTES)

    for half in range(2):                      # 128 权重一组
        base = half * 128
        seg = codes[:, base:base + 128]        # (nb, 128)
        low = seg & 0x0F                       # (nb,128) uint8
        high = (seg >> 4) & 0x03               # (nb,128)

        # ql 占 0..127（每半 64 字节），qh 占 128..191（每半 32 字节）
        ql = view[:, half * 64:half * 64 + 64]
        qh = view[:, 128 + half * 32:128 + half * 32 + 32]

        # 四段各 32 个值：0..31 → ql[0:32] 低半字节，32..63 → ql[32:64] 低半字节，
        # 64..95 → ql[0:32] 高半字节，96..127 → ql[32:64] 高半字节
        a = low[:, 0:32]
        b = low[:, 32:64]
        c = low[:, 64:96]
        e = low[:, 96:128]
        ql[:, 0:32] = a | (c << 4)
        ql[:, 32:64] = b | (e << 4)

        h0 = high[:, 0:32]
        h1 = high[:, 32:64]
        h2 = high[:, 64:96]
        h3 = high[:, 96:128]
        qh[:, :] = h0 | (h1 << 2) | (h2 << 4) | (h3 << 6)

    # 尾部 16 个 int8 scale（192..207）+ fp16 d（208..209）
    view[:, 192:208] = np.ascontiguousarray(scales.astype(np.int8)).view(np.uint8)
    view[:, 208:210] = _f16(d).view(np.uint8).reshape(nb, 2)
    return out.tobytes()


def _pack_q4k(codes: np.ndarray, scales6: np.ndarray, mins6: np.ndarray,
              d: np.ndarray, dmin: np.ndarray) -> bytes:
    """把 (nb, 256) 的 4-bit 码字与 6-bit 的 (scale, min) 拼成 block_q4_K。

    scales[12] 的排布（与 get_scale_min_k4 对偶）：
      [0:4]  低 6 位 = scale[0:4]，高 2 位 = scale[4:8] 的 bit4-5
      [4:8]  低 6 位 = min[0:4]，  高 2 位 = min[4:8]   的 bit4-5
      [8:12] 低 4 位 = scale[4:8] 的 bit0-3，高 4 位 = min[4:8] 的 bit0-3
    """
    nb = codes.shape[0]
    out = np.empty(nb * Q4K_BLOCK_BYTES, dtype=np.uint8)
    view = out.reshape(nb, Q4K_BLOCK_BYTES)

    sc = view[:, 4:16]
    for j in range(4):
        sc[:, j] = (scales6[:, j] & 0x3F) | ((scales6[:, j + 4] >> 4) << 6)
        sc[:, j + 4] = (mins6[:, j] & 0x3F) | ((mins6[:, j + 4] >> 4) << 6)
        sc[:, j + 8] = (scales6[:, j + 4] & 0x0F) | ((mins6[:, j + 4] & 0x0F) << 4)

    qs = view[:, 16:144]
    for j in range(4):
        seg = codes[:, j * 64:(j + 1) * 64]     # 64 个权重
        qs[:, j * 32:j * 32 + 32] = (seg[:, :32] & 0x0F) | ((seg[:, 32:] & 0x0F) << 4)

    view[:, 0:2] = _f16(d).view(np.uint8).reshape(nb, 2)
    view[:, 2:4] = _f16(dmin).view(np.uint8).reshape(nb, 2)
    return out.tobytes()


# ---------------------------------------------------------------- 量化

def quantize_q6k(x: np.ndarray) -> tuple[np.ndarray, bytes]:
    """(n_weights,) float32 → (fp16 d, block_q6_K 字节流)。

    **d 的约定是 ggml 的**：`d = max_scale / 127`，`scales` 字段存 0..127 的
    整数比例，解包时直接 `d * sc * q`，不再有额外的归一化除数。这一点不能
    自创——第一版把 127 乘进 scales、让 d 保持原尺度，自己读写是自洽的，
    但拿去解官方的 Q6_K 文件，整体小 127 倍，RMS 直接 0.99。
    """
    nb = x.size // QK_K
    blocks = x.reshape(nb, QK_K).astype(np.float32)

    groups = blocks.reshape(nb, QK_K // 16, 16)
    amax = np.abs(groups).max(axis=2)                     # (nb,16)
    scale = amax / 32.0                                   # 每组理想步长
    d = np.maximum(scale.max(axis=1) / 127.0, 0.0)        # 块基准（fp16）

    sc = np.where(d[:, None] > 0,
                  np.rint(scale / np.maximum(d[:, None], 1e-12)), 0.0)
    sc = sc.clip(-127, 127)

    step = d[:, None, None] * sc[:, :, None]
    usable = step > 0
    q = np.where(usable, groups / np.where(usable, step, 1.0), 0.0)
    codes = (np.rint(q).clip(-32, 31).astype(np.int8) + 32).astype(np.uint8).reshape(nb, QK_K)

    packed = _pack_q6k(codes, sc.astype(np.int8), d)
    return d.astype(np.float16), packed


def quantize_q4k(x: np.ndarray) -> tuple[np.ndarray, bytes]:
    """(n_weights,) float32 → (fp16 d, fp16 dmin, block_q4_K 字节流)。

    每 32 个权重一组求 (min, max)，各自压到 0..15；再把这 8 组的 scale/min
    统一归一，好让它们能塞进 6 bit。
    """
    nb = x.size // QK_K
    blocks = x.reshape(nb, QK_K).astype(np.float32)
    groups = blocks.reshape(nb, QK_K // QK_K_SCALE, QK_K_SCALE)

    lo = groups.min(axis=2)                                # (nb,8)
    hi = groups.max(axis=2)
    span = np.maximum(hi - lo, 1e-8)

    # d / dmin 是 ggml 的约定：都已除过 63，scales 字段直接存 0..63 的整数比例
    ideal = span / 15.0
    d = np.maximum(ideal.max(axis=1), 0.0) / 63.0
    scale = np.where(d[:, None] > 0,
                     np.rint(ideal / np.maximum(d[:, None], 1e-12)), 0.0).clip(0, 63)
    step = d[:, None] * scale

    # 解包是 `x = step*q - off`，所以量化必须是 `q = (x + off)/step`。
    # off 取该组负半轴深度（全正数据为 0），与 ggml 的 -min 同义。
    off = np.maximum(-lo, 0.0)
    dmin = np.maximum(off.max(axis=1), 0.0) / 63.0
    mins6 = np.where(dmin[:, None] > 0,
                     np.rint(off / np.maximum(dmin[:, None], 1e-12)), 0.0).clip(0, 63)
    off = mins6 * dmin[:, None]

    # span 为 0 的组（常量块）scale=0，除法无意义：直接给 0 码，解包回 lo
    usable = (step > 0)[:, :, None]
    q = np.where(usable, (groups + off[:, :, None]) / np.where(step > 0, step, 1.0)[:, :, None], 0.0)
    codes = np.rint(q).clip(0, 15).astype(np.uint8).reshape(nb, QK_K)

    packed = _pack_q4k(codes, scale.astype(np.uint8), mins6.astype(np.uint8),
                       d.astype(np.float16), dmin.astype(np.float16))
    return d.astype(np.float16), dmin.astype(np.float16), packed


# ---------------------------------------------------------------- 反量化（自检用）

def dequantize_q6k(d: np.ndarray, packed: bytes, n: int) -> np.ndarray:
    nb = n // QK_K
    buf = np.frombuffer(packed, dtype=np.uint8).reshape(nb, Q6K_BLOCK_BYTES)
    out = np.empty((nb, QK_K), dtype=np.float32)

    for half in range(2):
        ql = buf[:, half * 64:half * 64 + 64]
        qh = buf[:, 128 + half * 32:128 + half * 32 + 32]
        low = np.empty((nb, 128), dtype=np.uint8)
        low[:, 0:32] = ql[:, 0:32] & 0x0F
        low[:, 32:64] = ql[:, 32:64] & 0x0F
        low[:, 64:96] = ql[:, 0:32] >> 4
        low[:, 96:128] = ql[:, 32:64] >> 4

        high = np.empty((nb, 128), dtype=np.uint8)
        high[:, 0:32] = qh & 0x03
        high[:, 32:64] = (qh >> 2) & 0x03
        high[:, 64:96] = (qh >> 4) & 0x03
        high[:, 96:128] = (qh >> 6) & 0x03

        codes = (low | (high << 4)).astype(np.int16) - 32
        out[:, half * 128:(half + 1) * 128] = codes

    scales = buf[:, 192:208].view(np.int8).astype(np.float32)  # (nb,16)
    dd = d.astype(np.float32)[:, None]
    per16 = out.reshape(nb, 16, 16) * (dd[:, :, None] * scales[:, :, None])
    return per16.reshape(-1)


def dequantize_q4k(d: np.ndarray, dmin: np.ndarray, packed: bytes, n: int) -> np.ndarray:
    nb = n // QK_K
    buf = np.frombuffer(packed, dtype=np.uint8).reshape(nb, Q4K_BLOCK_BYTES)
    sc = buf[:, 4:16].astype(np.int32)

    # get_scale_min_k4 的向量化版本
    s6 = np.empty((nb, 8), dtype=np.int32)
    m6 = np.empty((nb, 8), dtype=np.int32)
    for j in range(4):
        s6[:, j] = sc[:, j] & 0x3F
        m6[:, j] = sc[:, j + 4] & 0x3F
        s6[:, j + 4] = (sc[:, j + 8] & 0x0F) | ((sc[:, j] >> 6) << 4)
        m6[:, j + 4] = (sc[:, j + 8] >> 4) | ((sc[:, j + 4] >> 6) << 4)

    qs = buf[:, 16:144]
    d32 = d.astype(np.float32)[:, None]
    dm32 = dmin.astype(np.float32)[:, None]
    out = np.empty((nb, QK_K), dtype=np.float32)
    for j in range(4):
        part = qs[:, j * 32:j * 32 + 32]
        lo = (part & 0x0F).astype(np.float32)
        hi = (part >> 4).astype(np.float32)
        out[:, j * 64:j * 64 + 32] = d32 * s6[:, j * 2][:, None] * lo \
            - dm32 * m6[:, j * 2][:, None]
        out[:, j * 64 + 32:j * 64 + 64] = d32 * s6[:, j * 2 + 1][:, None] * hi \
            - dm32 * m6[:, j * 2 + 1][:, None]
    return out.reshape(-1)


def rms_error(a: np.ndarray, b: np.ndarray) -> float:
    """相对 RMS 误差，量化质量的自检指标。"""
    denom = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
    if denom <= 0:
        return 0.0
    return float(np.sqrt(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))) / denom
