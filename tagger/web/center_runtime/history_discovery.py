"""从已知任务目录和缓存发现历史产物，不遍历用户的整个磁盘。"""
import hashlib
from pathlib import Path

from ...artifacts import shard_stem
from ...storage.atomic import read_json, write_json
from ...verification.local import paths

ARCHIVES = ('.tar', '.tar.gz', '.tgz', '.zip')


def locations(center, extra=()):
    root = Path(__file__).resolve().parents[3]
    outputs = {Path(center.config()['out_dir']).expanduser().resolve(),
               (root / 'work' / 'fox-work').resolve()}
    sources = {root / 'run' / 'hf-cache', root / 'work' / 'hf-cache', *outputs}
    hints = {}
    from ..history import HISTORY_DIR
    for file in HISTORY_DIR.glob('*.json'):
        data = read_json(file) or {}
        spec = data.get('spec') or {}
        if data.get('out_dir'):
            output = Path(data['out_dir']).expanduser().resolve()
            outputs.add(output)
            for package in spec.get('hf_files') or []:
                if spec.get('hf_repo'):
                    hints.setdefault((spec['hf_repo'], package), set()).add(output)
        for key in ('input_dir', 'hf_cache'):
            if spec.get(key):
                sources.add(Path(spec[key]).expanduser().resolve())
        for name in spec.get('input_shards') or []:
            sources.add(Path(name).expanduser().resolve())
    for binding in center.history.inventory.bindings():
        outputs.add(Path(binding['out_dir']))
        sources.add(Path(binding['source']))
    outputs.update(Path(p).expanduser().resolve() for p in extra)
    sources.update(outputs)
    return outputs, sources, hints


def archives(roots):
    found = set()
    for root in roots:
        if root.is_file() and str(root).lower().endswith(ARCHIVES):
            found.add(root.resolve())
        elif root.is_dir():
            found.update(p.resolve() for p in root.rglob('*') if p.is_file() and str(p).lower().endswith(ARCHIVES))
    return found


def catalog(center, sources):
    from ...fox_worker import api
    import json
    identity = json.loads(center.worker_cfg.read_text(encoding='utf-8'))
    rows = []
    wanted = [{'stem': stem, 'size': size} for stem, size in sorted(sources)]
    for start in range(0, len(wanted), 100):
        offset = 0
        while offset is not None:
            result = api(identity, '/api/v1/history/catalog',
                         {'offset': offset, 'sources': wanted[start:start + 100]}, timeout=15, retries=1)
            rows.extend(result['packages'])
            offset = result['next_offset']
    return rows


def discover(inventory):
    center = inventory.center
    data = read_json(inventory.path) or {}
    outputs, source_roots, hints = locations(center, data.get('search_dirs', []))
    outputs.update(folder for root in list(outputs) if (root / 'sources').is_dir()
                   for folder in (root / 'sources').iterdir() if folder.is_dir())
    sources = {}
    for source in archives(source_roots):
        sources.setdefault((shard_stem(source.name), source.stat().st_size), []).append(source)
    output_files = {}
    for output in outputs:
        if not output.is_dir():
            continue
        # 源隔离目录也可能只保留单图结果；汇总所在子目录同样自动识别。
        output_files[output] = paths(output, '', isolated=True)
        for file in output.rglob('artifacts-*'):
            if file.is_file() and file.suffix.lower() in ('.json', '.jsonl', '.parquet'):
                output_files.setdefault(file.parent, [])
                if file not in output_files[file.parent]:
                    output_files[file.parent].append(file)
    bindings = list(inventory.bindings())
    existing = {r['id'] for r in bindings}
    issues, hashes = [], data.get('source_hashes', {})
    packages = catalog(center, sources) if sources else []
    by_source = {}
    for package in packages:
        for source in sources.get((shard_stem(package['id']), package['size']), []):
            by_source.setdefault(source, []).append(package)
    for source, candidates in by_source.items():
        # 目录名或分片名不能解决同名仓库；歧义时以源 SHA256 核对。
        if len(candidates) > 1:
            stat = source.stat()
            key = f'{source}\n{stat.st_size}\n{stat.st_mtime_ns}'
            sha = hashes.get(key)
            if sha is None:
                with source.open('rb') as stream:
                    sha = hashlib.file_digest(stream, 'sha256').hexdigest()
                hashes[key] = sha
            candidates = [p for p in candidates if p['sha256'] == sha]
        if len(candidates) != 1:
            issues.append({'name': source.name, 'path': str(source), 'reason': '来源不唯一，请补充源仓库关联'})
            continue
        package = candidates[0]
        identity = hashlib.sha256((package['repo'] + '\n' + package['id']).encode()).hexdigest()
        if identity in existing:
            continue
        stem = shard_stem(package['id'])
        source_key = hashlib.sha256((package['repo'] + '\n' + package['id'] + '\n' + str(package['sha256'] or '')).encode()).hexdigest()
        matching = []
        for output, files in output_files.items():
            if files and output.name == source_key:
                matching = [output]
                break
            if any(p.stem == 'artifacts-' + stem or p.stem.startswith('artifacts-' + stem + '-') for p in files):
                matching.append(output)
        preferred = [p for p in hints.get((package['repo'], package['id']), ()) if output_files.get(p)]
        if preferred and not any(p.name == source_key for p in matching):
            matching = preferred
        if not matching:
            matching = [output for output, files in output_files.items() if files and
                        (any(p.stem.removeprefix('artifacts-').isdigit() for p in files)
                         or any(not p.name.startswith('artifacts-') for p in files))]
        # 同一次运行的嵌套目录只保留父目录，避免重复识别。
        matching = [p for p in matching if not any(q != p and q in p.parents for q in matching)]
        if len(matching) != 1:
            if matching:
                issues.append({'name': source.name, 'path': str(source), 'reason': '发现多个产物目录，请补充对应目录'})
            continue
        output = matching[0]
        if any(Path(r['out_dir']).resolve() == output and shard_stem(r['package']) == stem for r in bindings):
            issues.append({'name': source.name, 'path': str(source), 'reason': '同名分片已有其他来源关联，请分开产物目录'})
            continue
        bindings.append({'id': identity, 'repo': package['repo'], 'package': package['id'],
                         'source': str(source), 'out_dir': str(output), 'automatic': True})
        existing.add(identity)
    data.update(bindings=bindings, source_hashes=hashes, discovery_issues=issues)
    write_json(inventory.path, data)
    return len(bindings)
