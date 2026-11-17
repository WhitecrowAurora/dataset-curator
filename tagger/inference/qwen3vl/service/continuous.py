"""Admit queued requests between decode steps and return each completed slot."""
from __future__ import annotations

import asyncio

from .batching import BatchScheduler
from .input_pipeline import InputPipeline


class ContinuousScheduler(BatchScheduler):
    def __init__(self, engine, batch_size):
        super().__init__(engine, batch_size)
        self.inputs = InputPipeline(engine)

    async def submit(self, request):
        staged = self.inputs.stage(request)
        try:
            return await super().submit(staged)
        finally:
            self.inputs.cancel(staged)

    async def _run(self):
        futures = {}
        closing = False
        pending = []

        def cancel_completed():
            for slot, future in list(futures.items()):
                if future.done():
                    session.cancel(slot)
                    del futures[slot]

        async def decode():
            for slot, result in await asyncio.to_thread(session.step):
                future = futures.pop(slot)
                if not future.done():
                    future.set_result(result)

        async def admit(pending):
            pending = [(request, future) for request, future in pending if not future.done()]
            # A free slot must not pause other captions while its CPU inputs
            # are being prepared. GPU vision/prefill still runs on the model thread.
            while session.active and any(not future.done() and not self.inputs.ready(request)
                                         for request, future in pending):
                cancel_completed()
                if session.active:
                    await decode()
            prepared = []
            for request, future in pending:
                if future.done():
                    continue
                try:
                    value = await self.inputs.resolve(request)
                except Exception as exc:  # noqa: BLE001 - isolate failed image preparation
                    if not future.done():
                        future.set_exception(exc)
                else:
                    if not future.done():
                        prepared.append((value, future))
            pending = prepared
            if not pending:
                return
            try:
                slots = await asyncio.to_thread(session.begin, [request for request, _ in pending])
            except ValueError:
                # An oversized request must not discard other requests in its prefill.
                for request, future in pending:
                    if future.done():
                        continue
                    try:
                        slot = (await asyncio.to_thread(session.add, [request]))[0]
                        futures[slot] = future
                    except Exception as exc:  # noqa: BLE001 - per-request inference failure
                        if not future.done():
                            future.set_exception(exc)
            else:
                futures.update((slot, future) for slot, (_, future) in zip(slots, pending, strict=True))

        try:
            session = self.engine.create_session(self.batch_size)
            while not closing or session.active:
                cancel_completed()
                pending = []
                if not session.active and not closing:
                    item = await self.queue.get()
                    if item is None:
                        closing = True
                    else:
                        pending.append(item)
                        # Only wait for aggregation when idle; active decoding
                        # never waits ten milliseconds to fill a free slot.
                        deadline = asyncio.get_running_loop().time() + 0.01
                        while len(pending) < len(session.available):
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
                elif not closing and session.can_admit:
                    while len(pending) < len(session.available):
                        try:
                            item = self.queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if item is None:
                            closing = True
                            break
                        pending.append(item)
                await admit(pending)
                if session.active:
                    await decode()
        except Exception as exc:
            for _, future in pending:
                if not future.done():
                    future.set_exception(exc)
            for future in futures.values():
                if not future.done():
                    future.set_exception(exc)
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if item is not None and not item[1].done():
                    item[1].set_exception(exc)
            raise
        finally:
            await asyncio.to_thread(self.inputs.close)
