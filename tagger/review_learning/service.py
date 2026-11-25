from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..storage.atomic import read_json, write_json
from ..storage.review_feedback import events, summary
from .features import hair_features, text_features
from .ranker import scores, train

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-learning")
_LOCK = threading.Lock()
_PENDING = {}
_ERRORS = {}
_TRAIN_LOCK = threading.Lock()


def sources(root):
    return (Path(root) / "decisions.sqlite3", Path(root) / "hair/reviews.sqlite3")


def model_path(root):
    return Path(root) / "learning/preference_model.json"


def collect(root):
    return [event for path in sources(root) for event in events(path)]


def retrain(root):
    with _TRAIN_LOCK:
        root = Path(root)
        model = train(collect(root))
        model["sources"] = {str(path): summary(path)["latest_id"] for path in sources(root)}
        write_json(model_path(root), model)
        return model


def schedule(root):
    root = Path(root).resolve()
    with _LOCK:
        if root in _PENDING:
            _PENDING[root] = True
            return
        _PENDING[root] = False
    def run():
        try:
            while True:
                retrain(root)
                with _LOCK:
                    _ERRORS.pop(root, None)
                    if not _PENDING[root]:
                        _PENDING.pop(root)
                        return
                    _PENDING[root] = False
        except Exception as exc:
            with _LOCK:
                _PENDING.pop(root, None)
                _ERRORS[root] = str(exc)
    _EXECUTOR.submit(run)


def status(root):
    model = read_json(model_path(root)) or {"models": {}}
    key = Path(root).resolve()
    with _LOCK:
        running, error = key in _PENDING, _ERRORS.get(key)
    return {"events": sum(summary(path)["events"] for path in sources(root)), "training": running,
            "error": error, "trained_at": model.get("trained_at"), "models": model["models"]}


def annotate(payload, root):
    model = read_json(model_path(root)) or {}
    for item in payload.get("items", []):
        candidates = item.get("candidates", [])
        values = scores(model.get("models", {}).get(item["kind"]), [text_features(c["value"]) for c in candidates])
        for candidate, score in zip(candidates, values):
            candidate["preference"] = score
    return payload


def annotate_hair(payload, root):
    model = (read_json(model_path(root)) or {}).get("models", {}).get("hair")
    for record in payload.get("items", []):
        candidates = record.get("candidates", [])
        values = scores(model, [hair_features(record, c["color"]) for c in candidates])
        for candidate, score in zip(candidates, values):
            candidate["preference"] = score
    return payload
