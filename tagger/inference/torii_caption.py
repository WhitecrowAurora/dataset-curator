"""Torii 官方分角色 JSON 与既有 caption 文本契约的适配。"""
from __future__ import annotations

import json


def is_torii(model):
    return "toriigate" in str(model).lower().replace("-", "").replace("_", "")


def caption_prompt():
    from ..hair_color.vlm import TORII_PROMPT
    from ..text_encoding.settings import length_instruction
    return TORII_PROMPT + "\n# Description length:\n" + length_instruction()


def configure_payload(payload):
    from ..hair_color.vlm import TORII_SYSTEM
    payload["messages"][0]["content"] = TORII_SYSTEM
    payload["messages"][1]["content"][-1]["text"] = caption_prompt()
    payload.update(stop=["<|im_end|>", "<|endoftext|>"], cache_prompt=False)
    return payload


def caption_text(text):
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Torii 没有返回描述对象")
    parts = [str(v).strip() for v in value.values() if isinstance(v, str) and v.strip().lower() not in ("", "none", "null")]
    if not parts:
        raise ValueError("Torii 描述为空")
    return " ".join(parts)
