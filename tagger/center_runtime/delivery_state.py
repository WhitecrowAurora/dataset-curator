"""逐图验收结果随当前分片保存，供重启后的页面显示。"""
import json
import time
from pathlib import Path

from ..storage.atomic import write_json

NAME = '.fox-delivery.json'


def save(output, lease, status, *, error='', report=None):
    write_json(Path(output) / NAME, dict(
        lease_id=lease['lease_id'], repo=lease['repo'], package=lease['package'],
        status=status, error=error, report=report or {}, updated_at=time.time()))


def failed(output, lease, exc):
    from ..blob_client import BlobClientError
    permanent = isinstance(exc, BlobClientError) and exc.permanent
    save(output, lease, 'rejected' if permanent else 'retrying',
         error=str(exc), report=getattr(exc, 'report', None))
    return permanent


def snapshot(root):
    root = Path(root)
    try:
        current = json.loads((root / '.fox-current.json').read_text(encoding='utf-8'))
        value = json.loads((root / current['directory'] / NAME).read_text(encoding='utf-8'))
        if value['lease_id'] == current['lease_id']:
            return value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None
