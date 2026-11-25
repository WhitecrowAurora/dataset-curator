"""官方 ONNX 分类器与已有 PixAI / Qwen 服务的适配。"""
from __future__ import annotations

import base64
import contextlib
import io
import json
from pathlib import Path

import httpx

from .contracts import COLORS, LABELS, color_name
from ..runtime.model_cache import MODELS

REPO = "deepghs/anime_ch_hair_color"
REVISION = "c9af6e013b1984a7c06bc4a951f53c9fed98b956"
MODEL_NAMES = ("mobilenetv3_v0_dist", "caformer_s36_v0")
PERSON_TAGS = ("1girl", "1boy", *(f"{count}{group}" for group in ("girls", "boys")
                                      for count in ("2", "3", "4", "5", "6+")))
NONPERSON_SUBJECTS = frozenset(("animal", "doll", "statue", "object", "scene"))
MODEL_FILES = {
    "mobilenetv3_v0_dist": {"model.onnx": 16863428, "meta.json": 252},
    "caformer_s36_v0": {"model.onnx": 149640272, "meta.json": 293},
}


class HairModels:
    def __init__(self, root: Path):
        self.root = root

    def prepare(self):
        from huggingface_hub import hf_hub_download
        for name in MODEL_NAMES:
            for filename in ("model.onnx", "meta.json"):
                hf_hub_download(REPO, f"{name}/{filename}", revision=REVISION, local_dir=self.root)

    def predict(self, image, name: str) -> dict[str, float]:
        import numpy as np
        import onnxruntime as ort
        from PIL import Image

        if name not in MODEL_NAMES:
            raise ValueError("未知发色模型")
        def create():
            directory = self.root / name
            if not (directory / "model.onnx").is_file() or not (directory / "meta.json").is_file():
                self.prepare()
            labels = json.loads((directory / "meta.json").read_text(encoding="utf-8"))["labels"]
            if tuple(labels) != LABELS:
                raise ValueError("发色标签顺序与官方版本不一致")
            options = ort.SessionOptions()
            options.intra_op_num_threads = 4
            return ort.InferenceSession(str(directory / "model.onnx"),
                                        sess_options=options, providers=["CPUExecutionProvider"])
        path = self.root / name / "model.onnx"
        size = int(path.stat().st_size * 2) if path.is_file() else 512 << 20
        with MODELS.acquire(f"hair:{self.root.resolve()}:{name}", create, size) as session:
            inp = session.get_inputs()[0]
            height, width = inp.shape[-2:]
            height = height if isinstance(height, int) else 384
            width = width if isinstance(width, int) else 384
            data = np.asarray(image.resize((width, height), Image.Resampling.BILINEAR), dtype=np.float32)
            data = ((data / 255 - 0.5) / 0.5).transpose(2, 0, 1)[None]
            outputs = {item.name: value for item, value in zip(session.get_outputs(), session.run(None, {inp.name: data}))}
        scores = np.asarray(outputs["output"]).reshape(-1)
        if len(scores) != len(LABELS) or not np.isfinite(scores).all():
            raise ValueError("发色分类器输出不完整")
        return dict(zip(LABELS, map(float, scores)))


def pixai_baseline(scores: dict, source="inference") -> dict:
    hair = {}
    for name, score in scores.items():
        if name.replace("_", " ").endswith(" hair"):
            with contextlib.suppress(ValueError):
                hair[color_name(name)] = max(hair.get(color_name(name), 0), float(score))
    ranked = sorted(hair.items(), key=lambda item: item[1], reverse=True)
    person = max((float(scores.get(tag, 0)) for tag in PERSON_TAGS), default=0)
    ambiguous_tags = ("multicolored_hair", "multicolored hair", "2girls", "3girls", "4girls",
                      "5girls", "6+girls", "2boys", "3boys", "4boys", "5boys", "6+boys")
    multiple = scores.get("1girl", 0) > .5 and scores.get("1boy", 0) > .5
    return {"color": ranked[0][0] if ranked else "", "probability": ranked[0][1] if ranked else 0,
            "scores": hair, "source": source, "person_support": person > .5,
            "ambiguous": multiple or any(scores.get(tag, 0) > .5 for tag in ambiguous_tags)}


