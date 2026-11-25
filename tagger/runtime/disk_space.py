"""与运行时磁盘保护共用的开工检查。"""
from __future__ import annotations

import shutil
from pathlib import Path


def shortage(policy, free_gb: float, free_pct: float) -> str:
    if free_gb < policy.disk_min_free_gb or free_pct < policy.disk_min_free_pct:
        return (f'磁盘剩余 {free_gb:.1f} GB / {free_pct * 100:.1f}%，'
                f'低于设置下限 {policy.disk_min_free_gb:g} GB / '
                f'{policy.disk_min_free_pct * 100:g}%；请释放空间、'
                '更换输出目录或在「设置 → 磁盘保护」调整阈值')
    return ''


def require_space(policy, directory) -> None:
    from ..config import ConfigError

    path = Path(directory).expanduser().resolve()
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        raise ConfigError(f'无法检查输出目录的磁盘空间：{path}（{exc}）') from exc
    message = shortage(policy, usage.free / 1024**3,
                       usage.free / usage.total if usage.total else 1.0)
    if message:
        raise ConfigError(f'{message}；输出目录：{path}')
