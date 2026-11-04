"""打标质量评分与重试回环。

分三层，越往后越贵，所以默认只跑前两层：

    L1 规则层（几乎零成本，必跑）
       长度、重复率、拒答模式、tag 覆盖率、格式完整性

    L2 语义层（可选，需装 open_clip / transformers）
       图文 CLIP 相似度，用来抓"句子通顺但完全跑题"

    L3 复核层（可选，最贵）
       换一个 prompt 让 VLM 判断 caption 里的关键断言是否成立

评分维度仿照 sucai 里 fusion_scorer 的 1-5 分制，但评的对象不同：
那边评图像美学（aesthetic/composition/color），这里评标注质量
（faithful/completeness/fluency）。

分数出来后的动作：
    pass   直接收
    retry  调整参数重打（温度上调、提高分辨率、必要时换更大模型）
    drop   直接丢，别浪费算力
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .schema import Annotation


# ------------------------------------------------------------------ 策略


@dataclass(slots=True)
class QualityPolicy:
    # L1 规则
    # 字符兜底必须跟着 token 预算走：英文约 4.4 字符/token，
    # 32 token ≈ 140 字符、256 token ≈ 1130 字符。两边不一致时，
    # "token 在预算内但字符数不够"会被字符线判成过短，白丢合格样本。
    min_chars: int = 140
    max_chars: int = 1200
    min_tag_coverage: float = 0.60
    max_repeat_ratio: float = 0.35
    repeat_ngram: int = 4

    # NL 长度预算（token 口径）。0 表示不检查。
    # 用 token 而不是字符：中英混排时字符数的等效 token 浮动很大，
    # 32/256 这种区间只有按 token 量才对得上。
    nl_min_tokens: int = 32
    nl_max_tokens: int = 256

    # 判定线（1-5 分制）
    pass_score: float = 3.4
    drop_score: float = 1.8

    # 重试
    max_attempts: int = 2
    retry_temp_step: float = 0.15
    retry_temp_cap: float = 0.7
    retry_pixels_scale: float = 1.5
    retry_pixels_cap: int = 1048576
    escalate_model: bool = False   # 重试用尽仍不合格时是否建议换 8B

    # L2
    enable_clip: bool = False
    clip_min_score: float = 0.22
    clip_model: str = "ViT-L-14"
    clip_pretrained: str = "openai"

    @classmethod
    def from_dict(cls, d: dict) -> "QualityPolicy":
        allowed = set(cls.__slots__)
        return cls(**{k: v for k, v in (d or {}).items() if k in allowed})


# ------------------------------------------------------------------ 结果


@dataclass(slots=True)
class Dimensions:
    faithful: float = 3.0
    completeness: float = 3.0
    fluency: float = 3.0

    @property
    def score(self) -> float:
        """忠实度权重最高——训练信号错在内容上比错在文风上严重得多。"""
        return round(self.faithful * 0.5 + self.completeness * 0.3 + self.fluency * 0.2, 3)

    def to_dict(self) -> dict:
        return {"faithful": round(self.faithful, 2),
                "completeness": round(self.completeness, 2),
                "fluency": round(self.fluency, 2),
                "score": self.score}


@dataclass(slots=True)
class RetryPlan:
    attempt: int
    temperature: float
    max_pixels: int
    model_hint: str = "same"     # same | escalate


@dataclass(slots=True)
class Verdict:
    level: str                    # pass | retry | drop
    score: float
    dims: Dimensions
    reasons: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    retry: RetryPlan | None = None

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "score": self.score,
            "dims": self.dims.to_dict(),
            "reasons": self.reasons,
            "signals": self.signals,
            "retry": None if self.retry is None else {
                "attempt": self.retry.attempt,
                "temperature": self.retry.temperature,
                "max_pixels": self.retry.max_pixels,
                "model_hint": self.retry.model_hint,
            },
        }


# ------------------------------------------------------------------ 规则工具

_REFUSAL_PATTERNS = re.compile(
    r"\b(i'?m sorry|i cannot|i can'?t|as an ai|unable to (describe|view)|"
    r"i don'?t see|no image (provided|attached)|无法(描述|查看|识别)|抱歉[，,])",
    re.IGNORECASE,
)


def repeat_ratio(text: str, n: int = 4) -> float:
    """n-gram 重复率。VLM 量化后最容易出现的就是复读。"""
    words = re.findall(r"[\w']+", text.lower())
    if len(words) < n * 2:
        return 0.0
    grams = [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]
    if not grams:
        return 0.0
    return 1.0 - len(set(grams)) / len(grams)


def looks_like_refusal(text: str) -> bool:
    return bool(_REFUSAL_PATTERNS.search(text))


_TAG_ALIASES = {
    "1girl": ["girl", "woman", "female", "lady"],
    "1boy": ["boy", "man", "male", "gentleman"],
    "solo": ["alone", "single"],
    "looking at viewer": ["looking at the viewer", "facing the viewer",
                          "looking at the camera", "towards the viewer"],
    "long hair": ["long hair", "hair falling", "waist-length hair"],
}


def _phrase_matches(caption_low: str, phrase: str) -> bool:
    parts = [p for p in phrase.split() if p]
    if not parts:
        return True
    if len(parts) == 1:
        return parts[0] in caption_low
    return all(p in caption_low for p in parts)


def tag_matches(caption_low: str, tag: str) -> bool:
    """tag 是否被 caption 覆盖。

    多词 tag 要求每个词都出现但**不要求连续**——caption 里写 "long dark hair"
    时，`long_hair` 应该算覆盖，用连续子串匹配会漏判一大批。
    """
    t = tag.replace("_", " ").strip().lower()
    if t in _TAG_ALIASES:
        return any(_phrase_matches(caption_low, a) for a in _TAG_ALIASES[t])
    return _phrase_matches(caption_low, t)


def tag_coverage(caption: str, tags: list[tuple[str, float]],
                 *, min_prob: float = 0.6) -> tuple[float, list[str]]:
    """高置信 tag 在 caption 里被覆盖的比例，返回 (比例, 缺失列表)。"""
    strong = [t for t, p in tags if p >= min_prob]
    if not strong:
        return 1.0, []
    low = caption.lower()
    missing = [t for t in strong if not tag_matches(low, t)]
    return (len(strong) - len(missing)) / len(strong), missing


# ------------------------------------------------------------------ CLIP


class ClipScorer:
    """图文相似度。可选，缺依赖时自动跳过而不是报错。

    两个必须自己处理的问题：

    1. **线程安全**。打分是同一个模型实例上的前向，batch 维度还带隐式状态
       （`self._model` 会被搬到 cuda）。并发跑会互相踩权重与显存，所以这里
       加锁，并且 run_client 在启用 CLIP 时把并发摁到 1——锁只能保证不崩，
       保证不了吞吐。
    2. **设备只搬一次**。原来每次 score 都 `.cuda()`，于是每张图都要过一遍
       显存分配；而 CLIP 的模型留在 CPU 上又会把单张打分拖到几百毫秒。
    """

    def __init__(self, model_name: str = "ViT-L-14", pretrained: str = "openai"):
        self.available = False
        self.reason = ""          # 不可用时给人看的一句话（页面会显示）
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._torch = None
        self._device = "cpu"
        self._lock = threading.Lock()
        try:
            import open_clip  # type: ignore
            import torch  # type: ignore

            self._torch = torch
            self._model, _, self._preprocess = open_clip.create_model_and_transforms(
                model_name, pretrained=pretrained)
            self._model.eval()
            self._tokenizer = open_clip.get_tokenizer(model_name)
            self.available = True
        except Exception as exc:          # noqa: BLE001 - 缺依赖是常态，不该报错
            self.available = False
            self.reason = (f"CLIP 不可用（{type(exc).__name__}: {exc}）。"
                           f"装依赖：pip install open_clip_torch")

    def score(self, image, caption: str) -> float | None:
        if not self.available or image is None or not caption.strip():
            return None
        with self._lock:
            try:
                torch = self._torch
                if torch.cuda.is_available() and self._device != "cuda":
                    self._model = self._model.cuda()
                    self._device = "cuda"
                img = self._preprocess(image).unsqueeze(0)
                txt = self._tokenizer([caption])
                if self._device == "cuda":
                    img, txt = img.cuda(), txt.cuda()
                with torch.no_grad():
                    i_f = self._model.encode_image(img)
                    t_f = self._model.encode_text(txt)
                i_f = i_f / i_f.norm(dim=-1, keepdim=True)
                t_f = t_f / t_f.norm(dim=-1, keepdim=True)
                return float((i_f @ t_f.T).squeeze().item())
            except Exception:             # noqa: BLE001 - 打分失败不该中断打标
                return None


# ------------------------------------------------------------------ 门禁


class QualityGate:
    def __init__(self, policy: QualityPolicy | None = None, *,
                 clip: ClipScorer | None = None, verbose: bool = True):
        self.policy = policy or QualityPolicy()
        self.clip = clip if (clip and clip.available) else (
            ClipScorer(self.policy.clip_model, self.policy.clip_pretrained)
            if self.policy.enable_clip else None)
        self.verbose = verbose

    def evaluate(self, ann: Annotation, *, attempt: int = 0,
                 image=None, temperature: float = 0.2,
                 max_pixels: int = 589824) -> Verdict:
        p = self.policy
        text = ann.caption_dense or ann.caption_medium or ""
        stripped = text.strip()
        reasons: list[str] = []

        # ---- 空产出检测（最优先）
        # 这不是"质量低"，而是"这条数据根本不存在"：模型没吐内容、
        # 或者 JSON 结构塌了。必须直接丢并单独计数，不能混进重试队列
        # 当成"还能救的样本"——那样统计口径会被污染，看起来合格率还行，
        # 实际上一批数据全是空的。
        has_tag = bool(ann.tag_line.strip() or ann.tags)
        if not stripped and not has_tag:
            return Verdict(
                level="drop", score=1.0,
                dims=Dimensions(faithful=1.0, completeness=1.0, fluency=1.0),
                reasons=["输出为空：caption / tag / tag_line 全缺失"],
                signals={"empty": True, "chars": 0},
            )

        # ---- L1 规则
        # tag-only 模式下 caption 为空是预期，覆盖率改用 tag_line 评，
        # 否则每条都会被判成"覆盖率 0"而进重试队列。
        coverage_text = stripped or ann.tag_line
        coverage, missing = tag_coverage(coverage_text, ann.tags)
        rr = repeat_ratio(coverage_text, p.repeat_ngram) if stripped else 0.0
        refused = looks_like_refusal(text) if stripped else False
        chars = len(stripped)
        tokens = ann.qc.nl_tokens

        signals = {
            "chars": chars,
            "nl_tokens": tokens,
            "tag_coverage": round(coverage, 3),
            "missing_tags": missing[:8],
            "repeat_ratio": round(rr, 3),
            "refused": refused,
        }

        # 忠实度：覆盖率为主，CLIP 为辅
        faithful = 1.0 + coverage * 3.0
        if self.clip and self.clip.available and image is not None and stripped:
            cs = self.clip.score(image, text)
            if cs is not None:
                signals["clip_score"] = round(cs, 4)
                if cs < p.clip_min_score:
                    reasons.append(f"图文相似度低 ({cs:.2f} < {p.clip_min_score})")
                    faithful = min(faithful, 2.5)
                    signals["clip_low"] = True

        # 完整度：token 预算优先，字符数兜底
        # 只要 tag 不产 NL 的模式下，caption 为空是预期的，不扣分
        if not stripped and has_tag:
            # tag-only 模式：没有 NL 是设计如此，不按长度扣分
            completeness = 5.0
        elif p.nl_min_tokens and tokens and tokens < p.nl_min_tokens:
            completeness = 2.0
            reasons.append(f"NL 过短 ({tokens} < {p.nl_min_tokens} token)")
            signals["nl_too_short"] = True
        elif p.nl_max_tokens and tokens and tokens > p.nl_max_tokens:
            completeness = 2.5
            reasons.append(f"NL 过长 ({tokens} > {p.nl_max_tokens} token)")
            signals["nl_too_long"] = True
        elif chars < p.min_chars:
            completeness = max(1.0, 1.0 + 4.0 * chars / max(p.min_chars, 1) * 0.6)
            reasons.append(f"caption 过短 ({chars} < {p.min_chars})")
        elif chars > p.max_chars:
            completeness = 2.5
            reasons.append(f"caption 过长 ({chars} > {p.max_chars})")
        else:
            completeness = 5.0

        # 流畅度：重复惩罚
        if rr <= p.max_repeat_ratio:
            fluency = 5.0 - rr * (2.0 / max(p.max_repeat_ratio, 1e-6))
        else:
            fluency = max(1.0, 3.0 - (rr - p.max_repeat_ratio) * 6.0)
            reasons.append(f"重复率过高 ({rr:.2f} > {p.max_repeat_ratio})")

        if refused:
            faithful = 1.0
            fluency = 1.0
            reasons.append("疑似拒答")

        if coverage < p.min_tag_coverage and ann.tags:
            reasons.append(f"tag 覆盖率 {coverage:.0%} < {p.min_tag_coverage:.0%}")
            signals["low_coverage"] = True

        dims = Dimensions(faithful=max(1.0, min(5.0, faithful)),
                          completeness=max(1.0, min(5.0, completeness)),
                          fluency=max(1.0, min(5.0, fluency)))
        score = dims.score

        # ---- 判定
        if refused or score < p.drop_score:
            level = "drop"
        elif score < p.pass_score:
            level = "retry" if attempt < p.max_attempts else "drop"
        else:
            level = "pass"

        retry = None
        if level == "retry":
            retry = RetryPlan(
                attempt=attempt + 1,
                temperature=min(temperature + p.retry_temp_step * (attempt + 1),
                                p.retry_temp_cap),
                max_pixels=min(int(max_pixels * p.retry_pixels_scale ** (attempt + 1)),
                               p.retry_pixels_cap),
                model_hint="escalate" if (p.escalate_model and attempt + 1 >= p.max_attempts)
                else "same",
            )

        return Verdict(level=level, score=score, dims=dims, reasons=reasons,
                       signals=signals, retry=retry)


# ------------------------------------------------------------------ 重试队列


class RetryQueue:
    """把需要重打的样本单独记下来，不塞回主队列。

    独立文件的好处：可以按原因聚合统计（哪类图容易失败），
    也方便人工抽检—如果某一批全部落进 retry，多半是 prompt 或分辨率有问题。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def push(self, ann: Annotation, verdict: Verdict) -> None:
        rec = {
            "image_sha256": ann.image_sha256,
            "image_path": ann.image_path,
            "model_id": ann.provenance.model_id if ann.provenance else "",
            "ts": time.time(),
            "verdict": verdict.to_dict(),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def pending(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        return out

    def summary(self) -> dict:
        items = self.pending()
        by_reason: dict[str, int] = {}
        for it in items:
            for r in it.get("verdict", {}).get("reasons", []):
                key = r.split("(")[0].strip()
                by_reason[key] = by_reason.get(key, 0) + 1
        return {"total": len(items), "by_reason": by_reason}
