"""后台测试会话与本地报告；不建立调度身份、不领取租约。"""
import copy
import json
import threading
import time
import uuid
from pathlib import Path

from ...storage.atomic import write_json
from ..center_runtime.inference import Cancelled, InferenceRuntime
from . import catalog, samples, runner


class Benchmark:
    def __init__(self, center):
        self.center = center
        self.root = center.dir / 'benchmarks'
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.thread = None
        self.state = dict(running=False, status='idle', rows=[], message='', error='')
        try:
            saved = json.loads((self.root / 'latest.json').read_text(encoding='utf-8'))
            if not isinstance(saved, dict):
                raise ValueError('报告格式不正确')
            if saved.get('running'):
                saved.update(running=False, status='interrupted', message='上次测试已中断，可重新开始')
                for row in saved.get('rows', []):
                    if row.get('status') in ('preparing', 'warming', 'measuring'):
                        row['status'] = 'canceled'
            self.state = saved
        except (OSError, ValueError, TypeError):
            pass

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def update(self, **values):
        with self.lock:
            self.state.update(copy.deepcopy(values))

    def save(self):
        value = self.snapshot()
        write_json(Path(value['report_path']), value)
        write_json(self.root / 'latest.json', value)

    def start(self, request):
        catalog.validate(request, self.center)
        output = Path(request.output_dir).expanduser().resolve() / 'concurrency-tests' if request.output_dir.strip() else self.root
        from ..vllm_service import VLLM
        from ..multi_gpu import MULTI
        from ..jobs import MANAGER
        runtime = self.center.runtime
        with runtime.lock, self.lock:
            if self.state['running'] or runtime.active or runtime.working or self.center.offline.running:
                raise RuntimeError('请先停止打标、离线任务或退出托管，再开始并发测试')
            if MANAGER.running() or MULTI.status in ('starting', 'ready', 'partial', 'stopping'):
                raise RuntimeError('请先停止本机任务和多卡服务')
            if VLLM.snapshot()['status'] in ('starting', 'ready', 'stopping'):
                raise RuntimeError('请先在模型管理停止当前模型服务，测试会自动逐档启动')
            self.cancel = threading.Event()
            runtime.preparing = True
            runtime.cancel = self.cancel
            self.state = dict(id=uuid.uuid4().hex[:12], running=True, status='running', phase='samples',
                message='准备固定样本', error='', rows=[], request=request.model_dump(), created_at=time.time(), progress={})
            self.state['report_path'] = str(output / self.state['id'] / 'report.json')
            self.thread = threading.Thread(target=self._run, args=(request,), name='concurrency-benchmark', daemon=True)
            try:
                self.thread.start()
            except Exception:
                runtime.preparing = False
                self.state['running'] = False
                raise
            return self.snapshot()

    def stop(self):
        with self.lock:
            if self.state['running']:
                self.cancel.set()
                self.state['message'] = '正在停止测试并释放本轮模型服务'
            return self.snapshot()

    def close(self):
        self.stop()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10)

    def _run(self, request):
        from ...runtime.inference_queue import INFERENCE_LOCK
        runtime = self.center.runtime
        root = Path(self.state['report_path']).parent
        inference = InferenceRuntime(self.center._push, lambda phase, message: self.update(phase=phase, message=message))
        acquired = False
        try:
            acquired = INFERENCE_LOCK.acquire(blocking=False)
            if not acquired:
                raise RuntimeError('当前有本机推理或复核正在运行，请完成后重试')
            from ...config import load_ops, load_tier_table, DEFAULT_TIERS
            from ...runtime.disk_space import require_space
            require_space(load_ops(load_tier_table(DEFAULT_TIERS)).guard_policy(), root)
            root.mkdir(parents=True)
            source = samples.source_path(self.center, request.source)
            count, rejected = samples.prepare(source, root / 'sample.zip', max(request.sample_count, max(request.levels)), self.cancel)
            self.update(sample_count=count, rejected_images=rejected, source=str(source), report_path=str(root / 'report.json'))
            self.save()
            def update(**values):
                self.update(**values)
                if 'rows' in values and 'progress' not in values:
                    self.save()
            successes = runner.run(self.center, inference, request, root, root / 'sample.zip', count, self.cancel, update)
            failed = sum(row['status'] == 'failed' for row in self.snapshot()['rows'])
            message = f'测试完成 · {failed} 个档位失败，请展开明细' if failed else '测试完成'
            self.update(status='completed' if successes else 'failed', message=message if successes else '没有成功档位，请查看各档错误')
        except Cancelled:
            self.update(status='canceled', message='已停止，已完成档位保留')
        except Exception as exc:
            self.update(status='failed', error=self.center.credentials.redact(str(exc)), message='测试失败')
        finally:
            try:
                inference.stop(True)
            except Exception as exc:
                self.update(status='failed', error=self.center.credentials.redact(str(exc)), message='模型服务清理失败')
            finally:
                if acquired:
                    INFERENCE_LOCK.release()
                self.update(running=False, finished_at=time.time(), progress={})
                try:
                    self.save()
                except OSError as exc:
                    self.update(status='failed', error=self.center.credentials.redact(str(exc)), message='测试报告保存失败')
                finally:
                    with runtime.lock:
                        if runtime.cancel is self.cancel:
                            runtime.preparing = False
