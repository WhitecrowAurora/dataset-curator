"""发色复核编排：单图后台处理，批量任务按阶段提交与结算。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageOps

from ..storage.hair_reviews import HairReviews
from ..storage.atomic import read_json, write_json
from ..runtime.model_cache import MODELS
from .cascade import review
from .contracts import HairReviewRequest, HairWorkflowSettings, color_name
from .models import HairModels, pixai_baseline, pixai_predict
from .runtime import ReviewRuntime
from .vlm import predict


class HairReviewService:
    def __init__(self, root: Path, models_root: Path):
        self.store = HairReviews(root)
        self.store.mark_interrupted()
        self.root = models_root.parent.parent
        self.settings_path = root / "settings.json"
        self.models = HairModels(models_root)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hair-review")
        self.lock = threading.RLock()
        self.settings_lock = threading.RLock()
        self.writeback_lock = threading.RLock()
        self.runtime = ReviewRuntime(self.root)
        self.settings = HairWorkflowSettings.model_validate(read_json(self.settings_path) or {})
        self.configure(self.settings, persist=False)

    def configure(self, settings, persist=True):
        with self.settings_lock:
            self.settings = settings
            MODELS.configure(settings.cache_enabled, settings.cache_max_gb, settings.memory_reserve_gb)
            if persist:
                write_json(self.settings_path, settings.model_dump())
        return self.snapshot()

    def snapshot(self):
        return {"settings": self.settings.model_dump(), "cache": MODELS.snapshot()}

    def close(self):
        self.executor.shutdown(wait=True)
        self.runtime.stop()
        MODELS.close()

    def submit(self, request: HairReviewRequest) -> dict:
        try:
            raw = base64.b64decode(request.image_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("请提供可读取的图片") from exc
        return self.enqueue(raw, request.model_dump(exclude={"image_base64"}), run=True)

    def enqueue(self, raw, fields, *, run=False, review_id=None):
        try:
            with Image.open(io.BytesIO(raw)) as source:
                image = ImageOps.exif_transpose(source).convert("RGBA")
                background = Image.new("RGBA", image.size, "white")
                image = Image.alpha_composite(background, image).convert("RGB")
        except (ValueError, binascii.Error, OSError) as exc:
            raise ValueError("请提供可读取的图片") from exc
        crop = fields.get("crop")
        if crop is not None:
            x0, y0, x1, y1 = crop
            if not (0 <= x0 < x1 <= image.width and 0 <= y0 < y1 <= image.height):
                raise ValueError("头部裁框必须位于图片内，格式为 x0,y0,x1,y1")
        if fields.get("pixai_color"):
            color_name(fields["pixai_color"])
        review_id = review_id or uuid.uuid4().hex
        directory = self.store.root / "images"
        directory.mkdir(parents=True, exist_ok=True)
        image.save(directory / f"{review_id}.png")
        record = {"id": review_id, "image_name": Path(fields.get("image_name", "图片")).name,
                  "image_sha256": hashlib.sha256(raw).hexdigest(), "crop": crop,
                  "created": time.time(), "status": "pending", "selected": [],
                  "candidates": [], "observations": [], "reason": "等待模型复核",
                  "request": fields, "media": fields.get("media", self.settings.media),
                  "parent_id": fields.get("parent_id", ""), "job_id": fields.get("job_id", "")}
        self.store.put(record)
        if run:
            self.executor.submit(self.run_records, [review_id])
        return record

    def run_records(self, ids, should_stop=lambda: False, *, writeback_results=True):
        from ..runtime.inference_queue import INFERENCE_LOCK
        with INFERENCE_LOCK, self.lock:
            settings = self.settings.model_copy()
            from ..inference.media_route import MediaRouter, normalize_media
            router = MediaRouter(self.root / "models/media", self.store.root / "media-routes.sqlite")
            try:
                # 先完成 PixAI，再分数据类型运行 VLM，避免逐图切换模型。
                for review_id in ids:
                    if should_stop():
                        return
                    record = self.store.get(review_id)
                    if record["status"] not in ("pending", "failed"):
                        continue
                    record.update(status="pending", reason="等待模型复核")
                    self.store.put(record)
                    fields = record["request"]
                    canonical = normalize_media(record["media"])
                    if canonical == "unknown":
                        try:
                            raw = (self.store.root / "images" / f"{review_id}.png").read_bytes()
                        except OSError as exc:
                            record.update(status="failed", reason=f"复核图片不可读：{exc}")
                            self.store.put(record)
                            continue
                        fields["media_route"] = router.resolve(raw, source_sha256=record["image_sha256"])
                        canonical = fields["media_route"]["media"]
                    record["media"] = fields["media"] = canonical
                    self.store.put(record)
                    if fields.get("pixai_scores") is not None:
                        fields["baseline"] = pixai_baseline(fields["pixai_scores"], "existing_result")
                        self.store.put(record)
                    if fields.get("baseline") is None and fields.get("pixai_scores") is None and not fields.get("pixai_color") and fields.get("visible_hair") != "no":
                        try:
                            with Image.open(self.store.root / "images" / f"{review_id}.png") as source:
                                image = source.convert("RGB")
                            if record.get("crop"):
                                image = image.crop(record["crop"])
                            if self.runtime.before_start:
                                self.runtime.before_start()
                            started = time.perf_counter()
                            fields["baseline"] = pixai_predict(image, self.root / "models")
                            record["baseline_ms"] = round((time.perf_counter() - started) * 1000)
                            record["status"] = "pending"
                            self.store.put(record)
                        except Exception as exc:
                            record.update(status="failed", reason=f"PixAI 不可用：{exc}")
                            self.store.put(record)
                for media in ("anime", "photo", "unknown"):
                    for review_id in ids:
                        if should_stop():
                            return
                        record = self.store.get(review_id)
                        if record["media"] == media and record["status"] == "pending":
                            self._run(record, settings)
            finally:
                router.close()
                self.runtime.stop()
                if should_stop():
                    for key in ids:
                        record = self.store.get(key)
                        if record["status"] in ("pending", "running"):
                            record.update(status="failed", reason="任务已停止，复核未完成；可以重试或续做")
                            self.store.put(record)
                if writeback_results:
                    from .batch import writeback_many
                    with self.writeback_lock:
                        writeback_many({key: self.store.get(key) for key in ids}, self.store)

    def _run(self, record, settings=None):
        from .models import REPO, REVISION
        started = time.perf_counter()
        fields = record["request"]
        media = record["media"]
        kind = "torii" if media == "anime" else "qwen"
        settings = settings or self.settings.model_copy()
        try:
            record.update(status="running", reason="正在复核发色")
            self.store.put(record)
            with Image.open(self.store.root / "images" / f"{record['id']}.png") as source:
                image = source.convert("RGB")
            if record.get("crop"):
                image = image.crop(record["crop"])
            scores = fields.get("pixai_scores")
            baseline = fields.get("baseline") or (pixai_baseline(scores, "existing_result") if scores is not None else (
                {"color": color_name(fields["pixai_color"]), "source": "existing_result"}
                if fields.get("pixai_color") else None))
            if baseline is None and fields.get("visible_hair") != "no":
                self.runtime.stop()
                baseline = pixai_predict(image, self.root / "models")

            def judge():
                judge_settings = settings.model_copy()
                if fields.get("qwen_url") and fields["qwen_url"] != "http://127.0.0.1:8000":
                    judge_settings.qwen_url = fields["qwen_url"]
                    judge_settings.qwen_model = fields.get("qwen_model", judge_settings.qwen_model)
                url, model = self.runtime.endpoint(kind, judge_settings)
                return predict(image, kind, url, model)

            baseline = baseline or {}
            result = review(baseline, lambda name: self.models.predict(image, name),
                            judge, min_confidence=settings.min_confidence, media=media,
                            visible_hair="yes" if record.get("crop") else fields.get("visible_hair", "unknown"),
                            reviewer="toriigate" if kind == "torii" else "qwen3vl")
            record.update(result, elapsed_ms=record.get("baseline_ms", 0) + round((time.perf_counter() - started) * 1000),
                          provenance={"hair_repo": REPO, "hair_revision": REVISION,
                                      "vlm": kind, "input": "crop" if record.get("crop") else "original",
                                      "min_confidence": settings.min_confidence})
        except Exception as exc:
            record.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
        self.store.put(record)

    def recrop(self, review_id, crop, media=None):
        parent = self.store.get(review_id)
        if parent["status"] in ("pending", "running"):
            raise ValueError("请等待原图复核结束后再框选角色")
        if parent.get("parent_id"):
            review_id = parent["parent_id"]
            parent = self.store.get(review_id)
        fields = {"image_name": parent["image_name"], "crop": crop,
                  "media": media or parent.get("media", "unknown"), "visible_hair": "yes", "parent_id": review_id,
                  "job_id": parent.get("job_id", "")}
        raw = (self.store.root / "images" / f"{review_id}.png").read_bytes()
        return self.enqueue(raw, fields, run=True)

    def retry(self, review_id):
        record = self.store.get(review_id)
        unavailable = record["status"] == "review" and any(o.get("error") for o in record.get("observations", []))
        if record["status"] != "failed" and not unavailable:
            raise ValueError("只有失败或模型不可用的复核可以重试")
        record.update(status="pending", reason="等待重试")
        self.store.put(record)
        self.executor.submit(self.run_records, [review_id])
        return record
