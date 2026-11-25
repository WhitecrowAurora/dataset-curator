"""Torii 固定模型包到通用服务生命周期的适配。"""
from __future__ import annotations

from pathlib import Path

from ..config import ConfigError
from . import downloads, review_models

KEY = review_models.TORII_KEY


def model_entry() -> dict:
    item = next(x for x in review_models.catalog() if x["key"] == KEY)
    configured = Path(item["configured_path"])
    configured = configured if configured.is_absolute() else downloads.ROOT / configured
    directory = configured.parent if configured.is_file() else configured
    weight = directory / "ToriiGate-0.5-Q6_K.gguf"
    # 明确选了其他文件时不能悄悄换回同目录 Q6。
    selected = configured if configured.suffix.lower() == ".gguf" else weight
    ready = selected == weight and all(
        (directory / name).is_file() and (directory / name).stat().st_size == size
        for name, size in review_models.TORII_FILES.items())
    if not ready and not configured.exists() and item["ready"]:
        selected, ready = Path(item["use_path"]), True
        if not selected.is_absolute():
            selected = downloads.ROOT / selected
    return {**item, "ready": ready, "raw_ready": False, "gguf_ready": ready,
            "model_path": str(selected.resolve()), "gguf_quants": {"q6": str(selected)} if ready else {}}


def plan(engine, precision=None, slots=None) -> dict:
    from ..inference.qwen35.runtime import MAX_BATCH_SIZE
    if engine not in ("llamacpp", "native"):
        return {"ok": False, "error": "ToriiGate 支持 llama.cpp / native，请切换引擎"}
    if precision not in (None, "q6"):
        return {"ok": False, "error": "ToriiGate 当前使用 Q6_K 权重，请选择 Q6"}
    count = min(MAX_BATCH_SIZE, max(1, slots or 2)) if engine == "native" else 1
    return {"ok": True, "model": KEY, "precision": "q6", "max_model_len": 8192,
            "slots": count, "max_num_seqs": count, "max_pixels": 1_000_000,
            "summary": f"ToriiGate-0.5 · {engine} · Q6_K · 并发 {count}"}


def start(runtime, overrides, port, log_path, tick):
    from ..hair_color.contracts import HairWorkflowSettings

    selected = overrides.get("model_path") or model_entry()["model_path"]
    settings = HairWorkflowSettings(torii_dir=selected, torii_url="",
                                   torii_model=overrides.get("served_name") or "tagger",
                                   torii_engine=overrides["engine"])
    resolved = plan(overrides["engine"], overrides.get("precision"), overrides.get("slots"))
    if not resolved["ok"]:
        raise ConfigError(resolved["error"])
    runtime.endpoint("torii", settings, slots=resolved["slots"],
                     device=overrides.get("device") or "auto", port=port,
                     log_path=log_path, on_tick=tick)
    return {**runtime.fingerprint, "url": runtime.url, "caption_format": "torii"}
