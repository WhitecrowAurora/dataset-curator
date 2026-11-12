#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tagger MCP server —— 让 AI 助手通过 stdio 操作本机 Tagger 打标器。

设计取舍：
* 只封装已经存在的 Web API（tagger/web/app.py），不重新实现任何打标/打包逻辑，
  也不 import tagger 包 —— 任务状态在 Web 进程内存里，绕开它就会读到假状态。
* 零第三方依赖：只用标准库 urllib，任何 Python 3.10+ 都能跑，不需要给
  install.bat / requirements-embedded.txt 加依赖，分发包解出来就能配。
* stdout 只走 JSON-RPC；所有日志/异常写 stderr。子进程（web 服务）的输出
  必须重定向到文件，否则会混进协议流把整条连接搞坏。

启动：python mcp/tagger_mcp.py
自检：python mcp/tagger_mcp.py --selftest
环境变量：
  TAGGER_MCP_URL     默认 http://127.0.0.1:27100
  TAGGER_MCP_ROOT    默认本文件上一级（仓库根），web 启动时的 cwd
  TAGGER_MCP_TIMEOUT 单次 HTTP 超时秒数，默认 30
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(
    os.environ.get("TAGGER_MCP_ROOT") or Path(__file__).resolve().parent.parent
).resolve()
BASE = (os.environ.get("TAGGER_MCP_URL") or "http://127.0.0.1:27100").rstrip("/")
TIMEOUT = float(os.environ.get("TAGGER_MCP_TIMEOUT") or "30")
MAX_TEXT = 60000
# 默认 stderr 全静默：有客户端会把子进程 stderr 并进 stdout，一行日志就能
# 把 JSON-RPC 流打断。要看日志就显式设 TAGGER_MCP_DEBUG=1。
DEBUG = os.environ.get("TAGGER_MCP_DEBUG") == "1"


def _log(message: str) -> None:
    if DEBUG:
        sys.stderr.write(f"[tagger-mcp] {message}\n")
        sys.stderr.flush()

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "tagger", "version": "0.1.0"}

START_HINT = (
    "本机 Web 服务没起来。任选一种启动方式：\n"
    "1) 双击仓库根的 Tagger.Shell.exe（推荐，关窗口即停服务）\n"
    "2) 运行 run-web.cmd / run-web.sh\n"
    "3) 在仓库根执行：env\\python\\python.exe -m tagger.cli web --host 127.0.0.1 --port 27100 --no-open\n"
    "4) 让 AI 调 tagger_web_start（会把服务作为独立后台进程拉起）\n"
    "若报缺依赖，先跑一次 install.bat / install.sh。"
)

INSTRUCTIONS = (
    "Tagger 打标器：图像训练数据的 tag / NL 描述标注、分包与封包流水线。"
    "先调 tagger_health 确认本机 Web 服务（默认 http://127.0.0.1:27100）在跑；"
    "不通就调 tagger_web_start 或让用户双击 Tagger.Shell.exe。"
    "建任务前先读 tagger_defaults 与 tagger_job_form，用 tagger_browse / tagger_preview 核对素材目录与盘位。"
    "tagger_job_start 的 spec 只能用 /api/jobs 的真实字段，拼错字段会被静默忽略，"
    "拿不准就用 tagger_endpoints 查 /openapi.json。"
    "tagger_job_start 会真的启动耗时数小时的打标任务，调用前先向用户确认参数；"
    "同一时刻只允许一个任务在跑，重复创建返回 409；长任务用 tagger_job(action='events') 轮询，不要反复重建。"
    "完整字段表、工作流与排错见仓库根 agent.md。"
)


# --------------------------------------------------------------------------- HTTP


