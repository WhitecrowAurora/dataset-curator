"""现有冲突审查页的学习状态、样本导出与训练文本设置。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import conflicts
from ..review_learning import service
from ..storage.review_feedback import iter_events
from ..text_encoding.settings import TextEncoding, load, save
from ..text_encoding.tokenizer import TrainingText

router = APIRouter(prefix="/api/conflicts/learning", tags=["conflicts"])


@router.get("")
def state():
    spec = load()
    try:
        TrainingText(spec)
        tokenizer = {"ready": True}
    except (ValueError, OSError, ImportError) as exc:
        tokenizer = {"ready": False, "error": str(exc)}
    return {**service.status(conflicts.dir_of()), "text_encoding": spec.model_dump(), "tokenizer": tokenizer}


@router.post("/train")
def train():
    return service.retrain(conflicts.dir_of())


@router.get("/export")
def export():
    def lines():
        for path in service.sources(conflicts.dir_of()):
            for event in iter_events(path, latest=False, limit=-1):
                yield json.dumps({"source": str(path), **event}, ensure_ascii=False) + "\n"
    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": 'attachment; filename="review-feedback.jsonl"'})


@router.put("/text-encoding")
def configure(spec: TextEncoding):
    try:
        TrainingText(spec)
        save(spec)
        return state()
    except (ValueError, OSError, ImportError) as exc:
        raise HTTPException(400, str(exc)) from exc


class TextRequest(BaseModel):
    caption: str = ""
    tag: str = ""


@router.post("/text-encoding/check")
def check(request: TextRequest):
    try:
        counter = TrainingText()
        result = counter.metadata(request.caption, request.tag)
        return {**result, "over_limit": result["tokens"] > result["limit"]}
    except (ValueError, OSError, ImportError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/text-encoding/prepare")
def prepare():
    from ..text_encoding.download import prepare as download
    from .models import proxy_url
    try:
        return download(proxy=proxy_url())
    except (RuntimeError, ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
