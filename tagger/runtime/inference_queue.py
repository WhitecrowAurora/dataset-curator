"""本进程的批量打标和单图复核共用同一个执行时段。"""
from threading import RLock

INFERENCE_LOCK = RLock()
