"""从已有标签产物关联最终训练文本，不把分阶段 NL 当作无标签输出。"""
from __future__ import annotations

from contextlib import contextmanager

from ..inference.part_artifacts import completed_part


@contextmanager
def review_tags(records, *, root=None):
    images = {str(r["request"]["image_id"]) for r in records.values() if r.get("request", {}).get("image_id")}
    paths = set()
    if root is None:
        for record in records.values():
            request = record.get("request", {})
            paths.update(filter(None, [*request.get("artifact_paths", []), request.get("artifact_path"),
                                      *request.get("artifact_shards", []), request.get("artifact_shard")]))
    index = completed_part(root, "tag", image_ids=images, paths=None if root is not None else paths)
    try:
        yield index
    finally:
        index.close()


def tags_for_review(index, record):
    from ..hair_color.batch import patch_payload
    from .settings import load

    found = index.get(str(record.get("request", {}).get("image_id", "")))
    if found is None:
        if load().uses_tags:
            raise ValueError("训练模板包含 {tag}，但未找到该图片的标签产物，无法完整检查描述长度")
        return ()
    return patch_payload(dict(found[0]), record).get("tags", [])
