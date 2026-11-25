"""客户端主动释放租约：先停写当前包，再通知中心回收。"""
import threading


class LeaseControl:
    def __init__(self, center, request):
        self.center, self.request = center, request
        self.lock = threading.Lock()
        self.value = {'running': False, 'message': '', 'error': '', 'lease_id': ''}

    @property
    def running(self):
        return self.value['running']

    def snapshot(self):
        with self.lock:
            return dict(self.value)

    def start(self, body):
        lease_id, kind = body.get('lease_id'), body.get('kind')
        if not isinstance(lease_id, str) or not lease_id or kind not in ('active', 'prelock'):
            raise ValueError('请选择当前包或预取包')
        with self.center.runtime.lock, self.lock:
            if self.running:
                raise ValueError('正在释放，请等待服务器确认')
            if self.center.runtime.stopping:
                raise ValueError('正在停止，请稍后释放')
            if getattr(self.center, 'offline', None) and self.center.offline.running:
                raise ValueError('请先停止离线任务')
            self.value = {'running': True, 'message': '正在停止并释放' if kind == 'active' else '正在释放预取包',
                          'error': '', 'lease_id': lease_id}
        threading.Thread(target=self._run, args=(lease_id, kind), name='center-release-lease', daemon=True).start()

    def _run(self, lease_id, kind):
        center = self.center
        try:
            leases = self.request(center, 'status', {}).get('leases') or []
            selected = next((l for l in leases if l['lease_id'] == lease_id), None)
            if selected and selected['kind'] != kind:
                raise ValueError('预取分片已转为当前任务，请刷新后使用停止并释放')
            if kind == 'active':
                # 同时暂停托管及看门狗；停止期间可能已交付并领到下一包，最后重新读取。
                with center.runtime.lock, center._lock:
                    center.runtime.active = False
                    center.runtime.cancel.set()
                    center._wanted = False
                    center._next_spawn = 0.0
                    proc = center._proc
                center.runtime.stop()
                if proc is not None and proc.poll() is None:
                    raise ValueError('打标进程尚未停止，请等待后重试释放')
                leases = [l for l in self.request(center, 'status', {}).get('leases') or []
                          if l['kind'] in ('active', 'prelock')]
                if not any(l['lease_id'] == lease_id for l in leases):
                    leases.insert(0, {'lease_id': lease_id})
            else:
                leases = [selected] if selected else []
            for lease in leases:
                self.request(center, 'release', {'lease_id': lease['lease_id'], 'reason': 'user_release',
                    **({'expected_kind': 'prelock'} if kind == 'prelock' else {})})
            remaining = self.request(center, 'status', {}).get('leases') or []
            if any(l['lease_id'] == lease_id for l in remaining) or (kind == 'active' and
                    any(l['kind'] in ('active', 'prelock') for l in remaining)):
                raise ValueError('中心仍保留租约，请重试或联系管理员')
            message = '已暂停接单，当前包及预取包已释放；本地产物保留' if kind == 'active' else '预取包已释放，当前打标继续'
            with self.lock:
                self.value.update(message=message)
            center._push('[center] ' + message)
        except Exception as exc:
            error = center.credentials.redact(str(exc))
            with self.lock:
                self.value.update(error=error, message='释放未获中心确认，请重试；本地产物保留')
            center._push('[center] 释放未获中心确认：' + error)
        finally:
            center._board_cache = None
            with self.lock:
                self.value['running'] = False
