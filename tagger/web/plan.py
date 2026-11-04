"""计划任务：跑到 N 张之后自动停止或关机。

为什么放服务端而不是浏览器：动作要落到正在跑的任务上，而且用户关掉页面、
锁屏、甚至在手机上看进度时也得生效。浏览器里放个定时器，页面一关就没了。

为什么关机要留缓冲：`shutdown /t 60` 给 60 秒，这段时间里产物还在落盘
（停止任务后写盘不是瞬时的），也能用 `shutdown /a` 撤销。不留缓冲等于
赌"停止任务一定立刻写完"——赌输了这一批产物就是半个文件。

状态写 `run/plan.json`：服务重启后计划还在。触发过就记 `fired`，
不会因为重启又关一次机。
"""

from __future__ import annotations

import json
import platform
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class Plan:
    enabled: bool = False
    # 已完成多少张后触发。0 = 未设置
    after_images: int = 0
    # stop = 停掉当前打标任务；shutdown = 停掉后关机
    action: str = "stop"
    fired: bool = False
    fired_at: float = 0.0
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class _State:
    plan: Plan = field(default_factory=Plan)


class PlanWatcher:
    """进程内单例：一个后台线程盯着当前任务，到点执行一次。"""

    def __init__(self, state_path: str | Path | None = None, *,
                 running_job: Callable[[], object | None] | None = None,
                 stop_job: Callable[[str], bool] | None = None,
                 interval: float = 2.0, dry_run: bool = False) -> None:
        self._path = Path(state_path) if state_path else None
        self._running_job = running_job or _default_running
        self._stop_job = stop_job or _default_stop
        self._interval = max(0.5, interval)
        self._dry_run = dry_run
        self._lock = threading.Lock()
        self._plan = Plan()
        self._stop = threading.Event()
        self._load()
        self._thread = threading.Thread(target=self._loop, name="plan-watcher",
                                        daemon=True)
        self._thread.start()

    # ---- 对外

    def snapshot(self) -> dict:
        with self._lock:
            plan = Plan(**asdict(self._plan))
        # 顺带把"还差多少"算给前端，省得页面自己再拉一次任务
        done = total = 0
        job = self._running_job()
        if job is not None:
            try:
                counts = (job.snapshot() or {}).get("counts") or {}  # type: ignore[attr-defined]
                done = int(counts.get("done") or 0)
                total = int(counts.get("total") or 0)
            except (AttributeError, TypeError, ValueError):
                pass
        return {
            "plan": plan.as_dict(),
            "running": job is not None,
            "done": done,
            "total": total,
            "remaining": max(0, plan.after_images - done) if plan.enabled else 0,
        }

    def configure(self, *, after_images: int, action: str,
                  enabled: bool = True) -> dict:
        after = int(after_images)
        if after < 1:
            raise ValueError("触发张数至少 1")
        if action not in ("stop", "shutdown"):
            raise ValueError("动作只能是 stop / shutdown")
        with self._lock:
            self._plan = Plan(enabled=bool(enabled), after_images=after,
                              action=action)
        self._save()
        return self.snapshot()

    def cancel(self) -> dict:
        with self._lock:
            self._plan.enabled = False
            self._plan.fired = False
            self._plan.fired_at = 0.0
            self._plan.note = ""
        self._save()
        return self.snapshot()

    def close(self) -> None:
        self._stop.set()

    # ---- 内部

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._check()
            except Exception as exc:  # noqa: BLE001 - 守护线程不能死
                with self._lock:
                    self._plan.note = f"检查失败：{type(exc).__name__}: {exc}"

    def _check(self) -> None:
        with self._lock:
            if not self._plan.enabled or self._plan.fired:
                return
            plan = Plan(**asdict(self._plan))

        job = self._running_job()
        if job is None:
            return
        try:
            snap = job.snapshot() or {}  # type: ignore[attr-defined]
            done = int((snap.get("counts") or {}).get("done") or 0)
            job_id = str(snap.get("id") or "")
        except (AttributeError, TypeError, ValueError):
            return
        if done < plan.after_images or not job_id:
            return

        # 先钉上 fired 再动作：关机命令返回后进程可能立刻没了，
        # 晚写这一行就等于下次启动再关一次。
        with self._lock:
            self._plan.fired = True
            self._plan.fired_at = time.time()
        notes = [f"已完成 {done} 张，触发"]
        try:
            stopped = self._stop_job(job_id)
            notes.append("任务已停止" if stopped else "任务已自行结束")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"停止失败：{type(exc).__name__}: {exc}")
        if plan.action == "shutdown":
            notes.append(self._shutdown())
        with self._lock:
            self._plan.note = "；".join(notes)
        self._save()

    def _shutdown(self) -> str:
        if self._dry_run:
            return "dry-run：未真的关机"
        system = platform.system()
        try:
            if system == "Windows":
                subprocess.Popen(
                    ["shutdown", "/s", "/t", "60", "/c", "tagger 计划任务"],
                    close_fds=True)
                return "已安排 60 秒后关机（shutdown /a 可取消）"
            if system == "Darwin":
                subprocess.Popen(
                    ["osascript", "-e", 'tell app "System Events" to shut down'],
                    close_fds=True)
                return "已发出关机指令"
            subprocess.Popen(["shutdown", "-h", "+1"], close_fds=True)
            return "已安排 1 分钟后关机（shutdown -c 可取消）"
        except OSError as exc:
            return f"关机命令失败：{exc}"

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._plan = Plan(
                enabled=bool(raw.get("enabled")),
                after_images=int(raw.get("after_images") or 0),
                action=str(raw.get("action") or "stop"),
                fired=bool(raw.get("fired")),
                fired_at=float(raw.get("fired_at") or 0.0),
                note=str(raw.get("note") or ""),
            )
        except (OSError, ValueError, TypeError):
            # 文件坏了就当没设置：宁可少一次自动关机，不要因为脏数据关错
            self._plan = Plan()

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._plan.as_dict(), ensure_ascii=False,
                                      indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass


def _default_running() -> object | None:
    from .jobs import MANAGER

    return MANAGER.running()


def _default_stop(job_id: str) -> bool:
    from .jobs import MANAGER

    return MANAGER.stop(job_id)
