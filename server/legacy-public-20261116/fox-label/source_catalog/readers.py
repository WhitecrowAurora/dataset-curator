"""逐行读取仓库总表和分片索引；不整份载入内存。"""
import json
from pathlib import PurePosixPath


def value(row, field):
    current = row
    for key in field.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def rows(path, rows_key=""):
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        with path.open(encoding="utf-8-sig") as stream:
            for number, line in enumerate(stream, 1):
                if line.strip():
                    try:
                        yield json.loads(line)
                    except ValueError as exc:
                        raise ValueError("清单第 %d 行不是有效 JSON" % number) from exc
    elif suffix == ".json":
        try:
            import ijson
        except ImportError as exc:
            raise ValueError("JSON 总表需要安装 ijson") from exc
        with path.open("rb") as stream:
            yield from ijson.items(stream, (rows_key + "." if rows_key else "") + "item")
    elif suffix == ".parquet":
        try:
            import pyarrow.parquet as parquet
        except ImportError as exc:
            raise ValueError("Parquet 索引需要安装 pyarrow") from exc
        for batch in parquet.ParquetFile(path).iter_batches(batch_size=4096):
            yield from batch.to_pylist()
    else:
        raise ValueError("清单格式应为 JSONL、JSON 或 Parquet")


def identity(row, cfg):
    raw = value(row, cfg["id_field"])
    if raw is None and cfg['member_field']:
        member = value(row, cfg['member_field'])
        if isinstance(member, str) and member:
            from .archive import member_id
            raw = member_id(member)
    if isinstance(raw, bool) or not isinstance(raw, (str, int)) or not str(raw).strip():
        raise ValueError("清单缺少有效图片 ID：" + cfg["id_field"])
    return str(raw)


def package(row, cfg):
    raw = value(row, cfg["package_field"])
    return str(raw).replace("\\", "/") if isinstance(raw, str) else None


def index_path(cfg, package):
    return cfg["path"].replace("{package}", package).replace("{stem}", str(PurePosixPath(package).with_suffix("")))
