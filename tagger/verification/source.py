"""原始图片分片的完整 ID 清单。"""
import json
import tarfile
import zipfile

from ..naming import IMAGE_SUFFIXES, sanitize


def source_ids(path):
    if str(path).lower().endswith('.zip'):
        with zipfile.ZipFile(path) as archive:
            if 'manifest.jsonl' not in archive.namelist():
                raise ValueError('原始 ZIP 缺少 manifest.jsonl，无法可靠对账')
            with archive.open('manifest.jsonl') as stream:
                rows = [json.loads(line) for line in stream if line.strip()]
            images = [i.filename for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith(IMAGE_SUFFIXES)]
            members = [r.get('file') for r in rows]
            if len(set(images)) != len(images) or len(set(members)) != len(members) or set(images) != set(members):
                raise ValueError('ZIP 图片与 manifest.jsonl 不一致')
            return [r['image_id'] for r in rows]
    with tarfile.open(path, 'r:*') as archive:
        return [sanitize(m.name.rsplit('.',1)[0].replace('/','-')) for m in archive
                if m.isfile() and m.name.lower().endswith(IMAGE_SUFFIXES)]
