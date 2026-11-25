"""PixAI 管线的设备迁移，包含模型之外的筛选张量。"""
from __future__ import annotations


def move_pipeline(pipe, device):
    import torch
    pipe.model.to(device)
    pipe.device = torch.device(device)
    for name in ("tags_best_threshold", "tags_indices"):
        value = getattr(pipe, name, None)
        if value is not None:
            setattr(pipe, name, value.to(device))


def release_backend(backend):
    import gc
    import torch
    pipe = getattr(backend, "_pipe", None)
    if pipe is not None:
        move_pipeline(pipe, "cpu")
    else:
        closer = getattr(backend, "close", None)
        if closer:
            closer()
        for name in ("_model", "model", "_graphs"):
            if hasattr(backend, name):
                setattr(backend, name, {} if name == "_graphs" else None)
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
