"""扫描原分片成员，并核对存在的包内索引。"""
import json
import tarfile
import zipfile

from verification.naming import IMAGE_SUFFIXES, sanitize


def member_id(name):
    return sanitize(name.rsplit(".", 1)[0].replace("/", "-"))


def _check(ids, members, indexed):
    if not ids or len(set(ids)) != len(ids) or len(set(members)) != len(members):
        raise ValueError("原分片图片为空或 ID／成员重复")
    if indexed is not None:
        index = list(indexed)
        if len(index) != len(members) or len({r["file"] for r in index}) != len(index) or {r["file"] for r in index} != set(members):
            raise ValueError("包内索引与图片成员不一致")
        if {r["image_id"] for r in index} != set(ids):
            raise ValueError("包内索引图片 ID 与客户端命名不一致")
    return ids


def scan(path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            if "manifest.jsonl" not in archive.namelist():
                raise ValueError("原始 ZIP 缺少 manifest.jsonl")
            with archive.open("manifest.jsonl") as stream:
                index = [json.loads(line) for line in stream if line.strip()]
            members = [r.filename for r in archive.infolist() if not r.is_dir() and r.filename.lower().endswith(IMAGE_SUFFIXES)]
            return _check([r["image_id"] for r in index], members, index)
    ids, members, index = [], [], None
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            if not member.isfile():
                continue
            if member.name.lower().endswith(IMAGE_SUFFIXES):
                members.append(member.name)
                ids.append(member_id(member.name))
            elif member.name == "manifest.jsonl":
                with archive.extractfile(member) as stream:
                    index = [json.loads(line) for line in stream if line.strip()]
    return _check(ids, members, index)
