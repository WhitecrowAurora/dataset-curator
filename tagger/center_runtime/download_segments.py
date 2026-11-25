"""保存分片的下载区间，让续传不依赖当前并发数。"""
import hashlib
import json
from pathlib import Path

from ..storage.atomic import read_json, write_json


def plan_path(dest: Path) -> Path:
    return dest.parent / f".{dest.name}.segments.json"


def segment_plan(dest: Path, total: int, conns: int, source: str):
    path = plan_path(dest)
    saved = read_json(path)
    identity = {"source": source, "size": total}
    if (isinstance(saved, dict) and all(saved.get(k) == v for k, v in identity.items())
            and type(saved.get("segments")) is int and 1 < saved["segments"] <= 16):
        count = saved["segments"]
    else:
        count = max(1, min(16, conns))
    current = {**identity, "segments": count}
    if current != saved:
        write_json(path, current)
    # 缓存名同时绑定来源、大小和原始区间布局；旧版无范围记录的缓存不混用。
    key = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()[:16]
    width = -(-total // count)
    bounds = [(start, min(start + width, total) - 1) for start in range(0, total, width)]
    paths = [dest.parent / f".{dest.name}.seg-{key}-{i:03d}" for i in range(len(bounds))]
    return bounds, paths
