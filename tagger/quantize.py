"""W4A16 量化：把 bf16 权重压成 4bit，供 vLLM 加载。

为什么不直接 import llmcompressor 在 Web 进程里跑：

1. 量化会吃满显存并跑几十分钟，Web 进程不能被它拖住，也不该继承它的
   CUDA 上下文（vLLM 服务可能正跑在同一个进程树里）；
2. 依赖很重（torch + transformers + llmcompressor + datasets），只做打标的
   机器不该被强制安装；
3. 要能取消——进程组一收就干净，线程里 import 进来的 CUDA 上下文收不掉。

产物落在 models/ 之内（默认 models/nl/<key>-w4a16），完成后把路径写进
configs/models.json 的 quantized_path。**只有写进覆盖清单，下次启动
才会真的用它**——否则量化完了也没人知道该加载哪份权重。

目录名特意不带 awq/gptq：llm-compressor 产出的是 compressed-tensors 格式，
名字里带 "gptq" 会让 detect_quant_method 误判成 auto-gptq，vLLM 加载时
报的是一堆键名对不上。不带关键词就交给 vLLM 读 config.json 自己认。
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DEFAULT_TIERS, ConfigError, load_tier_table, overlay_path

ROOT = Path(__file__).resolve().parents[1]
MODELS_ROOT = ROOT / "models"

# llm-compressor 走 GPTQ/AWQ 两条路，产物都是 compressed-tensors 的 W4A16。
QUANT_METHODS = ("gptq", "awq")

# 缺一个都跑不起来。torch 单独列是因为它最容易"装了但没 CUDA"。
_REQUIRED = ("torch", "transformers", "llmcompressor", "datasets", "accelerate")

# 依赖探测要 importlib，不 import torch——后者在 Web 进程里要几百 MB 常驻。
_CAP_TTL_S = 60.0

_LOG_LINES = 200
_LINE_MAX = 400
_PROGRESS_PREFIX = "@@PROGRESS "


@dataclass
class QuantTask:
    model_key: str
    source: str
    out_dir: Path
    method: str
    status: str = "running"          # running / done / error / canceled
    percent: float = 0.0
    stage: str = "准备"
    message: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancel: bool = False
    proc: subprocess.Popen | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_LINES))

    def state(self) -> dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "model": self.model_key,
            "source": self.source,
            "out_dir": str(self.out_dir),
            "method": self.method,
            "status": self.status,
            "percent": round(self.percent, 1),
            "stage": self.stage,
            "message": self.message,
            "elapsed": round(elapsed, 1),
            # 结束时间：页面拿它判断"这条是不是已经收尾了"，以及做完成通知的去重。
            "finished_at": self.finished_at,
            "log_tail": list(self.log),
        }


def _module_ready(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _guard_out(out: Path) -> Path:
    """量化产物只允许落在 models/ 之内。

    这不是洁癖：路径可编辑，一个手滑指到素材目录再"清理失败产物"，就是
    成批删图。与 downloads._guard 同一条底线。
    """
    target = Path(out)
    if not target.is_absolute():
        target = ROOT / target
    target = target.resolve()
    root = MODELS_ROOT.resolve()
    if target == root or root not in target.parents:
        raise ValueError(f"拒绝写入：{target} 不在模型目录 {root} 之内")
    return target


def _model_source(key: str | None) -> tuple[str, dict[str, Any]]:
    """解析要量化的模型：返回 (key, 配置条目)。

    量化输入必须是**原始 bf16 权重**（path），不是 resolved_path——后者在
    已经量化过的情况下指向 4bit 目录，再量化一次没有意义。
    """
    table = load_tier_table(DEFAULT_TIERS)
    models = table.get("models") or {}
    if not models:
        raise ConfigError("分档表里没有任何 NL 模型，没有可量化的权重")
    chosen = (key or "").strip()
    if not chosen:
        # 空 = 按显存自动落档，和启动服务时同一口径；没有卡时退回第一个模型。
        try:
            from .hardware import query_gpus
            from .config import resolve_profile

            gpus = query_gpus()
            if gpus:
                chosen = resolve_profile(table, gpus[0]).model.key
        except Exception:                # noqa: BLE001 - 没有卡也要能看清单
            chosen = ""
        chosen = chosen or str(next(iter(models)))
    if chosen not in models:
        raise ConfigError(
            f"未知模型 {chosen!r}，可选：{', '.join(map(str, models))}")
    spec = models[chosen]
    if not isinstance(spec, dict) or not str(spec.get("path") or "").strip():
        raise ConfigError(f"模型 {chosen} 缺少原始权重路径，没法量化")
    return chosen, spec


def suggested_dir(key: str) -> Path:
    return MODELS_ROOT / "nl" / f"{key}-w4a16"


def _file_bytes(path: str | None) -> int:
    """产物大小（字节）。目录形态（safetensors 分片）就把里面的文件加起来。"""
    text = (path or "").strip()
    if not text:
        return 0
    target = Path(text)
    try:
        if target.is_file():
            return target.stat().st_size
        if target.is_dir():
            return sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    except OSError:
        return 0
    return 0


def _exists(path: str | None) -> bool:
    """量化路径存不存在。HF 仓库名（org/name）没法在这台机器上判，按存在算。"""
    text = (path or "").strip()
    if not text:
        return False
    p = Path(text)
    if not p.is_absolute():
        p = ROOT / p
    if p.exists():
        return True
    return "/" in text and "\\" not in text and not text.startswith(".")


class QuantManager:
    """进程级单例。同一时刻只跑一个量化——单卡上两份量化必有一份 OOM。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: QuantTask | None = None
        self._cap: tuple[float, bool, str] | None = None
        # 空 model 参数 = 按显存落档，解析要调一次 nvidia-smi。轮询每 1.5s
        # 一次，不缓存的话量化全程都在跑子进程查询。
        self._key_cache: tuple[float, str] | None = None

    # ---------------------------------------------------------------- 可用性

    def capability(self) -> tuple[bool, str]:
        """能不能在这台机器上量化。

        **不按平台拦截**：量化用的是 PyTorch（llm-compressor 的 GPTQ/AWQ 都是
        纯 torch 实现），Windows 原生完全跑得动；vLLM 只能跑 Linux/WSL2 是
        另一回事——产物是普通目录，WSL 走 /mnt 或拷到远端都能加载。真正
        的前提只有两个：依赖装齐、有 NVIDIA 显卡。
        """
        now = time.time()
        if self._cap is not None and now - self._cap[0] < _CAP_TTL_S:
            return self._cap[1], self._cap[2]
        missing = self.missing_modules()
        if missing:
            ok, reason = False, (
                f"缺少量化依赖：{', '.join(missing)}。"
                f"安装：pip install 'tagger[quantize]'")
        else:
            try:
                from .hardware import query_gpus

                gpus = query_gpus()
            except Exception:                 # noqa: BLE001 - 体检失败不该弹异常
                gpus = []
            if not gpus:
                ok, reason = False, (
                    "没有检测到 NVIDIA 显卡（nvidia-smi 不可用）；量化需要 CUDA")
            else:
                ok, reason = True, ""
        self._cap = (now, ok, reason)
        return ok, reason

    def missing_modules(self) -> list[str]:
        """还没装上的量化依赖（导入名）。

        单列一个方法是因为页面要**按这个名字**决定显示"一键安装"还是别的
        建议：只靠 reason 里那句中文去猜是不行的，改一个字就失灵。
        """
        return [m for m in _REQUIRED if not _module_ready(m)]

    def refresh_capability(self) -> None:
        """清掉可用性缓存。

        装完依赖回来必须立刻能看到"现在可以量化了"：缓存 TTL 是 60 秒，
        不主动清的话用户点完安装、回到弹窗仍是一片"这台机器不能量化"，
        只会以为装失败了，然后再点一次。
        """
        with self._lock:
            self._cap = None

    # ---------------------------------------------------------------- 查询

    def _current_key(self, model_key: str | None) -> str:
        """页面传空时落成哪个模型：进行中的任务优先，其次按显存落档（带缓存）。"""
        key = (model_key or "").strip()
        if key:
            return key
        with self._lock:
            task = self._task
        if task is not None:
            return task.model_key
        now = time.time()
        cached = self._key_cache
        if cached is not None and now - cached[0] < _CAP_TTL_S:
            return cached[1]
        try:
            resolved, _spec = _model_source(None)
        except (ConfigError, ValueError):
            return ""
        self._key_cache = (now, resolved)
        return resolved

    def snapshot(self, model_key: str | None = None) -> dict[str, Any]:
        """页面打开弹窗时拉一次：能不能量化、有没有现成权重、默认落哪。"""
        ok, reason = self.capability()
        out: dict[str, Any] = {
            "available": ok,
            "reason": reason,
            # 页面据此决定要不要出"一键安装"按钮；只靠 reason 的中文文案
            # 去判断，改一个字按钮就没了。
            "missing_deps": self.missing_modules(),
            "model": "",
            "source": "",
            "quantized_path": None,
            "has_quantized": False,
            "suggested_dir": "",
            "methods": list(QUANT_METHODS),
            "task": None,
        }
        try:
            key, spec = _model_source(self._current_key(model_key))
        except (ConfigError, ValueError) as exc:
            out["reason"] = str(exc)
            return out
        quantized = str(spec.get("quantized_path") or "").strip()
        suggested = suggested_dir(key)
        try:
            # 页面上要的是能直接编辑的相对路径；模型根被指到别处时退化成绝对路径。
            shown_dir = str(suggested.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            shown_dir = str(suggested)
        out.update({
            "model": key,
            "source": str(spec.get("path") or ""),
            "quantized_path": quantized or None,
            "has_quantized": _exists(quantized),
            # 完成摘要里要显示"产物多大"，不然用户不知道该不该留着原权重。
            "quantized_bytes": _file_bytes(quantized),
            "suggested_dir": shown_dir,
        })
        with self._lock:
            task = self._task
        if task is not None and task.model_key == key:
            out["task"] = task.state()
        return out

    def activity(self) -> dict[str, Any] | None:
        """给底部进度条 / 完成通知用的最小快照（没有任务时返回 None）。

        为什么不复用 snapshot()：那个要解析模型路径、算量化目录、还要查依赖，
        几秒轮询一次太贵；底栏只需要"跑到哪、还剩多久、成没成"。
        """
        with self._lock:
            task = self._task
        if task is None:
            return None
        return {
            "id": f"quantize:{task.model_key}",
            "kind": "quantize",
            "title": "量化",
            "detail": task.model_key,
            "status": task.status,
            "percent": round(task.percent, 1),
            "stage": task.stage,
            "message": task.message,
            "elapsed_s": round((task.finished_at or time.time()) - task.started_at, 1),
            "finished_at": task.finished_at,
            "log_tail": list(task.log)[-6:],
        }

    # ---------------------------------------------------------------- 启停

    def start(self, model_key: str | None = None, out_dir: str | None = None,
              method: str = "gptq") -> dict[str, Any]:
        method = (method or "gptq").strip().lower()
        if method not in QUANT_METHODS:
            raise ValueError(
                f"未知量化方法 {method!r}，可选：{', '.join(QUANT_METHODS)}")
        ok, reason = self.capability()
        if not ok:
            raise ConfigError(reason)
        key, spec = _model_source(model_key)
        raw_out = (out_dir or "").strip()
        target = _guard_out(Path(raw_out) if raw_out else suggested_dir(key))
        source = str(spec.get("path") or "")
        source_path = Path(source)
        if not source_path.is_absolute():
            source_path = ROOT / source_path
        if source_path.exists() and (target == source_path.resolve()
                                     or source_path.resolve() in target.parents):
            raise ValueError("输出目录不能是原始权重目录（或其子目录），会把源权重覆盖掉")
        with self._lock:
            if self._task is not None and self._task.status == "running":
                return self._task.state()
            task = QuantTask(model_key=key, source=source, out_dir=target,
                             method=method)
            self._task = task
        target.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._run, args=(task,), daemon=True,
                         name=f"quant-{key}").start()
        return task.state()

    def cancel(self) -> dict[str, Any] | None:
        with self._lock:
            task = self._task
        if task is None:
            return None
        if task.status == "running":
            task.cancel = True
            self._terminate(task)
            task.status = "canceled"
            task.stage = "已取消"
            task.message = "量化已取消；半成品目录留着，下次会覆盖写"
            task.finished_at = time.time()
        return task.state()

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _terminate(task: QuantTask) -> None:
        proc = task.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if platform.system() == "Windows":
                # terminate() 只杀直接子进程。量化进程自己不再 spawn，但 torch
                # 的编译/数据线程挂在同一棵树上，整棵收掉才不会留着占显存。
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                # llm-compressor 会 spawn worker，整组发 SIGINT 让它退
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass

    def _consume(self, task: QuantTask, line: str) -> None:
        text = line.rstrip()
        if text.startswith(_PROGRESS_PREFIX):
            parts = text[len(_PROGRESS_PREFIX):].strip().split(" ", 1)
            try:
                task.percent = max(0.0, min(100.0, float(parts[0])))
            except ValueError:
                return
            if len(parts) > 1 and parts[1].strip():
                task.stage = parts[1].strip()
            return
        if not text:
            return
        task.log.append(text[:_LINE_MAX])

    def _run(self, task: QuantTask) -> None:
        try:
            cmd = [
                sys.executable, "-m", "tagger.quantize_worker",
                "--model", task.source,
                "--out", str(task.out_dir),
                "--method", task.method,
            ]
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # 必须显式 utf-8：worker 按 utf-8 输出中文日志，沿用系统
                # locale（Windows 常是 cp936）会在读管道时解码失败。
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # 独立进程组，取消时整组回收（torch 的数据加载器也会 spawn）
                start_new_session=(platform.system() != "Windows"),
            )
            task.proc = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                self._consume(task, line)
                if task.cancel:
                    break
            code = proc.wait()
            if task.cancel:
                # cancel() 已经把状态写好了；这里只收尾，不覆盖成 failed。
                task.finished_at = task.finished_at or time.time()
                return
            if code != 0:
                task.status = "error"
                task.stage = "失败"
                task.message = f"量化进程退出（code={code}），看日志末尾"
                return
            # 写进覆盖清单，下次启动才会真的加载这份权重。
            self._register(task)
            task.status = "done"
            task.percent = 100.0
            task.stage = "完成"
            task.message = f"已量化到 {task.out_dir}"
        except Exception as exc:              # noqa: BLE001 - 失败必须让页面看见
            task.status = "error"
            task.stage = "失败"
            task.message = f"{type(exc).__name__}: {exc}"
        finally:
            task.finished_at = time.time()

    @staticmethod
    def _register(task: QuantTask) -> None:
        """把 quantized_path 写进 configs/models.json。

        覆盖条目是**整体替换**（apply_model_overlay 只认带 path 的条目），
        所以要把分档表当前生效的字段一起抄过去，不能只写一行路径。
        """
        table = load_tier_table(DEFAULT_TIERS)
        spec = (table.get("models") or {}).get(task.model_key) or {}
        path = str(spec.get("path") or task.source)
        doc: dict[str, Any] = {}
        p = overlay_path()
        if p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    doc = raw
            except (OSError, ValueError):
                doc = {}
        # 相对仓库根写：同一份 models.json 在 Windows 与 WSL（/mnt/...）下
        # 都能解析。写死 C:\... 的话，换到 WSL 起 vLLM 就找不到权重了。
        try:
            out_ref = str(task.out_dir.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            out_ref = str(task.out_dir)
        vlm = dict(doc.get("vlm") or {})
        entry: dict[str, Any] = {
            "path": path,
            "revision": spec.get("revision"),
            "family": str(spec.get("family") or "qwen3vl"),
            # 记成压缩格式名，模型管理页也认（_PRECISIONS 已放行 awq/gptq）。
            "precision": task.method,
            "quantized_path": out_ref,
        }
        # 覆盖是整体替换，少抄一个字段就等于把它抹掉：gguf / mmproj 一丢，
        # 默认引擎（llama.cpp）下次启动会直接报"没有配 GGUF 权重"。
        # gguf_quants 同理——抹掉它，精度的 q6/q8 就找不到文件。
        for field in ("gguf", "mmproj"):
            value = str(spec.get(field) or "").strip()
            if value:
                entry[field] = value
        quants = spec.get("gguf_quants")
        if isinstance(quants, dict) and quants:
            entry["gguf_quants"] = {str(k): str(v) for k, v in quants.items()}
        vlm[task.model_key] = entry
        doc["vlm"] = vlm
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        # load_tier_table 每次读盘，没有缓存要清：写完下一次 resolve_profile
        # 就会看到新的 quantized_path。
        os.replace(tmp, p)


QUANT = QuantManager()
