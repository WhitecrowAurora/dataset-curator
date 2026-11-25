"""逐档准备、预热、测速，按完整成功的实际并发比较吞吐。"""
import time

from ..center_runtime.inference import Cancelled
from ..center_runtime.probe import _execute


def execution_config(center, request, root, selected):
    from ..center_runtime.config import ExecutionConfig
    execution = ExecutionConfig(mode='nl', tag_backend='none', inference_source='local',
        nl_model=selected.model, nl_engine=selected.engine, nl_precision=selected.precision,
        nl_device=request.device, concurrency=request.levels[0], auto_download=False,
        auto_quantize=False, stop_service=True).model_dump()
    return {**center.config(), **execution, 'out_dir': str(root), 'tag_url': 'http://127.0.0.1:8000',
            'auto_upload': False, 'extra_args': '', 'cmd_template': ''}


def complete(result, count):
    return (result.get('rc') == 0 and result.get('successful') == count
            and not result.get('failed') and not result.get('skipped'))


def failure(output, stage, result):
    from collections import deque
    try:
        with (output / 'execution.log').open(encoding='utf-8', errors='replace') as stream:
            tail = ''.join(deque(stream, maxlen=8)).strip()[-1800:]
    except OSError:
        tail = ''
    return f'{stage}未完整成功（成功 {result.get("successful", 0)}，失败 {result.get("failed", 0)}）' + ('：' + tail if tail else '')


def run(center, inference, request, root, sample, count, cancel, update):
    from ..multi_gpu import read_metrics
    from ...hardware import visible_gpus

    series = [dict(id=f'config-{i}', **selected.model_dump()) for i, selected in enumerate(request.configurations)]
    bases = [execution_config(center, request, root, selected) for selected in request.configurations]
    update(series=series)
    gpus = visible_gpus()
    index = int(request.device[5:]) if request.device.startswith('cuda:') else 0
    physical = gpus[index].index if request.device != 'cpu' and index < len(gpus) else None
    rows = []
    for index, level in ((i, level) for i in range(len(series)) for level in request.levels):
        if cancel.is_set():
            raise Cancelled('已停止并发测试')
        selected = series[index]
        label = f'{index + 1}/{len(series)} · {selected["model"]} · {selected["engine"]} · {selected["precision"].upper()}'
        row = dict(series_id=selected['id'], requested=level, status='preparing', successful=0, failed=0)
        rows.append(row)
        update(rows=rows, current=level, current_series=selected['id'], phase='preparing', message=f'{label} · 准备并发 {level}', progress={})
        try:
            inference.stop(True)
            started = time.monotonic()
            cfg = inference.prepare({**bases[index], 'concurrency': level}, '', cancel)
            row['prepare_s'] = round(time.monotonic() - started, 2)
            row['actual'] = cfg['concurrency']
            if row['actual'] != level:
                row['warning'] = f'引擎实际使用 {row["actual"]} 并发'
            if count < row['actual']:
                row.update(status='skipped', warning=f'只有 {count} 张样本，无法覆盖实际 {row["actual"]} 并发；请增加样本')
                continue
            measured = next((r for r in rows[:-1] if r['series_id'] == selected['id'] and r.get('actual') == row['actual'] and r['status'] == 'completed'), None)
            if measured:
                row.update(status='skipped', warning=f'实际并发 {row["actual"]} 已测量，跳过重复档位')
                continue
            output = root / selected['id'] / f'concurrency-{level}'
            warm_dir = root / selected['id'] / f'concurrency-{level}-warmup'
            row['status'] = 'warming'
            update(rows=rows, phase='warming', message=f'{label} · 预热并发 {level}（实际 {row["actual"]}）', progress={})
            started = time.monotonic()
            warm = _execute(cfg, sample, warm_dir, center, cancel,
                            lambda p: update(progress=p))
            row['warmup_s'] = round(time.monotonic() - started, 2)
            if not complete(warm, count):
                raise ValueError(failure(warm_dir, '预热', warm))
            row['status'] = 'measuring'
            update(rows=rows, phase='measuring', message=f'{label} · 测量并发 {level}（实际 {row["actual"]}）', progress={})
            last = 0.0
            def observe(progress):
                nonlocal last
                now = time.monotonic()
                if now - last >= 1:
                    last = now
                    metrics = read_metrics().get(physical, {}) if physical is not None else {}
                    if metrics:
                        row['peak_vram_mb'] = max(row.get('peak_vram_mb', 0), metrics['mem_used_mb'])
                    update(progress=progress, rows=rows)
            started = time.monotonic()
            result = _execute(cfg, sample, output, center, cancel, observe)
            wall = time.monotonic() - started
            elapsed = result.get('elapsed_s') or wall
            row.update(successful=result.get('successful', 0), failed=result.get('failed', 0),
                       elapsed_s=round(elapsed, 2), process_s=round(wall, 2), log_path=str(output / 'execution.log'))
            if not complete(result, count):
                raise ValueError(failure(output, '测速', result))
            row.update(status='completed', rate_per_s=round(count / elapsed, 4))
        except Cancelled:
            row['status'] = 'canceled'
            raise
        except Exception as exc:
            row.update(status='failed', error=center.credentials.redact(str(exc)))
        finally:
            inference.stop(True)
            successes = [r for r in rows if r['series_id'] == selected['id'] and r['status'] == 'completed']
            if successes:
                peak = max(r['rate_per_s'] for r in successes)
                selected.update(peak_rate_per_s=peak,
                    recommended=min(r['actual'] for r in successes if r['rate_per_s'] >= peak * .9))
            update(rows=rows, series=series)
    successes = [r for r in rows if r['status'] == 'completed']
    if successes and len(series) == 1:
        update(recommended=series[0]['recommended'])
    return len(successes)
