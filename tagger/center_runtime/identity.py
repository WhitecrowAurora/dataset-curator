"""本机登录凭据：握手失败和断开连接后仍可沿用。"""
from __future__ import annotations

import json
import os
from pathlib import Path


def saved_path(config: Path) -> Path:
    return config.with_name(config.stem + "-identity.json")


def pending_path(config: Path) -> Path:
    return config.with_name(config.stem + "-login.json")


def read_for_login(config: Path) -> dict:
    for path in (pending_path(config), config, saved_path(config)):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def write_identity(path: Path, identity: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
