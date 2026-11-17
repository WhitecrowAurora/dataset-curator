"""推理资源失败判定，以及自动设备偏好的分类模型加载。"""
from __future__ import annotations


def resource_failure(text: str) -> bool:
    message = text.lower()
    return any(value in message for value in (
        "out of memory", "cuda error", "cuda runtime", "cublas", "cuda_malloc",
        "failed to allocate", "cannot allocate", "buffer allocation", "vulkan error",
        "no cuda-capable device", "no kernel image", "unsupported gpu architecture",
        "torch not compiled with cuda", "cuda is not available",
    ))


def load_tag_backend(backend, *, automatic: bool, log):
    try:
        backend.load()
        return backend
    except Exception as exc:
        if not automatic or not str(getattr(backend, "device", "")).startswith("cuda") or not resource_failure(str(exc)):
            raise
    log("TAG 模型无法在 GPU 加载，回退 CPU。")
    backend.device = "cpu"
    backend.load()
    return backend
