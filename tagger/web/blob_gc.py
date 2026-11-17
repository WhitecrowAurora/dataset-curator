"""孤儿块的定时清扫。

为什么需要它：分块仓库只按内容存块，**没人引用就等于纯粹占盘**。几类块会
自然变成孤儿，而且都不会自己消失：

- 传到一半放弃的上传（客户端算完块、传了一半、没 commit）
- commit 校验失败后重传，旧的那批中间块
- 成品被手工删掉（或 gc 删了 files/ 里的文件），它的块就没人引用了

`gc()` 一定是**先 dry_run 看报告再动手**（不可逆），手动点一次很容易忘。
所以这里做后台定时：默认每 6 小时跑一次真删，只留 48 小时内还在等 commit
的块（`linger_hours`）。

★失败静默：清扫是"顺手做的好事"，它坏了不该让服务报警。但也不能完全没
  痕迹——`last_error` / `last_result` 都挂在快照里，页面和 `/api/blob/stats`
  能查到"上次跑是什么时候、删了多少"。
"""

from __future__ import annotations

import os
import threading
import time

from .blobs import BLOBS, BlobStore

# 默认间隔 6 小时。太快没意义（块从"在途"变成"孤儿"需要时间），太慢盘子
# 会被一批失败的半截上传慢慢吃掉。均可由环境变量覆盖，见 _settings()。
DEFAULT_INTERVAL_H = 6.0
DEFAULT_LINGER_H = 48.0


def _float_env(name: str, default: float) -> float:
    """读一个浮点配置。空/非法 → 默认值；`0` 是合法值（= 关掉），要保留。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val >= 0 else default


def _settings() -> tuple[float, float]:
    """(间隔小时, 宽限小时)。

    `TAGGER_BLOB_GC_INTERVAL_H=0` 关掉定时清扫；宽限 0 = 只留"当前这一刻
    还被引用"的块，等于不看 mtime，对在途上传很危险，所以负数/0 回落到默认。
    """
    interval = _float_env("TAGGER_BLOB_GC_INTERVAL_H", DEFAULT_INTERVAL_H)
    linger = _float_env("TAGGER_BLOB_GC_LINGER_H", DEFAULT_LINGER_H)
    if linger <= 0:
        linger = DEFAULT_LINGER_H
    return interval, linger


class BlobJanitor:
    """定时跑 `BlobStore.gc(dry_run=False)` 的守护线程。

    之所以不用 `sched` / `apscheduler`：这里只需要一个"睡一会儿、干一次活、
    被叫醒就退出"的循环，引一个依赖不值当。线程是 daemon，进程退出自然结束。
    """

    def __init__(self, store: BlobStore) -> None:
        self.store = store
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.interval_h, self.linger_h = _settings()
        self.enabled = self.interval_h > 0
        self.started = 0.0
        self.last_run = 0.0
        self.last_error = ""
        self.last_result: dict = {}
        self.runs = 0

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """幂等：重复调用不会起第二个线程（create_app 可能被多次调用）。"""
        if not self.enabled:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="blob-janitor",
                                            daemon=True)
            self.started = time.time()
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        th = self._thread
        if th is not None:
            th.join(timeout=5)
        self._thread = None

    # ---------------------------------------------------------------- 主循环

    def _loop(self) -> None:
        # 启动后先等一个间隔再扫：服务刚起来时可能有一批上传正在路上，
        # 立刻扫一次纯属添乱（虽然 linger 也会豁免它们，但没必要制造日志）。
        period = self.interval_h * 3600
        while not self._stop.wait(period):
            self.run_once()

    def run_once(self) -> dict:
        """跑一次真删。异常一律吞掉并记进 last_error，绝不往上冒。"""
        try:
            res = self.store.gc(dry_run=False, linger_hours=self.linger_h)
        except Exception as exc:          # noqa: BLE001 - 清扫失败不能影响服务
            with self._lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.last_run = time.time()
                self.runs += 1
            return {"ok": False, "error": self.last_error}
        with self._lock:
            self.last_result = res
            self.last_error = ""
            self.last_run = time.time()
            self.runs += 1
        return {"ok": True, **res}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "interval_hours": self.interval_h,
                "linger_hours": self.linger_h,
                "running": bool(self._thread and self._thread.is_alive()),
                "started": self.started,
                "last_run": self.last_run,
                "last_error": self.last_error,
                "last_result": dict(self.last_result),
                "runs": self.runs,
                "root": str(self.store.root),
            }


# 进程级单例。绑的是 blobs.BLOBS 那个对象（而不是按同一个根另建一个），
# 因为 gc 必须和正在 commit 的那个实例共用一把锁。由 app.py 的 lifespan
# 启动/停止；测试里可以直接 new 一个挂自己的临时仓库。
JANITOR = BlobJanitor(BLOBS)
