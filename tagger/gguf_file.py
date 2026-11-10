"""GGUF 文件的最小读写实现（只为量化服务）。

只做三件事：读出张量、把张量换成量化后的字节、原样写回。

**元数据整段原样搬运**，不解析再编码。理由：GGUF 的元数据有十几种类型
（字符串、数组、嵌套数组……），Qwen 这类模型还塞了 tokenizer 词表和 chat
template。解析一遍再拼回去，任何一个类型编码写错，llama.cpp 就直接拒绝加载；
而量化根本不需要理解它们，只需要保证字节不变。

张量信息（名字/维度/类型/偏移）要重写，因为类型变了；数据段按 `general.alignment`
重新对齐。
"""

from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

GGUF_MAGIC = b"GGUF"
DEFAULT_ALIGNMENT = 32

# 我们认识的张量类型（其余遇到就跳过不量化）
GGML_TYPE_F32 = 0
GGML_TYPE_F16 = 1
GGML_TYPE_BF16 = 30
GGML_TYPE_Q4_K = 12
GGML_TYPE_Q6_K = 14

# blob 类型 -> 每个 tensor 元素占几个字节；0 表示变长/复杂类型，不在这里处理
_SIMPLE_VALUE_SIZE = {
    0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
    10: 8, 11: 8, 12: 8,
}


class GgufError(ValueError):
    """文件不是合法 GGUF，或者版本/类型不认识。"""


@dataclass
class TensorInfo:
    name: str
    dims: tuple[int, ...]
    type: int
    offset: int          # 相对数据段起点

    @property
    def n_elements(self) -> int:
        n = 1
        for d in self.dims:
            n *= int(d)
        return n


