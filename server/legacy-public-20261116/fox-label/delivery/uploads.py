"""回传文件本地副本，包含 HF 发布后的续作来源。"""
from pathlib import Path


def local_file(service, row):
    path = Path(row["local_path"]) if row["local_path"] else service.blobs.files / row["name"]
    if not path.is_file():
        raise ValueError("回传文件不存在，请重新上传")
    return path
