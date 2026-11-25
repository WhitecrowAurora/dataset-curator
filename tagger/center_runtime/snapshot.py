"""中心续做快照的单文件提交；兼容旧版两侧文件。"""
from pathlib import Path
import json

from ..storage.atomic import read_json

NAME = '.center-snapshot.json'


def exists(output):
    return any((Path(output) / name).is_file() for name in (NAME, '.center-resume.json'))


def state(output):
    output = Path(output)
    if (output / NAME).is_file():
        return json.loads((output / NAME).read_text(encoding='utf-8'))
    return {'done': read_json(output / '.center-resume.json') or {},
            'resolutions': read_json(output / '.center-resolutions.json') or []}


def superseded(path):
    path = Path(path)
    return (path.name.startswith('artifacts-')
            and path.name.endswith(('-resume-tag.jsonl', '-resume-nl.jsonl'))
            and (path.parent / NAME).is_file())
