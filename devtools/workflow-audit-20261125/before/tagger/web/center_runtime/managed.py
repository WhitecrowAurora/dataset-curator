"""被动托管协议：设备主动出站轮询，中心无需连接本机 Web 端口。"""
from __future__ import annotations

import json
import hashlib
import threading
import time
import uuid

import httpx

from ...storage.atomic import write_bytes
from ...center_runtime.network import httpx_options
from .config import REMOTE_FIELDS, REPORT_FIELDS, execution_config

PROTOCOL_VERSION = 1
POLL_PATH = "/api/v1/managed/poll"


class ManagedSession:
    def __init__(self, center, *, history_only=False):
        self.center = center
        self.identity = json.loads(center.worker_cfg.read_text(encoding="utf-8"))
        self.id = uuid.uuid4().hex
        self.status = "connecting"
        self.closed = False
        self.error = ""
        self.last_seq = 0
        self.results: list[dict] = []
        self.lock = threading.RLock()
        self.next_poll = 0.0
        self.last_success_at = 0.0
        self.waiting_reason = ""
        self.automation = {}
        self.start_requested = False
        self.start_intent = 'start'
        self.intent_id = ''
        self._start_generation = 0
        self.run_id = uuid.uuid4().hex
        self.failures = 0
        self.path = center.dir / ("history-managed-results.json" if history_only else "managed-results.json")
        self.restored = False
        self.identity_key = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        from ...center_runtime.network import config as network_config
        self.identity.update(network_config(center.config()))
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if (saved.get('identity_key') == self.identity_key and not saved.get('closed')
                    and isinstance(saved.get('session_id'), str) and saved['session_id']
                    and type(saved.get('last_seq')) is int and saved['last_seq'] >= 0
                    and isinstance(saved.get('results'), list)
                    and all(isinstance(r, dict) and isinstance(r.get('id'), str)
                        and type(r.get('seq')) is int and 0 < r['seq'] <= saved['last_seq']
                        and r.get('status') in ('accepted', 'applied', 'failed', 'canceled')
                        for r in saved['results'])):
                self.id, self.last_seq = saved['session_id'], saved['last_seq']
                self.results = saved['results'][-64:]
                self.restored = True
                self.start_requested = saved.get('start_requested') is True
                self.start_intent = 'force_start' if saved.get('start_intent') == 'force_start' else 'start'
                self.intent_id = str(saved.get('intent_id') or uuid.uuid4().hex) if self.start_requested else ''
                self.run_id = str(saved.get('run_id') or self.run_id)
                for result in self.results:
                    if result['status'] == 'accepted':
                        result.update(status='canceled', error='客户端重启，执行中的指令已中断')
                self._persist()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass

    def _persist(self):
        write_bytes(self.path, json.dumps({'session_id': self.id, 'last_seq': self.last_seq,
            'identity_key': self.identity_key, 'closed': self.closed, 'results': self.results,
            'start_requested': self.start_requested, 'start_intent': self.start_intent,
            'intent_id': self.intent_id, 'run_id': self.run_id},
            ensure_ascii=False).encode('utf-8'))

    def snapshot(self):
        return {"session_id": self.id, "status": self.status, "error": self.error,
                "waiting_reason": self.waiting_reason,
                "automation": self.automation,
                "start_pending": self.start_requested, "start_intent": self.start_intent,
                "last_seq": self.last_seq, "results": self.results[-8:]}

    def request_start(self, *, force=False):
        with self.lock:
            self.start_requested = True
            self.start_intent = 'force_start' if force else 'start'
            self.intent_id = uuid.uuid4().hex
            self._start_generation += 1
            self.next_poll = 0.0
            self._persist()

    def record(self, command, status, error=""):
        result = {"id": command["id"], "seq": command["seq"], "status": status,
                  "error": self.center.credentials.redact(error)}
        with self.lock:
            self.results = [r for r in self.results if r["id"] != result["id"]]
            self.results.append(result)
            self.results = self.results[-64:]
            self._persist()

    def poll(self, runtime, capabilities, *, released=False, inventory=None):
        if self.closed and not released:
            return []
        identity = self.identity
        body = {k: identity.get(k) for k in ("user_id", "device_id", "secret")}
        with self.center.runtime.lock, self.lock:
            cfg = execution_config(self.center.config())
            results = list(self.results)
            last_seq = self.last_seq
            requested = self.start_requested and not released and not self.restored
            start_generation = self._start_generation
            start_intent, intent_id = self.start_intent, self.intent_id
        if requested:
            body.update(intent=start_intent, intent_id=intent_id)
        body.update({"protocol_version": PROTOCOL_VERSION, "session_id": self.id,
                     "last_seq": last_seq, "state": json.loads(
                         self.center.credentials.redact(json.dumps(runtime))),
                     "released": released or self.restored,
                     "config": {k: v for k, v in cfg.items()
                                if k in REPORT_FIELDS},
                     "inventory": self.center.history.inventory.public() if inventory is None else inventory,
                     "capabilities": capabilities, "results": results})
        body['state']['execution_limit']={'run_id':self.run_id,'max_packages':self.center.config().get('max_packages',0)}
        body["parts_pref"] = self.center.config().get("parts_pref") or "both"
        body['capabilities']['remote_concurrency']={key:cfg.get('allow_remote_'+key,False) is True for key in REMOTE_FIELDS}
        url = str(identity["server"]).rstrip("/") + POLL_PATH
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False,
                          **httpx_options(url, self.center.config())) as client:
            response = client.post(url, json=body)
        if self.closed and not released:
            return []
        if released:
            with self.lock:
                self._persist()
            response.raise_for_status()
            return []
        if response.status_code in (404, 405, 501):
            self.status = "unsupported"
            self.error = "调度中心尚不支持被动托管，等待服务端升级"
            return []
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError("调度中心托管协议版本不匹配")
        if data.get("session_id") != self.id:
            raise ValueError("调度中心返回了过期的托管会话")
        self.last_success_at = time.time()
        if data.get("released") is True:
            with self.lock:
                self.id = uuid.uuid4().hex
                self.last_seq = 0
                self.results = []
                self.restored = False
                self._persist()
            self.center._push('[center] 原托管会话已退出，正在重新连接')
            return []
        commands = data.get("commands", [])
        if not isinstance(commands, list) or len(commands) > 16:
            raise ValueError("托管指令格式错误")
        self.status, self.error = "online", ""
        self.failures = 0
        self.waiting_reason = str(data.get("waiting_reason") or "")
        self.automation = data.get('automation') if isinstance(data.get('automation'), dict) else {}
        limit = self.automation.get('limit') or {}
        if limit.get('run_id') and limit['run_id'] != self.run_id:
            with self.lock:
                self.run_id = limit['run_id']
                self._persist()
        if (requested and data.get('intent_applied') == start_intent
                and data.get('intent_id', intent_id) == intent_id):
            with self.lock:
                if self._start_generation == start_generation:
                    self.start_requested = False
                self._persist()
            self.center._push('[center] ' + ('强制继续' if start_intent == 'force_start' else '恢复接单')
                              + '已获中心确认：' + self.waiting_reason)
        self.center.history.update_reports(data.get('history_reports', []))
        return commands

    def validate(self, command):
        if not isinstance(command, dict) or not isinstance(command.get("id"), str) or not command["id"]:
            raise ValueError("托管指令缺少 id")
        if command.get("session_id") != self.id:
            raise ValueError("拒绝过期会话的托管指令")
        seq = command.get("seq")
        if type(seq) is not int or seq < 1:
            raise ValueError("托管指令序号错误")
        if seq <= self.last_seq:
            return None
        if seq != self.last_seq + 1:
            raise ValueError(f"托管指令顺序错误，应为 {self.last_seq + 1}")
        if any(r["id"] == command["id"] for r in self.results):
            raise ValueError("指令 id 不能复用")
        action = command.get("action")
        if action not in ("start", "stop", "check", "configure", "probe"):
            raise ValueError("托管指令不支持这个操作")
        changes = command.get("config") or {}
        if action == "check":
            required = {"check_id", "candidate_id", "repo", "package", "size", "sha256"}
            if not isinstance(changes, dict) or not required <= set(changes) or set(changes)-required-{'annotation','reinject'}:
                raise ValueError("历史检查任务格式不正确")
            if changes.get('reinject','') not in ('','missing','force'):
                raise ValueError('画师补注入模式不正确')
            from ...center_runtime.annotation import options
            annotation = options(changes.get('annotation'))
            if changes.get('reinject') and not annotation.get('artist_table'):
                raise ValueError('画师补注入指令缺少画师表')
            return action, changes
        if not isinstance(changes, dict) or set(changes) - REMOTE_FIELDS:
            raise ValueError("托管指令含不支持的参数")
        if action in ("start", "stop", "probe") and changes:
            raise ValueError("启停和探测指令不能携带配置")
        if action == "configure" and not changes:
            raise ValueError("请指定 NL 并发或 TAG 批大小")
        if action=='configure' and any(not self.center.config().get('allow_remote_'+key,False) for key in changes):
            raise ValueError('本机尚未勾选允许服务器调整对应并发')
        if any(type(v) is not int for v in changes.values()):
            raise ValueError("并发和批大小必须为整数")
        execution_config({**self.center.config(), **changes})
        return action, changes