class Reader:
    """顺序读一个 GGUF，同时记下元数据段的字节区间。

    用 mmap 而不是 `fh.read()`：F16 的 8B 模型单个文件就 16 GB，全读进内存
    再开始量化，等于把内存需求翻倍——还什么都没算就 OOM 了。mmap 之后
    切片、`struct.unpack_from`、bytes 比较全都照旧可用。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = open(self.path, "rb")
        try:
            self._buf = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        except (ValueError, OSError):
            self._fh.close()
            raise GgufError(f"{self.path} 读不出来（空文件？）") from None

        if self._buf[:4] != GGUF_MAGIC:
            self.close()
            raise GgufError(f"{self.path} 不是 GGUF 文件（magic 不对）")

        self.version = struct.unpack_from("<I", self._buf, 4)[0]
        if self.version not in (2, 3):
            raise GgufError(f"GGUF 版本 {self.version} 不支持（只处理 v2/v3）")

        self.tensor_count = struct.unpack_from("<Q", self._buf, 8)[0]
        self.kv_count = struct.unpack_from("<Q", self._buf, 16)[0]

        pos = 24
        # 元数据整段留底：从第一个 kv 到 tensor info 之前
        self.metadata_start = pos
        pos = self._skip_kvs(pos)
        self.metadata_end = pos

        self.tensors: list[TensorInfo] = []
        pos = self._read_tensor_infos(pos)

        # alignment 是元数据里的一个键，必须**先**读它再算数据段起点，
        # 否则遇到 alignment=64 的文件会从头错位
        self.alignment = self._read_alignment()
        self.data_start = _align(pos, self.alignment)

    # ------------------------------------------------------------ 解析

    def _skip_kvs(self, pos: int) -> int:
        for _ in range(self.kv_count):
            pos = _skip_string(self._buf, pos)
            vtype = struct.unpack_from("<I", self._buf, pos)[0]
            pos += 4
            pos = _skip_value(self._buf, pos, vtype)
        return pos

    def _read_tensor_infos(self, pos: int) -> int:
        for _ in range(self.tensor_count):
            name, pos = _read_string(self._buf, pos)
            n_dims = struct.unpack_from("<I", self._buf, pos)[0]
            pos += 4
            dims = struct.unpack_from(f"<{n_dims}Q", self._buf, pos)
            pos += 8 * n_dims
            ttype = struct.unpack_from("<I", self._buf, pos)[0]
            pos += 4
            offset = struct.unpack_from("<Q", self._buf, pos)[0]
            pos += 8
            self.tensors.append(TensorInfo(name, tuple(int(d) for d in dims), ttype, offset))
        return pos

    def _read_alignment(self) -> int:
        """从元数据里捞 general.alignment；没有就用 32。"""
        pos = self.metadata_start
        for _ in range(self.kv_count):
            key, pos = _read_string(self._buf, pos)
            vtype = struct.unpack_from("<I", self._buf, pos)[0]
            pos += 4
            if key == "general.alignment" and vtype == 4:       # UINT32
                return int(struct.unpack_from("<I", self._buf, pos)[0])
            pos = _skip_value(self._buf, pos, vtype)
        return DEFAULT_ALIGNMENT

    # ------------------------------------------------------------ 取数据

    def close(self) -> None:
        buf = getattr(self, "_buf", None)
        if buf is not None and not buf.closed:
            buf.close()
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "Reader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def metadata_bytes(self) -> bytes:
        return self._buf[self.metadata_start:self.metadata_end]

    def tensor_bytes(self, info: TensorInfo, size: int) -> bytes:
        start = self.data_start + info.offset
        return self._buf[start:start + size]

    def read_float(self, info: TensorInfo) -> np.ndarray:
        """把一个浮点张量读成 float32 一维数组。"""
        n = info.n_elements
        if info.type == GGML_TYPE_F32:
            raw = self.tensor_bytes(info, n * 4)
            return np.frombuffer(raw, dtype="<f4").astype(np.float32)
        if info.type == GGML_TYPE_F16:
            raw = self.tensor_bytes(info, n * 2)
            return np.frombuffer(raw, dtype="<f2").astype(np.float32)
        if info.type == GGML_TYPE_BF16:
            raw = self.tensor_bytes(info, n * 2)
            u16 = np.frombuffer(raw, dtype="<u2")
            # bf16 就是 f32 的高 16 位
            return (u16.astype(np.uint32) << 16).view(np.float32)
        raise GgufError(f"张量 {info.name} 的类型 {info.type} 不支持（要 F16/BF16/F32）")


def write_gguf(path: Path, metadata: bytes, kv_count: int,
               tensors: list[tuple[TensorInfo, bytes]],
               alignment: int = DEFAULT_ALIGNMENT,
               on_write: Callable[[int, int], None] | None = None) -> None:
    """按给定的张量列表写一个新 GGUF。

    tensors 里每个 (info, data) 的 offset 会被重算——调用方不用关心对齐。

    头部在内存里拼（几 MB），数据段逐张量直接落盘。以前是把整个文件拼成一个
    bytearray 再 `fh.write(bytes(out))`：一份 4 GB 的产物峰值要多占 4 GB
    （bytearray 之外还复制了一份），而且那一次 write 全程报不出任何进度，
    页面上"写入文件"就是一句死的——几 GB 落盘时看着像卡死。

    `on_write(已写字节, 总字节)` 每写完一个张量回调一次，调用方据此报进度。
    """
    header = bytearray()
    header += GGUF_MAGIC
    header += struct.pack("<I", 3)
    header += struct.pack("<Q", len(tensors))
    header += struct.pack("<Q", kv_count)
    header += metadata

    # tensor info 段
    offsets: list[int] = []
    cursor = 0
    for info, data in tensors:
        offsets.append(cursor)
        cursor += _align(len(data), alignment)

    for (info, _), off in zip(tensors, offsets):
        header += _write_string(info.name)
        header += struct.pack("<I", len(info.dims))
        for d in info.dims:
            header += struct.pack("<Q", int(d))
        header += struct.pack("<I", info.type)
        header += struct.pack("<Q", off)

    header += b"\x00" * (_align(len(header), alignment) - len(header))

    base = len(header)
    # 最后一个张量之后的补齐字节不会被写出去（文件到数据结尾为止），
    # 所以总长要把它扣回来，否则进度永远差一截。
    total = base + cursor
    if tensors:
        last = len(tensors[-1][1])
        total += last - _align(last, alignment)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pos = base
    with open(path, "wb") as fh:
        fh.write(header)
        for (info, data), off in zip(tensors, offsets):
            want = base + off
            if pos < want:
                fh.write(b"\x00" * (want - pos))
                pos = want
            fh.write(data)
            pos += len(data)
            if on_write is not None:
                on_write(pos, total)


# ---------------------------------------------------------------- 基础工具

def _align(value: int, alignment: int) -> int:
    if alignment <= 1:
        return value
    return (value + alignment - 1) // alignment * alignment


def _read_string(buf: bytes, pos: int) -> tuple[str, int]:
    length = struct.unpack_from("<Q", buf, pos)[0]
    pos += 8
    return buf[pos:pos + length].decode("utf-8", "replace"), pos + length


def _skip_string(buf: bytes, pos: int) -> int:
    length = struct.unpack_from("<Q", buf, pos)[0]
    return pos + 8 + length


def _skip_value(buf: bytes, pos: int, vtype: int) -> int:
    if vtype == 8:                                  # STRING
        return _skip_string(buf, pos)
    if vtype == 9:                                  # ARRAY
        item_type = struct.unpack_from("<I", buf, pos)[0]
        count = struct.unpack_from("<Q", buf, pos + 4)[0]
        pos += 12
        if item_type == 8:
            for _ in range(count):
                pos = _skip_string(buf, pos)
            return pos
        size = _SIMPLE_VALUE_SIZE.get(item_type)
        if size is None:
            raise GgufError(f"元数据数组的元素类型 {item_type} 不认识")
        return pos + size * count
    size = _SIMPLE_VALUE_SIZE.get(vtype)
    if size is None:
        raise GgufError(f"元数据类型 {vtype} 不认识")
    return pos + size


def _write_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw
