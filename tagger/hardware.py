"""硬件体检：识别显卡、显存、算力等级，判断支持的精度档位。

为什么需要这个：
同一套打标流程要跑在 12G 到 32G、跨越 Ampere/Ada/Blackwell 的机器上。
精度档位不是"配置项"，而是硬件决定了哪些选项根本不存在——
FP8 需要 compute capability >= 8.9（Ada/Hopper 起），30 系（8.6）没有；
FP4 需要 >= 12.0（Blackwell）。搞错这一条，服务起得来但会退化成反量化，
比正确的低精度路径还慢。

本模块只做检测和判断，不做决策。决策在 config.py 的分档表里。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass


# 驱动太老时 nvidia-smi 不支持 compute_cap 字段，用型号名兜底推断。
# 数值对应 CUDA compute capability，格式 (major, minor)。
_NAME_TO_CC: list[tuple[str, tuple[int, int]]] = [
    (r"\bB(100|200)\b", (10, 0)),
    (r"\bRTX\s*50\d0\b|\bRTX\s*PRO\s*6000\b|\bGB20\d\b", (12, 0)),
    (r"\bH100\b|\bH200\b|\bGH200\b", (9, 0)),
    (r"\bL40\b|\bL4\b|\bRTX\s*40\d0\b|\bRTX\s*6000\s*Ada\b", (8, 9)),
    (r"\bA100\b|\bA6000\b|\bA5000\b|\bA4000\b", (8, 0)),
    (r"\bRTX\s*30\d0\b|\bA10\b|\bA40\b", (8, 6)),
    (r"\bRTX\s*20\d0\b|\bT4\b", (7, 5)),
]


@dataclass(slots=True, frozen=True)
class GPUInfo:
    index: int
    name: str
    vram_gb: float
    compute_cap: tuple[int, int] | None

    @property
    def cc_str(self) -> str:
        if not self.compute_cap:
            return "unknown"
        return f"{self.compute_cap[0]}.{self.compute_cap[1]}"

    @property
    def supports_fp8(self) -> bool:
        """FP8 tensor core：Ada(8.9) / Hopper(9.0) / Blackwell(12.0) 起。"""
        return bool(self.compute_cap) and self.compute_cap >= (8, 9)

    @property
    def supports_fp4(self) -> bool:
        """NVFP4：Blackwell(12.0) 起。"""
        return bool(self.compute_cap) and self.compute_cap >= (12, 0)

    @property
    def supports_int4(self) -> bool:
        """int4 反量化推理从 Turing 起就有，没有下限问题。"""
        return bool(self.compute_cap) and self.compute_cap >= (7, 0)


def _find_nvidia_smi() -> str | None:
    exe = shutil.which("nvidia-smi")
    if exe:
        return exe
    # Windows 上没进 PATH 时的常见位置
    for candidate in (
        r"C:\Windows\System32\nvidia-smi.exe",
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        "/usr/bin/nvidia-smi",
        "/usr/local/bin/nvidia-smi",
    ):
        try:
            import os

            if os.path.isfile(candidate):
                return candidate
        except OSError:
            continue
    return None


def _infer_cc_from_name(name: str) -> tuple[int, int] | None:
    for pattern, cc in _NAME_TO_CC:
        if re.search(pattern, name, re.IGNORECASE):
            return cc
    return None


def query_gpus() -> list[GPUInfo]:
    """查询所有 GPU。没有 NVIDIA 显卡时返回空列表，由调用方决定如何处理。"""
    smi = _find_nvidia_smi()
    if not smi:
        return []

    fields = "index,name,memory.total,compute_cap"
    try:
        out = subprocess.run(
            [smi, f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8", errors="replace",
            timeout=20,
        )
    except (subprocess.SubprocessError, OSError):
        return []

    if out.returncode != 0:
        # compute_cap 字段不被支持时退化成三字段查询
        try:
            out = subprocess.run(
                [smi, "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                encoding="utf-8", errors="replace",
                timeout=20,
            )
        except (subprocess.SubprocessError, OSError):
            return []
        if out.returncode != 0:
            return []
        fields = "index,name,memory.total"

    gpus: list[GPUInfo] = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            index = int(parts[0])
            name = parts[1]
            vram_gb = round(float(parts[2]) / 1024.0, 1)
        except ValueError:
            continue

        cc: tuple[int, int] | None = None
        if len(parts) >= 4 and parts[3]:
            m = re.match(r"(\d+)\.(\d+)", parts[3])
            if m:
                cc = (int(m.group(1)), int(m.group(2)))
        if cc is None:
            cc = _infer_cc_from_name(name)

        gpus.append(GPUInfo(index=index, name=name, vram_gb=vram_gb, compute_cap=cc))

    return gpus


def primary_gpu() -> GPUInfo | None:
    gpus = query_gpus()
    return gpus[0] if gpus else None


def describe(gpu: GPUInfo) -> str:
    """给 doctor 命令用的人类可读描述。"""
    flags = []
    flags.append("fp8" if gpu.supports_fp8 else "no-fp8")
    flags.append("fp4" if gpu.supports_fp4 else "no-fp4")
    flags.append("int4" if gpu.supports_int4 else "no-int4")
    return (
        f"[{gpu.index}] {gpu.name}  "
        f"{gpu.vram_gb} GiB  CC {gpu.cc_str}  "
        f"[{', '.join(flags)}]"
    )
