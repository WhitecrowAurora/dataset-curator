"""所有 Web 编排共享的模型服务启动锁。"""
import threading

GPU_START_LOCK = threading.Lock()
