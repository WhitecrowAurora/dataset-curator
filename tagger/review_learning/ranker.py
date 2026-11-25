"""小型成对偏好模型；按图片留出评估，分数仅用于人工审查建议。"""
from __future__ import annotations

import hashlib
import time

from .features import HAIR_FEATURES, TEXT_FEATURES, pairs


def _fit(rows):
    import numpy as np
    x = np.asarray(rows, dtype=np.float64)
    weights = np.zeros(x.shape[1], dtype=np.float64)
    for _ in range(160):
        margin = np.clip(x @ weights, -30, 30)
        gradient = x.T @ (1 / (1 + np.exp(margin))) / len(x)
        weights += .7 * (gradient - .02 * weights)
    return weights


def train(events):
    import numpy as np
    models = {}
    for kind in ("tag", "nl", "hair"):
        examples = []
        for event in events:
            if event["kind"] == kind:
                examples.extend((event["image_key"], np.subtract(good, bad).tolist()) for good, bad in pairs(event))
        if not examples:
            continue
        groups = sorted({image for image, _ in examples}, key=lambda value: hashlib.sha256(value.encode()).hexdigest())
        holdout = set(groups[:max(1, len(groups) // 5)]) if len(groups) >= 5 else set()
        training = [row for image, row in examples if image not in holdout]
        validation = [row for image, row in examples if image in holdout]
        weights = _fit(training)
        accuracy = None
        if validation:
            margins = np.asarray(validation) @ weights
            accuracy = float(np.mean(np.where(margins > 1e-8, 1., np.where(margins < -1e-8, 0., .5))))
        models[kind] = {"weights": weights.tolist(), "features": list(HAIR_FEATURES if kind == "hair" else TEXT_FEATURES),
                        "images": len(groups), "pairs": len(examples), "training_images": len(groups) - len(holdout),
                        "validation_images": len(holdout), "validation_pairs": len(validation), "validation_accuracy": accuracy,
                        "training_groups": [g for g in groups if g not in holdout], "validation_groups": sorted(holdout)}
    return {"version": 1, "trained_at": time.time(), "models": models, "window": 5000}


def scores(model, vectors):
    if not model or not vectors:
        return []
    import numpy as np
    values = np.asarray(vectors, dtype=np.float64) @ np.asarray(model["weights"])
    # 候选集内的相对偏好，不是标注正确率或绝对质量分。
    probabilities = np.exp(np.clip(values - values.max(), -30, 0))
    return (probabilities / probabilities.sum()).tolist()
