"""单个复核 VLM 的生命周期；阶段间释放显存，权重按内存预算缓存。"""
from __future__ import annotations

import os
import socket
import subprocess
from contextlib import nullcontext
from pathlib import Path

from ..llama_runtime import find_binary, server_env
from ..model_loader import wait_ready
from ..runtime.model_cache import MODELS, WeightFiles
from ..runtime.processes import spawn_owned


class ReviewRuntime:
    def __init__(self, root):
        self.root = Path(root)
        self.process = None
        self.kind = ""
        self.url = ""
        self.before_start = None
        self.startup_lock = None
        self.fingerprint = {}
        self.identity = None

    def stop(self):
        process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        self.process = None
        self.kind = ""
        self.url = ""
        self.identity = None
        self.fingerprint = {}

    def endpoint(self, kind, settings, *, slots=1, device=None, port=None, on_tick=None, log_path=None):
        with self.startup_lock or nullcontext():
            return self._endpoint(kind, settings, slots=slots, device=device, port=port,
                                  on_tick=on_tick, log_path=log_path)

    def _endpoint(self, kind, settings, *, slots=1, device=None, port=None, on_tick=None, log_path=None):
        if on_tick:
            on_tick(0)
        url = getattr(settings, kind + "_url")
        if url:
            self.stop()
            self.fingerprint = {"provider": "local", "model_id": getattr(settings, kind + "_model"),
                                "engine": "external", "model_path": "unknown", "precision": "unknown"}
            return url, getattr(settings, kind + "_model")
        directory = Path(getattr(settings, kind + "_dir"))
        if not directory.is_absolute():
            directory = self.root / directory
        selected = directory if directory.is_file() else None
        directory = selected.parent if selected else directory
        files = sorted(directory.glob("*.gguf"))
        weights = [selected] if selected else [p for p in files if "mmproj" not in p.name.lower()]
        projectors = [p for p in files if "mmproj" in p.name.lower()]
        if len(weights) != 1 or len(projectors) != 1:
            raise ValueError(f"{kind} 需要明确的 GGUF 权重和 mmproj；目录有多个版本时请直接选择 GGUF 文件：{directory}")
        model = getattr(settings, kind + "_model")
        native = kind == "torii" and settings.torii_engine == "native"
        from ..inference.qwen35.runtime import MAX_BATCH_SIZE
        slots = min(MAX_BATCH_SIZE, max(1, int(slots))) if native else 1
        identity = (kind, model, native, slots, device, port, tuple((str(p.resolve()), p.stat().st_size, p.stat().st_mtime_ns)
                                      for p in weights + projectors))
        if self.identity == identity and self.process and self.process.poll() is None:
            return self.url, model
        self.stop()
        if self.before_start:
            self.before_start()
        binary = None if native else find_binary()
        if not native and binary is None:
            raise ValueError("请先在模型管理中安装 llama.cpp")
        binary = Path(binary) if binary else None
        paths = weights + projectors
        key = "weights:" + str(weights[0].resolve())
        size = sum(p.stat().st_size for p in paths)
        _, free, reserve, budget = MODELS._budget()
        if MODELS.enabled and size <= budget and free - size >= reserve:
            with MODELS.acquire(key, lambda: WeightFiles(paths), size, lambda v: v.close(), "mapped_weights") as cache:
                cache.warm()
        child_env = dict(os.environ)
        worker_device = "auto"
        if device is not None:
            from ..hardware import visible_gpus
            gpus = visible_gpus()
            if device == "cpu" or (device == "auto" and not gpus):
                worker_device = "cpu"
                child_env["CUDA_VISIBLE_DEVICES"] = "-1"
            else:
                index = 0 if device == "auto" else int(device.removeprefix("cuda:"))
                if index >= len(gpus):
                    raise ValueError(f"设备 {device} 不存在")
                child_env["CUDA_VISIBLE_DEVICES"] = str(gpus[index].index)
                worker_device = "cuda:0"
        if port is None:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
        command = [str(binary), "-m", str(weights[0]), "--mmproj", str(projectors[0]),
                   "--host", "127.0.0.1", "--port", str(port), "--alias", model,
                   "-ngl", "0" if worker_device == "cpu" else "99", "-np", "1", "-c", "8192" if kind == "torii" else "4096",
                   "--image-min-tokens", "1024", "--image-max-tokens", "4096"]
        if worker_device == "cpu":
            command.append("--no-mmproj-offload")
        if kind == "torii":
            template = directory / "chat_template.jinja"
            if not template.is_file():
                template = Path(__file__).with_name("torii_chat_template.jinja")
            command += ["--jinja", "--chat-template-file", str(template)]
        native_info = None
        if native:
            from ..inference.qwen35.runtime import build_command
            from ..runtime.kernel_cache import environment as kernel_environment
            child_env = kernel_environment(self.root, child_env)
            child_env['PYTHONIOENCODING'] = 'utf-8'
            command, native_info = build_command(weights[0], projectors[0], model, port, slots=slots, device=worker_device)
        log = Path(log_path) if log_path else self.root / "run" / f"hair-{kind}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        if on_tick:
            on_tick(0)
        with log.open("a", encoding="utf-8") as out:
            self.process = spawn_owned(command, stdout=out, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                       env=dict(child_env, PYTHONUTF8="1") if native else server_env(str(binary), child_env),
                                       cwd=str(self.root))
        self.url = f"http://127.0.0.1:{port}"
        process = self.process
        def tick(remaining):
            if on_tick:
                on_tick(remaining)
            if process.poll() is not None:
                raise RuntimeError(f"{kind} 服务已退出（code={process.returncode}），请查看 {log}")
        try:
            wait_ready(self.url, timeout_s=300 if native else 120, on_tick=tick)
            tick(0)
            if native:
                import httpx
                response = httpx.get(self.url + "/version", timeout=10, trust_env=False)
                response.raise_for_status()
                native_info["execution"] = response.json().get("runtime", {})
        except BaseException:
            self.stop()
            raise
        self.kind = kind
        self.identity = identity
        execution = native_info.get("execution", {}) if native else {}
        self.fingerprint = {"provider": "local", "model_id": model, "engine": "native" if native else "llamacpp",
                            "slots": slots,
                            "model_path": str(weights[0].resolve()), "precision": execution.get("precision", "dequantized_fp16_or_fp32") if native else weights[0].stem,
                            "device": device or execution.get("device", "auto"), "max_pixels": execution.get("max_pixels", 1_000_000) if kind == "torii" else 0,
                            "native_runtime": native_info or {"binary": str(binary), "size": binary.stat().st_size,
                                               "mtime_ns": binary.stat().st_mtime_ns},
                            "model_revision": native_info["model_revision"] if native_info else None}
        return self.url, model
