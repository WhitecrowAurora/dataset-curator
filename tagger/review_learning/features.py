from __future__ import annotations

import math
import re

TEXT_FEATURES = ("length", "sentences", "chinese", "quotes", "repetition", "markup", "hedging", "quality_words", "colors", "materials", "positions", "actions")
HAIR_MODELS = ("pixai", "mobilenetv3_v0_dist", "caformer_s36_v0", "qwen3vl", "toriigate")
HAIR_FEATURES = tuple(f"{model}_{signal}" for model in HAIR_MODELS for signal in ("vote", "confidence")) + ("candidate_probability", "vote_fraction")


def text_features(text):
    words = re.findall(r"\w+", text.lower())
    ngrams = list(zip(words, words[1:], words[2:]))
    length = max(1, len(text))
    count = lambda pattern: min(1., len(re.findall(pattern, text, re.I)) / 5)
    return [math.log1p(length) / 10, count(r"[.!?。！？]"),
            len(re.findall(r"[\u4e00-\u9fff]", text)) / length,
            count(r'["“「]'), 1 - len(set(ngrams)) / max(1, len(ngrams)) if ngrams else 0.,
            count(r"[#*`]|^\s*[-•]"), count(r"\b(?:perhaps|maybe|probably|possibly|seems)\b|可能|似乎|大概"),
            count(r"\b(?:masterpiece|best quality|8k|ultra detailed)\b|杰作|顶级画质"),
            count(r"\b(?:blue|red|green|black|white|silver|grey|brown|pink|blonde|purple)\b|[蓝红绿黑白银灰棕粉金紫]色"),
            count(r"\b(?:metal|cloth|glass|matte|glossy|wood|silk|leather)\b|金属|布料|玻璃|哑光|光泽|木质"),
            count(r"\b(?:left|right|behind|foreground|background|above|below)\b|左侧|右侧|背景|前景|上方|下方"),
            count(r"\b(?:standing|sitting|holding|walking|running|looking)\b|站立|坐着|拿着|行走|奔跑|注视")]


def hair_features(record, color):
    values = []
    observations = record.get("observations", [])
    for model in HAIR_MODELS:
        found = [o for o in observations if o.get("model") == model and not o.get("error")]
        selected = next((o for o in found if o.get("color") == color), None)
        values.extend((float(selected is not None), float(selected.get("probability", 0) or 0) if selected else 0.))
    probability = next((float(c.get("probability", 0)) for c in record.get("candidates", []) if c.get("color") == color), 0.)
    return values + [probability, sum(values[::2]) / max(1, len(HAIR_MODELS))]


def pairs(event):
    if event["action"] != "confirm":
        return []
    data = event["value"]
    if event["kind"] == "hair":
        chosen = data["after"].get("selected", [])
        record = data["before"]
        alternatives = {c["color"] for c in record.get("candidates", [])}
        alternatives.update(o["color"] for o in record.get("observations", []) if o.get("color"))
        alternatives.discard("light")
        return [(hair_features(record, good), hair_features(record, bad)) for good in chosen for bad in sorted(alternatives - set(chosen))]
    candidates = data.get("candidates", [])
    chosen = next((c for c in candidates if c["digest"] == data.get("pick")), None)
    if chosen is None:
        return []
    return [(text_features(chosen["value"]), text_features(c["value"])) for c in candidates if c["digest"] != chosen["digest"]]
