"""将确认结果同步到逐图及分片的标签与描述，保留其他列。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..artifacts import is_artifact, iter_artifact_rows
from ..storage.locking import FileLock
from .batch import MISSING_REVIEW, patch_payload
from .caption_sync import patch_caption


def _caption(value, record, tags=()):
    encoded = isinstance(value, str)
    if not value:
        return value
    payload = json.loads(value) if encoded else value
    if not isinstance(payload, dict):
        raise ValueError("对应描述产物格式错误，无法同步发色")
    patch_caption(payload, record, tags)
    return json.dumps(payload, ensure_ascii=False) if encoded else payload


def update_json(path, record):
    from ..storage.atomic import read_json, write_json
    path = Path(path)
    with FileLock(path.with_suffix(path.suffix + ".hair.lock")):
        payload = read_json(path)
        if not isinstance(payload, dict):
            raise ValueError("对应标签产物已不存在或不可读取")
        image_id = record.get("request", {}).get("image_id")
        if image_id and payload.get("image_id") != image_id:
            raise ValueError("标签产物的图片编号已改变，请重新复核")
        nl_path = path.with_name(path.name[:-9] + "-nl.json") if path.name.endswith("-tag.json") else None
        if nl_path is not None and nl_path.is_file():
            with FileLock(nl_path.with_suffix(nl_path.suffix + ".hair.lock")):
                nl = read_json(nl_path)
                if not isinstance(nl, dict) or nl.get("image_id") != payload.get("image_id"):
                    raise ValueError("描述产物的图片编号已改变或不可读取，请重新复核")
                patched = patch_payload(payload, record)
                patch_caption(nl, record, patched.get("tags", []))
                write_json(path, patched)
                write_json(nl_path, nl)
        else:
            write_json(path, patch_payload(payload, record))


def update_file(path, records, *, image_records=None, tag_index=None):
    if tag_index is None:
        from ..text_encoding.artifacts import review_tags
        from ..text_encoding.settings import load
        if load().uses_tags:
            with review_tags(records) as index:
                return update_file(path, records, image_records=image_records, tag_index=index)
        tag_index = {}
    path = Path(path)
    matched = set()
    by_image = {r["request"]["image_id"]: r for r in (image_records if image_records is not None else records).values() if r.get("request", {}).get("image_id")}
    with FileLock(path.with_suffix(path.suffix + ".hair.lock")):
        def patch(row):
            tag = row.get("tag")
            encoded = isinstance(tag, str)
            if encoded:
                try:
                    tag = json.loads(tag)
                except ValueError:
                    return row
            record = None
            if isinstance(tag, dict):
                review_id = tag.get("hair_review", {}).get("id")
                record = records.get(review_id) or (by_image.get(tag.get("image_id")) if not review_id else None)
                if record is not None:
                    matched.add(record["id"])
                    tag = patch_payload(tag, record)
                    row["tag"] = json.dumps(tag, ensure_ascii=False) if encoded else tag
            elif tag is None or tag == "":
                record = by_image.get(str(row.get("image_id", "")))
            if record is not None and row.get("nl"):
                from ..text_encoding.artifacts import tags_for_review
                tags = tag.get("tags", []) if isinstance(tag, dict) else tags_for_review(tag_index, record)
                row["nl"] = _caption(row["nl"], record, tags)
                matched.add(record["id"])
            return row
        from ..center_runtime.snapshot import NAME
        if path.name == NAME:
            from ..storage.atomic import read_json, write_json
            snapshot = read_json(path)
            snapshot['rows'] = [patch(row) for row in snapshot['rows']]
            write_json(path, snapshot)
        elif path.suffix == ".jsonl":
            temporary = path.with_suffix(path.suffix + ".hair.tmp")
            with path.open("r", encoding="utf-8") as source, temporary.open("w", encoding="utf-8", newline="\n") as out:
                for line in source:
                    if line.strip():
                        out.write(json.dumps(patch(json.loads(line)), ensure_ascii=False) + "\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        else:
            import pyarrow as pa
            import pyarrow.parquet as pq
            temporary = path.with_suffix(path.suffix + ".hair.tmp")
            with pq.ParquetFile(path) as source:
                with pq.ParquetWriter(temporary, source.schema_arrow, compression="zstd") as writer:
                    for chunk in source.iter_batches(batch_size=2048):
                        writer.write_table(pa.Table.from_pylist([patch(row) for row in chunk.to_pylist()], schema=source.schema_arrow))
            os.replace(temporary, path)
    return matched


def update_artifacts(root, store, job_id):
    records = {key: store.get(key) for key in store.job_ids(job_id)}
    if not records:
        return
    from ..text_encoding.artifacts import review_tags
    from ..text_encoding.settings import load
    if load().uses_tags:
        with review_tags(records, root=root) as index:
            return _update_artifacts(root, store, records, index)
    return _update_artifacts(root, store, records, {})


def _update_artifacts(root, store, records, tag_index):
    images = {r.get("request", {}).get("image_id"): r for r in records.values()}
    matched_records = set()
    from ..center_runtime.snapshot import NAME, superseded
    from ..verification.history import read_rows
    for path in Path(root).rglob("*"):
        if path.is_file() and (is_artifact(path) or path.name == NAME) and not superseded(path):
            located = {key: r for key, r in records.items()
                       if str(path) in [*r.get("request", {}).get("artifact_shards", []), r.get("request", {}).get("artifact_shard")]}
            if not located:
                for row in read_rows([path]):
                    tag = row.get("tag")
                    if isinstance(tag, str):
                        try:
                            tag = json.loads(tag)
                        except ValueError:
                            continue
                    if (isinstance(tag, dict) and tag.get("hair_review", {}).get("id") in records
                            or not tag and row.get("nl") and str(row.get("image_id", "")) in images):
                        break
                else:
                    continue
            matched = update_file(path, records, image_records=located or records, tag_index=tag_index)
            matched_records.update(matched)
            for key in matched:
                store.set_writeback(key, shard=path)
    # JSON 产物的主客户端也在此建立回写位置。
    for path in Path(root).rglob("*-tag.json"):
        from ..storage.atomic import read_json
        payload = read_json(path)
        key = payload.get("hair_review", {}).get("id") if isinstance(payload, dict) else None
        if key in records:
            record = records[key]
            update_json(path, record)
            matched_records.add(key)
            store.set_writeback(key, path=path)
    for key in records.keys() - matched_records:
        if records[key].get("request", {}).get("image_id"):
            store.set_writeback(key, error=MISSING_REVIEW)
