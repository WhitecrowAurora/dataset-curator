"""托管凭据只在本机保存，不进入配置快照或命令行。"""
from __future__ import annotations

import json
from pathlib import Path

from ...storage.atomic import write_bytes


class Credentials:
    def __init__(self, directory: Path):
        self.path = directory / "credentials.json"
        self.token = ""
        self.remember = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.token = str(data.get("hf_token") or "")
            self.remember = bool(self.token)
        except (OSError, ValueError, AttributeError):
            pass

    def update(self, token: str | None = None, remember: bool | None = None):
        if token is not None:
            self.token = token.strip()
        if remember is not None:
            self.remember = remember
        data = {"hf_token": self.token if self.remember else ""}
        write_bytes(self.path, json.dumps(data).encode("utf-8"))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def redact(self, text: str) -> str:
        import re
        for token in {self.token, self.effective_token()} - {""}:
            text = text.replace(token, "***")
        text = re.sub(r'\bhf_[A-Za-z0-9]+\b', '***', text)
        text = re.sub(r'(?i)(--secret(?:\s+|=))(?:"[^"]*"|\S+)', r'\1***', text)
        return text

    def effective_token(self) -> str:
        from ...nl_api import saved_hf

        entry = saved_hf()
        return self.token or (entry.get("token", "") if entry.get("scope") == "global" else "")

    def snapshot(self):
        return {"hf_token_set": bool(self.effective_token()), "remember_hf": self.remember}
