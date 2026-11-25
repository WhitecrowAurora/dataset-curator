from __future__ import annotations

import os
from pathlib import Path
from string import Formatter

from pydantic import BaseModel, Field, model_validator

from ..storage.atomic import read_json, write_json

ROOT = Path(__file__).resolve().parents[2]
REPO = "Qwen/Qwen3.5-0.8B"
REVISION = "2fc06364715b967f1860aea9cf38778875588b17"
TARGET_TOKENS = 384
MAX_TOKENS = 512


class TextEncoding(BaseModel):
    tokenizer_path: str = "models/text/qwen3.5-0.8b"
    revision: str = REVISION
    max_length: int = Field(default=MAX_TOKENS, ge=1, le=MAX_TOKENS)
    template: str = "{caption}"
    add_special_tokens: bool = True
    chat_template: bool = False
    system_prompt: str = ""

    @model_validator(mode="after")
    def valid_template(self):
        if not self.tokenizer_path.strip():
            raise ValueError("请填写 tokenizer 目录")
        fields = list(Formatter().parse(self.template))
        for _, key, spec, conversion in fields:
            if key is not None and (key not in ("caption", "tag") or spec or conversion):
                raise ValueError("训练文本模板只支持 {caption} 和 {tag}")
        if not any(key in ("caption", "tag") for _, key, _, _ in fields):
            raise ValueError("模板至少需要 {caption} 或 {tag}")
        return self

    @property
    def target_length(self):
        return min(TARGET_TOKENS, self.max_length)

    @property
    def uses_tags(self):
        return any(key == "tag" for _, key, _, _ in Formatter().parse(self.template))

    @property
    def path(self):
        path = Path(self.tokenizer_path)
        return path if path.is_absolute() else ROOT / path


def settings_path():
    return Path(os.environ.get("TAGGER_TEXT_ENCODING_CONFIG") or ROOT / "work/conflicts/learning/text_encoding.json")


def load():
    return TextEncoding.model_validate(read_json(settings_path()) or {})


def save(value: TextEncoding):
    write_json(settings_path(), value.model_dump())
    return value


def length_instruction(*, zh: bool = False):
    spec = load()
    if zh:
        return (f"- 描述尽量控制在 {spec.target_length} token 以内；必要细节可以超过这个软目标，"
                f"但最终训练文本不得超过 {spec.max_length} token。优先保留主体与场景，"
                "精简重复细节和小字，不为达到长度目标补写内容。")
    return (f"- Aim for at most {spec.target_length} tokens of description. This is a soft target: "
            f"essential detail may exceed it, but the final training text must not exceed {spec.max_length} tokens. "
            "Prioritize the subject and setting; shorten repeated details and small print. Do not pad to reach the target.")
