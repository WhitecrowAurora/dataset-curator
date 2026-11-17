"""工具页的请求接入与后台文件清洗。"""
from __future__ import annotations

import copy
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..tools.jsonl import clean_file, input_files

router = APIRouter(prefix="/api/tools", tags=["tools"])


class CleanRequest(BaseModel):
    path: str
    recursive: bool = False


class JsonlCleaner:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}

    def snapshot(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return copy.deepcopy(self.jobs[job_id])

    def start(self, request: CleanRequest) -> dict:
        root, files = input_files(request.path, request.recursive)
        job_id = uuid.uuid4().hex[:12]
        output = root.parent / f"{root.stem}-cleaned-{job_id}"
        state = {"id": job_id, "status": "running", "input": str(root),
                 "output_dir": str(output), "total_files": len(files),
                 "processed_files": 0, "failed_files": 0, "rows": 0,
                 "changed_rows": 0, "removed_scores": 0, "invalid_rows": 0,
                 "current": "", "results": []}
        with self.lock:
            if any(j["status"] == "running" for j in self.jobs.values()):
                raise RuntimeError("已有 JSONL 清洗在运行")
            # 保留最近的处理记录，页面刷新仍可读取。
            if len(self.jobs) >= 10:
                self.jobs.pop(next(iter(self.jobs)))
            self.jobs[job_id] = state
        threading.Thread(target=self._run, args=(job_id, root, files, output),
                         name="tagger-jsonl-clean", daemon=True).start()
        return self.snapshot(job_id)

    def _run(self, job_id: str, root: Path, files: list[Path], output: Path):
        base = root.parent if root.is_file() else root
        for source in files:
            with self.lock:
                self.jobs[job_id]["current"] = str(source)
            try:
                result = clean_file(source, output / source.relative_to(base))
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                result = {"source": str(source), "error": str(exc)}
            with self.lock:
                state = self.jobs[job_id]
                state["processed_files"] += 1
                state["failed_files"] += int("error" in result)
                for key in ("rows", "changed_rows", "removed_scores", "invalid_rows"):
                    state[key] += result.get(key, 0)
                state["results"].append(result)
        with self.lock:
            state = self.jobs[job_id]
            state["status"] = "completed_with_errors" if state["failed_files"] or state["invalid_rows"] else "done"
            state["current"] = ""


cleaner = JsonlCleaner()


@router.post("/jsonl/clean", status_code=202)
def start_clean(request: CleanRequest) -> dict:
    try:
        return cleaner.start(request)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/jsonl/{job_id}")
def clean_status(job_id: str) -> dict:
    try:
        return cleaner.snapshot(job_id)
    except KeyError as exc:
        raise HTTPException(404, "清洗记录不存在或服务已重启") from exc
