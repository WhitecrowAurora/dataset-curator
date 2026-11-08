"""分包时缩小图片，默认原样保留字节与格式。"""

from __future__ import annotations

import io
import math
from pathlib import Path


def resize_bytes(data: bytes, *, mode: str = "none", value: int = 768,
                 quality: int = 92, name: str = "") -> tuple[bytes, str]:
    ext = Path(name).suffix.lower() or ".png"
    if mode == "none":
        return data, ext
    if mode not in ("longest", "shortest", "pixels") or value <= 0:
        raise ValueError("缩放方式须为 longest / shortest / pixels，目标须大于 0")

    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as source:
        fmt = source.format
        image = ImageOps.exif_transpose(source)
        width, height = image.size
        if mode == "pixels":
            scale = math.sqrt(value * 1_000_000 / (width * height))
        else:
            edge = max(width, height) if mode == "longest" else min(width, height)
            scale = value / edge
        if scale >= 1:
            return data, ext
        image = image.resize((max(1, round(width * scale)), max(1, round(height * scale))),
                             Image.Resampling.LANCZOS)
        formats = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp",
                   "BMP": ".bmp", "TIFF": ".tif", "GIF": ".gif"}
        if fmt not in formats:
            fmt = "PNG"
        ext = formats[fmt]
        if fmt == "JPEG":
            image = image.convert("RGB")
        buf = io.BytesIO()
        options = {"quality": quality} if fmt in ("JPEG", "WEBP") else {}
        if fmt == "JPEG":
            options["subsampling"] = 0
        image.save(buf, fmt, **options)
        return buf.getvalue(), ext
