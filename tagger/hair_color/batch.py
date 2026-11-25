"""批量结果与发色复核关联；主打标结束后串行复核，保留原标签供撤销。"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..storage.atomic import write_json
from .contracts import COLORS

HAIR_COLOR_TAGS = {f"{color} hair" for color in COLORS} | {
    "gray hair", "blond hair", "multicolored hair", "two tone hair", "two-tone hair",
}
MISSING_REVIEW = "标签分片中未找到对应的发色复核，产物可能已被覆盖，请重新复核"


def is_hair_tag(tag):
    name = str(tag).replace("_", " ")
    return name in HAIR_COLOR_TAGS


def patch_payload(payload, record):
    from ..schema import tag_names
    names = tag_names(payload.get("tags")) or [t.strip() for t in payload.get("tag", "").split(",") if t.strip()]
    original = payload.get("hair_review", {}).get("original_tags")
    if original is None:
        original = list(names)
    settled = record["status"] in ("accepted", "decided", "skipped")
    if settled:
        names = [t for t in names if not is_hair_tag(t)]
        names += [color + " hair" for color in record["selected"]]
        if len(record["selected"]) > 1 and not record.get("multiple_people"):
            names.append("multicolored hair")
    else:
        hair = [t for t in original if is_hair_tag(t)]
        if hair != [t for t in names if is_hair_tag(t)]:
            names = [t for t in names if not is_hair_tag(t)] + hair
    info = dict(payload.get("hair_review") or {})
    info.update({
        "id": record["id"], "status": record["status"], "selected": record["selected"],
        "reason": record["reason"], "original_tags": original})
    payload.update(tags=names, tag=", ".join(names), hair_review=info)
    from .caption_sync import patch_caption
    return patch_caption(payload, record, names)


def writeback(record):
    fields = record.get("request", {})
    shards = list(dict.fromkeys([*fields.get("artifact_shards", []), fields.get("artifact_shard", "")]))
    for shard in filter(None, shards):
        from .artifact_update import update_file
        if record["id"] not in update_file(shard, {record["id"]: record}):
            raise ValueError(MISSING_REVIEW)
    paths = list(dict.fromkeys([*fields.get("artifact_paths", []), fields.get("artifact_path", "")]))
    for target in filter(None, paths):
        from .artifact_update import update_json
        update_json(target, record)


def writeback_many(records, store):
    """每个分片只更新一次；逐图 JSON 仍独立原子写入。"""
    groups = {}
    for record in records.values():
        fields = record.get("request", {})
        for path in filter(None, [*fields.get("artifact_shards", []), fields.get("artifact_shard", "")]):
            groups.setdefault(path, {})[record["id"]] = record
    errors = {}
    from .artifact_update import update_file
    for path, selected in groups.items():
        try:
            matched = update_file(path, selected)
            errors.update(dict.fromkeys(selected.keys() - matched, MISSING_REVIEW))
        except (OSError, ValueError) as exc:
            errors.update({key: str(exc) for key in selected})
    for key, record in records.items():
        copy = dict(record, request={k: v for k, v in record.get("request", {}).items()
                                     if k not in ("artifact_shard", "artifact_shards")})
        try:
            writeback(copy)
        except (OSError, ValueError) as exc:
            errors[key] = str(exc)
        store.set_writeback(key, error=errors.get(key))


class HairBatch:
    def __init__(self, service, job_id, media):
        self.service, self.job_id, self.media = service, job_id, media

    def capture(self, raw, payload, image_id, image_name, *, media="", artifact_path=""):
        digest = hashlib.sha256(raw).hexdigest()
        review_id = hashlib.sha256(f"{self.job_id}:{image_id}:{digest}".encode()).hexdigest()[:32]
        existing = payload.get("hair_review", {}).get("id")
        if existing:
            try:
                saved = self.service.store.get(existing)
            except KeyError:
                pass
            else:
                if saved.get("image_sha256") == digest:
                    review_id = existing
        try:
            previous = self.service.store.get(review_id)
        except KeyError:
            previous = None
        if previous is None:
            from ..inference.media_route import normalize_media
            meta = payload.pop("_hair_scores", None)
            fields = {"image_name": image_name, "job_id": self.job_id, "image_id": image_id,
                      "media": normalize_media(self.media if self.media != "unknown" else media),
                      "visible_hair": "unknown", "artifact_path": artifact_path,
                      "pixai_scores": meta if meta is not None else None}
            record = self.service.enqueue(raw, fields, review_id=review_id)
        else:
            record = self.service.store.attach(review_id, self.job_id, path=artifact_path)
            payload.pop("_hair_scores", None)
        info = payload.setdefault("hair_review", {})
        info.update(id=review_id, status=record["status"])
        return record

    def finish(self, should_stop=lambda: False, *, out_dir=None):
        ids = self.service.store.job_ids(self.job_id)
        self.service.run_records(ids, should_stop, writeback_results=False)
        with self.service.writeback_lock:
            records = {key: self.service.store.get(key) for key in ids}
            if out_dir is None:
                writeback_many(records, self.service.store)
            else:
                from .artifact_update import update_artifacts
                update_artifacts(out_dir, self.service.store, self.job_id)
        counts = {}
        for record in records.values():
            counts[record["status"]] = counts.get(record["status"], 0) + 1
        return counts

    def capture_existing(self, item, payload, raw, path):
        self.capture(raw, payload, item.image_id, item.name, artifact_path=str(path))
        write_json(path, payload)
