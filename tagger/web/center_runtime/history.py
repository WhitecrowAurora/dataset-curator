"""历史产物接入状态、后台检查和本地 Web 接口。"""
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...storage.atomic import read_json, write_json
from .history_inventory import Inventory


class History:
    def __init__(self, center):
        self.center = center
        self.inventory = Inventory(center)
        self.path = center.dir / "history-reports.json"
        self.running = False
        self.status = "idle"
        self.message = ""
        self.cancel = threading.Event()
        self.thread = None
        self._reports_lock = threading.Lock()
        self._refreshing = False
        self._refreshed_at = 0.0

    def phase(self, status, message):
        self.status, self.message = status, message
        self.center._push("[history] " + message)

    def report(self, key, report, status, fingerprint=None):
        with self._reports_lock:
            rows = read_json(self.path) or {}
            rows[key] = {"status": status, "report": report,
                         'fingerprint': fingerprint or rows.get(key, {}).get('fingerprint')}
            write_json(self.path, rows)

    def update_reports(self, reports):
        if not reports:
            return
        with self._reports_lock:
            self._update_reports(reports)

    def _update_reports(self, reports):
        local = read_json(self.path) or {}
        changed = False
        states = {'registered': 'registered', 'incomplete': 'incomplete', 'verifying': 'verifying'}
        for item in reports:
            current = local.get(item.get('candidate_id'))
            if not current or current.get('report', {}).get('check_id') != item.get('check_id') or item.get('status') not in states:
                continue
            value = {**current, 'status': states[item['status']], 'report': {**item['report'], 'check_id': item['check_id']}}
            if value != current:
                local[item['candidate_id']] = value
                changed = True
        if changed:
            write_json(self.path, local)

    def snapshot(self):
        reports = read_json(self.path) or {}
        items = []
        for row in self.inventory.scan():
            saved = reports.get(row['id'], {})
            if saved.get('fingerprint') != row['fingerprint']:
                saved = {}
            items.append({**row, 'local_status': row['status'], **saved})
        blocked = '请先停止当前打标，再核对或补注入历史产物' if self.center.runtime.active or self.center.runtime.working or (getattr(self.center,'offline',None) and self.center.offline.running) else ''
        return {"running": self.running, "status": self.status, "message": self.message, 'blocked_reason':blocked,
                "items": items,
                "unlinked": self.inventory.unlinked(items)}

    def refresh_reports(self):
        with self._reports_lock:
            reports = read_json(self.path) or {}
            if self._refreshing or time.monotonic() - self._refreshed_at < 10 or not any(r.get('status') == 'verifying' for r in reports.values()):
                return
            self._refreshing = True
            self._refreshed_at = time.monotonic()
            ids = [r['report']['check_id'] for r in reports.values()
                   if r.get('status') == 'verifying' and r.get('report', {}).get('check_id')]
        def refresh():
            try:
                import json
                from ...fox_worker import api
                identity = json.loads(self.center.worker_cfg.read_text(encoding='utf-8'))
                from ...center_runtime.network import config as network_config
                identity.update(network_config(self.center.config()))
                for start in range(0, len(ids), 500):
                    self.update_reports(api(identity, '/api/v1/history/reports',
                                            {'check_ids': ids[start:start + 500]}, timeout=10, retries=0)['history_reports'])
            except Exception:
                pass
            finally:
                self._refreshing = False
        threading.Thread(target=refresh, name='history-reports', daemon=True).start()

    def start_local(self, *, submit=True, reinject='', candidate_id=''):
        if reinject not in ('','missing','force'):
            raise ValueError('画师补注入模式不正确')
        with self.center.runtime.lock:
            if self.running:
                return self.snapshot()
            if self.center.runtime.active or self.center.runtime.working or (getattr(self.center, 'offline', None) and self.center.offline.running):
                raise ValueError('请先停止当前打标，再核对历史产物')
            from .history_submit import execute
            self.running = True
            self.cancel = threading.Event()
            self.thread = threading.Thread(target=execute, args=(self,), kwargs={'submit': submit,'reinject':reinject,'candidate_id':candidate_id},
                                           name='center-history-local', daemon=True)
            self.thread.start()
        return self.snapshot()

    def launch(self, command, session):
        if getattr(self.center, 'leases', None) and self.center.leases.running:
            raise ValueError('正在释放租约，请等待服务器确认')
        if self.running or self.center.runtime.working or self.center.running or (getattr(self.center,'offline',None) and self.center.offline.running):
            raise ValueError("请先停止当前打标，再检查历史分片")
        self.running = True
        self.cancel = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(command, session), name="center-history", daemon=True)
        self.thread.start()

    def _run(self, command, session):
        try:
            from .history_check import execute
            self.phase("checking", "逐图检查历史分片")
            execute(self, command, session, self.cancel)
            self.phase("completed", "历史检查已回传")
            session.record(command, "applied")
        except Exception as exc:
            message = self.center.credentials.redact(str(exc))
            if getattr(exc, 'report', None):
                row = next(r for r in self.inventory.scan() if r['id']==command['config']['candidate_id'])
                self.report(row['id'], {**exc.report, 'error':message}, 'failed', row['fingerprint'])
            self.phase("failed", message)
            session.record(command, "canceled" if self.cancel.is_set() else "failed", message)
        finally:
            self.running = False


class Binding(BaseModel):
    repo: str
    package: str
    source: str
    out_dir: str


def router(center):
    routes = APIRouter(prefix="/api/center/history")

    @routes.get("")
    def state():
        center.history.refresh_reports()
        return center.history.snapshot()

    @routes.post('/discover')
    def discover():
        try:
            return center.history.start_local(submit=False)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.post('/check')
    def check(body: dict = None):
        try:
            body=body or {}
            return center.history.start_local(reinject=body.get('reinject',''),candidate_id=body.get('candidate_id',''))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.post('/directories')
    def directory(body: dict):
        from pathlib import Path
        try:
            path = Path(body.get('path', '')).expanduser().resolve()
            if not body.get('path') or not path.is_dir():
                raise ValueError('请选择保存历史产物或原分片的目录')
            with center.runtime.lock:
                if center.history.running or center.runtime.active or center.runtime.working:
                    raise ValueError('请等待当前操作结束')
                inventory = center.history.inventory
                data = read_json(inventory.path) or {}
                data['search_dirs'] = list(dict.fromkeys([*data.get('search_dirs', []), str(path)]))
                write_json(inventory.path, data)
            return center.history.start_local(submit=False)
        except (ValueError, OSError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.post("/bindings")
    def bind(body: Binding):
        try:
            with center.runtime.lock:
                center.history.inventory.bind(**body.model_dump())
            return center.history.snapshot()
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.delete("/bindings/{key}")
    def remove(key: str):
        try:
            with center.runtime.lock:
                center.history.inventory.remove(key)
            return center.history.snapshot()
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    return routes
