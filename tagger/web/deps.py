"""依赖检测 + 单包装卸。

页面上要回答两个问题：
  1. 这份环境里哪些包装了、哪些没装、哪些版本不对；
  2. 能不能只针对某一行点一下装/卸，而不是整份 requirements 重装一遍。

清单从 `requirements-*.txt` 现读，不在这里再抄一份——两份清单迟早会
对不上，而"页面说装了、实际没装"是最难排查的那种错觉。

安装走 `pip` 子进程（`sys.executable -m pip`），不是 `pip install` 命令：
嵌入式环境里 PATH 上的 pip 很可能指向别的 Python，装完这个环境里什么都没有。
"""

from __future__ import annotations

import importlib.metadata as md
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 仓库根：tagger/web/deps.py → 上两级
ROOT = Path(__file__).resolve().parent.parent.parent

REQ_CORE = ROOT / "requirements-embedded.txt"
REQ_TAG = ROOT / "requirements-tag.txt"
PYPROJECT = ROOT / "pyproject.toml"

# 附加依赖的说明（key = pyproject 里 extra 的名字）。
# 这些包故意**不进** requirements-*.txt：像 llmcompressor 这种几百 MB、又对
# torch 版本卡得很死的，装了只会拖累每个只想打标的人。所以它们只在真要用
# 那个功能时（页面上的「量化」/「NL 服务」）按需装。
EXTRA_NOTES = {
    "quantize": "4bit 量化（llmcompressor 等），只在页面点「开始量化」时用得上。",
    "convert": "本地量化成 GGUF（transformers 等），只在「本地量化」时用得上。",
    "serve": "vLLM 推理服务，仅 Linux / WSL2 可装。",
    "parquet": "parquet 打包与 HF 分片流式读取。",
}

# 日志缓冲上限。pip 装 torch 的日志能刷几千行，全留着只是浪费内存。
MAX_LOG = 400

# 单个包"导入自检"的超时。torch 冷启动几秒，vllm 可能三十秒往上；
# 超过一分钟基本可以认定是卡住了（缺 DLL、初始化死锁），再等没有意义。
VERIFY_TIMEOUT = 60

# torch 的轮子不在 PyPI。有 N 卡装 CUDA 版，没有就 CPU 版——装错了不会报错，
# 只会慢十倍，然后用户以为"这软件就这么慢"。
#
# ★这里的 cuXXX 必须与 requirements-tag.txt 里实测的那套（torch 2.14.1+cu126）
#   对齐。PyTorch 的索引是**按 CUDA 版本分目录、各自只放自己那份**：cu124 目录
#   最高只到 2.6.0，拿它去装钉死的 2.14.1 会直接 `No matching distribution found`，
#   也就是页面上点「安装」必然失败——和 Windows 上点 vllm 是同一类毛病。
#   测试 `test_torch_索引贴着清单里实测的_cuda_版本` 守着这条不漂。
TORCH_INDEX_GPU = "https://download.pytorch.org/whl/cu126"
TORCH_INDEX_CPU = "https://download.pytorch.org/whl/cpu"

# cu126 的轮子要求驱动支持 CUDA 12.6。驱动太老时 pip 照样装得上，要到
# `import torch` 才报 "CUDA driver version is insufficient"——页面一片绿、
# 一跑打标才炸，是最难查的那类失败。所以装之前先问一句 nvidia-smi。
TORCH_CUDA_MIN = (12, 6)

# nvidia-smi 要起进程（Windows 上约一百毫秒），而依赖页在 pip 跑的时候每秒
# 拉一次快照：缓存一分钟，别把轮询变成进程风暴。
_DRIVER_TTL_S = 60.0
_driver_cache: tuple[float, tuple[int, int] | None] | None = None


