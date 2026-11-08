"""WD 的张量预处理：从实际 PIL transform 提取可等价执行的步骤。"""

from __future__ import annotations

from dataclasses import dataclass, field


def normalize_preprocess(value: str | None) -> str:
    text = str(value or "pil").strip().lower()
    if text in ("", "pil", "cpu"):
        return "pil"
    if text in ("tensor", "gpu", "cuda", "tensor_gpu"):
        return "tensor"
    raise ValueError(f"未知预处理 {value!r}，可选 pil / tensor")


@dataclass
class TensorPrepPlan:
    resize: tuple[int, int] | None
    short_edge: int
    crop: tuple[int, int] | None
    mode: str
    mean: tuple[float, ...]
    std: tuple[float, ...]
    max_size: int | None = None
    _norm: dict = field(default_factory=dict, repr=False)

    def apply(self, image, device: str):
        import torch
        from torchvision.transforms import InterpolationMode
        from torchvision.transforms import functional as F

        image = image.to(device=device, dtype=torch.float32).div_(255.0)
        image = F.resize(image, list(self.resize) if self.resize else self.short_edge,
                         interpolation=InterpolationMode(self.mode),
                         max_size=self.max_size, antialias=True)
        if self.crop:
            image = F.center_crop(image, list(self.crop))
        key = str(image.device)
        if key not in self._norm:
            self._norm[key] = tuple(torch.tensor(values, device=image.device).view(-1, 1, 1)
                                    for values in (self.mean, self.std))
        mean, std = self._norm[key]
        return image.sub_(mean).div_(std)


def tensor_prep_plan(transform) -> TensorPrepPlan | None:
    """不支持的变换回落原实现，避免猜测裁剪口径。"""
    from torchvision import transforms as T

    ops = list(getattr(transform, "transforms", ()))
    if len(ops) not in (3, 4) or not isinstance(ops[0], T.Resize):
        return None
    resize = ops[0]
    crop = None
    if len(ops) == 4:
        if not isinstance(ops[1], T.CenterCrop):
            return None
        crop = ops[1].size
        crop = (crop, crop) if isinstance(crop, int) else tuple(crop)
    if not isinstance(ops[-2], T.ToTensor) or not isinstance(ops[-1], T.Normalize):
        return None
    mode = getattr(resize.interpolation, "value", "")
    if mode not in ("nearest", "bilinear", "bicubic"):
        return None
    size = resize.size
    if isinstance(size, int):
        size = (size,)
    fixed = tuple(size) if len(size) == 2 else None
    return TensorPrepPlan(fixed, 0 if fixed else int(size[0]), crop, mode,
                          tuple(ops[-1].mean), tuple(ops[-1].std), resize.max_size)
