"""Internal native worker, managed through the existing NL service lifecycle."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from .runtime import VERSION, load_engine
from .service.batching import BatchScheduler
from .service.continuous import ContinuousScheduler
from .service.requests import caption_request


def create_app(spec, engine_factory=load_engine, *, request_factory=caption_request, version=VERSION):
    @asynccontextmanager
    async def lifespan(app):
        engine = await asyncio.to_thread(engine_factory, spec)
        app.state.runtime_info = getattr(engine, "runtime_info", None)
        if spec.get("continuous_batching") and getattr(engine, "supports_continuous", True):
            scheduler = ContinuousScheduler(engine, spec["batch_size"],
                                             batch_window_s=spec.get("batch_window_s", 0.01))
        else:
            scheduler = BatchScheduler(engine, spec["batch_size"],
                                       batch_window_s=spec.get("batch_window_s", 0.01))
        scheduler.start()
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            await scheduler.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/version")
    def runtime_version():
        info = getattr(app.state, "runtime_info", None)
        return {"version": version, **({"runtime": info} if info else {})}

    @app.get("/v1/models")
    def models():
        return {"object": "list", "data": [{"id": spec["served_name"], "object": "model", "owned_by": "lulynx"}]}

    @app.post("/v1/chat/completions")
    async def completion(request: Request):
        try:
            body = await request.json()
            item = await asyncio.to_thread(request_factory, body, spec)
            result = await app.state.scheduler.submit(item)
        except asyncio.QueueFull as exc:
            raise HTTPException(429, "推理队列已满，请稍后重试") from exc
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        count = len(result.token_ids)
        return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()),
                "model": spec["served_name"], "choices": [{"index": 0, "finish_reason": result.finish_reason,
                "message": {"role": "assistant", "content": result.text}}],
                "usage": {"prompt_tokens": result.prompt_tokens, "completion_tokens": count,
                          "total_tokens": result.prompt_tokens + count}}

    return app


def main():
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--runtime", required=True)
    args = parser.parse_args()
    uvicorn.run(create_app(json.loads(args.runtime)), host="127.0.0.1", port=args.port)
