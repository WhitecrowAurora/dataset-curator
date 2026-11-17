"""本地分片产物发现；聚合格式和逐图 JSON 使用相同的行契约。"""
import json
import re
from pathlib import Path

from ..artifacts import shard_stem
from .history import read_rows


def paths(output, package, isolated=False):
    output = Path(output)
    stem = shard_stem(package)
    aggregate = [p for p in output.glob('artifacts-*') if p.suffix.lower() in ('.jsonl', '.json', '.parquet')
                 and p.name != f'artifacts-{stem}-delivery.jsonl'
                 and (isolated or re.fullmatch(r'artifacts-\d+', p.stem)
                      or p.stem == f'artifacts-{stem}' or p.stem.startswith(f'artifacts-{stem}-'))]
    individual = [p for pattern in ('*-tag.json', '*-nl.json', '*-tag.jsonl', '*-nl.jsonl')
                  for p in output.rglob(pattern) if p.is_file() and not p.name.startswith('artifacts-')]
    return sorted(aggregate + individual)


def rows(files, expected):
    known = set(expected)
    for path in files:
        part = next((p for p in ('tag', 'nl') if path.stem.endswith('-' + p)), None)
        if part and not path.name.startswith('artifacts-'):
            for value in read_rows([path]):
                iid = value.get('image_id', path.stem[:-len('-' + part)])
                if iid in known:
                    # 单图文件既有裸 payload，也有标准逐图行；保持同一内容的幂等归并。
                    raw = value.get(part) if 'image_id' in value and part in value else value
                    yield {**{k: value[k] for k in ('outcomes', 'excluded') if k in value},
                           'image_id': iid, part: raw, 'ts': value.get('ts', path.stat().st_mtime)}
        else:
            for row in read_rows([path]):
                if not re.fullmatch(r'artifacts-\d+', path.stem) or row.get('image_id') in known:
                    yield row


def failure_rows(path, package, expected):
    """逐侧取最后一条账本状态；一侧冲销不能消除另一侧失败。"""
    from ..fox_worker import _same_shard
    known, latest = set(expected), {}
    if not Path(path).is_file():
        return []
    for row in read_rows([path]):
        iid = row.get('image_id')
        if iid not in known or (row.get('shard') and not _same_shard(row['shard'], package)):
            continue
        for part in row.get('parts', ('tag', 'nl')):
            if part in ('tag', 'nl'):
                latest[iid, part] = row
    return [{'image_id': iid, 'ts': row.get('ts', 0), 'outcomes': {part: {'status': 'failed', 'reason': str(row.get('error') or '').strip()}}}
            for (iid, part), row in latest.items() if not row.get('resolved')]