def _driver_cuda() -> tuple[int, int] | None:
    """驱动支持的最高 CUDA 版本（nvidia-smi 表头那句）。没卡/读不出来时 None。"""
    global _driver_cache
    now = time.time()
    if _driver_cache is not None and now - _driver_cache[0] < _DRIVER_TTL_S:
        return _driver_cache[1]
    ver: tuple[int, int] | None = None
    try:
        out = subprocess.run(
            ["nvidia-smi"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", out.stdout or "")
        if m:
            ver = (int(m.group(1)), int(m.group(2)))
    except (OSError, subprocess.SubprocessError):
        ver = None
    _driver_cache = (now, ver)
    return ver


def torch_plan() -> tuple[str, str]:
    """(torch 索引, 给人看的一句话)。装不了 CUDA 版时退 CPU 版，并把原因说清。

    为什么要退而不是"照装 cu126"：CUDA 版本是驱动的能力上限，轮子本身看不出
    来。照装的话 pip 成功、页面显示"已装"，直到第一次打标才报驱动版本不足——
    用户没有任何线索能把两句联系起来。退到 CPU 版至少是"能跑、只是慢"，
    再把"为什么慢、怎么变快"写在页面上。
    """
    if not shutil.which("nvidia-smi"):
        return TORCH_INDEX_CPU, ""
    cuda = _driver_cuda()
    if cuda is None:
        return TORCH_INDEX_CPU, (
            "有显卡，但没读出驱动的 CUDA 版本，先按 CPU 版装（打标会慢十倍）；"
            "驱动装好之后点一次「重装」就会换成 CUDA 版")
    if cuda < TORCH_CUDA_MIN:
        return TORCH_INDEX_CPU, (
            f"驱动只支持 CUDA {cuda[0]}.{cuda[1]}，cu126 版 torch 装上了 import 也会报"
            f"驱动版本不足，所以走 CPU 版（慢十倍）；想用显卡把驱动升到 CUDA "
            f"{TORCH_CUDA_MIN[0]}.{TORCH_CUDA_MIN[1]} 以上")
    return TORCH_INDEX_GPU, ""

# 分发名 → 导入名。清单里写的是 pip 名，而 `import` 用的是另一套名字，
# 少了这张表，页面会把装好的 Pillow 误报成"缺失"。
_MODULE_ALIAS = {
    "pillow": "PIL",
    "pyyaml": "yaml",
    "pydantic-core": "pydantic_core",
    "typing-inspection": "typing_inspection",
    "annotated-doc": "annotated_doc",
    "annotated-types": "annotated_types",
    "python-dotenv": "dotenv",
    # 分发名和模块名对不上：pip 装 open_clip_torch，import 的却是 open_clip。
    # 少了这条，「检查完整性」会把装好的包报成 ModuleNotFoundError: open_clip_torch，
    # 而用户照着提示重装多少遍都不会变——错的是检查器，不是环境。
    "open-clip-torch": "open_clip",
}


# 只在特定平台有轮子的包（键按分发名归一：小写、下划线当连字符）。
# requirements 文件表达不了"这个包只有 Linux"，读完统一按这张表打标；
# pyproject 里的 extras 也走同一张表，否则「安装本组」在 Windows 上照样会装。
_PLATFORM_ONLY = {
    "vllm": ("linux",),
}


def _platforms_of(name: str) -> tuple[str, ...]:
    return _PLATFORM_ONLY.get(name.lower().replace("_", "-"), ())


@dataclass
class Item:
    name: str                       # 分发名（pip 认的那个）
    spec: str                       # 完整要求串，如 numpy==2.5.4 / idna>=3.10,<4
    group: str                      # core | tag | optional
    note: str = ""
    torch_index: bool = False
    # 只在这些平台可用（空 = 全平台）。典型是 vLLM：它的轮子只有 Linux，
    # Windows 上 pip 会装出一个缺 C 扩展的壳，import 时报
    # `No module named 'vllm_C_stable_libtorch'`，而且会把钉死的 torch 换掉。
    platforms: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        """当前平台能不能用。sys.platform 是 win32 / linux / darwin。"""
        return not self.platforms or any(
            sys.platform.startswith(p) for p in self.platforms)

    @property
    def requirement(self) -> str:
        """只要版本号那一段，给页面显示用（==2.5.4 / >=3.10,<4 / 空）。"""
        return self.spec[len(self.name):].strip()

    @property
    def module(self) -> str:
        base = self.name.replace("_", "-").lower()
        if base in _MODULE_ALIAS:
            return _MODULE_ALIAS[base]
        return base.replace("-", "_")


# 可选依赖：不在两份清单里（清单只收"标准流程必装"的），但确实有人会用到。
# 装不上 / 不装都不影响基本流程，所以单独一组。
_OPTIONAL: list[Item] = [
    Item("onnxruntime", "onnxruntime", "optional", "cl-tagger-v2 后端（不依赖 torch）"),
    Item("datasets", "datasets", "optional", "把 HuggingFace 数据集直接当输入源"),
    Item("vllm", "vllm", "optional", "NL 生成服务，仅 Linux / WSL2",
         platforms=("linux",)),
]

_GROUPS = [
    ("core", "运行环境", "界面、打标调度、产物读写都要它。缺了这些连服务都起不来。"),
    ("tag", "打标运行时", "tag 反推（图 → 标签）用。不跑打标可以不装。"),
    ("optional", "可选功能", "按需装，不装不影响主流程。"),
]

# 附加依赖（pyproject 的 extra）在页面上的展示名与说明。
#
# 为什么也搬进这一页：这几组原来只长在各自的功能弹窗里（「本地量化」里一个
# 按钮、「量化」里一个按钮），想"先把环境配齐再干活"的人在这一页根本看不到
# 它们缺什么——而这一页是唯一能回答"整台机器还缺什么"的地方。
#
# 但它们**不进**「安装缺失」那个批量动作：serve 在 Windows 上装了也跑不起来，
# llmcompressor 几百 MB，一次全装是误伤。所以每组只给自己的按钮。
_EXTRA_GROUPS: list[tuple[str, str, str]] = [
    ("convert", "本地量化（GGUF）",
     "safetensors → F16 → Q4_K_M/Q6_K。torch / transformers / safetensors 与"
     "「打标运行时」共用同一份，卸载了打标也会停。"),
    ("quantize", "4bit 量化（W4A16）",
     "给 vLLM 用，llmcompressor 几百 MB；只打标不用装。"),
    ("parquet", "parquet 打包",
     "打包产物与 HF 分片流式读取（pyarrow + datasets）。"),
    ("serve", "vLLM 推理服务",
     "仅 Linux / WSL2 可装；Windows 上装了也起不来。"),
]


def _parse(path: Path, group: str) -> list[Item]:
    """读 requirements 文件。注释、空行、`-r` 之类统统跳过。"""
    if not path.is_file():
        return []
    out: list[Item] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(.*)$", line)
        if not m:
            continue
        name, rest = m.group(1), m.group(2).strip()
        # 只有环境标记（; python_version<"3"）的条目这里不展开：当前清单里没有，
        # 真出现时按"不限定版本"处理也比崩掉好。
        if rest.startswith(";"):
            rest = ""
        out.append(Item(
            name=name,
            spec=name + rest,
            group=group,
            torch_index=name.lower() in ("torch", "torchvision"),
            platforms=_platforms_of(name),
        ))
    return out


def load_extras() -> dict[str, list[Item]]:
    """读 pyproject 的 `[project.optional-dependencies]`。

    ★**不在这里再抄一份清单**：页面上一键装的东西，必须和 `pip install .[extra]`
      装的是同一串，否则两边迟早在某次升级后对不上，而且是对不上的那一刻才
      发现（页面说"装好了"，运行时还是 import 不到）。

    读不到 `tomllib`（Python 3.10 没有）或没有 pyproject（打包分发）时返回空表：
    那不是错误，只是这台机器没有可一键装的附加项，页面退回显示后端给的
    pip 命令即可。
    """
    try:
        import tomllib
    except ModuleNotFoundError:                  # 3.10：requires-python 只保证 >=3.10
        return {}
    try:
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    table = (data.get("project") or {}).get("optional-dependencies") or {}
    out: dict[str, list[Item]] = {}
    for extra, specs in table.items():
        items: list[Item] = []
        for spec in specs or []:
            m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(.*)$", str(spec))
            if not m:
                continue
            name, rest = m.group(1), m.group(2).strip()
            if rest.startswith(";"):
                rest = ""
            items.append(Item(
                name=name, spec=name + rest, group="extra",
                note=EXTRA_NOTES.get(extra, ""),
                torch_index=name.lower() in ("torch", "torchvision"),
                platforms=_platforms_of(name),
            ))
        if items:
            out[extra] = items
    return out


def load_items() -> list[Item]:
    items = _parse(REQ_CORE, "core") + _parse(REQ_TAG, "tag") + list(_OPTIONAL)
    # 同名的只留第一条：两份清单不小心都写了某个包时，别在页面上出现两行。
    seen: set[str] = set()
    unique: list[Item] = []
    for it in items:
        key = it.name.lower().replace("_", "-")
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return unique


def installed_version(name: str) -> str | None:
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None
    except Exception:                       # noqa: BLE001 - 元数据坏了不该让整页挂掉
        return None


def _satisfied(spec: str, version: str) -> bool:
    """按 PEP 440 判一下版本。解析不了就当满足——宁可少报一个错，
    也不要因为清单里写了 pip 能懂但 packaging 不认的写法而整行飘红。"""
    try:
        from packaging.requirements import Requirement

        return Requirement(spec).specifier.contains(version, prereleases=True)
    except Exception:                       # noqa: BLE001
        return True


def describe(item: Item) -> dict[str, Any]:
    """一个包的全部展示信息。每次刷新都现算，不缓存——
    pip 装完之后进程内的 importlib 缓存可能还是旧的，但磁盘元数据是新的。"""
    ver = installed_version(item.name)
    if ver is None:
        status, detail = "missing", "未安装"
    elif _satisfied(item.spec, ver):
        status, detail = "ok", ver
    else:
        status, detail = "mismatch", ver
    return {
        "name": item.name,
        "spec": item.spec,
        "requirement": item.requirement,
        "module": item.module,
        "group": item.group,
        "note": item.note,
        "torch_index": item.torch_index,
        "installed": ver,
        "status": status,
        "detail": detail,
        # 当前平台用不了（例如 Windows 上的 vllm）：页面据此置灰并说明，
        # 而不是让用户点了「安装」再收获一个 import 失败。
        "supported": item.supported,
        "platforms": list(item.platforms),
    }


class DepManager:
    """进程级单例。同一时刻只允许一个 pip 动作。

    为什么串行：两个 pip 同时往一个 site-packages 里装，会各自读到对方写到
    一半的目录，然后以"找不到某个 .dist-info"之类的形式炸掉，而报错指向的
    是完全无关的包。排队等反而是最快的路。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._token = 0
        self._cancel = False
        self.status = "idle"            # idle | running | done | failed | cancelled
        self.action = ""                # install | uninstall | reinstall
                                        # | install-missing | uninstall-all | verify
        self.target = ""
        self.started = 0.0
        self.finished = 0.0
        self.exit_code: int | None = None
        self.log: list[str] = []
        # 批量动作的逐项进度。单包动作也会有一条，前端统一按列表画。
        self.batch: list[dict[str, Any]] = []
        # 导入自检结果：包名 → {ok, detail, ms}。只活在内存里——它描述的是
        # "现在这个进程所在的环境"，重启后本来就可能变。
        self.verify: dict[str, dict[str, Any]] = {}
        self.verify_at = 0.0

    # ---------------------------------------------------------------- 查询

    def snapshot(self) -> dict[str, Any]:
        items = load_items()
        rows = [describe(it) for it in items]
        # 与主清单重名的包（torch / transformers / pyarrow…）：在两组里点"卸载"
        # 改的是同一个 site-packages，所以 extra 这边要标出来，别让人以为它只
        # 属于某个功能。
        shared = {it.name.lower().replace("_", "-") for it in items}
        groups = []
        for key, title, hint in _GROUPS:
            rows_of = [r for r in rows if r["group"] == key]
            groups.append({
                "key": key,
                "kind": key,
                "title": title,
                "hint": hint,
                "items": rows_of,
                "ok": sum(1 for r in rows_of if r["status"] == "ok"),
                "missing": sum(1 for r in rows_of if r["status"] == "missing"),
                # 缺、但这台机器上装不了（Windows 上的 vllm）。页面拿它把
                # "要装几个"和按钮可用性算对，否则点下去必然收获一条报错。
                "blocked": sum(1 for r in rows_of
                                if r["status"] == "missing" and not r["supported"]),
                "total": len(rows_of),
            })
        extra_rows: list[dict[str, Any]] = []
        extras = load_extras()
        for key, title, hint in _EXTRA_GROUPS:
            members = extras.get(key) or []
            if not members:
                # pyproject 没这段（或读不到 tomllib）就不画空组：一个 0/0 的
                # 分区只会让人怀疑"是不是漏装了"。
                continue
            rows_of = []
            for it in members:
                row = describe(it)
                row["group"] = key
                row["shared"] = it.name.lower().replace("_", "-") in shared
                rows_of.append(row)
            extra_rows.extend(rows_of)
            groups.append({
                "key": key,
                "kind": "extra",
                "extra": key,
                "title": title,
                "hint": hint,
                "items": rows_of,
                "ok": sum(1 for r in rows_of if r["status"] == "ok"),
                "missing": sum(1 for r in rows_of if r["status"] == "missing"),
                "blocked": sum(1 for r in rows_of
                                if r["status"] == "missing" and not r["supported"]),
                "total": len(rows_of),
            })
        with self._lock:
            job = None
            if self.status != "idle":
                job = {
                    "status": self.status,
                    "action": self.action,
                    "target": self.target,
                    "started": self.started,
                    "finished": self.finished,
                    "elapsed": (self.finished or time.time()) - self.started if self.started else 0,
                    "exit_code": self.exit_code,
                    "log": list(self.log[-120:]),
                    "batch": [dict(b) for b in self.batch],
                }
        # extra 组也是同一批包，完整性结果一样要贴上去（漏了这两行，"导入失败"
        # 标记永远不会出现在附加依赖那几组里）。
        for r in [*rows, *extra_rows]:
            r["verified"] = self.verify.get(r["name"])
        torch_index, torch_note = torch_plan()
        return {
            "python": sys.executable,
            "torch_index": torch_index,
            # 走 CPU 版时的原因（驱动太老 / 读不出驱动）：页面底部照实说一句，
            # 否则用户只会觉得"装了显卡还这么慢"。
            "torch_note": torch_note,
            "summary": {
                "total": len(rows),
                "ok": sum(1 for r in rows if r["status"] == "ok"),
                "missing": sum(1 for r in rows if r["status"] == "missing"),
                "mismatch": sum(1 for r in rows if r["status"] == "mismatch"),
                # 与各分组的 blocked 同义：缺，但本机装不了。
                "blocked": sum(1 for r in rows
                               if r["status"] == "missing" and not r["supported"]),
                "broken": sum(1 for r in rows
                              if (self.verify.get(r["name"]) or {}).get("ok") is False),
            },
            "verify_at": self.verify_at,
            "groups": groups,
            "job": job,
        }

    def activity(self) -> dict[str, Any] | None:
        """给底部进度条 / 完成通知用的最小快照（没跑过任务就返回 None）。

        不复用 snapshot()：那个要 load_items() + 逐个查版本，几秒轮询一次太贵。
        """
        with self._lock:
            if self.status == "idle":
                return None
            batch = [dict(b) for b in self.batch]
            done = sum(1 for b in batch
                       if b.get("status") in ("ok", "done", "failed", "skipped"))
            return {
                "id": f"deps:{self.started:.0f}",
                "kind": "deps",
                "title": "依赖安装",
                "detail": self.target,
                "status": "done" if self.status == "ok" else self.status,
                "done": done,
                "total": len(batch),
                "elapsed_s": round(((self.finished or time.time()) - self.started)
                                   if self.started else 0, 1),
                "finished_at": self.finished,
                "error": "" if self.status in ("running", "ok") else
                         "\n".join(self.log[-8:]),
                "log_tail": list(self.log[-6:]),
            }

    def busy(self) -> bool:
        with self._lock:
            return self.status == "running"

    # ---------------------------------------------------------------- 动作

    def install(self, name: str) -> dict[str, Any]:
        item = self._find(name)
        if not item.supported:
            raise ValueError(
                f"{item.name} 只支持 {' / '.join(item.platforms)}，"
                f"当前系统（{sys.platform}）装上也是坏的")
        return self._start_many("install", [item])

    def uninstall(self, name: str) -> dict[str, Any]:
        # 卸载不设平台限制：装错了的（比如 Windows 上的 vllm）本来就该能卸掉。
        return self._start_many("uninstall", [self._find(name)])

    def reinstall(self, name: str) -> dict[str, Any]:
        item = self._find(name)
        if not item.supported:
            raise ValueError(
                f"{item.name} 只支持 {' / '.join(item.platforms)}，"
                f"当前系统（{sys.platform}）装上也是坏的")
        return self._start_many("reinstall", [item])

    # ---- 批量动作

    def install_missing(self) -> dict[str, Any]:
        """把所有「缺失」的包装上。

        只装 missing，**不碰 mismatch**：把版本不符的包装回清单版本意味着降级
        （开发机手动装过更新的 torch 时尤其明显），批量做这种事太容易误伤。
        要降级就单独点那一行的「重装」，那一下用户是知情且确认过的。
        """
        # 只装本平台能用的：Windows 上「安装缺失的包」不该顺手把 vllm 拉进来，
        # 它装出来是个 import 就炸的壳，还会把钉死的 torch 换成另一版。
        items = [it for it in load_items()
                 if it.supported and describe(it)["status"] == "missing"]
        if not items:
            raise ValueError("没有缺失的包，不用装")
        return self._start_many("install-missing", items)

    def install_extra(self, extra: str) -> dict[str, Any]:
        """装 pyproject 里某个 extra 的**缺失部分**（例如量化用的 llmcompressor）。

        只装 missing，不碰已装的：这个 extra 里也写着 torch / transformers，
        那是为了 `pip install .[quantize]` 单独能跑通；而在这台机器上它们
        早就装着打标用的钉死版本了。为了补一个 llmcompressor 把 torch 换一遍，
        等于把已产出的 tag 数值口径悄悄改掉。
        """
        items = load_extras().get(extra)
        if not items:
            raise ValueError(f"没有这个附加依赖：{extra}")
        # 平台装不了的先摘掉：vLLM 的轮子只有 Linux，Windows 上装出来是个
        # import 就炸的壳，还会把钉死的 torch 换掉。要跑请去 WSL2 那边单独装。
        todo = [it for it in items
                if it.supported and describe(it)["status"] == "missing"]
        if not todo:
            bad = [it for it in items if not it.supported]
            if bad:
                only = " / ".join(sorted({p for it in bad for p in it.platforms}))
                names = " / ".join(it.name for it in bad)
                raise ValueError(
                    f"{names} 只支持 {only}，当前系统（{sys.platform}）装了也跑不起来")
            raise ValueError(f"{extra} 声明的依赖都装着了，不用装")
        return self._start_many(f"install-{extra}", todo)

    def uninstall_all(self) -> dict[str, Any]:
        """把当前装着的包全卸了，环境退回裸解释器。

        只卸清单里有的、且确实装着的。清单之外的包留给用户——页面本来也不
        展示它们，"卸载全部"顺手把别人手动装的东西删掉太越权了。
        """
        items = [it for it in load_items() if describe(it)["status"] != "missing"]
        if not items:
            raise ValueError("没有已安装的包")
        return self._start_many("uninstall-all", items)

    def verify_all(self) -> dict[str, Any]:
        """逐个真正 `import` 一遍，验证包是能用的，不只是"元数据在"。

        为什么不信 importlib.metadata：dist-info 在、包坏了的组合很常见
        ——下载中断留下半个目录、杀软删掉某个 .pyd、CUDA 版 torch 缺 dll。
        这些情况下 `version()` 照样返回值，页面一片绿，等真跑打标才炸。
        """
        items = [it for it in load_items() if describe(it)["status"] != "missing"]
        if not items:
            raise ValueError("没有可检查的包")
        return self._start_many("verify", items)

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            self._cancel = True
            self.status = "cancelled"
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        return self.snapshot()

    def _find(self, name: str) -> Item:
        for it in load_items():
            if it.name.lower() == name.lower():
                return it
        # 清单之外的包也允许卸：用户可能手动装过别的东西，页面上既然看得到
        # 依赖树里的异常，就该能收拾它。装的时候按裸名交给 pip 解析。
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
            raise ValueError(f"包名不合法：{name}")
        return Item(name=name, spec=name, group="optional", note="手动指定")

    def _start_many(self, action: str, items: list[Item]) -> dict[str, Any]:
        """所有动作的唯一入口：一个包和一批包走同一条路。

        分开写两份循环只会让"单包装好了、批量却没落日志"这类偏差活下来。
        """
        with self._lock:
            if self.status == "running":
                raise RuntimeError("上一个任务还没结束，等它跑完或先取消")
            self.status = "running"
            self.action = action
            self.target = items[0].name if len(items) == 1 else f"{len(items)} 个包"
            self.started = time.time()
            self.finished = 0.0
            self.exit_code = None
            self.log = []
            self._cancel = False
            self.batch = [{"name": it.name, "status": "pending", "detail": ""}
                          for it in items]
            self._token += 1
            token = self._token
        threading.Thread(target=self._run_many, args=(token, items, action),
                         name="dep-task", daemon=True).start()
        return self.snapshot()

    def _alive(self, token: int) -> bool:
        with self._lock:
            return self._token == token and not self._cancel

    def _mark(self, idx: int, status: str, detail: str = "") -> None:
        with self._lock:
            if 0 <= idx < len(self.batch):
                self.batch[idx]["status"] = status
                if detail:
                    self.batch[idx]["detail"] = detail

    # ---------------------------------------------------------------- 内部

    def _emit(self, line: str) -> None:
        text = line.rstrip()
        if not text:
            return
        with self._lock:
            self.log.append(text)
            if len(self.log) > MAX_LOG:
                del self.log[: len(self.log) - MAX_LOG]

    def _pip(self, args: list[str], token: int) -> int:
        """跑一次 pip，把输出逐行收进日志。返回退出码。"""
        cmd = [sys.executable, "-m", "pip", "--disable-pip-version-check",
               "--no-input"] + args
        self._emit("$ " + " ".join(cmd[1:]))
        env = dict(os.environ)
        # 别让 pip 在子进程里又去找别的解释器的配置
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self._emit(f"启动 pip 失败：{exc}")
            return 1
        with self._lock:
            self._proc = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            self._emit(line)
            if not self._alive(token):
                break
        code = proc.wait()
        with self._lock:
            if self._proc is proc:
                self._proc = None
        return code

    def _one(self, item: Item, action: str, token: int) -> tuple[bool, str]:
        """处理一个包，返回 (成功, 一句话说明)。"""
        if action == "verify":
            return self._verify_one(item)
        if action in ("uninstall", "uninstall-all", "reinstall"):
            code = self._pip(["uninstall", "-y", item.name], token)
            # 卸载一个本来就没装的包时 pip 退出码非 0，但那不算失败：
            # 用户点"重装"往往就是因为这个包已经半死不活了。
            if action in ("uninstall", "uninstall-all"):
                return code == 0, "" if code == 0 else f"退出码 {code}"
            self._emit(f"—— {item.name} 卸载完成，开始重装 ——")
        args = ["install", "--no-warn-script-location", item.spec]
        if item.torch_index:
            index, note = torch_plan()
            args += ["--index-url", index]
            self._emit(f"（torch 系列走 PyTorch 索引：{index}）")
            if note:
                self._emit(f"（{note}）")
        code = self._pip(args, token)
        return code == 0, "" if code == 0 else f"退出码 {code}"

    def _verify_one(self, item: Item) -> tuple[bool, str]:
        """在**子进程**里 import 一次。

        不用本进程的 importlib：torch 会往服务进程里塞几百 MB 的库，vllm 更
        夸张；而且导入之后再也退不回来，检查反而污染了被测对象。子进程还能
        超时 kill——本进程里的 import 卡住就只能等它。
        """
        # 平台不支持的直接判过：这不是环境坏了，是这台机器上本来就跑不了。
        # 判成失败会让「检查完整性」整批退出码 1，用户照着红色去修却无从修起。
        if not item.supported:
            detail = f"跳过：仅支持 {' / '.join(item.platforms)}，当前是 {sys.platform}"
            with self._lock:
                self.verify[item.name] = {"ok": True, "detail": detail, "ms": 0}
                self.verify_at = time.time()
            return True, detail
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, "-c", f"import {item.module}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=VERIFY_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            detail = f"导入超时（>{VERIFY_TIMEOUT}s）"
            with self._lock:
                self.verify[item.name] = {"ok": False, "detail": detail, "ms": VERIFY_TIMEOUT * 1000}
            return False, detail
        except OSError as exc:
            detail = f"起不了子进程：{exc}"
            with self._lock:
                self.verify[item.name] = {"ok": False, "detail": detail, "ms": 0}
            return False, detail

        ms = round((time.time() - t0) * 1000)
        if proc.returncode == 0:
            detail = f"{ms / 1000:.1f}s"
            ok = True
        else:
            tail = [ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()]
            detail = (tail[-1] if tail else f"退出码 {proc.returncode}")[:200]
            ok = False
        with self._lock:
            self.verify[item.name] = {"ok": ok, "detail": detail, "ms": ms}
            self.verify_at = time.time()
        return ok, detail

    def _run_many(self, token: int, items: list[Item], action: str) -> None:
        failures = 0
        try:
            for idx, item in enumerate(items):
                if not self._alive(token):
                    break
                self._mark(idx, "running")
                try:
                    ok, detail = self._one(item, action, token)
                except Exception as exc:        # noqa: BLE001 - 单项失败不该中断整批
                    ok, detail = False, f"{type(exc).__name__}: {exc}"
                    self._emit(f"[{item.name}] 出错了：{detail}")
                if not self._alive(token):
                    self._mark(idx, "skipped", "已取消")
                    break
                self._mark(idx, "ok" if ok else "failed", detail)
                if not ok:
                    failures += 1
        except Exception as exc:                # noqa: BLE001 - 任何异常都要落到结论
            self._emit(f"出错了：{type(exc).__name__}: {exc}")
            failures += 1
        # 批量里个别包失败不该让整批看起来"没跑完"，但结论仍然要能让用户看见。
        self._finish(0 if failures == 0 else 1, token)

    def _finish(self, code: int, token: int) -> None:
        with self._lock:
            if self._token != token:
                return                          # 已被新一轮顶掉，别覆盖它的状态
            self.exit_code = code
            self.finished = time.time()
            if self.status == "cancelled":
                pass
            else:
                self.status = "done" if code == 0 else "failed"


DEPS = DepManager()