def _request(
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    body: Any = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """打一次 Web API，永远返回结构化结果，不抛异常给 AI。"""
    if not path.startswith("/"):
        return {"ok": False, "status": 0, "error": "path 必须以 / 开头", "data": None}

    method = method.upper()
    url = BASE + path
    if query:
        clean = {k: v for k, v in query.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean, doseq=True)

    data = None
    headers = {"Accept": "application/json"}
    if method in ("POST", "PUT", "PATCH") and body is None:
        body = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "status": 0,
            "error": f"连不上 {BASE}：{type(exc).__name__}: {exc}",
            "data": None,
            "hint": START_HINT,
        }

    try:
        payload: Any = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        payload = raw[:4000]

    out: dict[str, Any] = {"ok": 200 <= status < 300, "status": status, "data": payload}
    if not out["ok"]:
        out["error"] = f"HTTP {status}"
        if status == 409:
            out["hint"] = (
                "冲突：通常已有打标任务/推理服务在跑，或状态不允许这个动作。"
                "先 tagger_jobs 或 tagger_service(action='status') 看现状，别重试同一个请求。"
            )
        elif status == 400:
            out["hint"] = (
                "参数不合法：对照 tagger_defaults / tagger_job_form / tagger_endpoints 核对字段与取值。"
                "注意 Pydantic 会静默丢掉拼错的字段。"
            )
        elif status == 422:
            out["hint"] = "请求体校验失败：用 tagger_endpoints 查 /openapi.json 里的真实 schema。"
        elif status == 404:
            out["hint"] = "路径或 id 不存在：用 tagger_jobs / tagger_browse / tagger_endpoints 确认。"
    return out


