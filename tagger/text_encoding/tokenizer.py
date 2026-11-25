"""只加载本地 tokenizer；不会加载模型或隐式联网。"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from functools import lru_cache

from .settings import TextEncoding, load

_FILES = ("tokenizer.json", "tokenizer_config.json", "chat_template.jinja", "special_tokens_map.json", "added_tokens.json", "vocab.json", "merges.txt")


def require_training_text(mode):
    if mode == 'tag':
        return
    try:
        TrainingText().ids('Tokenizer readiness check.')
    except Exception as exc:
        from ..config import ConfigError
        raise ConfigError(f'开工前检查失败：训练 tokenizer 无法使用。请在模型管理中准备 Qwen3.5-0.8B tokenizer。{exc}') from exc


@lru_cache(maxsize=4)
def _fingerprint(path: str, signature: tuple):
    from pathlib import Path
    return {name: hashlib.sha256((Path(path) / name).read_bytes()).hexdigest() for name, _, _ in signature}


@lru_cache(maxsize=2)
def _tokenizer(path: str, signature: tuple):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)


class TrainingText:
    def __init__(self, spec: TextEncoding | None = None):
        self.spec = spec or load()
        path = self.spec.path
        try:
            for name in ("tokenizer.json", "tokenizer_config.json"):
                (path / name).stat()
            signature = tuple((name, (path / name).stat().st_mtime_ns, (path / name).stat().st_size)
                              for name in _FILES if (path / name).is_file())
        except FileNotFoundError as exc:
            raise ValueError("训练 tokenizer 尚未准备好，请在模型管理中准备 Qwen3.5-0.8B tokenizer") from exc
        self.tokenizer = _tokenizer(str(path), signature)
        self.fingerprint = _fingerprint(str(path), signature)

    def ids(self, caption: str, tags=()):
        tag = tags if isinstance(tags, str) else ", ".join(tags)
        text = self.spec.template.format(caption=caption, tag=tag)
        if self.spec.chat_template:
            messages = ([{"role": "system", "content": self.spec.system_prompt}] if self.spec.system_prompt else [])
            messages.append({"role": "user", "content": text})
            encoded = self.tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=False)
            # Transformers 新版默认可能返回 BatchEncoding；len(encoded) 是字段数。
            return encoded["input_ids"] if isinstance(encoded, Mapping) else encoded
        return self.tokenizer.encode(text, add_special_tokens=self.spec.add_special_tokens, truncation=False)

    def count(self, caption: str, tags=()):
        return len(self.ids(caption, tags))

    def fit(self, caption: str, tags=(), limit: int | None = None, *, required_prefix: str = ""):
        limit = min(limit or self.spec.max_length, self.spec.max_length)
        if required_prefix and self.count(required_prefix, tags) > limit:
            raise ValueError("训练 token 预算不足以保留必要描述前缀，请调整文本模板或标签长度")
        if self.count(caption, tags) <= limit:
            return caption
        if self.count("", tags) >= limit:
            raise ValueError("标签与文本模板已占满训练 token 预算，请提高上限或调整模板")
        parts = [m.end() for m in re.finditer(r"[。！？；;]|[.!?](?:\s|$)", caption)]
        for end in reversed(parts):
            candidate = caption[:end].rstrip()
            if candidate and (not required_prefix or candidate.startswith(required_prefix)) and self.count(candidate, tags) <= limit:
                return candidate
        lo, hi = 0, len(caption)
        while lo < hi:
            middle = (lo + hi + 1) // 2
            if self.count(caption[:middle], tags) <= limit:
                lo = middle
            else:
                hi = middle - 1
        result = caption[:lo].rstrip()
        while result and self.count(result, tags) > limit:
            result = result[:-1].rstrip()
        if required_prefix and not result.startswith(required_prefix):
            return required_prefix
        if not result:
            raise ValueError("当前模板的训练 token 预算不足以保留描述")
        return result

    def metadata(self, caption: str, tags=()):
        spec = self.spec.model_dump()
        tokens = self.count(caption, tags)
        return {"tokenizer": str(self.spec.path), "revision": self.spec.revision,
                "tokens": tokens, "target": self.spec.target_length,
                "over_target": tokens > self.spec.target_length, "limit": self.spec.max_length,
                "config_sha256": hashlib.sha256(self.spec.model_dump_json().encode()).hexdigest(),
                "tokenizer_sha256": self.fingerprint,
                "settings": spec}
