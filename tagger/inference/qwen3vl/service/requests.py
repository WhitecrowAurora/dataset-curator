"""The single-image caption subset consumed by the existing tagger client."""
from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image

from ..engine import CaptionRequest
from ..sampling import Sampling


def caption_request(body, spec):
    if body.get("model") != spec["served_name"]:
        raise ValueError(f"模型不存在：{body.get('model')}")
    if body.get("stream") or body.get("n", 1) != 1 or body.get("stop"):
        raise ValueError("专用引擎目前支持单图、单结果、非流式请求，不支持自定义 stop")
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) not in (1, 2):
        raise ValueError("请发送可选 system 和一个 user 消息")
    system = ""
    if len(messages) == 2:
        if messages[0].get("role") != "system" or not isinstance(messages[0].get("content"), str):
            raise ValueError("system 消息需要文本内容")
        system = messages[0]["content"]
    user = messages[-1]
    if user.get("role") != "user" or not isinstance(user.get("content"), list):
        raise ValueError("user 消息需要文本和图片")
    images, text = [], []
    for part in user["content"]:
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            text.append(part["text"])
        elif part.get("type") == "image_url":
            value = part.get("image_url") or {}
            url = value.get("url", "") if isinstance(value, dict) else ""
            if not url.startswith("data:image/") or ";base64," not in url:
                raise ValueError("图片请使用 base64 data URL")
            raw = base64.b64decode(url.split(",", 1)[1], validate=True)
            with Image.open(BytesIO(raw)) as image:
                images.append(image.convert("RGB"))
        else:
            raise ValueError("不支持的 user 内容类型")
    if len(images) != 1:
        raise ValueError("每个请求需要且只能包含一张图片")
    if body.get("frequency_penalty", 0) or body.get("presence_penalty", 0):
        raise ValueError("专用引擎目前只支持重复惩罚 repeat_penalty")
    options = Sampling(max_tokens=int(body.get("max_tokens", body.get("max_completion_tokens", spec["max_tokens"]))),
                       temperature=float(body.get("temperature", spec["temperature"])),
                       top_p=float(body.get("top_p", spec["top_p"])),
                       repeat_penalty=float(body.get("repeat_penalty", body.get("repetition_penalty", 1.0))),
                       repeat_last_n=int(body.get("repeat_last_n", 64)), top_k=int(body.get("top_k", 0)),
                       min_p=float(body.get("min_p", 0)), seed=body.get("seed"))
    return CaptionRequest(images[0], "\n".join(text), system, options)
