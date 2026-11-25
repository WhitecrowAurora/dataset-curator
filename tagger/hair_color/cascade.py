"""按顺序复核；Qwen 独立看图，其文字判断不伪装成概率。"""
from __future__ import annotations

import time
from collections.abc import Callable

from .contracts import candidate, color_name


def review(pixai: dict, classify: Callable[[str], dict], qwen: Callable[[], dict], *,
           min_confidence=.7, media="anime", visible_hair="yes", reviewer="qwen3vl") -> dict:
    started = time.perf_counter()
    observations = [{"model": "pixai", **pixai}]
    result = {"status": "review", "selected": [], "reason": "模型发色仍有分歧",
              "observations": observations, "candidates": [], "extra_colors": []}

    def finish(color="", reason=""):
        result.update(status="accepted" if color else "review", selected=[color] if color else [],
                      reason=reason or result["reason"], elapsed_ms=round((time.perf_counter() - started) * 1000))
        return result

    def read_model(model):
        scores = classify(model)
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        if not ranked:
            raise ValueError(f"{model} 没有返回发色分数")
        observations.append({"model": model, "color": ranked[0][0],
                             "probability": ranked[0][1], "scores": scores})
        result["candidates"] = [candidate(color, score) for color, score in ranked[:3]]
        return ranked[0]

    if visible_hair == "no":
        finish(reason="已确认没有可见头发")
        result["status"] = "skipped"
        return result
    first, first_score = read_model("mobilenetv3_v0_dist")
    baseline = pixai.get("color", "")
    ambiguous = bool(pixai.get("ambiguous"))
    second, second_score = read_model("caformer_s36_v0")
    try:
        judgment = qwen()
        color = color_name(judgment["color"]) if judgment.get("color") else ""
        observations.append({"model": reviewer, **judgment, "color": color})
        result["extra_colors"] = list(dict.fromkeys(c for c in [baseline, color, *judgment.get("candidate_colors", [])]
                                                   if c and c != "light"))
    except Exception as exc:
        observations.append({"model": reviewer, "color": "", "error": str(exc)})
        result["extra_colors"] = [baseline] if baseline and baseline != "light" else []
        return finish(reason="VLM 复核不可用，请人工确认")
    subject = judgment.get("subject")
    visible = judgment.get("visible_hair")
    if (subject in {"animal", "doll", "statue", "object", "scene", "nonhuman"}
            and visible is False and not color and not judgment.get("multiple_people")):
        finish(reason="VLM 判断没有可见的人物头发")
        result["status"] = "skipped"
        return result
    if subject != "person" or visible is not True:
        return finish(reason="VLM 未确认是人物且存在可见头皮发，请人工确认")
    # PixAI's person score is supporting evidence, not a detector; disagreement only blocks auto-writeback.
    if pixai.get("person_support") is False:
        return finish(reason="VLM 判断有人物头发，但 PixAI 未确认人物，请人工确认")
    if ambiguous or judgment.get("multiple_people") or judgment.get("multi_color") or not color:
        return finish(reason="多人、双色或发色不明确，请人工确认")
    # light 只表示浅色组，不能作为具体发色的自动裁决。
    supporting = [baseline] if pixai.get("probability", 1) >= min_confidence else []
    if media == "anime":
        supporting.extend(c for c, score in ((first, first_score), (second, second_score))
                          if score >= min_confidence)
    if color != "light" and color in supporting:
        return finish(color, "VLM 与已有模型判断一致")
    return finish()
