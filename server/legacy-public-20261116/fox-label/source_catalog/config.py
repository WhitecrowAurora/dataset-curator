"""每仓库清单配置；不包含访问凭据。"""
import re
from pathlib import PurePosixPath


DEFAULTS = dict(enabled=False, source="auto", path="manifest.jsonl", id_field="image_id",
                package_field="shard", member_field="file", rows_key="", revision="main")


def validate(body):
    out = {k: body.get(k, v) for k, v in DEFAULTS.items()}
    if not isinstance(out["enabled"], bool) or out["source"] not in ("auto", "manifest", "index", "archive"):
        raise ValueError("清单配置不正确")
    for key in DEFAULTS.keys() - {"enabled", "source"}:
        if not isinstance(out[key], str) or len(out[key]) > 500:
            raise ValueError("清单字段不正确：" + key)
        out[key] = out[key].strip()
    if not out["id_field"] or not re.fullmatch(r"[\w./-]+", out["revision"]):
        raise ValueError("请填写图片 ID 字段和仓库版本")
    path = out["path"].replace("\\", "/")
    if not path or path.startswith("/") or ".." in PurePosixPath(path).parts or ":" in path:
        raise ValueError("清单路径必须是仓库内的相对路径")
    if set(re.findall(r'\{([^}]+)\}', path)) - {'package', 'stem'}:
        raise ValueError('清单路径仅支持 {package} 和 {stem}')
    if out["source"] == "index" and "{package}" not in path and "{stem}" not in path:
        raise ValueError("分片索引路径需要 {package} 或 {stem}")
    out["path"] = path
    return out
