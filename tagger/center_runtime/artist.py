"""取得中心画师表并固定为本包本地来源。"""
from pathlib import Path
from .artifacts import request
from ..storage.atomic import write_json


def prepare(identity, task, output):
    from .annotation import options
    annotation = options(task.get('annotation'))
    if not annotation.get('artist_table'):
        return
    data = task.get('artist_data') or request(identity, task, 'artist', timeout=120)
    if data.get('source') != annotation['artist_table'] or data.get('shard') != task['package']:
        raise ValueError('中心画师表来源与派单不一致')
    if not isinstance(data.get('mapping'),dict) or any(not isinstance(v,list) or any(not isinstance(n,str) for n in v) for v in data['mapping'].values()):
        raise ValueError('中心画师表记录格式不正确')
    path = Path(output) / '.center-artist.json'
    write_json(path, data)
    task['artist_local_table'] = str(path)
