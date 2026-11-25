"""将离线结果和逐图证明封装为管理员网页可核对的回收包。"""
import json
import os
import zipfile
from pathlib import Path

from ...center_runtime.artifacts import prepare
from .offline_results import source_path


def export(offline, shard_dir, out_dir, package=''):
    with offline.center.runtime.lock, offline.lock:
        if offline.running or offline.center.runtime.active or offline.center.running:
            raise ValueError('请先停止当前执行，再导出回收包')
        manifest = getattr(offline, 'manifest', None)
        if not manifest:
            raise ValueError('请先导入离线任务清单')
        if not isinstance(out_dir, str) or not out_dir.strip():
            raise ValueError('请选择产物目录')
        out, shards = Path(out_dir).expanduser(), Path(shard_dir or '').expanduser()
        results_path = out / (manifest['id'] + '-results.json')
        results = json.loads(results_path.read_text(encoding='utf-8')) if results_path.is_file() else {}
        selected = [task for task in manifest['tasks'] if results.get(task['lease_id'], {}).get('done')
                    and (not package or task['repo'] + '/' + task['package'] == package)]
        if not selected:
            raise ValueError('该目录没有此离线任务已完成的分片')
        suffix = '-' + selected[0]['lease_id'] if package else ''
        target = out / (manifest['id'] + suffix + '-return.zip')
        temporary = target.with_suffix('.zip.part')
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', json.dumps({**manifest, 'tasks': selected}, ensure_ascii=False))
                for task in selected:
                    task_out = out / task['lease_id']
                    source = source_path(task, shards, task_out, results.get(task['lease_id'], {}).get('metrics', {}))
                    for path in prepare(task, task_out, source):
                        archive.write(path, task['lease_id'] + '/' + path.name)
            if temporary.stat().st_size > 64 * 1024 * 1024:
                raise ValueError('回收包超过网页单次 64 MiB 上限，请选择单个分片导出或使用联网补传')
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {'ok': True, 'path': str(target.resolve()), 'tasks': len(selected)}
