"""Prepare queued requests on one CPU thread while the model thread decodes."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


@dataclass(slots=True)
class _Prepared:
    request: object
    future: object


class InputPipeline:
    def __init__(self, engine):
        self.prepare = getattr(engine, "prepare_request", None)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lulynx-input")

    def stage(self, request):
        future = self.executor.submit(self.prepare, request) if self.prepare is not None else None
        return _Prepared(request, future)

    async def resolve(self, staged):
        return staged.request if staged.future is None else await asyncio.wrap_future(staged.future)

    @staticmethod
    def ready(staged):
        return staged.future is None or staged.future.done()

    @staticmethod
    def cancel(staged):
        if staged.future is not None:
            staged.future.cancel()

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
