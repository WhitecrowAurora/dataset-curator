"""Fused quantized decode and bounded prefill scratch on Volta and newer."""
from __future__ import annotations

import ctypes as C

import torch

from .runtime import get_runtime

KINDS = {"Q4_0": 0, "Q4_1": 1, "Q5_0": 2, "Q8_0": 3, "Q4_K": 4, "Q5_K": 5, "Q6_K": 6}


def _dequantize(runtime, function, stream, packed, output):
    rows, width = output.shape
    runtime.call(function, ((width + 255) // 256, min(rows, 65535), (rows + 65534) // 65535), 256, stream,
                 [C.c_void_p(packed.data_ptr()), C.c_void_p(output.data_ptr()),
                  C.c_int(rows), C.c_int(width), C.c_int(packed.stride(0))])


def dequantize(packed, width, kind):
    runtime = get_runtime()
    if runtime is None:
        return None
    rows = packed.shape[0]
    output = torch.empty(rows, width, device=packed.device, dtype=torch.float16)
    if rows:
        with torch.cuda.device(packed.device):
            stream = torch.cuda.current_stream(packed.device).cuda_stream
            functions = runtime.functions(packed.device, KINDS[kind])
            _dequantize(runtime, functions["dequantize"], stream, packed, output)
    return output


def linear(x, packed, rows, kind, tile_rows=256):
    """Small decode batches avoid both a dense copy and per-row-tile launches."""
    runtime = get_runtime()
    if runtime is None:
        return None
    width = x.shape[-1]
    flat = x.reshape(-1, width).contiguous()
    output = torch.empty(flat.shape[0], rows, device=x.device, dtype=x.dtype)
    if flat.shape[0] and rows:
        with torch.cuda.device(x.device):
            stream = torch.cuda.current_stream(x.device).cuda_stream
            functions = runtime.functions(x.device, KINDS[kind])
            if flat.shape[0] <= 8:
                runtime.call(functions["gemv"], ((rows + 3) // 4, flat.shape[0], 1), 128, stream,
                             [C.c_void_p(flat.data_ptr()), C.c_void_p(packed.data_ptr()),
                              C.c_void_p(output.data_ptr()), C.c_int(rows), C.c_int(width),
                              C.c_int(packed.stride(0))])
            else:
                scratch = torch.empty(min(tile_rows, rows), width, device=x.device, dtype=x.dtype)
                for start in range(0, rows, tile_rows):
                    end = min(start + tile_rows, rows)
                    weight = scratch[:end - start]
                    _dequantize(runtime, functions["dequantize"], stream, packed[start:end], weight)
                    output[:, start:end] = torch.nn.functional.linear(flat, weight)
    return output.reshape(*x.shape[:-1], rows)
