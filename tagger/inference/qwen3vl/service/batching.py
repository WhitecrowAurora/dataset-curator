"""One model thread, bounded request queue and short-window fixed batches."""
from __future__ import annotations

import asyncio


class BatchScheduler:
    def __init__(self, engine, batch_size):
        self.engine, self.batch_size = engine, batch_size
        self.queue = asyncio.Queue(maxsize=max(8, batch_size * 4))
        self.task = None
        self.closed = False

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def submit(self, request):
        if self.closed:
            raise RuntimeError("Caption scheduler has stopped")
        if self.task.done():
            self.task.result()
            raise RuntimeError("Caption scheduler has stopped")
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((request, future))
        return await future

    async def close(self):
        if not self.closed and not self.task.done():
            self.closed = True
            await self.queue.put(None)
        self.closed = True
        await self.task

    async def _run(self):
        closing = False
        while not closing:
            first = await self.queue.get()
            if first is None:
                break
            pending = [first]
            deadline = asyncio.get_running_loop().time() + 0.01
            while len(pending) < self.batch_size:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self.queue.get(), remaining)
                except TimeoutError:
                    break
                if item is None:
                    closing = True
                    break
                pending.append(item)
            pending = [(r, f) for r, f in pending if not f.cancelled()]
            if not pending:
                continue
            try:
                results = await asyncio.to_thread(self.engine.generate_batch, [r for r, _ in pending])
                if len(results) != len(pending):
                    raise RuntimeError("Caption engine returned an incomplete batch")
                for (_, future), result in zip(pending, results, strict=True):
                    if not future.done():
                        future.set_result(result)
            except ValueError:
                # A malformed/oversized prompt must not fail other valid requests.
                for request, future in pending:
                    if future.done():
                        continue
                    try:
                        result = (await asyncio.to_thread(self.engine.generate_batch, [request]))[0]
                        if not future.done():
                            future.set_result(result)
                    except Exception as exc:  # noqa: BLE001 - return inference failures to this request
                        if not future.done():
                            future.set_exception(exc)
            except Exception as exc:  # noqa: BLE001 - a batch failure must complete every future
                for _, future in pending:
                    if not future.done():
                        future.set_exception(exc)
