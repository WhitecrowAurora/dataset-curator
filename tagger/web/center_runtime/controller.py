"""主动开工与被动托管共用的执行编排。"""
from __future__ import annotations

import threading
import time

from .config import execution_config
from .inference import Cancelled, InferenceRuntime, options
from .managed import ManagedSession


class Controller:
    def __init__(self, center):
        self.center = center
        self.lock = threading.RLock()
        self.active = False
        self.preparing = False
        self.stopping = False
        self.status = "stopped"
        self.message = ""
        self.error = ""
        self.cancel = threading.Event()
        self.thread = None
        self.session: ManagedSession | None = None
        self.inference = InferenceRuntime(center._push, self.phase)
        self.resolved = {}
        self._capabilities = None
        self._capabilities_at = 0.0
        self._polling = False

    @property
    def working(self):
        return self.preparing or self.stopping or self.center.running or bool(self.center._next_spawn) or self.center.history.running

    def phase(self, status, message=""):
        with self.lock:
            self.status, self.message = status, message
        if message:
            self.center._push(f"[center] {message}")

    def snapshot(self):
        cfg = self.center.config()
        return {"active": self.active, "status": self.status, "message": self.message,
                "error": self.error, "preparing": self.preparing,
                "hosting_mode": cfg["hosting_mode"],
                "resolved": {k: v for k, v in self.resolved.items()
                             if k not in ("extra_args", "cmd_template")},
                "managed": self.session.snapshot() if self.session else None}

    def start(self):
        with self.lock:
            if self.active:
                return
            if self.preparing or self.stopping:
                raise ValueError("正在取消上一轮准备，请稍后重试")
            if getattr(self.center,"offline",None) and self.center.offline.running:
                raise ValueError("请先停止离线任务")
            execution_config(self.center.config())
            if self.center.history.running:
                raise ValueError("请等待历史检查结束")
            if not self.center.identity().get("device_id"):
                raise ValueError("请先连接调度中心")
            self.active = True
            self.error = ""
            self.session = None
            self.cancel = threading.Event()
            self._capabilities = None
            if self.center.config()["hosting_mode"] == "passive":
                self.session = ManagedSession(self.center)
                self.phase("managed_idle", "设备已托管，等待中心指令")
            else:
                self.launch()

    def launch(self, command=None):
        with self.lock:
            if self.preparing or self.stopping or self.center.running:
                raise ValueError("设备已在准备或执行")
            self.preparing = True
            self.error = ""
            self.cancel = threading.Event()
            self.center._wanted = True
            self.center._next_spawn = 0.0
            cfg = self.center.config()
            cancel = self.cancel
            self.thread = threading.Thread(target=self._prepare, args=(cfg, cancel, command, self.session),
                                           name="center-prepare", daemon=True)
            self.thread.start()

    def _prepare(self, cfg, cancel, command, session):
        try:
            self.phase("preparing", "准备本机打标配置")
            resolved = self.inference.prepare(cfg, self.center.credentials.effective_token(), cancel)
            with self.center._lock:
                if cancel.is_set() or not self.active:
                    raise Cancelled("已停止")
                self.resolved = resolved
                self.center._spawn(resolved)
            self.phase("running", "模型已就绪，开始领片")
            self._capabilities = None
            if command and session:
                session.record(command, "applied")
        except Cancelled:
            self.inference.stop(cfg["stop_service"])
            if command and session:
                session.record(command, "canceled", "本机已停止")
        except Exception as exc:
            self.error = self.center.credentials.redact(str(exc))
            self.phase("failed", self.error)
            self.center._wanted = False
            self.inference.stop()
            if not session:
                self.active = False
                self.center.save_config(wanted=False)
            if command and session:
                session.record(command, "failed", self.error)
        finally:
            self.preparing = False
            if not self.active and self.status != "failed":
                self.phase("stopped")
            elif cancel.is_set() and not self.stopping:
                self.phase("managed_idle" if session else "stopped")

    def stop_work(self):
        with self.lock:
            self.stopping = True
            self.cancel.set()
            self.center.history.cancel.set()
        try:
            self.center._stop_worker()
            self.inference.stop(self.center.config()["stop_service"])
            history_thread = self.center.history.thread
            if history_thread and history_thread is not threading.current_thread() and history_thread.is_alive():
                history_thread.join(timeout=10)
            thread = self.thread
            if thread and thread is not threading.current_thread() and thread.is_alive():
                thread.join(timeout=10)
            if thread and thread.is_alive():
                self.phase("stopping", "正在等待模型准备取消")
            else:
                self.phase("managed_idle" if self.active and self.session else "stopped")
        finally:
            self.stopping = False

    def stop(self):
        with self.lock:
            self.active = False
            self.stopping = True
            self.cancel.set()
            session = self.session
            if session:
                session.closed = True
        self.stop_work()
        if session:
            session.status = "released"
            threading.Thread(target=self._release, args=(session,),
                             name="center-release", daemon=True).start()
        self.center.save_config(wanted=False)

    def _release(self, session):
        try:
            session.poll({"status": "released", "running": False}, {}, released=True)
        except Exception as exc:
            self.center._push(f"[center] 退出托管通知未送达：{exc}")

    def tick(self):
        if not self.active or self.stopping:
            return
        if self.center.running and not self.inference.healthy():
            self.center._push("[center] 模型服务已退出，停止当前任务并准备恢复")
            self.center._stop_worker(graceful=False)
            if not self.active or self.stopping:
                return
            self.center._wanted = True
            self.center._schedule_restart(1)
            self.phase("recovering", "等待恢复模型服务")
        if not self.working:
            if self.status in ("running", "recovering"):
                self.inference.stop(self.center.config()["stop_service"])
                failed = self.center._last_rc not in (None, 0)
                if self.session:
                    self.phase("failed" if failed else "managed_idle",
                               "本轮执行失败，等待中心指令" if failed else "本轮已结束，等待中心指令")
                else:
                    self.active = False
                    self.center.save_config(wanted=False)
                    self.phase("failed" if failed else "stopped",
                               "本轮执行失败，请查看日志" if failed else "本轮已结束")
        if self.session and time.time() >= self.session.next_poll and not self._polling:
            self.session.next_poll = time.time() + (30 if self.session.status == "unsupported" else 5)
            self._polling = True
            threading.Thread(target=self._poll, args=(self.session,),
                             name="center-managed", daemon=True).start()

    def _poll(self, session):
        try:
            if self._capabilities is None or time.monotonic() - self._capabilities_at >= 30:
                catalog = options()
                self._capabilities = {"devices": catalog["devices"], "engines": catalog["engines"],
                                      "engine_status": catalog["engine_status"],
                                      "tag_models": [{"backend": x["backend"], "ready": x["ready"]}
                                                     for x in catalog["tag_models"]],
                                      "nl_models": [{k: x.get(k) for k in
                                                     ("key", "raw_ready", "ready", "gguf_ready", "quantized")}
                                                    for x in catalog["nl_models"]]}
                self._capabilities_at = time.monotonic()
            state = {"status": self.status, "message": self.message, "error": self.error,
                     "running": self.center.running, "last_rc": self.center._last_rc,
                     "progress": self.center.progress(),
                     "history_running": self.center.history.running,
                     "history_status": self.center.history.status,
                     "history_message": self.center.history.message}
            commands = session.poll(state, self._capabilities)
            for command in commands:
                with self.lock:
                    if not self.active or self.session is not session:
                        return
                    try:
                        validated = session.validate(command)
                        if validated is None:
                            continue
                        action, changes = validated
                        if action == "start" and self.working:
                            raise ValueError("设备已在执行，请先停止")
                        if action == "check":
                            session.last_seq = command["seq"]
                            session.record(command, "accepted")
                            self.center.history.launch(command, session)
                            continue
                        session.last_seq = command["seq"]
                        session.record(command, "accepted")
                        if action == "start":
                            self.launch(command)
                        elif action == "stop":
                            self.stopping = True
                            self.cancel.set()
                        elif action != "stop":
                            session.record(command, "applied")
                    except Exception as exc:
                        if (isinstance(command, dict) and isinstance(command.get("id"), str)
                                and command.get("session_id") == session.id
                                and type(command.get("seq")) is int
                                and command["seq"] in (session.last_seq, session.last_seq + 1)):
                            session.last_seq = command["seq"]
                            session.record(command, "failed", str(exc))
                            continue
                        else:
                            raise
                if action == "stop":
                    self.stop_work()
                    session.record(command, "applied")
        except Exception as exc:
            if session and not session.closed:
                session.status = "offline"
                session.error = self.center.credentials.redact(str(exc))
        finally:
            self._polling = False
