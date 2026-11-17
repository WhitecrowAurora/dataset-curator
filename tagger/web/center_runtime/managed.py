"""被动托管协议：设备主动出站轮询，中心无需连接本机 Web 端口。"""
from __future__ import annotations

import json
import threading
import uuid

import httpx

from ...storage.atomic import write_bytes
from .config import REMOTE_FIELDS, REPORT_FIELDS, execution_config

PROTOCOL_VERSION = 1
POLL_PATH = "/api/v1/managed/poll"


class ManagedSession:
    def __init__(self, center):
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
        self.path = center.dir / "managed-results.json"

    def snapshot(self):
        return {"session_id": self.id, "status": self.status, "error": self.error,
                "last_seq": self.last_seq, "results": self.results[-8:]}

    def record(self, command, status, error=""):
        result = {"id": command["id"], "seq": command["seq"], "status": status,
                  "error": self.center.credentials.redact(error)}
        with self.lock:
            self.results = [r for r in self.results if r["id"] != result["id"]]
            self.results.append(result)
            self.results = self.results[-64:]
            write_bytes(self.path, json.dumps({"session_id": self.id, "results": self.results},
                                             ensure_ascii=False).encode("utf-8"))

    def poll(self, runtime, capabilities, *, released=False, inventory=None):
        if self.closed and not released:
            return []
        identity = self.identity
        body = {k: identity.get(k) for k in ("user_id", "device_id", "secret")}
        body.update({"protocol_version": PROTOCOL_VERSION, "session_id": self.id,
                     "last_seq": self.last_seq, "state": json.loads(
                         self.center.credentials.redact(json.dumps(runtime))),
                     "released": released,
                     "config": {k: v for k, v in execution_config(self.center.config()).items()
                                if k in REPORT_FIELDS},
                     "inventory": self.center.history.inventory.public() if inventory is None else inventory,
                     "capabilities": capabilities, "results": list(self.results)})
        body["parts_pref"] = self.center.config().get("parts_pref") or "both"
        url = str(identity["server"]).rstrip("/") + POLL_PATH
        with httpx.Client(timeout=8.0, follow_redirects=False) as client:
            response = client.post(url, json=body)
        if self.closed and not released:
            return []
        if released:
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
        if data.get("released") is True:
            raise ValueError("托管会话已失效，请退出托管后重新进入")
        commands = data.get("commands", [])
        if not isinstance(commands, list) or len(commands) > 16:
            raise ValueError("托管指令格式错误")
        self.status, self.error = "online", ""
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
        if action not in ("start", "stop", "check"):
            raise ValueError("本机执行配置已锁定，托管仅支持开始、停止和检查")
        changes = command.get("config") or {}
        if action == "check":
            if not isinstance(changes, dict) or set(changes) != {"check_id", "candidate_id", "repo", "package", "size", "sha256"}:
                raise ValueError("历史检查任务格式不正确")
            return action, changes
        if not isinstance(changes, dict) or set(changes) - REMOTE_FIELDS:
            raise ValueError("托管指令含不支持的参数")
        if action == "stop" and changes:
            raise ValueError("停止指令不能携带配置")
        execution_config({**self.center.config(), **changes})
        return action, changes
