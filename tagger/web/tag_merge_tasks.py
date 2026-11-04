"""合并任务的登记表：把"几十秒的写盘"从 HTTP 请求里挪出去。

为什么需要这一层
----------------
合并十万条就要读写几十 MB，几十万条是分钟级。如果 `POST /merge` 一直挂着
不返回，浏览器只能干等：没有进度、不能切页、超时还会被当成失败——而实际上
后台早就写完了。所以这里只做一件事：起一个线程跑合并，前台拿 task_id 轮询。

为什么不复用 `jobs.py`
----------------------
那套是"打标任务"的：有阶段、有 GPU 保护、有历史落盘、有取消语义，还跟
`run/jobs.json` 绑着。合并是个纯 IO 的一次性动作，套进去等于为了复用而
背上它全部的约定（尤其是"重启后恢复"——合并任务重启后没有任何恢复价值，
重跑一遍就行）。

保留窗口
--------
只留最近 `KEEP_FINISHED` 个已结束的任务。页面拿到 done 之后就不再轮询了，
留着是为了"刷新页面回来还能看到上次的结果"，不是当历史记录用。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

KEEP_FINISHED = 20

_TASKS: dict[str, "MergeTask"] = {}
_LOCK = threading.Lock()

# 进度回调：stage ∈ {"read", "write"}，done/total 是该阶段内的计数。
Tick = Callable[[str, int, int], None]


@dataclass
class MergeTask:
    id: str
    kind: str = "merge"              # merge / check：两者共用这套轮询
    status: str = "running"          # running / done / error
    stage: str = "read"
    done: int = 0
    total: int = 0
    error: str = ""
    report: dict | None = None
    started: float = field(default_factory=time.time)
    finished: float = 0.0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "error": self.error,
            "report": self.report,
            # 让页面能算速率。服务端只给两个时间点，怎么显示交给前端。
            "started": self.started,
            "finished": self.finished,
            "elapsed_s": round((self.finished or time.time()) - self.started, 2),
        }


def _prune() -> None:
    """清掉超额的历史任务。调用方必须已经持有 _LOCK。"""
    done = [(tid, t) for tid, t in _TASKS.items() if t.status != "running"]
    if len(done) <= KEEP_FINISHED:
        return
    done.sort(key=lambda kv: kv[1].finished)
    for tid, _ in done[: len(done) - KEEP_FINISHED]:
        _TASKS.pop(tid, None)


def start(run: Callable[[Tick], object], *, kind: str = "merge") -> MergeTask:
    """在后台跑 `run(tick)`，返回任务句柄。

    `run` 的返回值若是带 `as_dict()` 的对象（MergeReport）就自动转成 dict：
    报告里有 Path、有 float，直接塞进 JSON 前统一过一道更省心。
    """
    task = MergeTask(id=uuid.uuid4().hex[:12], kind=kind)
    with _LOCK:
        _prune()
        _TASKS[task.id] = task

    def tick(stage: str, done: int, total: int) -> None:
        task.stage = stage
        task.done = done
        task.total = total

    def work() -> None:
        try:
            result = run(tick)
            task.report = (result.as_dict() if hasattr(result, "as_dict")
                           else result)  # type: ignore[assignment]
            task.status = "done"
        except BaseException as exc:      # noqa: BLE001 - 线程里不能漏异常
            task.status = "error"
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            task.finished = time.time()

    threading.Thread(target=work, name="tag-merge", daemon=True).start()
    return task


def get(task_id: str) -> MergeTask | None:
    with _LOCK:
        return _TASKS.get(task_id)


def running() -> list[MergeTask]:
    """所有还没结束的任务。底部进度条靠它把合并/自检也显示出来。"""
    with _LOCK:
        return [t for t in _TASKS.values() if t.status == "running"]
