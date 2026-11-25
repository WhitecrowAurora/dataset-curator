"""复用模型管理与服务生命周期，为领片准备本机推理资源。"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from urllib.parse import urlsplit

from ...config import ConfigError, DEFAULT_TIERS, load_tier_table, resolve_profile
from ...hardware import visible_gpus
from ... import nl_engine
from .. import models, downloads
from ..gpu_start import GPU_START_LOCK
from ..vllm_service import VLLM


class Cancelled(RuntimeError):
    pass


def options() -> dict:
    catalog = models.catalog()
    for item in catalog["vlm"]:
        item.update(models.weight_summary(item, Path(item["local_abs"])))
    from ..torii_service import model_entry
    catalog["vlm"].append(model_entry())
    engine_status = {engine: dict(zip(("available", "reason"), VLLM.local_capability(engine)))
                     for engine in nl_engine.ENGINES}
    return {"tag_models": catalog["tag"], "nl_models": catalog["vlm"],
            "devices": [{"value": f"cuda:{i}", "physical_index": g.index,
                         "name": g.name, "vram_gb": g.vram_gb}
                        for i, g in enumerate(visible_gpus())],
            "engines": [engine for engine, status in engine_status.items() if status["available"]],
            "engine_status": engine_status}


class InferenceRuntime:
    def __init__(self, log, phase):
        self.log, self.phase = log, phase
        self.owned_started: float | None = None
        self.download: tuple[str, str] | None = None
        self.quantizer = None
        self.local_required = False
        self.service_started: float | None = None

    def _wait(self, cancel, snapshot, label):
        while True:
            if cancel.is_set():
                raise Cancelled("已停止准备")
            state = snapshot() or {}
            status = state.get("status")
            if status in ("done", "ready"):
                return state
            if status in ("error", "failed", "canceled", "stopped"):
                raise ConfigError(f"{label}失败：{state.get('message') or state.get('error') or status}")
            if cancel.wait(0.5):
                raise Cancelled("已停止准备")

    def _download(self, kind, key, cfg, token, cancel):
        if cancel.is_set():
            raise Cancelled("已停止准备")
        if not cfg["auto_download"]:
            raise ConfigError(f"{key} 缺少权重，请在模型管理中准备，或开启自动下载")
        existing = downloads.DOWNLOADS.state_of(kind, key)
        owned = not existing or existing["status"] not in ("queued", "running")
        self.phase("downloading", f"下载 {key}")
        if kind == "review":
            from ..review_models import start_download
            start_download(key, token=token or None)
        else:
            models.start_download(kind, key, token=token or None)
        self.download = (kind, key) if owned else None
        try:
            self._wait(cancel, lambda: downloads.DOWNLOADS.state_of(kind, key), "模型下载")
        finally:
            if cancel.is_set() and owned:
                downloads.DOWNLOADS.cancel(kind, key)
            self.download = None

    def prepare(self, cfg: dict, token: str, cancel) -> dict:
        cfg = dict(cfg)
        from ...text_encoding.tokenizer import require_training_text
        if cancel.is_set():
            raise Cancelled("已停止准备")
        from ...config import load_ops
        from ...runtime.disk_space import require_space
        require_space(load_ops(load_tier_table(DEFAULT_TIERS)).guard_policy(), cfg['out_dir'])
        require_training_text(cfg['mode'])
        from ..torii_service import KEY as TORII_KEY
        from ...inference.torii_caption import is_torii
        torii = cfg["nl_model"] == TORII_KEY if cfg["inference_source"] == "local" else is_torii(cfg["served_name"])
        cfg["caption_format"] = "torii" if torii else ""
        if torii and cfg["mode"] == "both" and cfg["tag_backend"] == "none":
            raise ConfigError("Torii 描述模式请配合 PixAI 等分类模型生成标签")
        automatic_tag = cfg["tag_device"] == "auto"
        self.local_required = cfg["mode"] != "tag" and cfg["inference_source"] == "local"
        if cancel.is_set():
            raise Cancelled("已停止准备")
        from ..jobs import MANAGER
        if MANAGER.running() is not None:
            raise ConfigError("已有本机打标任务正在运行，请先停止")
        catalog = models.catalog()
        if cfg["mode"] != "nl" and cfg["tag_backend"] != "none":
            item = next((x for x in catalog["tag"] if x["backend"] == cfg["tag_backend"]), None)
            if item is None:
                raise ConfigError("所选 TAG 模型不在模型清单中")
            if not item["ready"]:
                self._download("tag", cfg["tag_backend"], cfg, token, cancel)
                item = models._entry("tag", cfg["tag_backend"])
            cfg["tag_model_dir"] = item["local_abs"]
            cfg["tag_model"] = item.get("repo")
        gpus = visible_gpus()
        if cfg["tag_device"].startswith("cuda:") and int(cfg["tag_device"][5:]) >= len(gpus):
            raise ConfigError(f"设备 {cfg['tag_device']} 不存在")
        if cfg["mode"] == "tag" and cfg["tag_device"] == "auto":
            cfg["tag_device"] = "cuda:0" if gpus else "cpu"
        if self.local_required and torii:
            self._prepare_torii(cfg, token, cancel, gpus)
        elif self.local_required:
            if cfg["nl_engine"] in ("llamacpp", "native"):
                if cfg["nl_engine"] == "native":
                    from ...inference.qwen3vl.runtime import resolve_device_profile
                else:
                    from ..service_start import resolve_device_profile
                profile, physical_index = resolve_device_profile(cfg["nl_device"],
                    model_override=cfg["nl_model"] or None, precision_override=cfg["nl_precision"],
                    max_model_len_override=cfg.get('max_model_len') or None,
                    gpu_mem_override=cfg["gpu_mem"] or None)
                idx = next((i for i, g in enumerate(gpus) if g.index == physical_index), 0)
                gpu = gpus[idx] if gpus and profile.device_name != "CPU" else None
            elif not gpus:
                raise ConfigError("没有可用的 NVIDIA 显卡")
            else:
                idx = 0 if cfg["nl_device"] == "auto" else int(cfg["nl_device"][5:])
                if idx >= len(gpus):
                    raise ConfigError(f"设备 {cfg['nl_device']} 不存在")
                gpu = gpus[idx]
                profile = resolve_profile(load_tier_table(DEFAULT_TIERS), gpu,
                                      model_override=cfg["nl_model"] or None,
                                      precision_override=cfg["nl_precision"],
                                      max_model_len_override=cfg.get('max_model_len') or None,
                                      gpu_mem_override=cfg["gpu_mem"] or None)
            cfg["nl_model"] = profile.model.key
            self._prepare_weights(cfg, profile, token, cancel)
            if cfg["nl_engine"] in ("llamacpp", "native"):
                profile, _ = resolve_device_profile(cfg["nl_device"],
                    model_override=cfg["nl_model"], precision_override=cfg["nl_precision"],
                    max_model_len_override=cfg.get('max_model_len') or None,
                    gpu_mem_override=cfg["gpu_mem"] or None)
            elif gpu is not None:
                profile = resolve_profile(load_tier_table(DEFAULT_TIERS), gpu,
                                      model_override=cfg["nl_model"],
                                      precision_override=cfg["nl_precision"],
                                      max_model_len_override=cfg.get('max_model_len') or None,
                                      gpu_mem_override=cfg["gpu_mem"] or None)
            # 同卡为分类模型预留显存；只调整并发与资源，不改变精度。
            if cfg["tag_device"] == "auto" and cfg["mode"] == "both":
                other = next((i for i in range(len(gpus)) if i != idx), None)
                cfg["tag_device"] = f"cuda:{other if other is not None else idx}" if gpus else "cpu"
            same_card = gpu is not None and cfg["mode"] == "both" and cfg["tag_backend"] != "none" and cfg["tag_device"] == f"cuda:{idx}"
            seqs = cfg["concurrency"]
            if not seqs:
                if cfg["nl_engine"] == "llamacpp":
                    budget = (gpu.vram_gb if gpu is not None else 0) * (cfg["gpu_mem"] or (0.8 if same_card else 0.9))
                    plan_profile = dataclasses.replace(profile, slots=0)
                    automatic = nl_engine.slot_plan(plan_profile, budget)[0]
                    gguf, mmproj = nl_engine.resolve_weights(plan_profile)
                    weights_gb = (gguf.stat().st_size + (mmproj.stat().st_size if mmproj else 0)) / 1024**3
                    per_slot_gb = nl_engine.kv_bytes_per_slot(profile) / 1024**3
                    fitting = int(max(0, budget - weights_gb - 1) / per_slot_gb)
                    seqs = max(1, min(automatic, fitting))
                else:
                    seqs = min(64, profile.max_num_seqs)
            cfg["concurrency"] = seqs
            cfg["gpu_mem"] = cfg["gpu_mem"] or min(profile.gpu_memory_utilization, 0.8 if same_card else 0.9)
            if cfg["nl_engine"] == "native":
                cfg["gpu_mem"] = 0
            opts = {"engine": cfg["nl_engine"], "model": cfg["nl_model"],
                    "precision": cfg["nl_precision"], "served_name": cfg["served_name"],
                    # 执行配置用 0 表示自动／不适用；服务启动契约用 None。
                    "device_index": gpu.index if gpu is not None else None, "gpu_mem": cfg["gpu_mem"] or None,
                    "slots": seqs if cfg["nl_engine"] in ("llamacpp", "native") else None,
                    "max_num_seqs": seqs if cfg["nl_engine"] == "vllm" else None}
            if cfg["nl_engine"] in ("llamacpp", "native"):
                opts["device"] = cfg["nl_device"]
            if cfg.get('max_model_len'):
                opts['max_model_len'] = cfg['max_model_len']
            if cfg["nl_engine"] in ("vllm", "native"):
                path = (profile.weights_path if cfg["nl_precision"] == "w4a16"
                        else models._entry("vlm", cfg["nl_model"])["local_abs"])
                local = Path(path)
                opts["model_path"] = str((local if local.is_absolute() else downloads.ROOT / local).resolve())
            self._start_service(cfg, opts, cancel)
        elif cfg["mode"] != "tag":
            self.phase("checking_service", "连接已有模型服务")
            probe = VLLM.probe(cfg["tag_url"])
            if not probe.get("ok"):
                raise ConfigError(probe.get("error") or "已有服务无法连接")
            cfg["concurrency"] = cfg["concurrency"] or 2
        else:
            cfg["concurrency"] = cfg["concurrency"] or max(2, cfg["tag_batch"] or 4)
        if cfg["tag_device"] == "auto":
            cfg["tag_device"] = "cuda:0" if gpus else "cpu"
        cfg["tag_batch"] = cfg["tag_batch"] or (4 if cfg["tag_device"].startswith("cuda:") else 1)
        if cfg["mode"] != "nl" and cfg["tag_backend"] != "none":
            from .classifier_check import check

            self.phase("loading_classifier", "检查 TAG 模型加载")
            try:
                check(cfg, token, cancel)
            except ConfigError as exc:
                from ...inference.devices import resource_failure
                if not automatic_tag or not cfg["tag_device"].startswith("cuda:") or not resource_failure(str(exc)):
                    raise
                self.log("TAG 模型无法在 GPU 加载，回退 CPU。")
                cfg["tag_device"] = "cpu"
                check(cfg, token, cancel)
        if cancel.is_set():
            raise Cancelled("已停止准备")
        return cfg

    def _prepare_torii(self, cfg, token, cancel, gpus):
        from .. import torii_service

        plan = torii_service.plan(cfg["nl_engine"], cfg["nl_precision"], cfg["concurrency"])
        if not plan["ok"]:
            raise ConfigError(plan["error"])
        item = torii_service.model_entry()
        if not item["ready"]:
            configured = Path(item["configured_path"])
            configured = configured if configured.is_absolute() else downloads.ROOT / configured
            standard = Path(item["local_abs"])
            if configured.exists() and configured.resolve() not in (standard.resolve(), Path(item["use_path"]).resolve()):
                raise ConfigError("所选 Torii 路径缺少 Q6_K 权重或 FP16 mmproj，请在模型管理中检查：" + str(configured))
            self._download("review", torii_service.KEY, cfg, token, cancel)
            item = torii_service.model_entry()
            if not item["ready"]:
                raise ConfigError("Torii 权重未完整下载，请在模型管理中检查")
        device = cfg["nl_device"]
        index = 0 if device in ("auto", "cpu") else int(device[5:])
        if device.startswith("cuda:") and index >= len(gpus):
            raise ConfigError(f"设备 {device} 不存在")
        if cfg["tag_device"] == "auto" and cfg["mode"] == "both":
            other = next((i for i in range(len(gpus)) if i != index), 0)
            cfg["tag_device"] = f"cuda:{other}" if gpus else "cpu"
        if cfg['concurrency'] > plan['slots']:
            self.log(f"[warn] Torii 请求并发 {cfg['concurrency']} 超出当前引擎能力，使用 {plan['slots']}，继续执行")
        cfg["concurrency"], cfg["gpu_mem"] = plan["slots"], 0
        self.log(f"ToriiGate-0.5 · {cfg['nl_engine']} · 并发 {cfg['concurrency']}")
        opts = {"engine": cfg["nl_engine"], "model": torii_service.KEY, "precision": "q6",
                "served_name": cfg["served_name"], "device": device, "slots": cfg["concurrency"],
                "model_path": item["model_path"], "gpu_mem": None}
        self._start_service(cfg, opts, cancel)

    def _start_service(self, cfg, opts, cancel):
        self.phase("starting_service", "启动 NL 模型服务")
        port = urlsplit(cfg["tag_url"]).port or 8000
        with GPU_START_LOCK:
            if cancel.is_set():
                raise Cancelled("已停止准备")
            from ..multi_gpu import MULTI
            from ..jobs import MANAGER
            if MANAGER.running() is not None or MULTI.status in ("starting", "ready", "partial", "stopping"):
                raise ConfigError("已有任务或多卡服务正在运行，请先停止后开工")
            state = VLLM.snapshot()
            if state["status"] in ("starting", "ready", "stopping"):
                matches = state["port"] == port and all(state["overrides"].get(k) == v for k, v in opts.items())
                if not matches and self.owned_started == state["started"]:
                    VLLM.stop()
                    state = VLLM.start(port=port, **opts)
                    self.owned_started = state["started"]
                    matches = True
                if not matches:
                    raise ConfigError("本机已有不同配置的模型服务，请先停止或选择使用已有服务")
            else:
                state = VLLM.start(port=port, **opts)
                self.owned_started = state["started"]
            self.service_started = state["started"]
        try:
            ready = self._wait(cancel, VLLM.snapshot, "NL 服务启动")
        except ConfigError:
            state = VLLM.snapshot()
            self.log(f"[NL] {state.get('engine')} · 日志：{state.get('log_path')}")
            for line in state.get('log_tail', [])[-30:]:
                self.log('[NL] ' + line)
            raise
        if cfg["nl_engine"] in ("llamacpp", "native"):
            cfg["concurrency"] = ready.get("fingerprint", {}).get("slots") or cfg["concurrency"]
        cfg["tag_url"] = f"http://127.0.0.1:{port}"
        cfg["runtime_fingerprint"] = ready.get("fingerprint") or {}

    def _prepare_weights(self, cfg, profile, token, cancel):
        key = cfg["nl_model"]
        gguf_route = cfg["nl_engine"] == "llamacpp" or (cfg["nl_engine"] == "native" and cfg["nl_precision"] in ("f16", "q4", "q5", "q6", "q8"))
        if gguf_route:
            try:
                nl_engine.resolve_weights(profile)
                return
            except ConfigError:
                if not cfg["auto_quantize"]:
                    raise ConfigError("所选 GGUF 精度文件尚未准备；请先本地量化或开启自动量化")
                if cfg["nl_precision"] == "q5":
                    raise ConfigError("Q5 目前需要提供已有 GGUF 文件；自动转换请选择 Q4 / Q6 / Q8")
        else:
            item = models._entry("vlm", key)
            weights = models.weight_summary(item, Path(item["local_abs"]))
            if cfg["nl_precision"] == "w4a16" and weights["quantized"]:
                return
            if cfg["nl_precision"] != "w4a16" and weights["raw_ready"]:
                return
            if cfg["nl_precision"] == "w4a16" and not cfg["auto_quantize"]:
                raise ConfigError("W4A16 权重不存在，请先量化或开启自动量化")
        if not models._entry("vlm", key)["raw_ready"]:
            self._download("vlm", key, cfg, token, cancel)
        if gguf_route or cfg["nl_precision"] == "w4a16":
            from ...gguf_convert import CONVERTER
            from ...quantize import QUANT
            manager = CONVERTER if gguf_route else QUANT
            state = manager.state() if manager is CONVERTER else manager.activity()
            if state and state["status"] == "running":
                raise ConfigError("已有量化任务正在运行，请完成后再开工")
            self.phase("quantizing", f"生成 {key} 的 {cfg['nl_precision']} 权重")
            if cancel.is_set():
                raise Cancelled("已停止准备")
            self.quantizer = manager
            if manager is CONVERTER:
                target = {"f16": "f16", "q4": "q4_k_m", "q6": "q6_k", "q8": "q8_0"}[cfg["nl_precision"]]
                manager.start(key, target)
                snapshot = manager.state
            else:
                manager.start(key)
                snapshot = manager.activity
            try:
                self._wait(cancel, snapshot, "本地量化")
            finally:
                if cancel.is_set():
                    manager.cancel()
                    if manager is CONVERTER:
                        manager.wait_done()
                self.quantizer = None

    def stop(self, stop_service=True):
        if self.download:
            downloads.DOWNLOADS.cancel(*self.download)
        if self.quantizer:
            self.quantizer.cancel()
        if self.owned_started is not None:
            if stop_service and VLLM.started == self.owned_started:
                VLLM.stop()
            if stop_service:
                self.owned_started = None

    def healthy(self) -> bool:
        if not self.local_required:
            return True
        VLLM._reap()
        return VLLM.status == "ready" and VLLM.started == self.service_started
