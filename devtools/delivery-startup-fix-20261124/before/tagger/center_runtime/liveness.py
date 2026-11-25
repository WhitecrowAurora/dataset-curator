"""执行期间上报存活；网络暂断不停止执行，管理员回收才停止。"""
import threading
import time

from .lease_state import publish

INTERVAL = 30


class Liveness(threading.Thread):
    def __init__(self, cfg, leases, state, api):
        super().__init__(daemon=True, name="center-liveness")
        self.cfg, self.leases, self.state, self.api = cfg, leases, state, api
        self.cancel = threading.Event()
        self.rejected = set()

    def run(self):
        observed = {'last_success_at': None, 'error': '', 'in_flight': False}
        while not self.cancel.is_set():
            observed.update(in_flight=True, next_at=None)
            publish(self.leases, observed)
            try:
                progress = self.state.progress()
                # 预锁不携带当前图片进度，避免把 A 包的计数写进 B 包。
                body = {"leases": [{"lease_id": l["lease_id"],
                                    **({"progress": progress} if l["kind"] == "active" else {})}
                                   for l in self.leases]}
                result = self.api(self.cfg, "/api/v1/heartbeat", body, timeout=8, retries=0)
                observed.update(last_success_at=time.time(), error='')
                self.rejected.update(result.get("rejected", []))
                active = next((l for l in self.leases if l["kind"] == "active"), None)
                if active and active["lease_id"] in self.rejected:
                    self.state.stop.set()
                if not active and any(l['lease_id'] in self.rejected for l in self.leases):
                    self.state.stop.set()
            except Exception:
                observed['error'] = '心跳上报失败，租约没有续期；正在自动重试'
            observed.update(in_flight=False, next_at=time.time() + INTERVAL)
            publish(self.leases, observed)
            if self.cancel.wait(INTERVAL):
                break
        observed.update(in_flight=False, next_at=None)
        publish(self.leases, observed)

    def finish(self):
        self.cancel.set()
        self.join(timeout=10)
