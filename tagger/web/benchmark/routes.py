"""并发测试的薄 HTTP 接入层。"""
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from .catalog import available
from .contracts import BenchmarkRequest
from .service import Benchmark


def router(center):
    manager = Benchmark(center)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await run_in_threadpool(manager.close)

    routes = APIRouter(prefix='/api/benchmark', lifespan=lifespan)

    @routes.get('/options')
    def options():
        return available(center)

    @routes.get('')
    def status():
        return manager.snapshot()

    @routes.post('/start', status_code=202)
    def start(request: BenchmarkRequest):
        try:
            return manager.start(request)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.post('/stop')
    def stop():
        return manager.stop()

    return routes
