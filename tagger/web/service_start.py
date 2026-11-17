"""llama.cpp 的设备选择和有限次数资源回退，始终保留所选模型与精度。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..config import ConfigError, DEFAULT_TIERS, load_tier_table, resolve_profile
from ..hardware import GPUInfo, visible_gpus
from ..inference.devices import resource_failure


def resolve_device_profile(device: str, **overrides):
    gpus = visible_gpus()
    if device != "cpu" and gpus:
        index = 0 if device == "auto" else int(device.removeprefix("cuda:"))
        if index >= len(gpus):
            raise ConfigError(f"设备 {device} 不存在")
        gpu = gpus[index]
        # llama.cpp 支持部分卸载，完整权重不必一次全部装入显存。
        table = load_tier_table(DEFAULT_TIERS)
        if overrides.get("precision_override") == "f16":
            selected = resolve_profile(table, gpu, **(overrides | {"precision_override": "q6"}))
            overrides = overrides | {"tier_override": selected.tier}
            gpu_for_size = replace(gpu, vram_gb=max(64, gpu.vram_gb))
        else:
            gpu_for_size = gpu
        profile = resolve_profile(table, gpu_for_size, **overrides)
        return replace(profile, vram_gb=gpu.vram_gb, device_name=gpu.name), gpu.index
    if device not in ("auto", "cpu"):
        raise ConfigError(f"设备 {device} 不存在")
    # CPU 也需要同一份模型/提示词契约，显存门槛仅在 GPU 执行时适用。
    profile = resolve_profile(load_tier_table(DEFAULT_TIERS),
                              GPUInfo(-1, "CPU", 64, None), **overrides)
    return replace(profile, vram_gb=0, device_name="CPU"), None


def start_llama(profile, *, device: str, device_index: int | None, port: int,
                log_path: Path, served_name: str, timeout_s: int, slots: int | None,
                tick, log, on_handle):
    from ..model_loader import start_server, wait_ready

    cpu = device == "cpu" or profile.device_name == "CPU"
    attempts = [(0, 1)] if cpu else [(99, slots), (99, 1), (32, 1), (16, 1), (8, 1), (0, 1)]
    if slots == 1 and not cpu:
        attempts.pop(1)
    for layers, concurrency in attempts:
        tick(0)
        handle = None
        offset = log_path.stat().st_size if log_path.exists() else 0
        used_profile = replace(profile, device_name="CPU", vram_gb=0) if layers == 0 else profile
        log(f"尝试 {'CPU' if layers == 0 else f'GPU（最多 {layers} 层）'} · 槽位 {concurrency or '自动'} · 精度 {profile.precision}")
        try:
            handle = start_server(used_profile, port=port, log_path=log_path, wait=False,
                                  served_name=served_name, engine="llamacpp",
                                  slots_override=concurrency, device_index=device_index,
                                  gpu_layers=layers)
            on_handle(handle)

            def poll(remaining):
                tick(remaining)
                if handle.process is not None and handle.process.poll() is not None:
                    raise ConfigError(f"llama.cpp 进程已退出（code={handle.process.returncode}）")

            wait_ready(handle.base_url, timeout_s=timeout_s, on_tick=poll)
            return handle
        except Exception as exc:
            if handle is not None:
                handle.stop()
            on_handle(None)
            tick(0)
            try:
                with log_path.open("rb") as stream:
                    stream.seek(0, 2)
                    stream.seek(max(offset, stream.tell() - 4096))
                    detail = stream.read().decode("utf-8", errors="replace")
            except OSError:
                detail = ""
            if layers == 0 or not resource_failure(f"{exc}\n{detail}"):
                raise
            log("GPU 资源不足或后端不可用，减少 GPU 占用后重试。")
    raise ConfigError("所选模型未能启动")
