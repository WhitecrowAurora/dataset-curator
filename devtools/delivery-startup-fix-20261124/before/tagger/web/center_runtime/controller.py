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
        from .telemetry import Telemetry
        self.telemetry = Telemetry()
        self.pending = None
        self.probe_result = {}
        self.probing = False

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
                "telemetry": self.telemetry.value, "probe": self.probe_result,
                "pending_action": self.pending[0]["action"] if self.pending else "",
                "resolved": {k: v for k, v in self.resolved.items()
                             if k not in ("extra_args", "cmd_template")},
                "managed": self.session.snapshot() if self.session else None}

    def start(self, *, request_start=True, force=False):
        with self.lock:
            if getattr(self.center, 'leases', None) and self.center.leases.running:
                raise ValueError('正在释放租约，请等待服务器确认')
            if self.active:
                return
            if self.preparing or self.stopping:
                raise ValueError("正在取消上一轮准备，请稍后重试")
            if getattr(self.center,"offline",None) and self.center.offline.running:
                raise ValueError("请先停止离线任务")
            execution_config(self.center.config())
            from ...text_encoding.tokenizer import require_training_text
            require_training_text(self.center.config()['mode'])
            if self.center.history.running:
                raise ValueError("请等待历史检查结束")
            if not self.center.identity().get("device_id"):
                raise ValueError("请先连接调度中心")
            session = ManagedSession(self.center)
            if request_start:
                session.request_start(force=force)
            self.active = True
            self.error = ""
            self.session = session
            self.pending = None
            self.cancel = threading.Event()
            self._capabilities = None
            self.phase("managed_idle", "设备已托管，等待中心指令")

    def resume(self, *, force=False):
        with self.lock:
            if not self.active or not self.session or self.session.closed:
                raise ValueError('请先开始托管')
            if self.working or self.pending or self.preparing or self.stopping:
                raise ValueError('请等待当前执行或调整结束')
            self.session.request_start(force=force)
            self.error = ''
            self.center._wanted = True
            self.center.save_config(wanted=True)
            self.phase('managed_idle', ('正在强制继续' if force else '正在恢复自动接单') + '，等待中心确认')

    def launch(self, command=None):
        with self.lock:
            if getattr(self.center, 'leases', None) and self.center.leases.running:
                raise ValueError('正在释放租约，请等待服务器确认')
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
            if self.pending:
                self.pending[1].record(self.pending[0], "canceled", "已停止，待执行调整已取消")
                self.pending = None
            (self.center.dir / "drain-request").unlink(missing_ok=True)
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
                with session.lock:
                    session._persist()
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
        self.telemetry.refresh()
        if self.pending and not self.working:
            with self.lock:
                if not self.active or self.stopping or not self.pending or self.working:
                    return
                command, session = self.pending
                self.pending = None
                (self.center.dir / "drain-request").unlink(missing_ok=True)
                if command["action"] == "configure":
                    try:
                        if any(not self.center.config().get('allow_remote_'+key,False) for key in command['config']):
                            raise ValueError('本机已关闭对应远程并发授权，调整未应用')
                        self.inference.stop(True)
                        self.center.save_config(**command["config"])
                        self.resolved = {}
                        session.record(command, "applied")
                        self.phase("managed_idle", "并发配置已更新，等待自动接单")
                    except Exception as exc:
                        session.record(command, 'failed', str(exc))
                        self.phase('managed_idle', '并发调整失败，请查看指令结果')
                else:
                    self._launch_probe(command, session)
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
                                      "actions": ["configure", "probe", "force_continue"],
                                      "engine_status": catalog["engine_status"],
                                      "tag_models": [{"backend": x["backend"], "ready": x["ready"]}
                                                     for x in catalog["tag_models"]],
                                      "nl_models": [{k: x.get(k) for k in
                                                     ("key", "raw_ready", "ready", "gguf_ready", "quantized")}
                                                    for x in catalog["nl_models"]]}
                self._capabilities_at = time.monotonic()
            state = {"status": self.status, "message": self.message, "error": self.error,
                     "running": self.center.running, "last_rc": self.center._last_rc,
                     "preparing": self.preparing,
                     "progress": self.center.progress(),
                     "telemetry": self.telemetry.value,
                     "resolved": {k: self.resolved[k] for k in ("concurrency", "tag_batch", "tag_device", "nl_device", "nl_engine", "nl_model") if k in self.resolved},
                     "probe": self.probe_result,
                     "pending_action": self.pending[0]["action"] if self.pending else "",
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
                        if command.get('canceled') is True:
                            session.last_seq = command['seq']
                            session.record(command, 'canceled', '中心已取消旧指令')
                            continue
                        if action == "start" and (self.center.running or self.preparing) and not self.probing:
                            # 轮询响应可能使用启动前快照；重复开始按幂等确认处理。
                            session.last_seq = command["seq"]
                            session.record(command, "applied")
                            continue
                        if action == 'start' and (self.working or self.pending):
                            raise ValueError("设备正在检查或调整，请稍后开始")
                        if action == "check":
                            session.last_seq = command["seq"]
                            session.record(command, "accepted")
                            self.center.history.launch(command, session)
                            continue
                        session.last_seq = command["seq"]
                        session.record(command, "accepted")
                        if action in ("configure", "probe"):
                            if self.pending or self.probing:
                                raise ValueError("已有调整或探测正在执行")
                            self.pending = (command, session)
                            if self.working:
                                (self.center.dir / "drain-request").touch()
                                self.message = "完成当前分片后" + ("调整并发" if action == "configure" else "探测并发")
                            continue
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
                session.failures += 1
                delay = min(30, 5 * 2 ** min(session.failures - 1, 3))
                session.next_poll = time.time() + delay
                if session.failures == 1:
                    self.center._push(f'[center] 调度中心暂时断连，指令结果已保留；稍后自动重连：{session.error}')
        finally:
            self._polling = False

    def _launch_probe(self, command, session):
        from .probe import run
        self.preparing = True
        self.probing = True
        self.probe_result = {}
        self.cancel = threading.Event()
        self.phase("probing", "使用本机已有图片探测并发")
        def execute():
            try:
                self.probe_result = run(self.center, self.inference, self.cancel, self.phase)
                if session:
                    session.record(command, "applied")
                self.phase("managed_idle" if session else "stopped", "并发探测完成；建议值可在模型配置中采用")
            except Cancelled:
                if session:
                    session.record(command, "canceled", "探测已停止")
                self.phase("managed_idle" if session else "stopped")
            except Exception as exc:
                self.probe_result = {"error": self.center.credentials.redact(str(exc))}
                if session:
                    session.record(command, "failed", str(exc))
                self.phase("managed_idle" if session else "stopped", "探测失败，请查看报告")
            finally:
                self.inference.stop(True)
                self.resolved = {}
                self.preparing = False
                self.probing = False
                self._capabilities = None
        self.thread = threading.Thread(target=execute, name="center-probe", daemon=True)
        self.thread.start()

    def probe(self):
        with self.lock:
            if getattr(self.center, 'leases', None) and self.center.leases.running:
                raise ValueError('正在释放租约，请等待服务器确认')
            if self.active or self.working or self.center.offline.running:
                raise ValueError("请先停止本机任务；托管设备由服务端下发探测")
            self._launch_probe(None, None)
        return self.center.snapshot()
