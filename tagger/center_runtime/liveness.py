"""执行期间上报存活；网络暂断不停止执行，管理员回收才停止。"""
import threading


class Liveness(threading.Thread):
    def __init__(self, cfg, leases, state, api):
        super().__init__(daemon=True, name="center-liveness")
        self.cfg, self.leases, self.state, self.api = cfg, leases, state, api
        self.cancel = threading.Event()
        self.rejected = set()

    def run(self):
        while not self.cancel.is_set():
            try:
                body = {"leases": [{"lease_id": l["lease_id"]} for l in self.leases]}
                result = self.api(self.cfg, "/api/v1/heartbeat", body, timeout=8, retries=0)
                self.rejected.update(result.get("rejected", []))
                active = next((l for l in self.leases if l["kind"] == "active"), None)
                if active and active["lease_id"] in self.rejected:
                    self.state.stop.set()
                if not active and any(l['lease_id'] in self.rejected for l in self.leases):
                    self.state.stop.set()
            except Exception:
                pass
            if self.cancel.wait(30):
                break

    def finish(self):
        self.cancel.set()
        self.join(timeout=10)
