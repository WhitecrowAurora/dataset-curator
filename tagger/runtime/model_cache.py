"""按当前可用内存保留空闲模型；使用中的对象不参与回收。"""
from __future__ import annotations

import gc
import mmap
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

GIB = 1 << 30


def memory_info():
    import psutil
    value = psutil.virtual_memory()
    return value.total, value.available


@dataclass
class Entry:
    value: object
    size: int
    release: object
    kind: str
    users: int = 0


class ModelCache:
    def __init__(self, probe=memory_info):
        self.probe = probe
        self.entries = OrderedDict()
        self.lock = threading.RLock()
        self.enabled = True
        self.max_gb = 0.0
        self.reserve_gb = 0.0
        self.stopping = threading.Event()
        self.thread = None

    def configure(self, enabled=True, max_gb=0.0, reserve_gb=0.0):
        with self.lock:
            self.enabled, self.max_gb, self.reserve_gb = enabled, max_gb, reserve_gb
            self._trim()

    def _budget(self):
        try:
            total, free = self.probe()
        except (ImportError, OSError):
            return 0, 0, 0, 0
        reserve = int(self.reserve_gb * GIB) if self.reserve_gb else max(2 * GIB, int(total * .2))
        resident = sum(e.size for e in self.entries.values() if e.kind != "mapped_weights")
        budget = max(0, free + resident - reserve)
        # 给正在解码、加载和其他软件的短期增长留空间。
        budget = min(budget, int(total * .5))
        if self.max_gb:
            budget = min(budget, int(self.max_gb * GIB))
        return total, free, reserve, budget if self.enabled else 0

    def _discard(self, key):
        entry = self.entries.pop(key)
        if entry.release:
            entry.release(entry.value)

    def _trim(self, incoming=0):
        _, free, reserve, budget = self._budget()
        used = sum(e.size for e in self.entries.values())
        excess = max(used + incoming - budget, 0)
        pressure = max(reserve + incoming - free, 0)
        for key, entry in list(self.entries.items()):
            if not entry.users and (excess > 0 or pressure > 0 or not self.enabled):
                self._discard(key)
                excess -= entry.size
                if entry.kind != "mapped_weights":
                    pressure -= entry.size
        # 删除最后一个 Python 引用才会让 ONNX session 实际释放。
        if excess > 0 or pressure > 0:
            gc.collect()

    def _start_monitor(self):
        if self.thread is None or not self.thread.is_alive():
            self.stopping.clear()
            def monitor():
                while not self.stopping.wait(5):
                    with self.lock:
                        self._trim()
            self.thread = threading.Thread(target=monitor, name="model-cache", daemon=True)
            self.thread.start()

    @contextmanager
    def acquire(self, key, create, size, release=None, kind="cpu_session"):
        with self.lock:
            self._start_monitor()
            self._trim(incoming=0 if key in self.entries else size)
            entry = self.entries.get(key)
            if entry is None:
                entry = Entry(create(), size, release, kind)
                self.entries[key] = entry
            entry.users += 1
            self.entries.move_to_end(key)
        try:
            yield entry.value
        finally:
            with self.lock:
                entry.users -= 1
                self._trim()

    def forget(self, prefix=""):
        with self.lock:
            for key, entry in list(self.entries.items()):
                if key.startswith(prefix) and not entry.users:
                    self._discard(key)
        gc.collect()

    def snapshot(self):
        with self.lock:
            total, free, reserve, budget = self._budget()
            return {"enabled": self.enabled, "total_gb": round(total / GIB, 2),
                    "available_gb": round(free / GIB, 2), "reserve_gb": round(reserve / GIB, 2),
                    "budget_gb": round(budget / GIB, 2), "retained_gb": round(sum(e.size for e in self.entries.values()) / GIB, 2),
                    "models": [{"name": k, "kind": e.kind, "size_gb": round(e.size / GIB, 3),
                                "active": bool(e.users)} for k, e in self.entries.items()]}

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=6)
        self.thread = None
        self.forget()


class WeightFiles:
    """文件映射缓存；OS 可在压力下回收页面，不是已加载的推理引擎。"""
    def __init__(self, paths):
        self.maps = []
        try:
            for path in paths:
                with Path(path).open("rb") as source:
                    mapped = mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ)
                self.maps.append(mapped)
        except BaseException:
            self.close()
            raise

    def warm(self):
        for mapped in self.maps:
            for offset in range(0, len(mapped), 8 << 20):
                # 有限临时块触发文件缓存；不额外复制整套权重。
                mapped[offset:offset + (8 << 20)]

    def close(self):
        for mapped in self.maps:
            mapped.close()
        self.maps.clear()


MODELS = ModelCache()
