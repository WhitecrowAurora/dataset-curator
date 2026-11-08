"""原始权重（safetensors）→ GGUF F16 → Q4_K_M / Q6_K。

为什么需要这一步
----------------
默认引擎是 llama.cpp，它只吃 GGUF。而很多模型根本没有发布 GGUF（自训、微调、
某些破限版），这时候"下载完整权重"之后本地就再也走不下去了。这条链补的就是
中间那段：把 transformers 格式的原始权重直接变成能跑的 GGUF。

为什么调 llama.cpp 的官方脚本，而不是自己写
--------------------------------------------
`convert_hf_to_gguf.py` 里每个架构的张量改名、rope 参数、tokenizer 元数据、
视觉塔到 mmproj 的映射，全是踩过坑的口径。自己复刻一份的风险不是"跑不起来"，
而是**跑起来了但产出变差**——量化档位对不上、rope 差一点、某个张量没做过
permute，症状只有"打标结果怪怪的"。所以这里只做四件事：取脚本、装依赖、
跑、把产物登记回清单。

为什么不缓存中间产物
--------------------
F16 的 8B 权重约 17G，而用户要的是 Q4/Q6（6G 上下）。中间那份只在同一次任务
里有意义，任务结束就删——留着它只会让 `models/` 目录涨得莫名其妙，还会被
"清理磁盘"的人当成垃圾删掉。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from . import gguf_quant, llama_runtime
from .config import ConfigError

ROOT = Path(__file__).resolve().parents[1]
MODELS_ROOT = ROOT / "models"
# 跟 llama.cpp 运行时放一起：同一个版本、同一条升级路径，用户不用记两个目录
TOOLS_DIR = llama_runtime.INSTALL_ROOT / "convert"
SCRIPT = TOOLS_DIR / "convert_hf_to_gguf.py"
TOOLS_TAG = TOOLS_DIR / "VERSION"

SOURCE_TARBALL = "https://github.com/ggml-org/llama.cpp/archive/refs/tags/{tag}.tar.gz"
RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
_TIMEOUT = httpx.Timeout(connect=20.0, read=120.0, write=120.0, pool=20.0)

# 转换本身只要 torch + transformers；量化那步是 numpy（见 gguf_quant）。
# sentencepiece 只有部分架构的 tokenizer 用得上，缺了会报得很清楚，不列成硬门槛。
_REQUIRED = ("torch", "transformers")
_LOG_LINES = 400

_TOOLS_LOCK = threading.Lock()


# ------------------------------------------------------------------ 工具链
_TAG_MEMO = ""


def installed_tag() -> str:
    """脚本对应哪个 llama.cpp 版本。

    优先用我们下载时记的，其次看运行时的版本标记。两者都没有就取最新发布——
    只是"版本号"用途，取新的一点不影响兼容（GGUF 的格式是稳定的）。
    查过一次就记住：这个函数被状态接口每次调用，不能每回都去打 GitHub。
    """
    global _TAG_MEMO
    if _TAG_MEMO:
        return _TAG_MEMO
    for candidate in (TOOLS_TAG, llama_runtime.VERSION_FILE):
        try:
            text = candidate.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            continue
        if text and text[0].strip():
            _TAG_MEMO = text[0].strip()
            return _TAG_MEMO
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            data = client.get(RELEASE_API).json()
        tag = str(data.get("tag_name") or "").strip()
        if tag:
            _TAG_MEMO = tag
            return tag
    except Exception:                                   # noqa: BLE001 - 离线也要能用
        pass
    return "master"


def _extract_members(tar: tarfile.TarFile, dest: Path) -> None:
    """只解转换需要的那几个路径。

    整包解出来是几百 MB 的源码（examples/、tests/、ggml/…），我们只要两个
    Python 包和一个入口脚本。
    """
    wanted = ("convert_hf_to_gguf.py", "conversion/", "gguf-py/")
    for member in tar.getmembers():
        # 压缩包里顶层是 llama.cpp-<tag>/，按"/"切掉第一段
        parts = member.name.split("/", 1)
        if len(parts) != 2:
            continue
        rel = parts[1]
        if not rel.startswith(wanted):
            continue
        target = dest / rel
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = tar.extractfile(member)
        if source is None:
            continue
        with source, target.open("wb") as handle:
            shutil.copyfileobj(source, handle)


def ensure_tools(log: Callable[[str], None] | None = None) -> Path:
    """确保转换脚本在位，缺就按当前版本取一份。返回脚本路径。"""
    with _TOOLS_LOCK:
        if SCRIPT.is_file() and (TOOLS_DIR / "conversion").is_dir():
            return SCRIPT
        say = log or (lambda _msg: None)
        tag = installed_tag()
        url = SOURCE_TARBALL.format(tag=tag)
        say(f"取转换脚本（llama.cpp {tag}）")
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "src.tar.gz"
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with archive.open("wb") as handle:
                        total = int(response.headers.get("content-length") or 0)
                        done = 0
                        step = 0
                        for chunk in response.iter_bytes(1 << 20):
                            handle.write(chunk)
                            done += len(chunk)
                            # 每 20% 报一次：源码包才十几 MB，逐块刷屏没意义
                            if total and done * 5 // total > step:
                                step = done * 5 // total
                                say(f"下载源码 {done / 1048576:.0f}/"
                                    f"{total / 1048576:.0f} MB")
            with tarfile.open(archive, "r:gz") as tar:
                _extract_members(tar, TOOLS_DIR)
        if not SCRIPT.is_file():
            raise ConfigError(
                "转换脚本解压失败：包结构可能变了（找不到 convert_hf_to_gguf.py）")
        try:
            TOOLS_TAG.write_text(tag + "\n", encoding="utf-8")
        except OSError:
            pass
        say(f"转换脚本就绪（{SCRIPT}）")
        return SCRIPT


def missing_deps() -> list[str]:
    import importlib

    out: list[str] = []
    for name in _REQUIRED:
        try:
            importlib.import_module(name)
        except Exception:                               # noqa: BLE001
            out.append(name)
    return out


def capability() -> tuple[bool, str]:
    """能不能转换。脚本不在不算"不能"——首次转换会自动去取。"""
    missing = missing_deps()
    if missing:
        return False, (
            f"缺少 {', '.join(missing)}：转换要读 transformers 权重。"
            "在「依赖」页装 convert 这一组，或 pip install .[convert]")
    return True, ""


def snapshot(model: str = "") -> dict[str, Any]:
    ok, reason = capability()
    source: dict[str, Any] = {}
    if model:
        try:
            src = source_dir(model)
            source = {
                "dir": str(src),
                "bytes": _safetensors_bytes(src),
                "ready": True,
            }
        except ConfigError as exc:
            # 不算错误：还没下原始权重时页面只是把提示和下载入口摆出来
            source = {"dir": "", "bytes": 0, "ready": False, "hint": str(exc)}
    return {
        "available": ok,
        "reason": reason,
        "script": str(SCRIPT),
        "script_ready": SCRIPT.is_file(),
        "tag": installed_tag(),
        # 只放两个预设档：Q4_K（纯 4bit）和它们同一档位，作为独立选项只会
        # 让"我这模型到底有几档"更混乱，而清单里的映射键也只有 q4/q6/q8。
        "targets": [
            {"key": key, "label": gguf_quant.TARGETS[key]["label"],
             "note": gguf_quant.TARGETS[key]["note"]}
            for key in ("q4_k_m", "q6_k")
        ],
        "source": source,
        "task": CONVERTER.state(),
    }


# ------------------------------------------------------------------ 输入
def source_dir(key: str) -> Path:
    """原始 safetensors 目录。

    两种来源：`path` 本身就是本地目录；或者它在 HF 上，用户已经下到
    `models/nl/<key>/`（下载清单选「原始权重」时就是整仓库落这里）。
    """
    from .config import DEFAULT_TIERS, load_tier_table
    from .web import downloads as dl

    spec = (load_tier_table(DEFAULT_TIERS).get("models") or {}).get(key)
    if not isinstance(spec, dict):
        raise ConfigError(f"清单里没有模型 {key}")
    declared = str(spec.get("path") or "").strip()

    candidates: list[Path] = []
    if declared:
        given = Path(declared)
        # `path` 也可能是 HF 仓库名（Qwen/Qwen3-VL-8B-Instruct）。按路径拼出来
        # 会得到 H:\tagger\Qwen\Qwen3-VL-8B-Instruct 这种假位置，报错里列着它
        # 只会让人以为"路径配错了"。
        looks_local = (given.is_absolute() or given.exists()
                       or declared.startswith((".", "models/", "models\\")))
        if looks_local:
            candidates.append(given if given.is_absolute() else ROOT / given)
    candidates.append(dl.resolve_dir("vlm", key, None))

    for path in candidates:
        if (path / "config.json").is_file() and any(path.glob("*.safetensors")):
            return path
    raise ConfigError(
        f"{key} 的原始权重不在本地。\n"
        f"到「模型管理」页把这条的下载档位选成「原始权重（用于本地量化）」下完再来；"
        f"我找过：{', '.join(str(c) for c in candidates)}")


def _safetensors_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.glob("*.safetensors"))


def default_outputs(src: Path, key: str, target: str) -> tuple[Path, Path, Path]:
    """(F16 中间文件, mmproj, 最终文件)。都放在该模型的目录下。"""
    dest = src if src.is_dir() else src.parent
    f16 = dest / f"{key}-F16.gguf"
    # 名字必须跟脚本的规则一致：`--mmproj` 只加 `mmproj-` 前缀，不加后缀
    mmproj = dest / f"mmproj-{key}-F16.gguf"
    final = dest / f"{key}-{target.upper()}.gguf"
    return f16, mmproj, final


def is_vision(src: Path) -> bool:
    """这条权重带不带视觉塔。

    带才传 `--mmproj`：纯文本模型传了会被脚本拒掉，而"到底带不带"只有
    config.json 说得准（Qwen3VL 是 Qwen3VLForConditionalGeneration）。
    """
    try:
        config = json.loads((src / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if isinstance(config, dict) and config.get("vision_config"):
        return True
    archs = config.get("architectures") if isinstance(config, dict) else None
    return any("ConditionalGeneration" in str(a) for a in (archs or []))


# ------------------------------------------------------------------ 任务
@dataclass
class ConvertTask:
    model_key: str
    target: str                       # q4_k_m / q6_k
    src: Path
    f16: Path
    mmproj: Path
    out: Path
    status: str = "running"           # running / done / error / canceled
    stage: str = "准备"
    percent: float = 0.0
    message: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancel: bool = False
    log: deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_LINES))

    def state(self) -> dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "model": self.model_key,
            "target": self.target,
            "source": str(self.src),
            "out_file": str(self.out),
            "status": self.status,
            "stage": self.stage,
            "percent": round(self.percent, 1),
            "message": self.message,
            "elapsed": round(elapsed, 1),
            "finished_at": self.finished_at,
            "log_tail": list(self.log)[-12:],
        }


class Converter:
    """一次只跑一个：转换吃满 CPU、还占几 G 临时盘，并行只会互相拖死。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: ConvertTask | None = None
        self._proc: subprocess.Popen | None = None

    def state(self) -> dict[str, Any] | None:
        with self._lock:
            task = self._task
        return task.state() if task is not None else None

    def activity(self) -> dict[str, Any] | None:
        """给底栏进度条 / 完成通知用的最小快照（没跑过就 None）。

        转换要十几分钟，用户一定会切走；不接进 activities 的话，底栏在整个
        过程中都是空的，看不出机器到底还在不在干活。
        """
        with self._lock:
            task = self._task
        if task is None:
            return None
        return {
            "id": f"convert:{task.model_key}:{task.target}",
            "kind": "convert",
            "title": "本地量化",
            "detail": f"{task.model_key} → {task.target.upper()}",
            "status": task.status,
            "percent": round(task.percent, 1),
            "stage": task.stage,
            "message": task.message,
            "elapsed_s": round((task.finished_at or time.time()) - task.started_at, 1),
            "finished_at": task.finished_at,
            "log_tail": list(task.log)[-6:],
        }

    def start(self, key: str, target: str = "q6_k") -> dict[str, Any]:
        flag = gguf_quant.resolve_flag(target)
        ok, reason = capability()
        if not ok:
            raise RuntimeError(reason)
        src = source_dir(key)
        f16, mmproj, out = default_outputs(src, key, target)
        if out == f16:
            raise ConfigError("输出和中间文件重名，换个档位再试")

        with self._lock:
            if self._task is not None and self._task.status == "running":
                return self._task.state()
            task = ConvertTask(model_key=key, target=target, src=src,
                               f16=f16, mmproj=mmproj, out=out)
            self._task = task
        threading.Thread(target=self._run, args=(task, flag), daemon=True,
                         name=f"convert-{key}").start()
        return task.state()

    def cancel(self) -> dict[str, Any] | None:
        with self._lock:
            task, proc = self._task, self._proc
        if task is None:
            return None
        if task.status == "running":
            task.cancel = True
            task.status = "canceled"
            task.stage = "已取消"
            task.message = "已取消；中间文件已删除"
            task.finished_at = time.time()
            # 转换是子进程，必须真杀掉，否则它会把剩下的张量算完
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        return task.state()

    # -------------------------------------------------------------- 内部
    def _run(self, task: ConvertTask, flag: str) -> None:
        try:
            ensure_tools(task.log.append)
            if task.cancel:
                return
            self._convert(task)
            if task.cancel:
                return
            self._quantize(task, flag)
            if task.cancel:
                return
            self._register(task)
            task.status = "done"
            task.percent = 100.0
            task.stage = "完成"
            size = task.out.stat().st_size / (1024 ** 3)
            task.message = (
                f"已生成 {task.out.name}（{size:.2f} GB）。"
                f"在新建任务页把权重精度选成 {flag} 就会用它。")
            task.log.append(task.message)
        except gguf_quant.CancelledError:
            pass
        except Exception as exc:                        # noqa: BLE001
            task.status = "error"
            task.stage = "失败"
            task.message = f"{type(exc).__name__}: {exc}"
            task.log.append(task.message)
        finally:
            task.finished_at = time.time()
            # F16 中间文件必须清：它是全流程里最大的一份，留着就是白占 17G
            if task.f16.is_file() and task.status in ("done", "canceled", "error"):
                try:
                    task.f16.unlink()
                    task.log.append(f"已删除中间文件 {task.f16.name}")
                except OSError:
                    pass
            with self._lock:
                self._proc = None

    def _convert(self, task: ConvertTask) -> None:
        """跑官方脚本。视觉模型要跑两遍：文本塔一遍、视觉塔（mmproj）一遍。

        脚本的 `--mmproj` 是开关不是路径（只给输出名加 `mmproj-` 前缀），
        一次只转一种，所以分两趟。
        """
        expected = max(1, _safetensors_bytes(task.src))
        task.stage = "转换（safetensors → GGUF）"
        self._run_script(task, [], task.f16, expected, 5.0, 60.0)
        if not task.f16.is_file():
            raise ConfigError("转换进程正常退出，但没有生成 GGUF 文件")
        if is_vision(task.src):
            task.stage = "导出视觉投影（mmproj）"
            self._run_script(task, ["--mmproj"], task.f16, 1, 60.0, 70.0)
            if task.mmproj.is_file():
                task.log.append(f"视觉投影已导出：{task.mmproj.name}")
            else:
                # 不致命：llama-server 不带 mmproj 也能跑文本，只是看不了图
                task.log.append("没有生成 mmproj——起服务后图片会无法识别")
        else:
            task.log.append("纯文本模型，不需要 mmproj")

    def _run_script(self, task: ConvertTask, extra: list[str], out: Path,
                    expected: int, lo: float, hi: float) -> None:
        cmd = [
            sys.executable, str(SCRIPT), str(task.src),
            "--outfile", str(out),
            # F16 而不是 BF16：量化内核按 F16 读，BF16 会被当成未知类型
            "--outtype", "f16",
            *extra,
        ]
        task.log.append("$ " + " ".join(cmd))
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace",
                                creationflags=creation)
        with self._lock:
            self._proc = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.rstrip()
            if text:
                task.log.append(text)
            if task.cancel:
                proc.terminate()
                proc.wait()
                raise gguf_quant.CancelledError()
            target = task.mmproj if extra else task.f16
            if target.is_file():
                # 用输出文件大小对源文件大小估进度：转换是逐张量写的，
                # 这个比值基本单调，比"假装转了 50%"诚实。
                ratio = min(1.0, target.stat().st_size / expected)
                task.percent = round(lo + ratio * (hi - lo), 1)
        code = proc.wait()
        if code != 0:
            raise ConfigError(
                f"转换失败（退出码 {code}）。日志末尾：\n"
                + "\n".join(list(task.log)[-8:]))

    def _quantize(self, task: ConvertTask, flag: str) -> None:
        task.stage = f"量化（F16 → {flag}）"
        stats = gguf_quant.quantize_file(
            task.f16, task.out, flag,
            log=task.log.append,
            progress=lambda pct, stage: self._set(task, 70 + pct * 0.28, stage),
            should_stop=lambda: task.cancel,
        )
        if task.cancel:
            raise gguf_quant.CancelledError()
        task.percent = 98.0
        skipped = stats.get("skipped", 0)
        if skipped:
            task.log.append(f"{skipped} 个小张量保持原精度")

    def _set(self, task: ConvertTask, pct: float, stage: str) -> None:
        task.percent = round(min(99.0, pct), 1)
        if stage:
            task.stage = stage

    def _register(self, task: ConvertTask) -> None:
        """把产物登记进覆盖文件：之后页面/CLI 都按新档位跑。"""
        from .config import DEFAULT_TIERS, load_tier_table, overlay_path

        task.stage = "登记"
        spec = (load_tier_table(DEFAULT_TIERS).get("models") or {}).get(task.model_key) or {}
        doc: dict[str, Any] = {}
        p = overlay_path()
        if p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    doc = raw
            except (OSError, ValueError):
                doc = {}

        def rel(path: Path) -> str:
            # 相对仓库根：同一份 models.json 在 Windows 与 WSL 下都能解析
            try:
                return str(path.relative_to(ROOT)).replace("\\", "/")
            except ValueError:
                return str(path)

        entry: dict[str, Any] = dict(doc.get("vlm", {}).get(task.model_key) or {})
        entry["path"] = str(spec.get("path") or entry.get("path") or "")
        if spec.get("revision") is not None:
            entry["revision"] = spec.get("revision")
        entry["family"] = str(spec.get("family") or "qwen3vl")
        entry["precision"] = str(spec.get("precision") or "int4")

        quants = dict(entry.get("gguf_quants") or {})
        for field_name in ("gguf", "mmproj", "download_repo"):
            value = str(spec.get(field_name) or "").strip()
            if value:
                entry[field_name] = value
        quants.update({str(k): str(v) for k, v in (spec.get("gguf_quants") or {}).items()})

        tier_key = {"q4_k_m": "q4", "q6_k": "q6"}.get(task.target, task.target)
        quants[tier_key] = rel(task.out)
        entry["gguf_quants"] = quants
        # 默认那档如果还是空的（或指向一个不存在的文件），就把新的顶上去，
        # 否则用户压完了、清单里却没有直接可跑的那一份。
        current = str(entry.get("gguf") or "").strip()
        if not current or not (ROOT / current).is_file():
            entry["gguf"] = rel(task.out)
        if task.mmproj.is_file():
            entry["mmproj"] = rel(task.mmproj)
        if spec.get("download_files"):
            entry["download_files"] = list(spec["download_files"])

        vlm = dict(doc.get("vlm") or {})
        vlm[task.model_key] = entry
        doc["vlm"] = vlm
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        import os

        os.replace(tmp, p)
        task.log.append(f"已登记 {task.model_key} → {tier_key} = {rel(task.out)}")


CONVERTER = Converter()

__all__ = ["CONVERTER", "Converter", "capability", "default_outputs",
           "ensure_tools", "installed_tag", "snapshot", "source_dir"]
