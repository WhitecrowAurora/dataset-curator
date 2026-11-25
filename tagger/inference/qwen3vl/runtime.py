"""Translate the existing run profile into the native caption runtime."""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

from ...config import GGUF_PRECISIONS, ConfigError

VERSION = "lulynx-qwen3vl-0.26"
PRECISIONS = ("fp16", "bf16", "fp8", "int8", "w4a16", "f16", "q4", "q5", "q6", "q8")
OPTIMIZED_PRECISIONS = ("q4", "q5", "q6", "q8", "fp8", "int8")
ROOT = Path(__file__).resolve().parents[3]


def check_environment():
    from ...runtime.python_dev import ensure as ensure_python_dev
    if importlib.util.find_spec('triton') is not None:
        ensure_python_dev()
    missing = [name for name in ("torch", "torchvision", "transformers", "safetensors", "gguf", "fastapi", "uvicorn")
               if importlib.util.find_spec(name) is None]
    if missing:
        raise ConfigError(f"专用引擎缺少依赖：{', '.join(missing)}。安装 tagger[native-nl]。")


def resolve_device_profile(device="auto", *, device_index=None, **overrides):
    from ...config import DEFAULT_TIERS, load_tier_table, resolve_profile
    from ...hardware import GPUInfo, query_gpus, visible_gpus

    if device not in ("auto", "cpu") and not (device.startswith("cuda:") and device[5:].isdigit()):
        raise ConfigError("NL 设备请选择自动、CPU 或有效 CUDA 设备")
    gpus = query_gpus() if device_index is not None else visible_gpus()
    if device_index is not None:
        gpus = [gpu for gpu in gpus if gpu.index == device_index]
        if not gpus:
            raise ConfigError(f"GPU {device_index} 不存在")
    if device == "cpu" or (device == "auto" and not gpus):
        gpu = GPUInfo(-1, "CPU", 64, None)
    else:
        index = 0 if device == "auto" or device_index is not None else int(device.removeprefix("cuda:"))
        if index >= len(gpus):
            raise ConfigError(f"设备 {device} 不存在")
        gpu = gpus[index]
    profile = resolve_profile(load_tier_table(DEFAULT_TIERS), gpu, engine="native", **overrides)
    return (replace(profile, vram_gb=0) if gpu.index == -1 else profile), (None if gpu.index == -1 else gpu.index)


def plan(**overrides):
    try:
        slots = overrides.pop("slots_override", None)
        profile, _ = resolve_device_profile(**overrides)
        batch = int(slots or profile.max_num_seqs)
        profile = replace(profile, max_num_seqs=batch, slots=batch, max_num_batched_tokens=0)
        out = {"ok": True, "tier": profile.tier, "model": profile.model.key,
               "model_path": profile.weights_path, "precision": profile.precision,
               "max_pixels": profile.max_pixels, "max_model_len": profile.max_model_len,
               "max_num_seqs": slots or profile.max_num_seqs, "max_num_batched_tokens": 0,
               "gpu_memory_utilization": None, "vram_gb": profile.vram_gb,
               "device": profile.device_name, "warnings": profile.warnings, "summary": profile.summary(),
               "quantization": profile.precision if profile.precision in ("fp8", "int8", "w4a16") else None,
               "weight_quantized": profile.weight_quantized}
        if overrides.get("gpu_mem_override"):
            out["warnings"] = [*out["warnings"], "专用引擎按需分配显存；显存比例不生效，请调整批大小或上下文长度"]
        try:
            spec = specification(profile, slots=slots)
            out["model_path"] = spec["model_dir"]
            out["weights"] = {"ok": True, "gguf": Path(spec["gguf"]).name if spec["gguf"] else "",
                              "mmproj": Path(spec["mmproj"]).name if spec["mmproj"] else ""}
        except ConfigError as exc:
            out["weights"] = {"ok": False, "error": str(exc)}
        return out
    except ConfigError as exc:
        return {"ok": False, "error": str(exc)}


