"""同目录写完、同步后替换，避免覆盖有效状态。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                          suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        sync_directory(path.parent)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def write_json(path: Path, value: object, *, backup: bool = False) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if backup and path.is_file():
        try:
            previous = path.read_bytes()
            json.loads(previous)
        except (OSError, ValueError, UnicodeError):
            pass
        else:
            write_bytes(path.with_suffix(path.suffix + ".bak"), previous)
    write_bytes(path, data)


def read_json(path: Path, *, backup: bool = False):
    for candidate in (path, path.with_suffix(path.suffix + ".bak")) if backup else (path,):
        try:
            return json.loads(candidate.read_bytes())
        except (OSError, ValueError, UnicodeError):
            continue
    return None
