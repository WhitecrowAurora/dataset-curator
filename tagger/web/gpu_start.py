"""所有 Web 编排共享的模型服务启动锁。"""
import threading
from contextlib import contextmanager

GPU_START_LOCK = threading.Lock()


@contextmanager
def model_start_slot():
    from fastapi import HTTPException
    from ..runtime.inference_queue import INFERENCE_LOCK
    if not INFERENCE_LOCK.acquire(blocking=False):
        raise HTTPException(409, "任务或发色复核正在推理，请等当前阶段结束后启动模型服务")
    try:
        yield
    finally:
        INFERENCE_LOCK.release()
