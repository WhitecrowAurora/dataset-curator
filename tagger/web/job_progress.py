"""TAG / NL 的独立推理进度；在途状态只保留到图片结算。"""

from __future__ import annotations

import threading
import time


class OutputProgressHook:
    """扩展现有逐图回调，保留 run_client 的调用契约。"""

    def __init__(self, hook) -> None:
        self.hook = hook

    def __call__(self, kind, task, outcome, stats) -> None:
        self.hook(kind, task, outcome, stats)

    def on_part(self, part, task, stats) -> None:
        self.hook(f"{part}_ready", task, None, stats)


class OutputProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, set[str]] = {}
        self._counts = {part: {"done": 0, "skipped": 0, "failed": 0}
                        for part in ("tag", "nl")}
        self._first = {part: 0.0 for part in self._counts}
        self._last = {part: 0.0 for part in self._counts}

    def _done(self, part: str) -> None:
        now = time.time()
        self._counts[part]["done"] += 1
        self._first[part] = self._first[part] or now
        self._last[part] = now

    def ready(self, part: str, task) -> None:
        with self._lock:
            ready = self._active.setdefault(task.image_id, set())
            if part not in ready:
                ready.add(part)
                self._done(part)

    def settle(self, kind: str, task, outcome) -> None:
        with self._lock:
            ready = self._active.pop(task.image_id, set())
            for part, counts in self._counts.items():
                if part in ready or kind == "busy":
                    continue
                if kind == "done" and getattr(outcome, part, None) is not None:
                    self._done(part)
                elif kind in ("skipped", "blacklisted", "excused", "retired"):
                    counts["skipped"] += 1
                else:
                    counts["failed"] += 1

    def snapshot(self, total: int, started: float, *, finished: bool,
                 total_known: bool = True) -> dict:
        now = time.time()
        with self._lock:
            result = {}
            for part, counts in self._counts.items():
                settled = sum(counts.values())
                end = self._last[part] if finished else now
                begin = self._first[part] or started
                if end <= begin:
                    begin = started
                elapsed = max(0.0, end - begin) if begin and end else 0.0
                rate = counts["done"] / elapsed if elapsed > 0 else 0.0
                remaining = max(0, total - settled)
                result[part] = {
                    **counts, "total": total, "settled": settled,
                    "progress": min(1.0, settled / total) if total_known and total else None,
                    "rate_per_s": round(rate, 2),
                    "eta_s": round(remaining / rate, 1) if total_known and total and rate > 0 else None,
                }
            return result
