"""发色流程的固定模型包；与通用 tag / NL 清单分开管理。"""
from __future__ import annotations

from pathlib import Path

from ..hair_color.contracts import HairWorkflowSettings
from ..hair_color.models import MODEL_FILES, MODEL_NAMES, REPO, REVISION
from ..storage.atomic import read_json
from ..inference import media_route
from . import downloads as dl

TORII_KEY = "torii-gate-0.5"
TORII_REPO = "DraconicDragon/ToriiGate-0.5-GGUF"
TORII_REVISION = "8bebf1fbf883b6d8f93ff3e8d157c615fe6b608c"
TORII_FILES = {
    "ToriiGate-0.5-Q6_K.gguf": 3985524576,
    "ToriiGate-0.5-fp16.mmproj.gguf": 672423360,
}


def catalog() -> list[dict]:
    entries = []
    for key in (*MODEL_NAMES, TORII_KEY, "anime-real"):
        torii = key == TORII_KEY
        media = key == "anime-real"
        dest = dl.MODELS_ROOT / (f"nl/{key}" if torii else f"media/{media_route.MODEL}" if media else f"hair-color/{key}")
        expected = TORII_FILES if torii else media_route.FILES if media else MODEL_FILES[key]
        missing = [name for name, size in expected.items()
                   if not (dest / name).is_file() or (dest / name).stat().st_size != size]
        state = dl.DOWNLOADS.state_of("review", key)
        try:
            shown = dest.relative_to(dl.ROOT).as_posix()
        except ValueError:
            shown = str(dest)
        entry = {
            "key": key,
            "title": "ToriiGate-0.5" if torii else "动漫 / 真人自动分流" if media else ("MobileNetV3 发色" if key == MODEL_NAMES[0] else "CAFormer 发色"),
            "note": ("动漫描述与补充复核 · llama.cpp Q6_K + FP16 视觉编码器" if torii
                     else "CPU MobileNetV3 · 未知来源逐图判定；低置信使用 Qwen · OpenRAIL" if media
                     else "CPU ONNX · 10 类发色；银、灰、白、金统一为浅色组"),
            "download_repo": TORII_REPO if torii else media_route.REPO if media else REPO,
            "revision": TORII_REVISION if torii else media_route.REVISION if media else REVISION,
            "local_dir": shown, "local_abs": str(dest),
            "missing_files": missing, "ready": not missing,
            "download": state,
        }
        if torii:
            settings = read_json(dl.ROOT / "work/conflicts/hair/settings.json")
            settings = settings if isinstance(settings, dict) else {}
            entry["configured_path"] = settings.get("torii_dir", HairWorkflowSettings().torii_dir)
            entry["use_path"] = f"{shown}/ToriiGate-0.5-Q6_K.gguf"
            configured = Path(entry["configured_path"])
            configured = configured if configured.is_absolute() else dl.ROOT / configured
            entry["in_use"] = configured.resolve() in (dest.resolve(), (dest / "ToriiGate-0.5-Q6_K.gguf").resolve())
        entries.append(entry)
    return entries


def start_download(key: str, *, proxy: str = "", token: str | None = None) -> dict:
    if key == TORII_KEY:
        repo, revision = TORII_REPO, TORII_REVISION
        dest, allow = dl.MODELS_ROOT / f"nl/{key}", list(TORII_FILES)
    elif key in MODEL_NAMES:
        repo, revision = REPO, REVISION
        dest = dl.MODELS_ROOT / "hair-color"
        allow = [f"{key}/model.onnx", f"{key}/meta.json"]
    elif key == "anime-real":
        repo, revision = media_route.REPO, media_route.REVISION
        dest = dl.MODELS_ROOT / "media"
        allow = [f"{media_route.MODEL}/{name}" for name in media_route.FILES]
    else:
        raise ValueError(f"未知的复核模型：{key}")
    from ..nl_api import resolve_hf_token

    return dl.DOWNLOADS.start("review", key, repo, dest, proxy=proxy,
                              allow=allow, revision=revision,
                              token=resolve_hf_token(repo, token or ""))
