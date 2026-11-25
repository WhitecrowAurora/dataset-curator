"""本机磁盘保护阈值；未设置的字段沿用分档配置。"""
from __future__ import annotations

import math
import os
from pathlib import Path

from .atomic import read_json, write_json

PATH = Path(os.environ.get('TAGGER_DISK_SETTINGS', '') or
            Path(__file__).resolve().parents[2] / 'configs' / 'disk.json')
RANGES = {'disk_min_free_gb': (0.0, 1048576.0), 'disk_min_free_pct': (0.0, 1.0)}


def normalize(values: dict) -> dict[str, float]:
    result = {}
    for key, (low, high) in RANGES.items():
        if key not in values:
            continue
        value = float(values[key])
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'磁盘保护参数 {key} 超出范围')
        result[key] = value
    return result


def load() -> dict[str, float]:
    raw = read_json(PATH, backup=True)
    if not isinstance(raw, dict):
        return {}
    try:
        return normalize(raw)
    except (TypeError, ValueError):
        return {}


def save(values: dict) -> dict[str, float]:
    normalized = normalize(values)
    write_json(PATH, normalized, backup=True)
    return normalized