def pixai_predict(image, models_root: Path | None = None) -> dict:
    import torch
    from ..cpu_tagger import build_backend
    from .pixai_runtime import move_pipeline

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    def create():
        directory = models_root / "pixai" if models_root else None
        if directory is None or not (directory / "config.json").is_file():
            from huggingface_hub import snapshot_download
            directory = Path(snapshot_download("pixai-labs/pixai-tagger-v1.0", local_files_only=True))
        backend = build_backend("pixai", model_dir=directory, device=device, precision="bf16" if device != "cpu" else "fp32", verbose=False)
        backend.enable_hair_scores()
        backend.load()
        move_pipeline(backend._pipe, "cpu")
        return backend

    def release(backend):
        hook = getattr(backend, "_hair_hook", None)
        if hook is not None:
            hook.remove()
            backend._hair_hook = None
        backend._pipe = None

    with MODELS.acquire(f"pixai:hair:{models_root}", create, 2 << 30, release, "cpu_model") as backend:
        pipe = backend._pipe
        with backend._lock:
            move_pipeline(pipe, device)
            try:
                # 只跑一次未筛分数；不触发 snapshot_scores 的第二次前向。
                scores = backend.predict(image).hair_scores
                if scores is None:
                    raise ValueError("PixAI 没有返回同次前向的发色分数")
                return pixai_baseline(scores)
            finally:
                move_pipeline(pipe, "cpu")
                if device != "cpu":
                    torch.cuda.empty_cache()


def qwen_predict(image, url: str, model: str) -> dict:
    data = io.BytesIO()
    image.save(data, format="PNG")
    encoded = base64.b64encode(data.getvalue()).decode("ascii")
    prompt = (
        "Classify whether any real person or drawn humanlike character is visible. Use subject=person if at least "
        "one is present, including when animals or objects are also present. Use animal, doll, statue, object, or "
        "scene only when no person is visible; use uncertain when unclear. A human-shaped statue, plush, doll, or "
        "mannequin is not a person. An upright or clothed animal, animal-headed character, or furry/feathered "
        "creature is still animal unless a separate human or clearly humanlike drawn person is visible. Set "
        "multiple_people=true when more than one person is visible. "
        "Inspect only scalp hair growing from a person. Animal fur, manes, feathers, facial hair, wigs on objects, "
        "and carved hair are not human scalp hair. Set visible_hair=true only when a person's scalp hair is "
        "actually visible. For a non-person subject, visible_hair must be false and color must be null. For an "
        "uncertain subject, hidden scalp, or no visible scalp hair, use null or false for visible_hair as "
        "appropriate and color=null. Ignore clothing, background, accessories, shadows and colored lighting. "
        "Return only a JSON object with keys subject, color, visible_hair, multi_color, multiple_people, reason. "
        "color must be one of " + ", ".join(c for c in COLORS if c != "light") +
        ", or null if unclear/no visible scalp hair. Use white, silver, grey, blonde separately. "
        "multi_color and multiple_people must be booleans. reason is a short explanation."
    )
    from ..netutil import trust_env_for
    with httpx.Client(timeout=120, trust_env=trust_env_for(url)) as client:
        response = client.post(url.rstrip("/") + "/v1/chat/completions", json={
            "model": model, "temperature": 0, "max_tokens": 180,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
            ]}],
        })
        response.raise_for_status()
    text = response.json()["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    result = json.loads(text)
    if not isinstance(result, dict) or any(type(result.get(key)) is not bool for key in ("multi_color", "multiple_people")):
        raise ValueError("Qwen 未返回有效的发色 JSON 判断")
    raw_subject = result.get("subject")
    if not isinstance(raw_subject, str) or not raw_subject.strip():
        raise ValueError("Qwen 未返回有效的对象类型判断")
    subject = raw_subject.strip().lower().replace("_", " ")
    subject = {"human": "person", "humanoid": "person", "people": "person",
               "none": "scene", "no subject": "scene"}.get(subject, subject)
    if subject not in {"person", "animal", "doll", "statue", "object", "scene", "uncertain"}:
        subject = "uncertain"
    result["subject"] = subject
    visible = result.get("visible_hair")
    if visible is not None and type(visible) is not bool:
        raise ValueError("Qwen 未返回有效的头发可见性判断")
    raw_color = result.get("color")
    if raw_color is not None and not isinstance(raw_color, str):
        raise ValueError("Qwen 未返回有效的发色判断")
    color = ""
    if raw_color and raw_color.strip().lower() not in {"null", "none", "unknown", "uncertain"}:
        try:
            color = color_name(raw_color)
        except ValueError as exc:
            raise ValueError("Qwen 返回了不支持的发色") from exc
        if color == "light":
            raise ValueError("Qwen 返回了不能自动裁决的浅色组")

    # 颜色只有在模型同时确认“人物 + 可见头皮发”时才是有效证据。
    # 非人物主体不能有“人类头皮发”；将矛盾字段规范化，同时保留原值供审计。
    result.pop("color_warning", None)
    result.pop("visible_hair_warning", None)
    result.pop("raw_visible_hair", None)
    if subject in NONPERSON_SUBJECTS and visible is not False:
        result["raw_visible_hair"] = visible
        result["visible_hair_warning"] = "非人物主体不能提供人类头皮发证据，已规范为不可见"
        visible = False
    if color and (subject != "person" or visible is not True):
        result["raw_color"] = color
        result["color_warning"] = "没有确认可见的人物头皮发，已丢弃颜色证据"
        color = ""
    result.pop("candidate_colors", None)
    result["visible_hair"] = visible
    result["color"] = color or None
    result["raw"] = text
    return result