def _merge(*parts: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for part in parts:
        if isinstance(part, dict):
            out.update(part)
    return out


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    """把结果包成 MCP tool result；HTTP 失败也算 isError，让 AI 一眼看见。"""
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if len(text) > MAX_TEXT:
        text = (
            text[:MAX_TEXT]
            + f"\n...（已截断，原长 {len(text)} 字符；请缩小 limit / offset / 分页范围）"
        )
    return {
        "content": [{"type": "text", "text": text}],
        "isError": not bool(result.get("ok", True)),
    }


def _fail(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


# --------------------------------------------------------------------------- 工具实现


def tool_health(_args: dict[str, Any]) -> dict[str, Any]:
    r = _request("GET", "/api/health", timeout=5)
    if r["ok"]:
        r["next"] = (
            "服务正常。建任务前依次看 tagger_defaults、tagger_job_form、"
            "tagger_browse / tagger_preview、tagger_models + tagger_service(action='status')。"
        )
    else:
        r["how_to_start"] = "可调用 tagger_web_start 拉起服务，或让用户双击 Tagger.Shell.exe。"
    return r


def tool_web_start(args: dict[str, Any]) -> dict[str, Any]:
    """把 Web 服务作为独立后台进程拉起。stdout 绝不能继承，见模块注释。"""
    parsed = urllib.parse.urlsplit(BASE)
    port = int(args.get("port") or parsed.port or 27100)
    host = args.get("host") or parsed.hostname or "127.0.0.1"
    wait_s = float(args.get("wait_s") or 40)

    alive = _request("GET", "/api/health", timeout=3)
    if alive["ok"]:
        return {"ok": True, "already_running": True, "data": alive["data"]}

    candidates = [
        ROOT / "env" / "python" / "python.exe",
        ROOT / "env" / "linux" / "bin" / "python",
        Path(sys.executable),
    ]
    py = next((p for p in candidates if p.is_file()), Path(sys.executable))

    log = ROOT / "work" / "mcp-web.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = log.open("ab")
    except OSError as exc:
        return {"ok": False, "error": f"写不了日志 {log}：{exc}", "data": None}

    cmd = [
        str(py), "-m", "tagger.cli", "web",
        "--host", host, "--port", str(port), "--no-open",
    ]
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": handle,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000200 | 0x00000008  # NEW_PROCESS_GROUP | DETACHED
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        handle.close()
        return {"ok": False, "error": f"起不来：{exc}", "command": cmd, "data": None}
    finally:
        handle.close()

    deadline = time.monotonic() + wait_s
    last: dict[str, Any] = {"ok": False, "status": 0}
    while time.monotonic() < deadline:
        time.sleep(0.6)
        if proc.poll() is not None:
            last = {"ok": False, "error": f"进程提前退出（code={proc.returncode}），看 {log}"}
            break
        last = _request("GET", "/api/health", timeout=3)
        if last["ok"]:
            break
    return {
        "ok": bool(last.get("ok")),
        "pid": proc.pid,
        "python": str(py),
        "cwd": str(ROOT),
        "url": f"http://{host}:{port}",
        "log": str(log),
        "health": last,
        "note": "这是常驻后台进程，不会随 MCP 会话退出；停止请关 Tagger.Shell.exe 或按此 pid 精确结束。",
    }


def tool_defaults(_args: dict[str, Any]) -> dict[str, Any]:
    r = _request("GET", "/api/defaults")
    if r["ok"]:
        r["hint"] = "建任务时没给的字段会用服务端默认值，不要自己编数值。"
    return r


def tool_job_form(_args: dict[str, Any]) -> dict[str, Any]:
    r = _request("GET", "/api/job-form")
    if r["ok"]:
        r["hint"] = (
            "spec 是上次成功提交的任务参数（可能为空）。它是 tagger_job_start 最靠谱的模板："
            "取出来改 input_dir / out_dir / engine / output_mode 等几项再提交。"
        )
    return r


def tool_browse(args: dict[str, Any]) -> dict[str, Any]:
    return _request("GET", "/api/fs", query={"path": args.get("path") or ""})


def tool_preview(args: dict[str, Any]) -> dict[str, Any]:
    extra = args.get("extra") or {}
    query = {
        "path": args.get("path"),
        "pack": args.get("pack", True),
        "formats": args.get("formats") or "",
        "out_dir": args.get("out_dir") or "",
        "layout": args.get("layout") or "",
        "recursive": args.get("recursive"),
        "limit": args.get("limit"),
        **extra,
    }
    if not query["path"]:
        return {"ok": False, "error": "必须给 path（素材目录绝对路径）", "data": None}
    r = _request("GET", "/api/preview", query=query)
    if r["ok"]:
        r["hint"] = "确认张数、命名样例与预计盘位后，再调 tagger_job_start。"
    return r


def tool_models(_args: dict[str, Any]) -> dict[str, Any]:
    r = _request("GET", "/api/models")
    if r["ok"]:
        r["hint"] = (
            "weight 状态在 download / local 字段里。缺权重用 tagger_api('POST', "
            "'/api/models/download', body={'kind':'vlm','key':'<key>'})；"
            "起服务前报「GGUF 权重不存在」就是缺权重，不是缺依赖。"
        )
    return r


_SERVICE_ROUTES = {
    "status": ("GET", "/api/vllm", None),
    "report": ("GET", "/api/vllm/report", None),
    "capability": ("GET", "/api/vllm/capability", "query"),
    "plan": ("GET", "/api/vllm/plan", "query"),
    "start": ("POST", "/api/vllm/start", "body"),
    "stop": ("POST", "/api/vllm/stop", None),
    "probe": ("GET", "/api/vllm/probe", "query"),
    "llama": ("GET", "/api/llama", None),
    "multi": ("GET", "/api/multi", None),
    "deps": ("GET", "/api/deps", None),
}


def tool_service(args: dict[str, Any]) -> dict[str, Any]:
    action = args.get("action") or "status"
    if action not in _SERVICE_ROUTES:
        return {"ok": False, "error": f"未知 action：{action}", "allowed": sorted(_SERVICE_ROUTES), "data": None}
    method, path, where = _SERVICE_ROUTES[action]
    options = args.get("options") or {}
    if action == "capability" and not options.get("engine"):
        options = {"engine": "llamacpp"}
    r = _request(method, path, query=options if where == "query" else None,
                 body=options if where == "body" else None)
    if r["ok"] and action == "plan":
        r["hint"] = "plan 只算不启动；确认 warnings 后再 action='start'。"
    if r["ok"] and action == "start":
        r["hint"] = "服务是异步就绪的，用 action='status' 轮询，就绪后再建 vlm 任务。"
    return r


def tool_jobs(_args: dict[str, Any]) -> dict[str, Any]:
    r = _request("GET", "/api/jobs")
    if r["ok"]:
        r["hint"] = "同一时刻只允许一个打标任务在跑；建新任务前先看这里。"
    return r


def tool_job_start(args: dict[str, Any]) -> dict[str, Any]:
    spec = args.get("spec")
    if not isinstance(spec, dict) or not spec:
        return {"ok": False, "error": "spec 必须是非空对象；先读 tagger_job_form / tagger_defaults", "data": None}
    r = _request("POST", "/api/jobs", body=spec)
    if r["ok"]:
        r["hint"] = (
            "任务已开跑（202，不是已完成）。用 tagger_job(job_id=..., action='events') 轮询，"
            "或 action='status' 看快照；结束后 action='results' / 'stats'。"
        )
    return r


_JOB_ACTIONS = {
    "status": ("GET", ""),
    "events": ("GET", "/events"),
    "results": ("GET", "/results"),
    "stats": ("GET", "/stats"),
    "stop": ("POST", "/stop"),
    "pause": ("POST", "/pause"),
    "resume": ("POST", "/resume"),
    "restart": ("POST", "/restart"),
}


def tool_job(args: dict[str, Any]) -> dict[str, Any]:
    action = args.get("action") or "status"
    if action == "resume_state":
        return _request("GET", "/api/resume")
    if action not in _JOB_ACTIONS:
        return {"ok": False, "error": f"未知 action：{action}", "allowed": sorted(_JOB_ACTIONS) + ["resume_state"], "data": None}
    job_id = args.get("job_id")
    if not job_id:
        return {"ok": False, "error": "必须给 job_id（先 tagger_jobs）", "data": None}
    method, suffix = _JOB_ACTIONS[action]
    query: dict[str, Any] | None = None
    if action == "events":
        query = {"after": args.get("after", 0)}
    elif action == "results":
        query = {
            "offset": args.get("offset", 0),
            "limit": args.get("limit", 40),
            "q": args.get("q") or "",
            "only_failed": args.get("only_failed", False),
        }
    elif action == "stats":
        query = {"bucket": args.get("bucket", 1000)}
    if isinstance(args.get("extra"), dict):
        query = _merge(query, args["extra"])
    return _request(method, f"/api/jobs/{urllib.parse.quote(str(job_id), safe='')}{suffix}",
                    query=query)


def tool_artifacts(args: dict[str, Any]) -> dict[str, Any]:
    path = args.get("path")
    if not path:
        return {"ok": False, "error": "必须给 path（产物目录绝对路径）", "data": None}
    return _request("GET", "/api/artifacts", query={"path": path})


def tool_datajob(args: dict[str, Any]) -> dict[str, Any]:
    action = args.get("action") or "list"
    kind = args.get("kind") or "split"
    params = args.get("params") or {}
    job_id = args.get("job_id") or ""
    if action == "preview":
        return _request("POST", "/api/datajob/preview", body={"kind": "split", **params})
    if action == "start":
        return _request("POST", "/api/datajob/start", body={"kind": kind, "params": params})
    if action == "list":
        return _request("GET", "/api/datajob")
    if not job_id:
        return {"ok": False, "error": "status / stop 需要 job_id", "data": None}
    if action == "status":
        return _request("GET", f"/api/datajob/{urllib.parse.quote(str(job_id), safe='')}")
    if action == "stop":
        return _request("POST", f"/api/datajob/{urllib.parse.quote(str(job_id), safe='')}/stop")
    return {"ok": False, "error": f"未知 action：{action}", "allowed": ["preview", "start", "list", "status", "stop"], "data": None}


def tool_endpoints(args: dict[str, Any]) -> dict[str, Any]:
    needle = (args.get("filter") or "").lower()
    r = _request("GET", "/openapi.json", timeout=15)
    if not r["ok"]:
        return r
    paths = (r.get("data") or {}).get("paths") or {}
    rows: list[dict[str, str]] = []
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method, meta in ops.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch"):
                continue
            meta = meta if isinstance(meta, dict) else {}
            row = {
                "method": method.upper(),
                "path": path,
                "summary": meta.get("summary") or meta.get("operationId") or "",
            }
            blob = f"{row['method']} {row['path']} {row['summary']}".lower()
            if not needle or needle in blob:
                rows.append(row)
    shown = rows[:200]
    return {
        "ok": True,
        "count": len(rows),
        "truncated": len(rows) > len(shown),
        "endpoints": shown,
        "hint": "没封装成专用工具的能力，用 tagger_api(method, path, query, body) 直接调；"
                "完整请求体 schema 在 /openapi.json 里。",
    }


def tool_api(args: dict[str, Any]) -> dict[str, Any]:
    method = str(args.get("method") or "GET").upper()
    path = args.get("path") or ""
    if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        return {"ok": False, "error": f"不支持的 method：{method}", "data": None}
    if not (path.startswith("/api/") or path == "/openapi.json"):
        return {"ok": False, "error": "只允许 /api/* 与 /openapi.json", "data": None}
    query = args.get("query") or {}
    body = args.get("body")
    if not isinstance(query, dict):
        return {"ok": False, "error": "query 必须是对象", "data": None}
    return _request(method, path, query=query, body=body)


TOOL_IMPL = {
    "tagger_health": tool_health,
    "tagger_web_start": tool_web_start,
    "tagger_defaults": tool_defaults,
    "tagger_job_form": tool_job_form,
    "tagger_browse": tool_browse,
    "tagger_preview": tool_preview,
    "tagger_models": tool_models,
    "tagger_service": tool_service,
    "tagger_jobs": tool_jobs,
    "tagger_job_start": tool_job_start,
    "tagger_job": tool_job,
    "tagger_artifacts": tool_artifacts,
    "tagger_datajob": tool_datajob,
    "tagger_endpoints": tool_endpoints,
    "tagger_api": tool_api,
}


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    return out


_STR = {"type": "string"}
_NUM = {"type": "number"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_DICT = {"type": "object"}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "tagger_health",
        "description": "探活本机 Tagger Web 服务：Python/平台/GPU、前端是否已构建、当前在跑的任务 id。服务没起时返回具体启动指引。",
        "inputSchema": _obj({}),
    },
    {
        "name": "tagger_web_start",
        "description": "把 Tagger Web 服务作为独立后台进程拉起（等价 tagger web --no-open），轮询 /api/health 直到就绪。只在 health 不通时用。",
        "inputSchema": _obj({
            "port": {**_INT, "description": "端口，默认取 TAGGER_MCP_URL 的端口（27100）"},
            "host": {**_STR, "description": "监听地址，默认 127.0.0.1；不要改成 0.0.0.0，服务无鉴权"},
            "wait_s": {**_NUM, "description": "等待就绪的秒数，默认 40"},
        }),
    },
    {
        "name": "tagger_defaults",
        "description": "取新建任务页的服务端默认参数（分档、质检、硬件保护、封包质量等）。建任务前必读，别自己编数值。",
        "inputSchema": _obj({}),
    },
    {
        "name": "tagger_job_form",
        "description": "取上次填写的表单记忆 form 与上次成功提交的 spec（configs/ui-form.json）。spec 是 tagger_job_start 最靠谱的模板。",
        "inputSchema": _obj({}),
    },
    {
        "name": "tagger_browse",
        "description": "浏览目录选素材：返回子目录、当前层图片数、素材包（zip/tar）与盘符。path 留空从当前目录/盘符根开始。",
        "inputSchema": _obj({"path": {**_STR, "description": "目录绝对路径；留空看盘符"}}),
    },
    {
        "name": "tagger_preview",
        "description": "建任务前预演：会发现多少张、命名样例、画师覆盖、预计写盘量。只扫路径不读图片字节，不加载模型。",
        "inputSchema": _obj({
            "path": {**_STR, "description": "素材目录绝对路径（必填）"},
            "pack": {**_BOOL, "description": "按封包口径预演，默认 true；只打标任务要传 false"},
            "formats": {**_STR, "description": "逗号分隔，如 parquet,zip；留空只估算打标产物"},
            "out_dir": _STR,
            "layout": {**_STR, "description": "flat | mirror"},
            "recursive": _BOOL,
            "limit": _INT,
            "extra": {**_DICT, "description": "其它 query 原样透传：artist / artist_depth / artist_table / include_zips / include_loose / seq_width / start_seq / jpeg_quality / jpeg_subsampling / reencode"},
        }, ["path"]),
    },
    {
        "name": "tagger_models",
        "description": "模型清单与权重就绪状态（NL 的 vlm、tag 分类头的 tag），含下载进度。起推理服务前先确认权重存在。",
        "inputSchema": _obj({}),
    },
    {
        "name": "tagger_service",
        "description": "管理 NL 推理服务：status/report/capability/plan/start/stop/probe/llama/multi/deps。start 之前先 plan 看分档与 warnings。",
        "inputSchema": _obj({
            "action": {"type": "string", "enum": sorted(_SERVICE_ROUTES), "description": "默认 status"},
            "options": {**_DICT, "description": "query 或 body 字段；start 用 VllmStartRequest 字段（engine/model/tier/precision/port/slots/served_name...）"},
        }),
    },
    {
        "name": "tagger_jobs",
        "description": "列出所有打标任务（内存活任务 + 盘上历史）及其状态/进度快照。建新任务前先看这里。",
        "inputSchema": _obj({}),
    },
    {
        "name": "tagger_job_start",
        "description": "新建一个打标任务并立即开跑（POST /api/jobs，返回 202 快照）。这是耗时数小时的真实写操作，调用前必须向用户确认参数。同一时刻只允许一个任务在跑。spec 字段必须与 /api/jobs 的 JobRequest 一致，拼错的字段会被静默忽略。",
        "inputSchema": _obj({
            "spec": {
                **_DICT,
                "description": "JobRequest 对象。常用：input_dir / out_dir / engine(local|vlm) / backend / output_mode(both|nl|tag) / vlm_provider(local|openai|anthropic) / url / vlm_model / api_base / api_key / prompt_id / concurrency / caption_zh_ratio / caption_chars / limit / force / pack / formats / layout / jpeg_quality / hf_repo / hf_token。完整字段见 agent.md 或 tagger_endpoints 查 /openapi.json。",
            },
        }, ["spec"]),
    },
    {
        "name": "tagger_job",
        "description": "单个打标任务：status 快照 / events 增量事件（配 after 游标轮询）/ results 分页结果 / stats 分桶统计 / stop|pause|resume|restart 控制 / resume_state 查断点。",
        "inputSchema": _obj({
            "job_id": {**_STR, "description": "任务 id（必填，status 等动作）"},
            "action": {"type": "string", "enum": sorted(_JOB_ACTIONS) + ["resume_state"], "description": "默认 status"},
            "after": {**_INT, "description": "events 的事件游标，传上次返回的最大 seq"},
            "offset": _INT,
            "limit": {**_INT, "description": "results 每页条数，上限 200，默认 40"},
            "q": {**_STR, "description": "results 搜索关键字"},
            "only_failed": _BOOL,
            "bucket": {**_INT, "description": "stats 桶宽，必须是 100 的倍数，默认 1000"},
            "extra": {**_DICT, "description": "额外 query 参数原样透传"},
        }, ["job_id"]),
    },
    {
        "name": "tagger_artifacts",
        "description": "列出产物目录里可回传的分片（*.parquet / *.jsonl）及每片行数 rows。排除 failures/manifest/state 等非产物文件。",
        "inputSchema": _obj({"path": {**_STR, "description": "产物目录绝对路径（必填）"}}, ["path"]),
    },
    {
        "name": "tagger_datajob",
        "description": "数据任务：分包 split / 拼接 merge / 封包 pack。preview 只扫路径；start 传 params（未知字段会被后端拒绝）；list/status/stop 管进度。去重与 FTP 回传用 tagger_api。",
        "inputSchema": _obj({
            "action": {"type": "string", "enum": ["preview", "start", "list", "status", "stop"], "description": "默认 list"},
            "kind": {"type": "string", "enum": ["split", "merge", "pack"], "description": "start/preview 用，默认 split"},
            "params": {**_DICT, "description": "split: input_dir/out_dir/table/per_shard/layout...；merge: roots/out/require...；pack: input_dir/out_dir/formats/jpeg_quality/assign_ids...。字段表见 agent.md。"},
            "job_id": _STR,
        }),
    },
    {
        "name": "tagger_endpoints",
        "description": "从 /openapi.json 列出全部 HTTP 路由（method/path/summary），可按关键字过滤。找没封装成专用工具的能力时用。",
        "inputSchema": _obj({"filter": {**_STR, "description": "路径或摘要的子串，如 jobs / prompts / conflicts / center；留空列全部"}}),
    },
    {
        "name": "tagger_api",
        "description": "通用逃生口：直接调任意 /api/* 或 /openapi.json。用于提示词、预设、去重、回传、冲突裁决、调度中心、自检等未封装端点。",
        "inputSchema": _obj({
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"], "description": "默认 GET"},
            "path": {**_STR, "description": "以 /api/ 开头的路径，如 /api/prompts"},
            "query": {**_DICT, "description": "URL query 参数"},
            "body": {"description": "JSON 请求体（POST/PUT 用）"},
        }, ["method", "path"]),
    },
]


# --------------------------------------------------------------------------- MCP 协议


def _rpc_ok(rid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_err(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    params = msg.get("params") or {}
    rid = msg.get("id")

    if method == "initialize":
        want = params.get("protocolVersion")
        version = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _rpc_ok(rid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _rpc_ok(rid, {})
    if method == "tools/list":
        return _rpc_ok(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        impl = TOOL_IMPL.get(name)
        if impl is None:
            return _rpc_ok(rid, _fail(f"未知工具：{name}。可用：{', '.join(TOOL_IMPL)}"))
        if not isinstance(arguments, dict):
            return _rpc_ok(rid, _fail("arguments 必须是对象"))
        try:
            return _rpc_ok(rid, _ok(impl(arguments)))
        except Exception as exc:  # noqa: BLE001 - 单个工具出错不能弄死整条连接
            return _rpc_ok(rid, _fail(f"工具 {name} 异常：{type(exc).__name__}: {exc}"))
    if method in ("resources/list", "resources/templates/list", "prompts/list"):
        empty = {
            "resources/list": {"resources": []},
            "resources/templates/list": {"resourceTemplates": []},
            "prompts/list": {"prompts": []},
        }
        return _rpc_ok(rid, empty[method])
    return _rpc_err(rid, -32601, f"未知方法：{method}")


def serve() -> None:
    """stdio transport：一行一个 UTF-8 JSON-RPC 消息；通知不回包。

    必须按字节读再自己 decode：Windows 上 sys.stdin 默认走控制台编码（cp936），
    客户端发来的 UTF-8 中文路径会直接变成乱码；utf-8-sig 顺带吃掉首行 BOM，
    否则 PowerShell / 某些客户端的第一条 initialize 会被当成垃圾行丢掉。
    """
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    for raw in stream:
        if isinstance(raw, bytes):
            line = raw.decode("utf-8-sig", "replace").strip()
        else:
            line = raw.strip().lstrip("\ufeff").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _log("忽略非 JSON 行")
            continue
        if not isinstance(msg, dict):
            continue
        if "id" not in msg:  # notification，例如 notifications/initialized
            continue
        resp = handle(msg)
        if resp is None:
            continue
        # ensure_ascii=True：外层纯 ASCII，彻底避开 Windows 控制台编码把协议流搞坏
        sys.stdout.write(json.dumps(resp, ensure_ascii=True) + "\n")
        sys.stdout.flush()


def selftest() -> int:
    """不依赖 Web 服务的协议自检：握手、工具表、参数校验、降级路径。"""
    checks: list[tuple[str, bool, str]] = []

    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": PROTOCOL_VERSIONS[0]}})
    checks.append(("initialize", bool(init and "result" in init), json.dumps(init, ensure_ascii=False)[:120]))

    listing = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in (listing or {}).get("result", {}).get("tools", [])]
    ok = len(names) == len(TOOL_IMPL) and all(
        {"name", "description", "inputSchema"} <= set(t) for t in TOOLS
    )
    checks.append(("tools/list", ok, f"{len(names)} 个工具"))

    missing = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "tagger_job_start", "arguments": {}}})
    txt = json.dumps(missing, ensure_ascii=False)
    checks.append(("参数校验", bool(missing and missing["result"]["isError"]), txt[:120]))

    health = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "tagger_health", "arguments": {}}})
    checks.append(("tools/call 降级", bool(health and "result" in health),
                   json.dumps(health, ensure_ascii=False)[:120]))

    bad = handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "tagger_api", "arguments": {"method": "GET", "path": "/"}}})
    checks.append(("路径白名单", bool(bad and bad["result"]["isError"]),
                   json.dumps(bad, ensure_ascii=False)[:120]))

    print(f"Tagger MCP selftest  ROOT={ROOT}  URL={BASE}")
    for label, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    failed = sum(1 for _, p, _ in checks if not p)
    print(f"结果：{len(checks) - failed}/{len(checks)} 通过"
          f"（Web 服务未启动属于预期，tools/call 降级检查只看结构）")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        raise SystemExit(selftest())
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    _log(f"ready  root={ROOT}  api={BASE}")
    try:
        serve()
    except KeyboardInterrupt:
        pass
