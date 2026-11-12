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

import os
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
    gpus = visible_gpus()
    return gpus[0] if gpus else None


def free_vram_gb(index: int = 0) -> float | None:
    """某张卡**此刻**还剩多少显存（GiB）。拿不到返回 None。

    为什么必须问空闲量而不是只看总量：GPUInfo.vram_gb 是出厂总量，而
    Windows 桌面合成、浏览器、以及同一个进程里已经加载的另一个模型
    都会先占掉几 GB。按总量去推 batch 大小，结果就是"参数算下来装得下、
    真跑起来 OOM"——16GB 卡上 batch 8 超出约 2 GiB 那次正是如此。

    index 用 nvidia-smi 的物理卡号（与 GPUInfo.index 同源），不是 torch
    的 cuda:序号——两者在设了 CUDA_VISIBLE_DEVICES 时不一致。
    """
    smi = _find_nvidia_smi()
    if not smi:
        return None
    try:
        out = subprocess.run(
            [smi, f"--id={int(index)}",
             "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if out.returncode != 0:
        return None
    line = (out.stdout or "").strip().splitlines()
    if not line:
        return None
    try:
        return round(float(line[0].strip()) / 1024.0, 2)
    except ValueError:
        return None


def cuda_visible_env() -> str:
    """环境里的 CUDA_VISIBLE_DEVICES 原文（去空白）。没设 = 空串。"""
    return str(os.environ.get("CUDA_VISIBLE_DEVICES") or "").strip()


def visible_gpus_error() -> str:
    """visible_gpus() 为空时的一句话原因。空串 = 本来就没有卡，不怪环境变量。

    为什么要单独一个函数：现在"指定了不存在的卡"也会让 visible_gpus 返回空，
    如果调用方只会说"没有检测到显卡"，用户会去查驱动、重装 CUDA，而真正该改的
    是环境变量里的那个号。
    ★**只在 visible_gpus() 真的为空时才有话说**。早期版本只看"有没有设变量"，
      于是 `CUDA_VISIBLE_DEVICES=2` 明明选到了卡，日志里还是跟着一句"没有指向
      任何一张可用的卡"——明明成功了却在报错，比不报更糟。
    """
    raw = cuda_visible_env()
    if not raw or raw.lower() == "all":
        return ""
    if visible_gpus():
        return ""
    physical = [g.index for g in query_gpus()]
    where = ("本机物理卡号：" + "/".join(str(i) for i in physical)) if physical else "本机没有可用的 NVIDIA 显卡"
    return (f"CUDA_VISIBLE_DEVICES={raw} 没有指向任何一张可用的卡（{where}）。"
            f"注意这里写的是 nvidia-smi 的物理卡号，不是 torch 的 cuda:序号；"
            f"不想限制就删掉这个环境变量。")


def visible_gpus() -> list[GPUInfo]:
    """**这台进程能看见的卡**，按 CUDA 的设备序排好。

    为什么不能直接用 query_gpus()[0]：那是 nvidia-smi 的**物理序**，而
    CUDA（torch、llama.cpp、vLLM）认的是 CUDA_VISIBLE_DEVICES 重映射之后
    的顺序。两者经常不一致——一台机器上 smi 是 [GTX1050, V100, V100]，而
    torch 眼里 0/1 是那两张 V100。用物理序取第一张就会拿 4GB 卡去推算槽位
    和精度，而模型实际跑在 16GB 上，配额只剩几分之一（真实工单）。

    规则与 CUDA 一致：
      * 没设 / 空 / `all` → 全部卡，按物理序；
      * `2` / `2,1` → 按**列表顺序**取，重复项保留（CUDA 也保留，两处出现同一张卡
        就是两个设备号）。

    ★**显式设了却一张都没对上时返回空列表**，不再退回全部卡。以前的
    `picked or gpus` 会把 `CUDA_VISIBLE_DEVICES=5`（只有两张卡）静默变成"用全部
    卡"：分档按物理 0 号算，而子进程其实看不到那些卡——症状是"按 4GB 卡分档、
    却跑在 16GB 上"，或者干脆 CUDA 初始化失败，日志里一句话都没有。
    宁可返回空让调用方报一句人话（"你指的是 5 号卡，本机只有 0/1"）。
    """
    gpus = query_gpus()
    if not gpus:
        return []
    raw = cuda_visible_env()
    if not raw or raw.lower() == "all":
        return gpus
    # `-1` / `none` / `NoDevFiles`：驱动语义是"没有可用设备"。
    # 混合写法（如 `0,-1`）同样整串失效，不是"除了 -1 之外都有效"。
    lowered = raw.lower()
    if lowered in ("-1", "none", "nodevfiles"):
        return []
    if any(p.strip() == "-1" for p in raw.split(",")):
        return []
    by_index = {g.index: g for g in gpus}
    picked: list[GPUInfo] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            idx = int(part)
        except ValueError:
            # UUID / MIG 之类：nvidia-smi 的 index 列对不上，跳过。
            continue
        gpu = by_index.get(idx)
        if gpu is not None:
            picked.append(gpu)
    return picked


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