def specification(profile, *, slots=None, served_name="tagger", decode_attention=None):
    from ...nl_engine import resolve_weights

    if decode_attention is None:
        decode_attention = "native" if profile.precision in OPTIMIZED_PRECISIONS else "sdpa"
    if decode_attention not in ("sdpa", "native"):
        raise ConfigError("专用 attention 请选择 sdpa 或 native")
    if not profile.model.family.startswith("qwen3vl"):
        raise ConfigError("专用引擎目前只支持 dense Qwen3-VL")
    if profile.precision not in PRECISIONS:
        raise ConfigError(f"专用引擎不支持精度 {profile.precision}")
    supplied = Path(profile.weights_path)
    path = (supplied if supplied.is_absolute() else ROOT / supplied).resolve()
    if not path.is_dir() and not supplied.is_absolute() and profile.precision != "w4a16":
        # A remote model id points to the download catalog's local directory.
        # Honor its persisted directory override without importing the Web app.
        from ...config import load_model_overlay
        overlay = load_model_overlay()
        local = (overlay.get("dirs") or {}).get(f"vlm:{profile.model.key}")
        path = Path(local) if local else ROOT / "models" / "nl" / profile.model.key
        path = (path if path.is_absolute() else ROOT / path).resolve()
    if not (path / "config.json").is_file():
        raise ConfigError(f"专用引擎需要本地模型配置与 tokenizer：{path}")
    if profile.precision in GGUF_PRECISIONS:
        def local_weight(raw):
            original = Path(raw)
            original = original if original.is_absolute() else ROOT / original
            candidate = path / original.name
            return str(candidate) if not original.is_file() and candidate.is_file() else raw

        model = replace(profile.model, gguf=local_weight(profile.model.gguf),
                        mmproj=local_weight(profile.model.mmproj),
                        gguf_quants={key: local_weight(value) for key, value in (profile.model.gguf_quants or {}).items()})
        gguf, mmproj = resolve_weights(replace(profile, model=model))
    else:
        gguf, mmproj = None, None
    if gguf is None and not any(path.glob("*.safetensors")):
        raise ConfigError(f"本地 safetensors 权重不存在：{path}")
    if profile.precision == "w4a16":
        quant = json.loads((path / "config.json").read_text(encoding="utf-8")).get("quantization_config") or {}
        if not profile.model.quantized_path or quant.get("quant_method") != "compressed-tensors":
            raise ConfigError("专用 W4A16 需要已生成的 compressed-tensors packed 权重")
    batch = int(slots or profile.max_num_seqs)
    if not 1 <= batch <= 64:
        raise ConfigError("专用引擎批大小请选择 1～64")
    optimized = (profile.device_name != "CPU" and importlib.util.find_spec("triton") is not None
                 and (profile.precision != "bf16" or decode_attention == "native"))
    return {"model_dir": str(path), "gguf": str(gguf) if gguf else None,
            "mmproj": str(mmproj) if mmproj else None, "precision": profile.precision,
            "device": "cpu" if profile.device_name == "CPU" else "cuda:0",
            "max_pixels": profile.max_pixels, "context_capacity": profile.max_model_len,
            "batch_size": batch, "served_name": served_name, "optimized": optimized,
            "continuous_batching": True,
            "prefill_layers": 0,
            "decode_attention": decode_attention,
            "prefill_attention": "sdpa",
            "decode_graph": True,
            # Q4_K_M keeps some matrices in Q6_K; repack both original formats.
            "q6_repack": optimized and profile.precision in ("q4", "q6"),
            "q4_repack": optimized and profile.precision == "q4",
            "gguf_repack": {"q5": ["Q5_K", "Q6_K"], "q8": ["Q8_0"]}.get(profile.precision, []) if optimized else [],
            "temperature": profile.temperature, "top_p": profile.top_p, "max_tokens": profile.max_tokens}


def build_command(spec, port):
    # Embedded Python does not put cwd on sys.path. Keep this an internal worker.
    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            "from tagger.runtime.kernel_cache import configure; configure(); "
            "from tagger.inference.qwen3vl.server import main; main()")
    return [sys.executable, "-u", "-B", "-c", code, "--port", str(port), "--runtime", json.dumps(spec)]


def load_engine(spec):
    import torch

    from .engine import CaptionEngine

    dtype = torch.float32 if spec["device"] == "cpu" else torch.bfloat16 if spec["precision"] == "bf16" else torch.float16
    if spec["device"] != "cpu":
        # This worker owns its process. Keep cuBLAS accumulation consistent with
        # the packed kernels' FP32 accumulation during large prefills.
        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False
    optimized = spec["optimized"] and importlib.util.find_spec("triton") is not None
    # Matrix representations select their own dtype support; dense BF16 can
    # reuse the common decoder kernels without invoking FP16-only products.
    return CaptionEngine(spec["model_dir"], spec["gguf"], spec["mmproj"], device=spec["device"],
                         dtype=dtype, max_pixels=spec["max_pixels"], context_capacity=spec["context_capacity"],
                         optimized=optimized,
                         prefill_layers=spec.get("prefill_layers", 0),
                         decode_attention=spec.get("decode_attention", "sdpa"),
                         prefill_attention=spec.get("prefill_attention", "sdpa"),
                         decode_graph=spec.get("decode_graph", True),
                         q6_repack=spec.get("q6_repack", False),
                         q4_repack=spec.get("q4_repack", False),
                         gguf_repack=tuple(spec.get("gguf_repack", ())),
                         native_precision=spec["precision"] if spec["precision"] in ("fp8", "int8") else None)
