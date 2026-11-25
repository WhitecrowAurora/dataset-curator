"""固定本轮图片集，所有档位使用相同图片和标识。"""
import io
import json
import tarfile
import zipfile
from pathlib import Path

from PIL import Image

from ..center_runtime.inference import Cancelled

EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
MAX_IMAGE = 32 * 1024 * 1024


def recent_source(center):
    root = Path(center.config()['out_dir']).resolve()
    try:
        meta = json.loads((root / '.fox-current.json').read_text(encoding='utf-8'))
        path = (root / meta['directory'] / meta['package']).resolve()
        path.relative_to(root)
        return path if path.is_file() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def source_path(center, value):
    path = Path(value).expanduser().resolve() if value.strip() else recent_source(center)
    if path is None or not path.exists():
        raise ValueError('没有可用的最近分片，请选择本地图片目录或已下载分片')
    if not path.is_dir() and path.suffix.lower() not in EXTS | {'.zip', '.tar', '.tgz', '.gz', '.parquet'}:
        raise ValueError('请选择图片目录、图片或 TAR / ZIP / Parquet 分片')
    return path


def images(path, cancel):
    if path.is_dir():
        for file in path.rglob('*'):
            if cancel.is_set():
                raise Cancelled('已停止并发测试')
            if file.is_file() and file.suffix.lower() in EXTS and file.stat().st_size <= MAX_IMAGE:
                yield file.name, file.read_bytes()
    elif path.suffix.lower() in EXTS:
        if path.stat().st_size <= MAX_IMAGE:
            yield path.name, path.read_bytes()
    elif path.name.lower().endswith(('.tar', '.tar.gz', '.tgz')):
        with tarfile.open(path, 'r|*') as archive:
            for item in archive:
                if cancel.is_set():
                    raise Cancelled('已停止并发测试')
                if item.isfile() and Path(item.name).suffix.lower() in EXTS and item.size <= MAX_IMAGE:
                    stream = archive.extractfile(item)
                    if stream:
                        yield item.name, stream.read()
    elif path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                if cancel.is_set():
                    raise Cancelled('已停止并发测试')
                if not item.is_dir() and Path(item.filename).suffix.lower() in EXTS and item.file_size <= MAX_IMAGE:
                    yield item.filename, archive.read(item)
    elif path.suffix.lower() == '.parquet':
        from ...hf_source import iter_parquet_samples
        rows = iter_parquet_samples(str(path))
        try:
            for row in rows:
                if cancel.is_set():
                    raise Cancelled('已停止并发测试')
                if len(row['data']) <= MAX_IMAGE:
                    yield row['image_id'] + '.jpg', row['data']
        finally:
            rows.close()


def prepare(source, output, limit, cancel):
    rows, rejected = [], 0
    stream = images(source, cancel)
    try:
        with zipfile.ZipFile(output, 'w') as archive:
            for name, data in stream:
                if cancel.is_set():
                    raise Cancelled('已停止并发测试')
                try:
                    with Image.open(io.BytesIO(data)) as image:
                        image.verify()
                except (OSError, ValueError, Image.DecompressionBombError):
                    rejected += 1
                    continue
                key = f'{len(rows):06d}'
                filename = key + Path(name).suffix.lower()
                rows.append(dict(image_id=key, file=filename))
                archive.writestr(filename, data)
                if len(rows) >= limit:
                    break
            archive.writestr('manifest.jsonl', '\n'.join(json.dumps(row) for row in rows))
    finally:
        stream.close()
    if len(rows) < 4:
        raise ValueError('至少需要 4 张可读取的图片，请换一个样本目录或分片')
    return len(rows), rejected
