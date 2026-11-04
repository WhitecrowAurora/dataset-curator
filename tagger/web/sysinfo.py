"""底部状态条要的硬件读数：CPU/GPU 使用率与温度。

为什么自己算而不装 psutil：这个项目到现在是零额外依赖，为了底栏四个数字
再拖一个带 C 扩展的包不值当。CPU 使用率用系统累计计数器做差（Windows 走
`GetSystemTimes`，Linux 读 `/proc/stat`），差值就是**两次轮询之间**的平均
占用——底栏本来就是看趋势，瞬时值反而抖得没法看。

温度拿不到时返回 None，前端显示 `--`：显示 0°C 比不显示更糟，那会让人
以为机器冷得很。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

# GPU 读数要起 nvidia-smi 子进程。底栏最快 1 秒一次，不缓存等于每秒拉一个
# 进程起来——那点开销会直接算进 CPU 占用里，数字自己把自己抬高。
GPU_CACHE_S = 2.0

_lock = threading.Lock()
_last_cpu: tuple[float, float] | None = None       # (busy, total) 累计
_gpu_cache: tuple[float | None, float | None, float | None, float | None] = (
    None, None, None, None)
_gpu_ts = 0.0


# ------------------------------------------------------------------ CPU

def _cpu_times() -> tuple[float, float] | None:
    """返回 (busy, total) 的累计计数。读不到返回 None。"""
    if os.name == "nt":
        return _win_cpu_times()
    try:
        line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    try:
        parts = [float(x) for x in line.split()[1:]]
    except ValueError:
        return None
    if len(parts) < 4:
        return None
    # user nice system idle iowait irq softirq steal ...
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
    return sum(parts) - idle, sum(parts)


def _win_cpu_times() -> tuple[float, float] | None:
    """Windows 的 GetSystemTimes：kernel 时间**包含** idle，别重复加。"""
    try:
        import ctypes
        from ctypes import wintypes

        idle = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return None

        def ft(v: "wintypes.FILETIME") -> float:
            return float((v.dwHighDateTime << 32) | v.dwLowDateTime)

        i, k, u = ft(idle), ft(kernel), ft(user)
        total = k + u
        return total - i, total
    except (AttributeError, OSError):
        return None


def cpu_percent() -> float | None:
    """两次调用之间的平均 CPU 占用。第一次调用会短采样 50ms 拿基准。"""
    global _last_cpu
    now = _cpu_times()
    if now is None:
        return None
    with _lock:
        prev = _last_cpu
        _last_cpu = now
    if prev is None:
        time.sleep(0.05)
        again = _cpu_times()
        if again is None:
            return None
        with _lock:
            _last_cpu = again
        prev, now = now, again
    dbusy = now[0] - prev[0]
    dtotal = now[1] - prev[1]
    if dtotal <= 0:
        return None
    return max(0.0, min(100.0, dbusy / dtotal * 100.0))


# CPU 温度刻意不显示：Windows 没有标准的用户态接口（psutil 在 Windows 上
# 的 sensors_temperatures() 也直接返回空），Linux 的 thermal_zone 又常常报的
# 是主板/电池而不是 CPU 核心。底栏一个"经常是 --、偶尔还是错的"读数，
# 只会让人反复怀疑是不是服务挂了。


# ------------------------------------------------------------------ 内存

_TG = 1024 ** 3


def mem_usage() -> tuple[float, float] | None:
    """(已用GB, 总量GB)。

    不用 psutil：这两行分别是 Windows 的一次 `GlobalMemoryStatusEx`
    和 Linux 读一遍 `/proc/meminfo`，都没有外部进程开销。
    拿不到就返回 None，前端显示 `--`，不拿 0 充数。
    """
    if os.name == "nt":
        return _win_mem_usage()
    try:
        info: dict[str, float] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            name, _, rest = line.partition(":")
            fields = rest.split()
            if fields:
                info[name.strip()] = float(fields[0]) * 1024.0
        total = info.get("MemTotal")
        # MemAvailable 才是"真的还能用"，MemFree 会把缓存算成已用，
        # 在跑完一个大任务后能差出十几 G。
        avail = info.get("MemAvailable") or info.get("MemFree")
        if not total or not avail:
            return None
        return (total - avail) / _TG, total / _TG
    except (OSError, ValueError, IndexError):
        return None


def _win_mem_usage() -> tuple[float, float] | None:
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = _MemoryStatusEx()
        st.dwLength = ctypes.sizeof(_MemoryStatusEx)
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
            ctypes.byref(st))
        if not ok:
            return None
        total = float(st.ullTotalPhys)
        avail = float(st.ullAvailPhys)
        if total <= 0:
            return None
        return (total - avail) / _TG, total / _TG
    except (AttributeError, OSError):
        return None


# ------------------------------------------------------------------ GPU

def _nvidia_smi() -> str | None:
    exe = shutil.which("nvidia-smi")
    if exe:
        return exe
    for candidate in (r"C:\Windows\System32\nvidia-smi.exe",
                      "/usr/bin/nvidia-smi", "/usr/local/bin/nvidia-smi"):
        if os.path.isfile(candidate):
            return candidate
    return None


def gpu_meters() -> tuple[float | None, float | None, float | None, float | None]:
    """(利用率%, 温度°C, 显存已用GB, 显存总量GB)。没有 N 卡时全是 None。"""
    global _gpu_cache, _gpu_ts
    now = time.monotonic()
    if now - _gpu_ts < GPU_CACHE_S:
        return _gpu_cache
    exe = _nvidia_smi()
    result: tuple[float | None, float | None, float | None, float | None] = (
        None, None, None, None)
    if exe:
        try:
            out = subprocess.run(
                [exe, "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                # 多卡机器只报第一张：底栏不够宽，而且"哪张卡"这个问题
                # 该去多卡页看，底栏只回答"这台机器忙不忙"。
                first = out.stdout.strip().splitlines()[0]
                parts = [p.strip() for p in first.split(",")]
                if len(parts) >= 4:
                    # memory.* 单位是 MiB（nounits 只是不带后缀），要除 1024
                    result = (float(parts[0]), float(parts[1]),
                              float(parts[2]) / 1024.0,
                              float(parts[3]) / 1024.0)
        except (subprocess.SubprocessError, OSError, ValueError):
            result = (None, None, None, None)
    with _lock:
        _gpu_cache = result
        _gpu_ts = now
    return result


def snapshot() -> dict:
    """底栏右侧那一排。字段名直接对应界面上的标签，省一层翻译。

    `*_pct` 与 `*_gb` 都给：底栏只放得下百分比，悬浮提示里再给 GB 明细。
    """
    gpu_pct, gpu_temp, vram_used, vram_total = gpu_meters()
    mem = mem_usage()
    mem_used, mem_total = mem if mem else (None, None)

    def pct(used: float | None, total: float | None) -> float | None:
        if used is None or not total:
            return None
        return max(0.0, min(100.0, used / total * 100.0))

    return {
        "cpu_pct": cpu_percent(),
        "mem_pct": pct(mem_used, mem_total),
        "mem_used_gb": mem_used,
        "mem_total_gb": mem_total,
        "gpu_pct": gpu_pct,
        "gpu_temp_c": gpu_temp,
        "vram_pct": pct(vram_used, vram_total),
        "vram_used_gb": vram_used,
        "vram_total_gb": vram_total,
    }
