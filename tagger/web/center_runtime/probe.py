"""用当前分片的小样本执行真实打标流程，比较并发与 TAG 批大小。"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tarfile
import time
import uuid
import zipfile
from pathlib import Path

from ...storage.atomic import write_json
from ...runtime.processes import spawn_owned
from .inference import Cancelled


def samples(center, limit=12):
    from ...client import ShardReader, load_tasks, TaskRow
    from ...naming import normalize_ext
    from ...hf_source import iter_parquet_samples

    root = Path(center.config()['out_dir']).resolve()
    meta = json.loads((root / '.fox-current.json').read_text(encoding='utf-8'))
    folder = (root / meta['directory']).resolve()
    folder.relative_to(root)
    source = (folder / meta['package']).resolve()
    source.relative_to(folder)
    result = []
    if source.name.endswith(('.tar', '.tar.gz', '.tgz')):
        with tarfile.open(source, 'r|*') as archive:
            for item in archive:
                if not item.isfile() or normalize_ext(item.name) not in ('.jpg', '.jpeg', '.png', '.webp', '.bmp'):
                    continue
                if item.size < 10240 or item.size > 32 * 1024 * 1024:
                    continue
                stream = archive.extractfile(item)
                data = stream.read() if stream else b''
                result.append((TaskRow(str(source), Path(item.name).stem, item.name), data))
                if len(result) >= limit:
                    break
    elif source.suffix == '.parquet':
        rows = iter_parquet_samples(str(source))
        try:
            for row in rows:
                if len(row['data']) < 10240:
                    continue
                result.append((TaskRow(str(source), row['image_id'], row['image_id'] + '.jpg',
                                       artist=row.get('artist', ''), artists=tuple(row.get('artists', ()))), row['data']))
                if len(result) >= limit:
                    break
        finally:
            rows.close()
    else:
        tasks = load_tasks([source])
        reader = ShardReader([str(source)])
        try:
            for task in tasks:
                data = reader.read(task.shard, task.file)
                if len(data) >= 10240:
                    result.append((task, data))
                if len(result) >= limit:
                    break
        finally:
            reader.close()
            if hasattr(tasks, 'close'):
                tasks.close()
    if len(result) < 4:
        raise ValueError('当前分片没有至少 4 张可用的本地图片；请先完成一个分片再探测')
    return result


def _execute(cfg, sample, output, center, cancel, on_progress=None):
    from ...center_runtime.progress import read

    env = center._env(cfg)
    # 不领取租约，不读取正式产物；输出写进独立探测目录。
    env['FOXLABEL_PARTS'] = cfg['mode']
    root = Path(__file__).resolve().parents[3]
    # 内嵌解释器忽略 cwd / PYTHONPATH，沿用 native 服务的显式模块加载方式。
    code = f"import sys; sys.path.insert(0, {str(root)!r}); from tagger.cli import main; sys.exit(main())"
    argv = [sys.executable, '-B', '-c', code, 'client', '--shard', str(sample),
            '--out-dir', str(output), '--progress-file', str(output / '.fox-progress'),
            '--mode', cfg['mode'], '--force', '--seed', '42', '--quiet', '--retry-budget', '0']
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'execution.log').open('wb') as log:
        proc = spawn_owned(argv, cwd=str(root), env=env,
                           stdout=log, stderr=subprocess.STDOUT,
                           **({'start_new_session': True} if os.name != 'nt' else {}))
        try:
            while proc.poll() is None:
                if cancel.wait(.25):
                    raise Cancelled('已停止并发探测')
                if on_progress:
                    on_progress(read(output / '.fox-progress'))
            progress = read(output / '.fox-progress')
            return {'rc': proc.returncode, **progress}
        finally:
            if proc.poll() is None:
                from ..center import _kill_tree
                _kill_tree(proc.pid, force=True)
                proc.wait(timeout=10)


def run(center, inference, cancel, phase):
    cfg = center.config()
    if cfg['mode'] != 'tag' and cfg['inference_source'] != 'local':
        raise ValueError('已有服务的实际槽位由服务所有者管理，无法在本机探测服务并发上限')
    batch = samples(center)
    root = center.dir / 'probes' / uuid.uuid4().hex[:12]
    root.mkdir(parents=True)
    sample = root / 'sample.zip'
    with zipfile.ZipFile(sample, 'w') as archive:
        rows = []
        for i, (task, data) in enumerate(batch):
            row = dataclasses.asdict(task)
            row['file'] = f'{i:03d}' + Path(task.file).suffix
            rows.append(row)
            archive.writestr(row['file'], data)
        archive.writestr('manifest.jsonl', '\n'.join(json.dumps(row, ensure_ascii=False) for row in rows))
    dimensions = []
    if cfg['mode'] != 'nl' and cfg['tag_backend'] != 'none':
        dimensions.append(('tag_batch', [1, 2, 4, 8]))
    if cfg['mode'] != 'tag':
        from ...inference.qwen35.runtime import MAX_BATCH_SIZE
        cap = (MAX_BATCH_SIZE if cfg['nl_engine'] == 'native' else 1) if cfg['nl_model'] == 'torii-gate-0.5' else 12
        dimensions.append(('concurrency', [n for n in (1, 2, 4, 6, 8, 12) if n <= cap and n <= len(batch)]))
    report = {'sample_count': len(batch), 'rows': [], 'recommended': {}, 'created_at': time.time(),
              'note': '小样本预热与真实打标测量；建议值需确认下发，长期稳定性仍需正式任务观察'}
    try:
        for key, levels in dimensions:
            candidates = []
            for level in levels:
                if cancel.is_set():
                    raise Cancelled('已停止并发探测')
                phase('probing', f'探测 {key} = {level} · {len(batch)} 张')
                inference.stop(True)
                trial = {**cfg, **report['recommended'], key: level, 'auto_download': False, 'auto_quantize': False}
                started = time.monotonic()
                try:
                    resolved = inference.prepare(trial, center.credentials.effective_token(), cancel)
                    phase('probing', f'测量 {key} = {level}（实际 {resolved[key]}）')
                    warm = _execute(resolved, sample, root / f'{key}-{level}-warmup', center, cancel)
                    if warm.get('rc') != 0:
                        raise ValueError('预热打标失败，详细日志保存在探测目录')
                    result = _execute(resolved, sample, root / f'{key}-{level}', center, cancel)
                    rate = result.get('rate_per_s', 0)
                    row = {'field': key, 'requested': level, 'actual': resolved[key],
                           'rate_per_s': rate, 'successful': result.get('successful', 0),
                           'failed': result.get('failed', 0), 'rc': result.get('rc'),
                           'elapsed_s': round(time.monotonic() - started, 1)}
                    if result.get('rc') == 0 and result.get('successful', 0) > 0 and not result.get('failed'):
                        candidates.append(row)
                    else:
                        row['error'] = '打标未完整成功，此档位不参与建议'
                except Cancelled:
                    raise
                except Exception as exc:
                    row = {'field': key, 'requested': level, 'error': center.credentials.redact(str(exc))}
                report['rows'].append(row)
                write_json(root / 'report.json', report)
                if row.get('error'):
                    break
            if candidates:
                peak = max(r['rate_per_s'] for r in candidates)
                report['recommended'][key] = min(r['actual'] for r in candidates if r['rate_per_s'] >= peak * .9)
    finally:
        inference.stop(True)
        write_json(root / 'report.json', report)
    if not report['recommended']:
        raise ValueError('没有测得成功档位，请查看本机探测日志')
    return report
