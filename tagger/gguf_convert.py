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
permute，症状只有"打标结果怪怪的"。所以这里只做四件事：找脚本、装依赖、
跑、把产物登记回清单。

脚本从哪来
----------
`vendor/llama-convert/` 里内置了同一份（约 2 MB 纯 Python，随发布包走），
所以正常情况下**不需要联网**。只有内置那份被删/残缺时才回落到
`runtime/llama.cpp/convert/`：老版本下载的、或用户手工解压的，再不行才去
GitHub 取（37 MB 源码包，国内经常就是卡在这一步）。

为什么不缓存中间产物
--------------------
F16 的 8B 权重约 17G，而用户要的是 Q4/Q6（6G 上下）。中间那份只在同一次任务
里有意义，任务结束就删——留着它只会让 `models/` 目录涨得莫名其妙，还会被
"清理磁盘"的人当成垃圾删掉。
"""

from __future__ import annotations

import json
import os
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
# ★ 内置的那一份：随程序一起发（vendor\llama-convert，约 2 MB 纯 Python）。
#   有这个就不该再为 37 MB 源码包等一次下载——那一步正是国内最卡的地方。
BUNDLED_DIR = ROOT / "vendor" / "llama-convert"
# 下载 / 手工解压的落点：跟 llama.cpp 运行时放一起，同一个版本、同一条升级路径
TOOLS_DIR = llama_runtime.INSTALL_ROOT / "convert"
# 内嵌解释器是 isolated 模式（env\python\python312._pth）：PYTHONPATH 无效、
# 脚本目录也不进 sys.path，所以官方脚本只能由这个 wrapper 补 path 再跑。
RUNNER = Path(__file__).resolve().with_name("convert_runner.py")

SOURCE_TARBALL = "https://github.com/ggml-org/llama.cpp/archive/refs/tags/{tag}.tar.gz"
RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
_TIMEOUT = httpx.Timeout(connect=20.0, read=120.0, write=120.0, pool=20.0)

# 源码包（37 MB）的几条路。国内直连 GitHub 实测 30 KB/s 级别——下二十分钟，
# 界面上就是"卡在 8/37 MB 不动"。按顺序试，别随机：出问题时日志里要能看出
# 走的是哪条。反代只是把 github.com 前缀换个域名，路径不变。
_SOURCE_ROUTES = (
    "https://github.com/ggml-org/llama.cpp/archive/refs/tags/{tag}.tar.gz",
    "https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/archive/refs/tags/{tag}.tar.gz",
    "https://ghfast.top/https://github.com/ggml-org/llama.cpp/archive/refs/tags/{tag}.tar.gz",
)
# 慢线路尽早放弃：15 秒不到 3 MB 就换下一条（换一条 30 秒就下完了）
_SLOW_SECONDS = 15.0
_SLOW_BYTES = 3 << 20

# 转换本身只要 torch + transformers；量化那步是 numpy（见 gguf_quant）。
# ★ sentencepiece 也算硬门槛：Qwen / Llama 这类权重带 `tokenizer.model` 时，
#   官方脚本走 `conversion/base.py::_set_vocab_sentencepiece`，缺了会**在读完
#   config 与 tokenizer 之后**才抛 ModuleNotFoundError（base.py:2548）——用户
#   已经等了几分钟，日志还写得很像"少装了个 pip 包"。宁可点按钮前就拦住。
_REQUIRED = ("torch", "transformers", "sentencepiece")
# 官方脚本要到某一步才 import 的第三方包：真漏了就在这里点名 + 给安装命令，
# 别只丢一句"退出码 1"。只列我们点得出名字、给得出装法的。
_DEP_HINT = ("sentencepiece", "mistral_common", "torch", "transformers")
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
    for candidate in (tools_dir() / "VERSION", llama_runtime.VERSION_FILE):
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


def _complete(base: Path) -> bool:
    """这个目录里的脚本 + 它 import 的两个包是不是都在。

    只判 `convert_hf_to_gguf.py` 会漏掉"上次解压到一半"：新版脚本第 18 行是
    `from conversion import (...)`，那个包在同一份源码里是另外两个目录。脚本在、
    包不在，一跑就是 `ModuleNotFoundError: No module named 'conversion'`——从报错
    看像是少装了一个 pip 包，实际是解压残缺（断网 / 盘满 / 进程被杀都会留下）。
    """
    return ((base / "convert_hf_to_gguf.py").is_file()
            and (base / "conversion" / "__init__.py").is_file()
            and (base / "gguf-py" / "gguf" / "__init__.py").is_file())


def tools_dir() -> Path:
    """转换脚本用哪一份：**内置的优先**。

    内置那份随程序发（`vendor\\llama-convert`），所以绝大多数机器第一次转换
    就直接开工，不用为 37 MB 源码包等下载。只有它被删掉 / 残缺时，才回落到
    `runtime\\llama.cpp\\convert`（老版本下载的，或用户手工解压的）。
    """
    return BUNDLED_DIR if _complete(BUNDLED_DIR) else TOOLS_DIR


def script_path() -> Path:
    return tools_dir() / "convert_hf_to_gguf.py"


def tools_ready() -> bool:
    return _complete(tools_dir())


def _settings_proxy() -> str:
    """页面设置里的下载代理。

    取源码包这一步**不能**用"下载源（镜像站）"——那是 HF 的前缀，跟 GitHub
    无关；而 GitHub 直连不通时，用户配的那个代理是唯一的出路。
    """
    try:
        from .config import load_model_overlay

        return str(load_model_overlay().get("proxy") or "")
    except Exception:                       # noqa: BLE001 - 读不到就当没代理
        return ""


def _stream_source(url: str, archive: Path, say: Callable[[str], None],
                   proxy: str) -> None:
    """把源码包写到 archive。太慢就抛，让调用方换下一条路。"""
    kwargs: dict[str, Any] = {
        "follow_redirects": True,
        "timeout": httpx.Timeout(20.0, read=30.0),
    }
    if proxy:
        kwargs["proxy"] = proxy
    started = time.time()
    total = done = 0
    step = 0
    with httpx.Client(**kwargs) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            with archive.open("wb") as handle:
                for chunk in response.iter_bytes(1 << 20):
                    handle.write(chunk)
                    done += len(chunk)
                    # 每 20% 报一次：源码包才三十几 MB，逐块刷屏没意义
                    if total and done * 5 // total > step:
                        step = done * 5 // total
                        say(f"下载源码 {done / 1048576:.0f}/{total / 1048576:.0f} MB")
                    spent = time.time() - started
                    if spent > _SLOW_SECONDS and done < _SLOW_BYTES:
                        raise TimeoutError(
                            f"太慢：{spent:.0f}s 只下了 {done / 1048576:.1f} MB")
    if total and done < total:
        raise OSError(f"只下到 {done}/{total} 字节")


def _download_source(archive: Path, tag: str, say: Callable[[str], None],
                     proxy: str) -> None:
    """按 `_SOURCE_ROUTES` 依次试，第一条能下的就收工。"""
    last: Exception | None = None
    for route in _SOURCE_ROUTES:
        url = route.format(tag=tag)
        host = url.split("/")[2]
        say(f"下载源码（{host}）{'，走代理' if proxy else ''}")
        try:
            _stream_source(url, archive, say, proxy)
            return
        except Exception as exc:            # noqa: BLE001 - 换下一条路
            last = exc
            say(f"{host} 不行：{type(exc).__name__}: {exc}")
            archive.unlink(missing_ok=True)
    raise ConfigError(
        f"取转换脚本失败：GitHub 和几个公共反代都不可用（{last}）。\n"
        f"① 在「模型管理 → 下载网络」填个代理再点一次；\n"
        f"② 或手动下载 llama.cpp 源码包，把 convert_hf_to_gguf.py、conversion/、"
        f"gguf-py/ 解到 {TOOLS_DIR}")


def ensure_tools(log: Callable[[str], None] | None = None, *,
                 force: bool = False) -> Path:
    """确保转换脚本在位，缺就按当前版本取一份。返回脚本路径。

    内置那份（`vendor\\llama-convert`）齐了就直接用，`force=True` 也一样——
    它随程序发，重下一份既慢又不解决问题（真跑挂了是别的层的事）。
    `force=True` 只对"下载目录里那套"有意义：跑起来才发现包缺失时重取一遍。
    """
    with _TOOLS_LOCK:
        if _complete(BUNDLED_DIR):
            return BUNDLED_DIR / "convert_hf_to_gguf.py"
        if not force and tools_ready():
            return script_path()
        script = TOOLS_DIR / "convert_hf_to_gguf.py"
        say = log or (lambda _msg: None)
        tag = installed_tag()
        say(f"取转换脚本（llama.cpp {tag}）")
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        # 旧目录先清掉：残留的包会和这次解出来的混在一起，症状是"明明换过
        # 版本，行为还是老的"。只清这两个，llama-*.exe 那些不归这里管。
        for name in ("conversion", "gguf-py"):
            shutil.rmtree(TOOLS_DIR / name, ignore_errors=True)
        script.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "src.tar.gz"
            _download_source(archive, tag, say, _settings_proxy())
            with tarfile.open(archive, "r:gz") as tar:
                _extract_members(tar, TOOLS_DIR)
        if not script.is_file():
            raise ConfigError(
                "转换脚本解压失败：包结构可能变了（找不到 convert_hf_to_gguf.py）")
        if not _complete(TOOLS_DIR):
            raise ConfigError(
                f"转换脚本解压不完整（{TOOLS_DIR}）：脚本有了，conversion / "
                f"gguf-py 没齐。删掉这个目录再点一次就行。")
        try:
            (TOOLS_DIR / "VERSION").write_text(tag + "\n", encoding="utf-8")
        except OSError:
            pass
        say(f"转换脚本就绪（{script}）")
        return script


def missing_deps() -> list[str]:
    import importlib

    out: list[str] = []
    for name in _REQUIRED:
        try:
            importlib.import_module(name)
        except Exception:                               # noqa: BLE001
            out.append(name)
    return out


def _missing_module(tail: list[str]) -> str:
    """日志末尾里"少装了哪个包"。只认 `_DEP_HINT` 里点得出名字的那几个。"""
    text = "\n".join(tail)
    for mod in _DEP_HINT:
        if f"No module named '{mod}'" in text:
            return mod
    return ""


def capability() -> tuple[bool, str]:
    """能不能转换。脚本不在不算"不能"——内置一份，缺了才去取。"""
    missing = missing_deps()
    if missing:
        return False, (
            f"缺少 {', '.join(missing)}：转换要读 transformers 权重，还要解 "
            "tokenizer（部分架构走 sentencepiece）。"
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
        "script": str(script_path()),
        # 不是"脚本这个文件在不在"，而是"这套脚本能不能真跑"：半套解压
        # （只有脚本、没有 conversion 包）也算没就位。
        "script_ready": tools_ready(),
        # 用的是随包发的那份（不需要联网）还是下载的那份
        "bundled": _complete(BUNDLED_DIR),
        "tag": installed_tag(),
        # 三个档：F16 是"只转不压"（完整权重，就是 tiers.yaml 的默认档，
        # 8B 约 15.6 G）；Q4/Q6 是压缩档。少放一档只会让"我这模型到底有几档"
        # 更混乱，而清单里的映射键也只有 f16/q4/q6/q8。
        "targets": [
            {"key": "f16", "label": "F16（完整权重）",
             "note": "不压缩，约等于原始精度；8B 约 15.6 G，显存紧张就选 Q4_K_M"},
            *[
                {"key": key, "label": gguf_quant.TARGETS[key]["label"],
                 "note": gguf_quant.TARGETS[key]["note"]}
                for key in ("q4_k_m", "q6_k")
            ],
        ],
        "source": source,
        # 已经压好的档（含"配置里有、文件没了"的情况）：页面据此说明
        # "本地已经有 Q4/Q6，直接用就行"，而不是只说缺原始权重。
        "quants": _existing_tiers(model) if model else {},
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
        f"{key} 的原始权重不在本地（本地量化要吃 transformers 的 safetensors）。\n"
        + "\n".join(_describe_dir(p) for p in candidates))


def _describe_dir(path: Path) -> str:
    """候选目录里到底有什么。

    ★ 必须说清"只有 GGUF"这一种：目录里摆着几份压好的 .gguf 时，一句
      "原始权重不在本地"会让人以为程序没看见文件（实测反馈过）。
    """
    if not path.is_dir():
        return f"{path}：不存在——在「模型管理」点这一行的「下载原始权重」"
    ggufs = sorted(p.name for p in path.glob("*.gguf"))
    safes = sorted(p.name for p in path.glob("*.safetensors"))
    has_config = (path / "config.json").is_file()
    if ggufs and not safes:
        head = "、".join(ggufs[:3]) + ("…" if len(ggufs) > 3 else "")
        return (f"{path}：只有 {len(ggufs)} 份 GGUF（{head}），没有 safetensors。\n"
                f"  这些是已经压好的量化档，直接用就行"
                f"（「模型管理」把这条的精度切成对应档）；只有想重压一份才需要原始权重。")
    if safes and not has_config:
        return f"{path}：有 {len(safes)} 份 safetensors，但缺 config.json（整仓库下完才算齐）"
    if safes:
        return f"{path}：有 safetensors，但没有能用的权重分片"
    return f"{path}：目录是空的——点这一行的「下载原始权重」"


def _existing_tiers(key: str) -> dict[str, dict[str, Any]]:
    """清单里登记过的 GGUF 档，以及产物是否真的在盘上。

    页面靠它回答"我明明看见有权重"：那些是压好的 GGUF，不是原始权重。
    `bytes == 0` 表示配置里指着一个已经不存在的文件（改名/删掉了）。
    """
    from .config import DEFAULT_TIERS, load_tier_table

    spec = (load_tier_table(DEFAULT_TIERS).get("models") or {}).get(key)
    if not isinstance(spec, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, raw in (spec.get("gguf_quants") or {}).items():
        path = Path(str(raw))
        path = path if path.is_absolute() else ROOT / path
        out[str(name).lower()] = {
            "file": str(raw),
            "bytes": path.stat().st_size if path.is_file() else 0,
        }
    return out


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
        # f16 = 只转不压：产物就是那份中间文件本身，所以它不能算"重名"，
        # 也不能走 resolve_flag（量化档位表里没有 f16 这一项）。
        keep_f16 = target == "f16"
        flag = "" if keep_f16 else gguf_quant.resolve_flag(target)
        ok, reason = capability()
        if not ok:
            raise RuntimeError(reason)
        src = source_dir(key)
        f16, mmproj, out = default_outputs(src, key, target)
        if out == f16 and not keep_f16:
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
            if not tools_ready():
                # 首次转换要去 GitHub 取源码包（37 MB，国内直连很慢）。
                # 不写这一句，界面就停在"0% · 1 分钟"，像个死程序。
                task.stage = "获取转换脚本（首次要下 ~37 MB）"
            ensure_tools(task.log.append)
            if task.cancel:
                return
            task.stage = "准备"
            self._convert(task)
            if task.cancel:
                return
            if flag:
                self._quantize(task, flag)
            else:
                task.stage = "跳过压缩（F16 完整权重）"
                task.log.append("按 F16 直出，不做量化")
            if task.cancel:
                return
            self._register(task)
            task.status = "done"
            task.percent = 100.0
            task.stage = "完成"
            size = task.out.stat().st_size / (1024 ** 3)
            task.message = (
                f"已生成 {task.out.name}（{size:.2f} GB）。"
                f"在新建任务页把权重精度选成 {flag or 'F16'} 就会用它。")
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
            failed = task.status in ("canceled", "error")
            if failed:
                # 半截产物比"没有产物"更坏：页面/清单都以为这一档好了，直到起
                # 服务或跑 NL 才发现读不动。删的时候按 mtime 认"这次真写过"，
                # 免得把上一轮成功的 mmproj 一起带走。
                for path in {task.out, task.f16, task.mmproj}:
                    try:
                        if path.is_file() and path.stat().st_mtime >= task.started_at - 1:
                            path.unlink()
                            task.log.append(f"已删除不完整的产物 {path.name}")
                    except OSError:
                        pass
            # F16 中间文件必须清：它是全流程里最大的一份，留着就是白占 17G。
            # 除非**选的就是 F16** —— 那它不是中间产物，是成品。
            if (task.target != "f16" and task.f16.is_file()
                    and task.status in ("done", "canceled", "error")):
                try:
                    task.f16.unlink()
                    task.log.append(f"已删除中间文件 {task.f16.name}")
                except OSError:
                    pass
            with self._lock:
                self._proc = None

    def _convert(self, task: ConvertTask) -> None:
        """跑官方脚本。视觉模型要跑两遍：文本塔一遍、视觉塔（mmproj）一遍。

        ★ 第二趟的 `--outfile` 必须是 **mmproj 自己的路径**，不能沿用 F16 那个。
          脚本里 `mmproj-` 前缀只在 "--outfile 是个目录" 时才自动加
          （conversion/base.py::MmprojModel.prepare_metadata 的 is_dir 分支）；
          传文件路径时它只做一次 `fill_templated_filename`，文件名原样返回 ——
          也就是**直接覆盖第一趟的 F16**。实测踩过：mmproj pass 覆盖掉 15.6G 的
          F16，接着量化把那份 1.1G 的 mmproj 当成文本模型压，产出 0.47G 的
          残废 Q6，而 mmproj-*.gguf 从来不存在（起服务时才报"视觉投影文件不存在"）。
        """
        expected = max(1, _safetensors_bytes(task.src))
        task.stage = "转换（safetensors → GGUF）"
        self._run_script(task, [], task.f16, expected, 5.0, 60.0)
        if not task.f16.is_file():
            raise ConfigError("转换进程正常退出，但没有生成 GGUF 文件")
        if is_vision(task.src):
            task.stage = "导出视觉投影（mmproj）"
            self._run_script(task, ["--mmproj"], task.mmproj, 1, 60.0, 70.0)
            if task.mmproj.is_file():
                task.log.append(f"视觉投影已导出：{task.mmproj.name}")
            else:
                # 视觉模型没有 mmproj 等于白转：llama-server 不带它也能起来，
                # 但每张图都会当成纯文本请求，模型开始"看图说话"地编内容。
                # 与其留一个起服务才炸的坑，不如在这里就把话说清。
                raise ConfigError(
                    f"视觉模型没有导出 mmproj（应该是 {task.mmproj.name}）。\n"
                    f"这个模型的架构 {task.src.name} 用的是带视觉塔的权重，"
                    f"少了它 NL 打标看不了图——换个版本重新转，或把日志报给维护者。")
        else:
            task.log.append("纯文本模型，不需要 mmproj")

    def _run_script(self, task: ConvertTask, extra: list[str], out: Path,
                    expected: int, lo: float, hi: float,
                    retried: bool = False) -> None:
        cmd = [
            # -u：脚本的输出是块缓冲的，不加这个，进度行会攒到 8 KB 才吐出来，
            # 界面就成了"几分钟一条日志都没有"。
            # 脚本由 convert_runner 代跑：内嵌 Python 是 isolated 模式，PYTHONPATH
            # 无效、脚本目录也不进 sys.path，path 只能在子进程里补（见该文件头）。
            sys.executable, "-u", str(RUNNER), str(tools_dir()), str(task.src),
            "--outfile", str(out),
            # F16 而不是 BF16：量化内核按 F16 读，BF16 会被当成未知类型
            "--outtype", "f16",
            *extra,
        ]
        task.log.append("$ " + " ".join(cmd))
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        # ★ 不要在这里拼 PYTHONPATH 了：以前就是这么写的，对系统 Python 有效、
        #   对内嵌 Python 完全无效（sys.flags.ignore_environment=1 把环境变量
        #   整个跳过），于是"脚本下载好了，一跑还是 No module named 'conversion'"。
        env = dict(os.environ)
        # -u 之外的保险：有些库自己重设 stdout 缓冲
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=env,
                                creationflags=creation)
        with self._lock:
            self._proc = proc
        assert proc.stdout is not None
        started = time.time()

        def pump() -> None:
            """把子进程输出搬进日志。单独一个线程，主循环才能每 0.2s 看一次
            取消和文件大小——原来 `for line in proc.stdout` 是阻塞的，
            加载权重的几分钟里既不能取消、也不更新进度。"""
            for raw in proc.stdout:              # type: ignore[union-attr]
                text = raw.rstrip()
                if text:
                    task.log.append(text)

        reader = threading.Thread(target=pump, daemon=True, name="convert-log")
        reader.start()

        target = task.mmproj if extra else task.f16
        while True:
            if task.cancel:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise gguf_quant.CancelledError()
            if target.is_file():
                # 用输出文件大小对源文件大小估进度：转换是逐张量写的，
                # 这个比值基本单调，比"假装转了 50%"诚实。
                ratio = min(1.0, target.stat().st_size / expected)
                task.percent = round(lo + ratio * (hi - lo), 1)
            elif not extra and time.time() - started > 30:
                # 前面这几分钟全花在 import torch + 读 16 G safetensors 上，
                # 输出文件还没创建。说清楚，别让人以为死机了。
                task.stage = "转换（加载原始权重，前几分钟没有进度是正常的）"
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        reader.join(timeout=5)
        code = proc.wait()
        if code != 0:
            tail = list(task.log)[-8:]
            # 自愈一次：解压残缺时重取一份脚本再跑，别让人对着一句
            # ModuleNotFoundError 去猜是哪一层缺东西。只重试一次——
            # 真的不兼容时第二次就会把日志原样抛出来。
            # 只认**我们自己那两个包**：缺 sentencepiece / mistral_common 这类
            # 第三方模块时重下脚本毫无意义，只会白多等一次下载。
            own_missing = any(f"No module named '{mod}'" in line
                              for line in tail for mod in ("conversion", "gguf"))
            # 内置那份是随程序发的，重下没有意义（只会白等一次 37 MB）；
            # 只有"下载目录里那套"残缺时才值得重取一次。
            if not retried and own_missing and not _complete(BUNDLED_DIR):
                task.log.append("脚本自检失败（缺模块），重取一份转换脚本后重试")
                ensure_tools(task.log.append, force=True)
                self._run_script(task, extra, out, expected, lo, hi, retried=True)
                return
            if own_missing:
                raise ConfigError(
                    f"转换脚本缺文件（{tools_dir()}）：conversion / gguf-py 没进去。"
                    f"重新解压一份发布包，或把这个目录整个删掉再点一次：\n"
                    + "\n".join(tail))
            # 缺第三方包是"装一下就好"的错，但发现得太晚——脚本是跑到 tokenizer
            # /量化那一步才 import 的。点名 + 给命令，别让人对着"退出码 1"猜。
            dep = _missing_module(tail)
            if dep:
                raise ConfigError(
                    f"转换要用的 {dep} 没装：在「依赖」页装 convert 这一组，或\n"
                    f"    {sys.executable} -m pip install {dep}\n"
                    f"装完重跑一次（中间产物不会复用，F16 得重新转）。\n"
                    f"日志末尾：\n" + "\n".join(tail))
            raise ConfigError(
                f"转换失败（退出码 {code}）。日志末尾：\n" + "\n".join(tail))

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
        # 产物大小要说得过去：Q4 ≈ F16 的 30%、Q6 ≈ 40%、Q8 ≈ 53%。远低于这个
        # 区间只可能是「压到一半就断了」或者「拿错了输入」——实测踩过：mmproj
        # 覆盖了 F16 之后，量化把 1.1G 的 mmproj 当文本模型压，产出 0.47G 的
        # 残废 Q6，而页面照样标"已压好"，直到起服务才炸。
        if task.f16.is_file() and task.out.is_file():
            ratio = task.out.stat().st_size / max(1, task.f16.stat().st_size)
            if ratio < 0.15:
                raise ConfigError(
                    f"量化产物只有 {task.out.stat().st_size / (1024 ** 3):.2f} GB，"
                    f"不到源文件（F16 {task.f16.stat().st_size / (1024 ** 3):.2f} GB）"
                    f"的 {ratio:.0%}——这次压缩是坏的，产物已删。"
                    f"重跑一次；若反复出现，先看磁盘剩余空间。")
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
        for field_name in ("gguf", "mmproj"):
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

        vlm = dict(doc.get("vlm") or {})
        vlm[task.model_key] = entry
        doc["vlm"] = vlm
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, p)
        task.log.append(f"已登记 {task.model_key} → {tier_key} = {rel(task.out)}")


CONVERTER = Converter()

__all__ = ["CONVERTER", "Converter", "capability", "default_outputs",
           "ensure_tools", "installed_tag", "snapshot", "source_dir"]
