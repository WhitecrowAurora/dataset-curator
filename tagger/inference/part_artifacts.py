"""串行标签/描述阶段复用本侧已落盘结果，沿用可溢出的索引。"""
from __future__ import annotations

import json
from pathlib import Path

from ..artifacts import find_artifacts, is_artifact, iter_artifact_rows
from ..storage.atomic import read_json
from ..storage.collections import SpillMap
from ..verification.payload import payload_done


def completed_part(root, part, *, image_ids=None, paths=None, resolutions=(), include_excluded=False):
    from ..verification.local import is_delivery
    index = SpillMap()
    decisions = {r['image_id']: r for r in resolutions if r['part'] == part}
    def reusable(iid, stamp, path):
        decision = decisions.get(iid)
        return not decision or (decision['state'] == 'resolved' and path.name == '.center-snapshot.json') or (decision['state'] == 'redone' and
            stamp > decision.get('received_at', decision['updated_at']))
    from ..verification.local import rows as local_rows
    from ..center_runtime.snapshot import superseded
    artifacts = find_artifacts(root) if paths is None else (Path(p) for p in paths)
    for path in artifacts:
        if is_delivery(path) or superseded(path):
            continue
        file_stamp = path.stat().st_mtime
        records = iter_artifact_rows(path) if is_artifact(path) else local_rows([path], image_ids)
        for row in records:
            if not reusable(str(row.get('image_id')), float(row.get('ts') or file_stamp), path):
                continue
            if image_ids is not None and str(row.get("image_id")) not in image_ids:
                continue
            raw = row.get(part)
            if payload_done(part, raw):
                if isinstance(raw, str):
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        payload = {'caption' if part == 'nl' else 'tag': raw}
                else:
                    payload = raw
                if isinstance(payload, str):
                    payload = {'caption' if part == 'nl' else 'tag': payload}
                index[str(row["image_id"])] = (payload, str(path))
            elif include_excluded:
                from ..verification.history import _reason
                if str(row['image_id']) not in index and _reason(row, part)[0] == 'excluded':
                    index[str(row['image_id'])] = ({}, str(path))
    json_paths = Path(root).rglob(f"*-{part}.json") if paths is None else ()
    for path in json_paths:
        payload = read_json(path)
        if (payload_done(part, payload) and isinstance(payload, dict)
                and reusable(str(payload.get('image_id')), path.stat().st_mtime, path)
                and (image_ids is None or str(payload.get("image_id")) in image_ids)):
            index[str(payload["image_id"])] = (payload, str(path))
    return index


def attach_review(batch, task, payload, raw, path):
    """续做只关联已有标签；分片留到批结束一次写回。"""
    record = batch.capture(raw(), payload, task.image_id, task.file, media=task.media)
    from ..center_runtime.snapshot import NAME
    if Path(path).suffix == ".json" and Path(path).name != NAME:
        from ..storage.atomic import write_json
        write_json(path, payload)
        batch.service.store.set_writeback(record["id"], path=path)
    else:
        batch.service.store.set_writeback(record["id"], shard=path)
