"""去重：发分包前的独立扫描，与打标任务并行跑。

单独建模块（不进 jobs.py）的理由与 uploads.py 相同：

1. 去重是磁盘 IO + CPU 的活，不占 GPU——打标跑着的时候照样可以给下一批
   素材去重，所以它不进任务簿的互斥，也不走 WS 事件流。前端每秒轮询，
   进度就够用；几小时的长活按事件流推几十万条 item 事件是浪费。
2. 生命周期不同：没有模型加载、没有 tag/pack 两阶段，就是"扫 → 分组 →
   写产出"一条线。

同一时刻只允许一个去重：两个扫描抢同一批磁盘毫无收益，还会把
hashes.db 的写锁搅在一起。与打标/回传互不排斥。

状态与 run_dedup 的对应：DedupStopped → stopped（哈希已在缓存，重跑
续上）；其他异常 → failed；正常 → done 且带 summary。素材文件全程
只读，没有"资源保护"的需要。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from pathlib import Path

from ..dedup import DedupParams, DedupStopped, run_dedup

LOG_CAP = 200          # 页面日志环形缓冲
RATE_WINDOW = 12.0     # 速率按最近这么多秒的进度估


class DedupJob:
    """一次去重扫描的全部状态。读写跨线程（工作线程 + HTTP 轮询），过锁。"""

    def __init__(self, job_id: str, params: DedupParams):
        self.id = job_id
        self.params = params
        # pending | running | done | stopped | failed
        self.status = "pending"
        self.error = ""
        self.created = time.time()
        self.started = 0.0
        self.ended = 0.0
        self.stop_flag = threading.Event()
        self.scanned = 0
        self.total = 0
        self.current = ""
        self.summary: dict | None = None
        self._logs: deque[str] = deque(maxlen=LOG_CAP)
        self._window: deque[tuple[float, int]] = deque()  # (时刻, 已结算) 估速率
        self._lock = threading.Lock()

    # ---- 回调（跑在工作线程里）

    def _on_progress(self, done: int, total: int, current: str) -> None:
        now = time.time()
        with self._lock:
            self.scanned = done
            self.total = total
            self.current = current
            self._window.append((now, done))
            while len(self._window) > 2 and now - self._window[0][0] > RATE_WINDOW:
                self._window.popleft()

    def _on_log(self, msg: str) -> None:
        with self._lock:
            self._logs.append(msg)

    # ---- 快照

    def snapshot(self) -> dict:
        with self._lock:
            window = list(self._window)
            logs = list(self._logs)
            scanned, total, current = self.scanned, self.total, self.current
            status, error = self.status, self.error
            summary = dict(self.summary) if self.summary else None
        rate = 0.0
        if len(window) >= 2:
            dt = window[-1][0] - window[0][0]
            dd = window[-1][1] - window[0][1]
            if dt > 0 and dd > 0:
                rate = dd / dt
        eta = 0.0
        if rate > 0 and total > scanned:
            eta = (total - scanned) / rate
        return {
            "id": self.id,
            "input_dir": self.params.input_dir,
            "out_dir": self.params.resolved_out_dir(),
            "params": {"mode": self.params.mode, "threshold": self.params.threshold,
                       "keep": self.params.keep,
                       "recursive": self.params.recursive,
                       "workers": self.params.workers},
            "status": status,
            "error": error,
            "created": self.created,
            "started": self.started,
            "ended": self.ended,
            "scanned": scanned,
            "total": total,
            "current": current,
            "rate": round(rate, 1),
            "eta_s": round(eta, 1),
            "logs": logs[-50:],
            "summary": summary,
        }

    # ---- 执行（跑在去重线程里）

    def run(self) -> None:
        self.status = "running"
        self.started = time.time()
        try:
            self.summary = run_dedup(
                self.params,
                on_progress=self._on_progress,
                should_stop=self.stop_flag.is_set,
                on_log=self._on_log,
            )
            self.status = "done"
        except DedupStopped:
            with self._lock:
                self._logs.append("已停止：扫过的哈希都在缓存里，重跑会接着来")
            self.status = "stopped"
        except Exception as exc:          # noqa: BLE001 - 任何意外都落到状态里
            self.error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._logs.append(f"失败：{self.error}")
            self.status = "failed"
        finally:
            self.ended = time.time()


class DedupManager:
    """进程级单例（app.py 持有）。同一时刻只允许一个去重在跑。"""

    def __init__(self) -> None:
        self._jobs: dict[str, DedupJob] = {}
        self._lock = threading.Lock()

    def create(self, *, input_dir: str, out_dir: str = "", mode: str = "similar",
               threshold: int = 8, keep: str = "first",
               recursive: bool = True, workers: int = 8) -> DedupJob:
        if not input_dir:
            raise ValueError("必须给输入目录")
        if not Path(input_dir).exists():
            raise ValueError(f"输入路径不存在: {input_dir}")
        params = DedupParams(
            input_dir=input_dir, out_dir=out_dir or "", mode=mode,
            threshold=int(threshold), keep=keep, recursive=bool(recursive),
            workers=int(workers))
        params.validate()   # ValueError 带中文原因，端点转 400
        with self._lock:
            running = [j for j in self._jobs.values()
                       if j.status in ("pending", "running")]
            if running:
                raise RuntimeError("已有去重在跑。等它结束或先停止。")
            job = DedupJob(uuid.uuid4().hex[:12], params)
            self._jobs[job.id] = job
        thread = threading.Thread(target=job.run, name=f"dedup-{job.id}",
                                  daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> DedupJob | None:
        return self._jobs.get(job_id)

    def stop(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status not in ("pending", "running"):
            return False
        job.stop_flag.set()
        return True


# app.py 持有的进程级单例。与 MANAGER / UPLOADS 同一模式。
DEDUPS = DedupManager()
