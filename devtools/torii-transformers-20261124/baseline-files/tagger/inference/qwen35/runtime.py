"""由现有复核服务管理的 native worker 配置。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

VERSION = "lulynx-qwen35-0.3"
REPO = "Minthy/ToriiGate-0.5"
REVISION = "7d6b237af26e69f6ee9b381fe7beb3340cecabae"
METADATA = ("config.json", "preprocessor_config.json", "video_preprocessor_config.json",
            "tokenizer_config.json", "tokenizer.json", "chat_template.jinja")


def prepare_metadata(directory):
    from huggingface_hub import hf_hub_download
    directory = Path(directory) / "native-metadata"
    for name in METADATA:
        if not (directory / name).is_file():
            hf_hub_download(REPO, name, revision=REVISION, local_dir=directory)
    return directory


def build_command(weights, projector, model, port, *, slots=1, device="auto"):
    from ..qwen3vl.runtime import check_environment
    check_environment()
    metadata = prepare_metadata(weights.parent)
    spec = {"metadata": str(metadata), "gguf": str(weights), "mmproj": str(projector),
            "served_name": model, "device": device, "batch_size": min(4, max(1, int(slots))), "max_tokens": 2048,
            "temperature": .5, "top_p": .9, "max_pixels": 1_000_000, "context_capacity": 8192}
    root = Path(__file__).resolve().parents[3]
    code = (f"import sys; sys.path.insert(0, {str(root)!r}); "
            "from tagger.runtime.kernel_cache import configure; configure(); "
            "from tagger.inference.qwen35.runtime import main; main()")
    command = [sys.executable, "-u", "-B", "-c", code,
               "--port", str(port), "--runtime", json.dumps(spec)]
    return command, {"version": VERSION, "model_revision": REVISION, "metadata": str(metadata),
                     "batch_size": spec["batch_size"]}


def load_engine(spec):
    import torch
    from .engine import CaptionEngine
    device = spec.get("device", "auto")
    if device == "auto":
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
    if device.startswith("cuda"):
        torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False
    engine = CaptionEngine(spec["metadata"], spec["gguf"], spec["mmproj"], device=device,
                         max_pixels=spec["max_pixels"], context_capacity=spec["context_capacity"],
                         optimized=spec.get("optimized", "auto"), linear_backend=spec.get("linear_backend", "auto"))
    engine.runtime_info["batch_size"] = spec["batch_size"]
    return engine


def caption_request(body, spec):
    from ..qwen3vl.service.requests import caption_request as parse
    body = dict(body)
    stop = body.pop("stop", [])
    stop = [stop] if isinstance(stop, str) else stop
    if not isinstance(stop, list) or any(v not in ("<|im_end|>", "<|endoftext|>") for v in stop):
        raise ValueError("Torii native 支持模型结束标记作为 stop")
    messages = body.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0].get("content"), list):
        parts = messages[0]["content"]
        if messages[0].get("role") == "system" and all(v.get("type") == "text" for v in parts):
            body["messages"] = [dict(messages[0], content="".join(v["text"] for v in parts)), *messages[1:]]
    return parse(body, spec)


def main():
    import argparse
    import uvicorn
    from ..qwen3vl.server import create_app
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--runtime", required=True)
    args = parser.parse_args()
    spec = json.loads(args.runtime)
    uvicorn.run(create_app(spec, engine_factory=load_engine, request_factory=caption_request,
                          version=VERSION), host="127.0.0.1", port=args.port)
