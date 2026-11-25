"""离线产物与原始缓存的关联、按分配项确认本地完成。"""
from pathlib import Path

from ...center_runtime.artifacts import inspect


def source_path(task, shards, output, metrics):
    candidates = ([Path(metrics['source'])] if metrics.get('source') else []) + [
        output / task['package'],
        shards / task.get('repo_type', 'dataset') / task['repo'] / task['package'],
        shards / task['package'],
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise ValueError('找不到原始分片，请在素材目录放回该文件：' + task['package'])


def check(task, shards, output, metrics):
    source = source_path(task, shards, output, metrics)
    report = inspect(task, output, source)[-1]
    missing = [p for p in task['missing_parts'] if p not in report['complete_parts']]
    if missing:
        details = []
        for part in missing:
            counts = report['parts'][part]
            details.append(f"{part.upper()} 缺 {counts['missing']} 张、失败 {counts['failed']} 张")
        if report['conflicts']:
            details.append(report['conflicts'][0])
        raise ValueError('离线产物未齐全（' + '；'.join(details) + '）：' + task['package'])
    return source
