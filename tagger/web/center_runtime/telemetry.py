"""调度遥测复用现有硬件读数，采样在后台完成。"""
import threading
import time


class Telemetry:
    def __init__(self):
        self.value = {}
        self.at = 0.0
        self.sampling = False

    def refresh(self):
        if self.sampling or time.monotonic() - self.at < 5:
            return
        self.sampling = True
        threading.Thread(target=self._sample, name="center-telemetry", daemon=True).start()

    def _sample(self):
        try:
            from ..multi_gpu import read_metrics
            from ...hardware import visible_gpus
            from ..sysinfo import cpu_percent

            names = {g.index: (i, g.name) for i, g in enumerate(visible_gpus())}
            self.value = {"sampled_at": time.time(), "cpu_percent": cpu_percent(),
                          "gpus": [{"physical_index": index, "device": f"cuda:{names[index][0]}",
                                    "name": names[index][1], **metrics}
                                   for index, metrics in read_metrics().items() if index in names]}
        except Exception:
            self.value = {"sampled_at": time.time(), "gpus": []}
        finally:
            self.at = time.monotonic()
            self.sampling = False
