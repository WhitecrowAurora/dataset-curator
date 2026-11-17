"""Small NVRTC/driver bridge; no nvcc, C++ compiler, or Triton dependency."""
from __future__ import annotations

import ctypes as C
import logging
import os
import sys
from ctypes.util import find_library
from functools import lru_cache
from pathlib import Path
from threading import RLock

_lock = RLock()


def _bind(library, name, arguments):
    function = getattr(library, name)
    function.argtypes, function.restype = arguments, C.c_int
    return function


def _check(status, operation):
    if status:
        raise RuntimeError(f"CUDA compatibility kernel: {operation} failed (code={status})")


def _load_nvrtc():
    import torch

    folders = [Path(torch.__file__).resolve().parent / "lib"]
    folders.extend(Path(p) / "nvidia" / sub / "lib"
                   for p in sys.path if p for sub in ("cuda_nvrtc", "cu13"))
    toolkit = os.environ.get("CUDA_PATH") or os.environ.get("CUDA_HOME")
    if toolkit:
        folders.append(Path(toolkit) / ("bin" if os.name == "nt" else "lib64"))
    pattern = "nvrtc64*.dll" if os.name == "nt" else "libnvrtc.so*"
    for folder in folders:
        for path in sorted(folder.glob(pattern)):
            if ".alt." in path.name:
                continue
            try:
                return C.CDLL(str(path))
            except OSError:
                continue
    name = find_library("nvrtc")
    if name:
        return C.CDLL(name)
    raise OSError("NVRTC library is absent from the PyTorch CUDA runtime")


class Runtime:
    def __init__(self):
        self.nvrtc = _load_nvrtc()
        self.driver = C.CDLL("nvcuda.dll" if os.name == "nt" else "libcuda.so.1")
        self._ptx_cache, self._modules = {}, {}
        pointer = C.c_void_p
        self.create = _bind(self.nvrtc, "nvrtcCreateProgram",
                            [C.POINTER(pointer), C.c_char_p, C.c_char_p, C.c_int,
                             C.POINTER(C.c_char_p), C.POINTER(C.c_char_p)])
        self.compile = _bind(self.nvrtc, "nvrtcCompileProgram", [pointer, C.c_int, C.POINTER(C.c_char_p)])
        self.destroy = _bind(self.nvrtc, "nvrtcDestroyProgram", [C.POINTER(pointer)])
        self.ptx_size = _bind(self.nvrtc, "nvrtcGetPTXSize", [pointer, C.POINTER(C.c_size_t)])
        self.ptx = _bind(self.nvrtc, "nvrtcGetPTX", [pointer, pointer])
        self.log_size = _bind(self.nvrtc, "nvrtcGetProgramLogSize", [pointer, C.POINTER(C.c_size_t)])
        self.log = _bind(self.nvrtc, "nvrtcGetProgramLog", [pointer, pointer])
        self.context = _bind(self.driver, "cuCtxGetCurrent", [C.POINTER(pointer)])
        self.load = _bind(self.driver, "cuModuleLoadData", [C.POINTER(pointer), pointer])
        self.function = _bind(self.driver, "cuModuleGetFunction", [C.POINTER(pointer), pointer, C.c_char_p])
        self.launch = _bind(self.driver, "cuLaunchKernel",
                            [pointer, *([C.c_uint] * 7), pointer, C.POINTER(pointer), C.POINTER(pointer)])

    def compile_ptx(self, kind, architecture):
        key = kind, architecture
        if key in self._ptx_cache:
            return self._ptx_cache[key]
        source = f"#define KIND {kind}\n".encode() + Path(__file__).with_name("gguf.cu").read_bytes()
        program = C.c_void_p()
        _check(self.create(C.byref(program), source, b"gguf.cu", 0, None, None), "nvrtcCreateProgram")
        try:
            # Disable contraction in the reader to preserve GGUF dequantization.
            options = [f"--gpu-architecture=compute_{architecture}".encode(), b"--std=c++11", b"--fmad=false"]
            status = self.compile(program, len(options), (C.c_char_p * len(options))(*options))
            if status:
                length = C.c_size_t()
                _check(self.log_size(program, C.byref(length)), "nvrtcGetProgramLogSize")
                log = C.create_string_buffer(length.value)
                _check(self.log(program, log), "nvrtcGetProgramLog")
                raise RuntimeError(f"CUDA compatibility kernel compilation failed: {log.value.decode(errors='replace')}")
            length = C.c_size_t()
            _check(self.ptx_size(program, C.byref(length)), "nvrtcGetPTXSize")
            ptx = C.create_string_buffer(length.value)
            _check(self.ptx(program, ptx), "nvrtcGetPTX")
            self._ptx_cache[key] = ptx.raw
            return self._ptx_cache[key]
        finally:
            _check(self.destroy(C.byref(program)), "nvrtcDestroyProgram")

    def _module(self, context, kind, architecture):
        key = context, kind, architecture
        if key in self._modules:
            return self._modules[key]
        module = C.c_void_p()
        _check(self.load(C.byref(module), self.compile_ptx(kind, architecture)), "cuModuleLoadData")
        functions = {}
        for name in ("dequantize", "gemv"):
            function = C.c_void_p()
            _check(self.function(C.byref(function), module, name.encode()), "cuModuleGetFunction")
            functions[name] = function
        # Modules belong to the CUDA context and live as long as the worker.
        self._modules[key] = module, functions
        return self._modules[key]

    def functions(self, device, kind):
        import torch

        context = C.c_void_p()
        _check(self.context(C.byref(context)), "cuCtxGetCurrent")
        major, minor = torch.cuda.get_device_capability(device)
        with _lock:
            return self._module(context.value, kind, major * 10 + minor)[1]

    def call(self, function, grid, threads, stream, arguments):
        params = (C.c_void_p * len(arguments))(*(C.addressof(a) for a in arguments))
        _check(self.launch(function, *grid, threads, 1, 1, 0, C.c_void_p(stream), params, None), "cuLaunchKernel")


@lru_cache(maxsize=1)
def get_runtime():
    try:
        return Runtime()
    except OSError as exc:
        logging.getLogger(__name__).warning("CUDA 量化加速不可用，使用 Torch 兼容计算：%s", exc)
        return None
