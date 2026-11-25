"""CLI、worker 与调度页面共用的逐图进度文件。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ..storage.atomic import write_json

FIELDS = ("done", "total", "phase", "total_known", "successful", "failed", "skipped",
          "rate_per_s", "elapsed_s", "updated_at", "image_id", "tag", "nl", "finished")


def read(path: Path, *, since: float = 0) -> dict:
    try:
        if path.stat().st_mtime < since:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: data[k] for k in FIELDS if k in data} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class ProgressWriter:
    def __init__(self, path: Path, mode="both"):
        from ..web.job_progress import OutputProgress

        self.path, self.mode = Path(path), mode
        self.parts = OutputProgress()
        self.started = time.time()
        self.last = 0.0
        self.stats = None
        self.image_id = ""
        self.lock = threading.RLock()

    def on_part(self, part, task, stats):
        with self.lock:
            self.parts.ready(part, task)
            self._write(stats, task)

    def __call__(self, kind, task, outcome, stats):
        with self.lock:
            if task is not None:
                self.parts.settle(kind, task, outcome, parts=("tag", "nl") if self.mode == "both" else (self.mode,))
            self._write(stats, task)

    def _write(self, stats, task=None, *, final=False):
        self.stats = stats
        if task is not None:
            self.image_id = task.image_id
        now = time.time()
        done = sum(getattr(stats, k, 0) for k in
                   ("done", "skipped", "failed", "blacklisted", "busy", "blank_skipped"))
        end = bool(getattr(stats, "input_complete", done >= stats.total)) and done >= stats.total
        if not final and not end and now - self.last < 1:
            return
        self.last = now
        elapsed = max(0.001, now - self.started)
        known = bool(getattr(stats, "input_complete", False))
        parts = self.parts.snapshot(stats.total, self.started, finished=final, total_known=known)
        payload = {"done": done, "total": stats.total, "total_known": known, "phase": self.mode,
                   "successful": stats.done, "failed": stats.failed,
                   "skipped": max(0, done - stats.done - stats.failed),
                   "elapsed_s": round(elapsed, 1), "rate_per_s": round(max(0, stats.done - getattr(stats, 'cached', 0)) / elapsed, 3),
                   "updated_at": now, "image_id": self.image_id, "finished": final,
                   **{part: parts[part] for part in (("tag", "nl") if self.mode == "both" else (self.mode,))}}
        try:
            write_json(self.path, payload)
        except OSError:
            pass

    def finish(self):
        with self.lock:
            if self.stats is not None:
                self._write(self.stats, final=True)
