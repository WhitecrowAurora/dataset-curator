"""Local safetensors shards and explicit quantization metadata."""
from __future__ import annotations

import json
import re
from pathlib import Path

from safetensors import safe_open


class Checkpoint:
    def __init__(self, path: str | Path):
        self.root = Path(path)
        self.config = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        index = self.root / "model.safetensors.index.json"
        if index.is_file():
            self.files = json.loads(index.read_text(encoding="utf-8"))["weight_map"]
        else:
            self.files = {}
            for file in sorted(self.root.glob("*.safetensors")):
                with safe_open(file, framework="pt", device="cpu") as reader:
                    for key in list(reader.keys()):
                        if key in self.files:
                            raise ValueError(f"Duplicate checkpoint tensor: {key}")
                        self.files[key] = file.name
        if not self.files:
            raise ValueError("No local safetensors weights found")

    def tensor(self, key):
        with safe_open(self.root / self.files[key], framework="pt", device="cpu") as reader:
            # Detach from the mapped file; Windows must be able to release it.
            return reader.get_tensor(key).clone()

    def optional(self, key):
        return self.tensor(key) if key in self.files else None

    def resolve(self, *candidates):
        for key in candidates:
            if key in self.files or key + ".weight" in self.files or key + ".weight_packed" in self.files:
                return key
        raise KeyError(f"Missing checkpoint tensor/module: {candidates}")

    def scheme(self, module):
        quant = self.config.get("quantization_config") or {}
        for pattern in quant.get("ignore", []):
            if module == pattern or (pattern.startswith("re:") and re.fullmatch(pattern[3:], module)):
                return None
        matches = []
        for scheme in quant.get("config_groups", {}).values():
            for target in scheme.get("targets", []):
                if target == "Linear" or target == module or (target.startswith("re:") and re.fullmatch(target[3:], module)):
                    matches.append(scheme)
                    break
        if len(matches) > 1:
            raise ValueError(f"Ambiguous quantization scheme for {module}")
        return matches[0] if matches else None

