"""按运行保存日志，并以有限读取提供最近输出。"""
from datetime import datetime
from pathlib import Path
import os
import threading
import time


def new_path(directory, prefix):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f'{prefix}-{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}-{time.time_ns() % 1_000_000_000:09d}.log'


def tail(path, lines=120):
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - 256 * 1024)
            stream.seek(start)
            raw = stream.read().decode('utf-8', errors='replace')
        rows = raw.splitlines()
        return (rows[1:] if start else rows)[-lines:]
    except OSError:
        return []


class RunLog:
    def __init__(self, directory, prefix):
        self.directory = Path(directory).resolve()
        previous = sorted(self.directory.glob(f'{prefix}-*.log'), key=lambda p: p.stat().st_mtime_ns)
        self.previous = tail(previous[-1]) if previous else []
        self.path = new_path(self.directory, prefix)
        self.lock = threading.Lock()

    def append(self, line):
        with self.lock, self.path.open('a', encoding='utf-8') as stream:
            stream.write(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n')
