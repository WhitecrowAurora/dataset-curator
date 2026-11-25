"""逐图媒体判定；明确来源优先，未知图片使用 CPU 二分类器。"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
import zipfile
from pathlib import Path

from ..runtime.model_cache import MODELS

REPO = "deepghs/anime_real_cls"
REVISION = "097a6c1e9e62866a435810c883a45a7cb2c7077d"
MODEL = "mobilenetv3_v1.4_dist"
FILES = {"model.onnx": 16821153, "meta.json": 118}


def normalize_media(value):
    name = str(value or "").strip().lower().replace("-", "_")
    if name in {"anime", "manga", "danbooru", "illustration_anime", "二次元", "动漫", "漫画"}:
        return "anime"
    if name in {"photo", "real", "photograph", "photography", "真人", "照片", "realistic"}:
        return "photo"
    return "unknown"


class MediaRouter:
    def __init__(self, model_root: Path, state: Path, log=lambda message: None):
        self.root, self.log = Path(model_root), log
        state.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state)
        self.db.execute("CREATE TABLE IF NOT EXISTS routes (digest TEXT PRIMARY KEY, result TEXT)")
        self.warned = False
        self.unavailable = ""

    def close(self):
        self.db.close()

    def predict(self, image):
        import numpy as np
        import onnxruntime as ort
        from PIL import Image

        directory = self.root / MODEL
        def create():
            from huggingface_hub import hf_hub_download
            for name in FILES:
                if not (directory / name).is_file():
                    hf_hub_download(REPO, f"{MODEL}/{name}", revision=REVISION, local_dir=self.root)
            if json.loads((directory / "meta.json").read_text(encoding="utf-8"))["labels"] != ["anime", "real"]:
                raise ValueError("动漫/真人分类器标签顺序不匹配")
            options = ort.SessionOptions()
            options.intra_op_num_threads = 4
            return ort.InferenceSession(str(directory / "model.onnx"), sess_options=options,
                                        providers=["CPUExecutionProvider"])
        with MODELS.acquire(f"media:{directory.resolve()}", create, 64 << 20) as session:
            inp = session.get_inputs()[0]
            height, width = inp.shape[-2:]
            data = np.asarray(image.resize((width, height), Image.Resampling.BILINEAR), dtype=np.float32)
            data = ((data / 255 - .5) / .5).transpose(2, 0, 1)[None]
            output = dict(zip((v.name for v in session.get_outputs()), session.run(None, {inp.name: data})))
            scores = np.asarray(output["output"]).reshape(-1)
        if scores.size != 2 or not np.isfinite(scores).all() or (scores < 0).any() or (scores > 1).any():
            raise ValueError("动漫/真人分类器概率无效")
        winner = int(scores.argmax())
        return {"media": ("anime", "photo")[winner] if scores[winner] >= .8 else "unknown",
                "source": REPO, "revision": REVISION, "scores": dict(zip(("anime", "photo"), map(float, scores)))}

    def resolve(self, raw, media="", *, source_sha256=""):
        explicit = normalize_media(media)
        if explicit != "unknown":
            return {"media": explicit, "source": "dataset", "original": media}
        digest = (source_sha256 or hashlib.sha256(raw).hexdigest()) + ":" + REVISION
        row = self.db.execute("SELECT result FROM routes WHERE digest=?", (digest,)).fetchone()
        if row:
            return json.loads(row[0])
        if self.unavailable:
            return {"media": "unknown", "source": "fallback", "error": self.unavailable}
        from PIL import Image, ImageOps
        try:
            with Image.open(io.BytesIO(raw)) as source:
                image = ImageOps.exif_transpose(source).convert("RGBA")
                image = Image.alpha_composite(Image.new("RGBA", image.size, "white"), image).convert("RGB")
            try:
                result = self.predict(image)
            except Exception as exc:
                self.unavailable = str(exc)
                raise
        except Exception as exc:
            if not self.warned:
                self.log(f"逐图媒体判定不可用，未知图片使用 Qwen：{exc}")
                self.warned = True
            return {"media": "unknown", "source": "fallback", "error": str(exc)}
        self.db.execute("INSERT OR REPLACE INTO routes VALUES (?,?)", (digest, json.dumps(result)))
        self.db.commit()
        return result

    def select(self, kind, task, data, reader):
        explicit = normalize_media(task.media)
        if explicit == "unknown" and data is None:
            try:
                data = reader.read(task.shard, task.file)
            except (OSError, KeyError, RuntimeError, tarfile.TarError, zipfile.BadZipFile):
                return kind == "qwen", task, data
        result = self.resolve(data, task.media) if explicit == "unknown" else {
            "media": explicit, "source": "dataset", "original": task.media}
        from dataclasses import replace
        task = replace(task, media=result["media"], media_route=result)
        return ("torii" if result["media"] == "anime" else "qwen") == kind, task, data
