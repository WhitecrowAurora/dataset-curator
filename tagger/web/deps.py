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
# ★这里的 cuXXX 必须与 requirements-tag.txt 里记的那套对齐。PyTorch 的索引是
#   **按 CUDA 版本分目录、各自只放自己那份**：cu124 目录最高只到 2.6.0，拿它去
#   装 2.14.1 会直接 `No matching distribution found`，也就是页面上点「安装」
#   必然失败——和 Windows 上点 vllm 是同一类毛病。
#   测试 `test_torch_索引贴着清单里实测的_cuda_版本` 守着这条不漂。
TORCH_INDEX_GPU = "https://download.pytorch.org/whl/cu126"
TORCH_INDEX_CPU = "https://download.pytorch.org/whl/cpu"

# 50 系（Blackwell，CC 12.0+）必须走 cu130：cu126 的轮子只编译到 sm_90，
# 老架构靠 PTX JIT 还能兜住，sm_120 跨了大版本 JIT 兜不住，实跑报
# "no kernel image is available for execution on the device"。
# 代价是 cu130 要求驱动 >= 580：驱动不够的老卡仍留在 cu126，绝不整体换档
# （换了会让一批 55x/56x 驱动的机器从"能用"变成 import 就报驱动版本不足）。
TORCH_INDEX_BLACKWELL = "https://download.pytorch.org/whl/cu130"
TORCH_CUDA_MIN_BLACKWELL = (13, 0)

# cu126 的轮子要求驱动支持 CUDA 12.6。驱动太老时 pip 照样装得上，要到
# `import torch` 才报 "CUDA driver version is insufficient"——页面一片绿、
# 一跑打标才炸，是最难查的那类失败。所以装之前先问一句 nvidia-smi。
TORCH_CUDA_MIN = (12, 6)
# Blackwell 的硬件下限：CC 12.0 起才是 50 系（含 5090 / RTX PRO 6000 / GB 系列）。
_BLACKWELL_CC = (12, 0)
# Turing（20 系 / T4 / Quadro RTX）：CC 7.5。见 torch_plan 里的显式分支——
# CUDA 13 把 7.5 定成**最低门槛**（砍的是 7.5 之前），cu130 里确有 sm_75 内核，
# 所以 Turing 只是"能装但不该装"，不是装不了。
_TURING_CC = (7, 5)

# nvidia-smi 要起进程（Windows 上约一百毫秒），而依赖页在 pip 跑的时候每秒
# 拉一次快照：缓存一分钟，别把轮询变成进程风暴。
_DRIVER_TTL_S = 60.0
_driver_cache: tuple[float, tuple[int, int] | None] | None = None
# 显卡架构探测单独缓存：query_gpus() 也起进程，和驱动版本一样不能跟着轮询跑。
_CC_TTL_S = 60.0
_cc_cache: tuple[float, tuple[int, int] | None] | None = None


def _driver_cuda() -> tuple[int, int] | None:
    """驱动支持的最高 CUDA 版本。没卡/读不出来时 None。

    ★两条路，**先读表头、再查表**（顺序是调换过的，见下方 1/2 与实现里的
      注释）。历史上踩过的坑，都是实测反馈回来的：

      1. 早先只解析文本，而**nvidia-smi 的文本是本地化的**：中文系统上表头
         写作 `CUDA 版本: 11.8`，英文正则 `CUDA Version:` 一个字符都对不上，
         于是页面把有卡的机器当成没卡、直接按 CPU 版装（2080 Ti 那位用户）。
         修法是改成"问 nvidia-smi 要驱动号，再查表换算"，格式与语言无关。
      2. 但查表**必然漏档**——表是人手抄的分界点。驱动 595.79 表头写 13.2，
         表里最高只到 580→13.0，于是被算成 13.0；2080 Ti 的 610→13.3 同理。
         漏档不影响装哪一档（都 ≥12.6，都走 cu126），但**把版本号说错**，
         用户拿它跟 nvidia-smi 对照只会更困惑。
         所以现在的顺序是：表头（驱动自己算的，总是精确）→ 查表（兜底）。

    表头解析**中英文标签都认**，见 `_driver_cuda_from_text`。
    """
    global _driver_cache
    now = time.time()
    if _driver_cache is not None and now - _driver_cache[0] < _DRIVER_TTL_S:
        return _driver_cache[1]
    ver: tuple[int, int] | None = None
    smi = _smi_path()
    # ★先读表头，再退回查表。顺序是调换过的，理由值得记住：
    #
    #   nvidia-smi 表头那个 "CUDA Version" 是**驱动自己算出来的**，总是精确；
    #   而 `_DRIVER_CUDA_TABLE` 是人手抄的分界点，**必然漏档**。开发机上就撞到
    #   过：驱动 595.79 表头写 13.2，查表却因为表里只有 580→13.0 而算成 13.0。
    #   2080 Ti 那位 610→13.3 是同一类缺口，只是那次先被发现了。
    #
    #   漏档不影响装哪一档（13.0 / 13.2 / 13.3 都 ≥12.6，都走 cu126），但会
    #   让页面显示一个和 nvidia-smi 对不上的版本号。用户拿这个数字去排查时，
    #   只会更困惑——这比"少显示一个信息"糟得多。
    #
    #   查表不再是主路，只作为"表头解析不出来"时的兜底（老驱动表格格式变了、
    #   或者本地化把标签翻了）。
    if smi:
        ver = _driver_cuda_from_text(smi)
        if ver is None:
            try:
                out = subprocess.run(
                    [smi, "--query-gpu=driver_version", "--format=csv,noheader"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if out.returncode == 0:
                    first = (out.stdout or "").strip().splitlines()
                    ver = _cuda_from_driver(first[0].strip()) if first else None
            except (OSError, subprocess.SubprocessError):
                ver = None
    _driver_cache = (now, ver)
    return ver


def _smi_path() -> str | None:
    """nvidia-smi 的路径。`shutil.which` 在 Windows 上查不到 System32 时
    还有 `hardware._find_nvidia_smi` 那几个固定位置兜底——那边已经有一份了，
    这里复用，别再抄一遍路径表。"""
    exe = shutil.which("nvidia-smi")
    if exe:
        return exe
    try:
        from ..hardware import _find_nvidia_smi

        return _find_nvidia_smi()
    except Exception:                         # noqa: BLE001 - 探测失败当没有
        return None


# 驱动号 → 能跑的最高 CUDA。nvidia-smi 表头那个 "CUDA Version" 就是这张表的
# 产物，所以不必去解析它。只列分界点，返回不超过它的最高一档。
#
# ★分界用 nvidia-smi 表里**实测出现的** "驱动/表头" 组合（见下方注释），
#   不靠估。
#
# 表只要"不漏档"就够，具体那档差 0.1 无所谓：torch_plan 只拿它跟 12.6 / 13.0
# 两条线比大小，13.0 和 13.3 走的是同一条路（都是"够新，装 cu126"）。缺一档的
# 真实后果是**页面把驱动版本号说错**，用户按错的版本号去排查会很困惑——2080 Ti
# 那位用户就是驱动 610 / CUDA 13.3，以前被显示成 13.0。
_DRIVER_CUDA_TABLE: tuple[tuple[int, tuple[int, int]], ...] = (
    (610, (13, 3)),     # 610.x → CUDA 13.3（2080 Ti 用户实测：驱动 610 / CUDA 13.3）
    (580, (13, 0)),     # 580.x → CUDA 13.0，50 系必需
    (570, (12, 8)),     # 572.x → CUDA 12.8
    (560, (12, 6)),     # 561.x → CUDA 12.6：**cu126 轮子的下限**，别漏这一档
    (550, (12, 4)),     # 551.x → CUDA 12.4
    (545, (12, 3)),
    (535, (12, 2)),     # 536.x → CUDA 12.2
    (530, (12, 1)),
    (525, (12, 0)),
    (520, (11, 8)),
    (510, (11, 6)),
    (495, (11, 5)),
    (470, (11, 4)),
    (450, (11, 0)),
)


def _cuda_from_driver(driver: str) -> tuple[int, int] | None:
    """`472.12` → (11, 4)。读不出主版本号时 None。"""
    m = re.match(r"(\d+)\.", str(driver or "").strip())
    if not m:
        return None
    major = int(m.group(1))
    for floor, cuda in _DRIVER_CUDA_TABLE:
        if major >= floor:
            return cuda
    # 比表里最老的还老：给它最低那一档，让下游按"驱动太老"给出升驱动的提示，
    # 而不是当成"读不出来"退 CPU（两者对用户说的话不一样）。
    return _DRIVER_CUDA_TABLE[-1][1]


def _driver_cuda_from_text(smi: str | None) -> tuple[int, int] | None:
    """退路：解析 nvidia-smi 的**表格输出**。中英文标签都认。"""
    if not smi:
        return None
    try:
        out = subprocess.run(
            [smi],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (out.stdout or "").splitlines():
        # 英文 "CUDA Version: 13.2"；中文 "CUDA 版本: 11.8"
        m = re.search(r"CUDA\s*(?:Version|版本)\s*[:：]\s*(\d+)\.(\d+)", line)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    return None


def _gpu_cc() -> tuple[int, int] | None:
    """本机第一张卡的 compute capability。没卡/读不出来时 None。

    为什么要看架构而不是只数显存：torch 的轮子是按 CUDA 版本分目录编的，而
    每个目录编了哪些 sm_xxx 不一样。cu126 那份实测只到 sm_90——40 系(8.9)还
    能靠 PTX JIT 兜住，50 系(12.0)兜不住，实跑必然报 no kernel image。
    取第一张**可见**卡：resolve_profile 的调用方现在都走 visible_gpus，
    这里若仍按物理序取第一张，选轮子（sm_xx）与选分档就可能落在两张架构
    不同的卡上——装出来的 wheel 跑不了正在用的那张。
    """
    global _cc_cache
    now = time.time()
    if _cc_cache is not None and now - _cc_cache[0] < _CC_TTL_S:
        return _cc_cache[1]
    cc: tuple[int, int] | None = None
    try:
        from ..hardware import visible_gpus

        gpus = visible_gpus()
        if gpus:
            cc = gpus[0].compute_cap
    except Exception:                         # noqa: BLE001 - 探测失败按"不知道"处理
        cc = None
    _cc_cache = (now, cc)
    return cc


def torch_plan() -> tuple[str, str]:
    """(torch 索引, 给人看的一句话)。装不了 CUDA 版时退 CPU 版，并把原因说清。

    为什么要退而不是"照装 cu126"：CUDA 版本是驱动的能力上限，轮子本身看不出
    来。照装的话 pip 成功、页面显示"已装"，直到第一次打标才报驱动版本不足——
    用户没有任何线索能把两句联系起来。退到 CPU 版至少是"能跑、只是慢"，
    再把"为什么慢、怎么变快"写在页面上。

    三档顺序是"先看架构，再看驱动"：
      1. 50 系（CC >= 12.0）→ cu130。这一档换 CUDA 版本不是优化而是必需，
         cu126 在 sm_120 上根本没有 kernel；
      2. Turing（CC 7.5，20 系 / T4）→ 固定 cu126。**显式分支，不靠"掉到
         兜底恰好对"**：Turing 用户的驱动通常很新（R580 是 LTS），驱动号会
         映射到 CUDA 13，但 cu130 对 7.5 只是"能跑"——sm_75 内核在，可它
         本来就缺 TF32/bf16，换大版本运行时只有额外开销，而 20/30/40 系的
         实测基准全在 cu126 上。所以架构优先于驱动号：驱动再新也留 cu126。
      3. 其余 N 卡 → cu126（本机实测能跑，40/30 系都在这个目录里）；
      4. 无卡 / 驱动读不出 → CPU。
    第 1 档驱动不够时**不降回 cu126**——那等于装一个必然 no kernel image 的
    组合，看着"装了 CUDA 版"，一跑就炸。直接退 CPU 并把"升驱动"说清。

    ★Turing 的驱动下限同样必须满足：cu126 要驱动支持 CUDA 12.6（约 560+）。
      R580 分支覆盖到 7.5 之前，所以只要用户驱动是新的，这一条不会拦住他；
      真正停在 47x~55x 的 Turing 机器仍然退 CPU，并在页面上写明"升到 12.6"。
    """
    if not shutil.which("nvidia-smi") and not _smi_path():
        return TORCH_INDEX_CPU, ""
    cuda = _driver_cuda()
    cc = _gpu_cc()
    if cuda is None:
        return TORCH_INDEX_CPU, (
            "有显卡，但没读出驱动的 CUDA 版本，先按 CPU 版装（打标会慢十倍）；"
            "驱动装好之后点一次「重装」就会换成 CUDA 版。"
            "（诊断：NVIDIA 面板里那个「CUDA 版本」是多少？"
            "或者在命令行跑 nvidia-smi 看能不能出表）")
    if cc is not None and cc >= _BLACKWELL_CC:
        if cuda < TORCH_CUDA_MIN_BLACKWELL:
            return TORCH_INDEX_CPU, (
                f"{cc[0]}.{cc[1]} 的卡（50 系）需要 cu130 版 torch，而驱动只支持 "
                f"CUDA {cuda[0]}.{cuda[1]}；cu126 在 sm_120 上没有 kernel，"
                f"装了也是跑一张报一次错。先按 CPU 版装（慢十倍），把驱动升到 "
                f"CUDA {TORCH_CUDA_MIN_BLACKWELL[0]}.{TORCH_CUDA_MIN_BLACKWELL[1]} "
                f"（580 以上）后点「重装」")
        return TORCH_INDEX_BLACKWELL, ""
    if cuda < TORCH_CUDA_MIN:
        return TORCH_INDEX_CPU, (
            f"驱动只支持 CUDA {cuda[0]}.{cuda[1]}，cu126 版 torch 装上了 import 也会报"
            f"驱动版本不足，所以走 CPU 版（慢十倍）；想用显卡把驱动升到 CUDA "
            f"{TORCH_CUDA_MIN[0]}.{TORCH_CUDA_MIN[1]} 以上")
    if cc is not None and cc >= _TURING_CC:
        # Turing 到这里是明确的"就用 cu126"，不写也能靠最后的 return 拿到同样的
        # 值——但那样一来，将来谁把兜底改成别的档（比如按驱动号跟着升 cu130），
        # 20 系就会在无人察觉的情况下被换掉。这一支是给它钉的桩。
        return TORCH_INDEX_GPU, ""
    return TORCH_INDEX_GPU, ""


# ---------------------------------------------------------------- torch 变体
#
# 上面是**本机推荐**；这一节是给页面列出的**可选清单**。两者分开是因为
# "推荐哪个"和"允许装哪些"不是一回事：5090 上推荐 cu130，但用户明确要装
# cu126 比对产出时不该被拦。所以页面只做标注（推荐 / 可用 / 不建议），
# 点「重装」时按用户选的那一档真装——**只推荐，不强制**。
#
# 为什么不能随便装一个：cuXXX 决定 wheel 里编了哪些 sm_xxx。cu126 实测只到
# sm_90（40 系靠 PTX JIT 兜住），50 系是 sm_120，必须 cu130。装错了不是慢，
# 是每张图都报 "no kernel image is available"。
TORCH_VARIANTS: tuple[dict[str, Any], ...] = (
    {"key": "auto", "index": "", "label": "自动（按本机显卡）",
     "min_cuda": (), "note": "按显卡架构与驱动自动挑一档，绝大多数情况选它就对了。"},
    {"key": "cu130", "index": TORCH_INDEX_BLACKWELL, "label": "CUDA 13.0（cu130）",
     "min_cuda": TORCH_CUDA_MIN_BLACKWELL,
     "note": "50 系（CC 12.0+）**必须**用这一档：cu126 的轮子没有 sm_120 内核，"
             "装上每张图都报 no kernel image。要求驱动 580 以上。"
             "注意 cu130 是 13.x 系列的目录名，不代表只能用 13.0。"},
    {"key": "cu126", "index": TORCH_INDEX_GPU, "label": "CUDA 12.6（cu126）",
     "min_cuda": TORCH_CUDA_MIN,
     "note": "20 / 30 / 40 系统一推荐（torch 2.14.1+cu126 就是实测那套）。"
             "40 系（8.9）在这里有原生内核；改用 cu130 反而没有 8.9 内核、"
             "只能靠 PTX JIT，启动更慢。50 系装它会缺内核。"},
    {"key": "cpu", "index": TORCH_INDEX_CPU, "label": "CPU 版",
     "min_cuda": (), "note": "不要求显卡，打标速度约慢十倍；只用来验证或排障。"},
)


# torch 与 torchvision 是**同一套 ABI 编出来的**：版本必须配套（2.14.1 ↔
# 0.29.1），CUDA 档位也必须一致。单独重装其中一个会同时踩两个坑：
#
#   1. pip 看到 torchvision==0.29.1 要求 torch==2.14.1 时，会把刚装好的
#      torch 换回它自己挑的版本——用户以为"我重装了 torch"，实际装回去的是
#      torchvision 拉的那个；
#   2. 档位不一致（torch 是 cu126、torchvision 是 cu130）时 import 报
#      "undefined symbol"，报错指向的是一串 C++ 符号名，与"档位选错"毫无
#      字面关系，用户基本无从下手。
#
# 所以点其中一个的「安装 / 重装」，另一个自动跟上。这是**页面行为**，不是
# 隐式魔法：批量列表里会多出一行、日志里会写清为什么要带上它，用户看得见。
# 「卸载」不连带——收拾半死不活的环境时，能单独拆一个反而是需要的。
_TORCH_PEER = {"torch": "torchvision", "torchvision": "torch"}

# 配对处理时的固定顺序：**torchvision 在前**。理由见 `_with_torch_siblings`：
# 反过来会让装 torch 的那一步被 torchvision 的依赖约束牵着走，可能把刚下的
# 那一档轮子换掉。
_TORCH_ORDER = ("torchvision", "torch")


def _arch_name(cc: tuple[int, int] | None) -> str:
    """CC → 人话（"8.9（40 系）"）。页面提示要让人一眼认出自己那张卡。

    按**区间**判而不是"≥ 某档"：`>= (8, 9)` 会把 9.0（Hopper / H100）也说成
    40 系。这里只用于展示文案，判错不至于装错，但把 H100 叫成"40 系"同样会
    让人不再相信页面上其他话。
    """
    if cc is None:
        return "未知架构"
    major, minor = cc
    tag = ""
    if major >= 12:
        tag = "（50 系，Blackwell）"
    elif major == 9:
        tag = "（Hopper）"
    elif major == 8:
        if minor == 9:
            tag = "（40 系，Ada）"
        elif minor in (6, 7):
            tag = "（30 系，Ampere）"
        elif minor == 0:
            tag = "（A100，Ampere）"
    elif major == 7:
        if minor >= 5:
            tag = "（20 系，Turing）"
    return f"{cc[0]}.{cc[1]}{tag}"


def _variant(key: str) -> dict[str, Any] | None:
    for v in TORCH_VARIANTS:
        if v["key"] == key:
            return v
    return None


def torch_index_for(key: str | None = None) -> str:
    """某个变体对应的索引地址。空 / auto = 按本机推荐；认不出的键按 auto。"""
    v = _variant(str(key or "auto").strip().lower())
    if v is None or v["key"] == "auto":
        return torch_plan()[0]
    return str(v["index"])


def torch_options() -> list[dict[str, Any]]:
    """给页面的 torch 变体清单，每个带上"在本机能不能用"的结论。

    页面据此把不可用的那些**置灰但可见**（而不是藏起来）：用户知道 50 系
    还有 cu130 这回事，比看见一条莫名其妙的 no kernel image 报错强。
    """
    rec, rec_note = torch_plan()
    # 与 torch_plan 用同一个判据（_smi_path 带 System32 兜底）：一边认有卡、
    # 一边认没卡的话，页面会推荐 cu126 却把所有档位标成"本机不可用"。
    has_card = bool(_smi_path())
    cuda = _driver_cuda() if has_card else None
    cc = _gpu_cc() if has_card else None
    out: list[dict[str, Any]] = []
    for v in TORCH_VARIANTS:
        key = str(v["key"])
        min_cuda: tuple[int, int] = v["min_cuda"] or ()
        index = rec if key == "auto" else str(v["index"])
        reason = ""
        ok = True
        if key == "auto":
            reason = rec_note or (f"本机推荐 {index.rsplit('/', 1)[-1]}"
                                  if index != TORCH_INDEX_CPU else "本机推荐 CPU 版")
        elif key in ("cu126", "cu130"):
            need = (13, 0) if key == "cu130" else TORCH_CUDA_MIN
            if cuda is None:
                ok = False
                reason = "没读到驱动版本（nvidia-smi 不可用）"
            elif cuda < need:
                ok = False
                reason = (f"驱动只支持 CUDA {cuda[0]}.{cuda[1]}，"
                          f"这一档要 {need[0]}.{need[1]} 以上")
            # 架构与驱动的方向相反：cu130 在非 Blackwell 上"能装但不必要"，
            # cu126 在 Blackwell 上"装得上但跑不了"。前者只是不建议，后者禁用。
            if ok and key == "cu126" and cc is not None and cc >= _BLACKWELL_CC:
                ok = False
                reason = (f"本机是 {cc[0]}.{cc[1]}（50 系），cu126 的轮子没有 "
                          f"sm_120 内核，跑起来会报 no kernel image")
            if ok and key == "cu130" and cc is not None and cc < _BLACKWELL_CC:
                # Turing 要单独说清"不是缺内核，是没必要"：它的用户驱动往往很新，
                # 容易以为"新驱动当然配新 CUDA"。cu130 里确实有 sm_75 的内核
                # （CUDA 13 砍的是 7.5 **之前**），只是 7.5 本身没有 TF32/bf16，
                # 换大版本运行时只有开销，而实测基准都在 cu126 上。
                if cc >= _TURING_CC:
                    reason = (f"本机是 {cc[0]}.{cc[1]}（Turing），cu130 能跑但没有必要："
                              f"它同样编了 sm_75，可 20 系没有 TF32/bf16，换版本只有开销。"
                              f"cu126 才是实测口径")
                else:
                    reason = (f"本机是 {cc[0]}.{cc[1]}，cu130 能用但没有必要；"
                              f"cu126 才是实测口径")
        out.append({
            "key": key,
            "label": v["label"],
            "index": index,
            "note": v["note"],
            "ok": ok,
            "recommended": key == "auto" or (key != "auto" and index == rec),
            "reason": reason,
        })
    return out


def torch_hint() -> dict[str, Any]:
    """顶部栏那一行提示：本机该用哪一档、当前装的对不对。

    为什么不直接写死一张"20/30/40→cu126、50→cu130"的表：档位规则在
    `torch_plan()` 里已经有一份，再抄一份到提示里，两处迟早打架——顶栏说
    cu130、依赖页说 cu126，用户不知道该信哪个。这里**从同一份数据推导**，
    规则只存在一处。

    返回的 status 只有三种，前端按它选颜色：
      ok      已装的就是推荐档，一切正常
      switch  装错了/该换档，需要用户动手（带 reason）
      cpu     没有 CUDA 版可用（没卡、驱动太老、或探测失败）
    """
    if not _smi_path():
        return {"status": "cpu", "arch": "未检测到显卡", "target": "",
                "current": "", "reason": "未检测到 NVIDIA 显卡，走 CPU 版"}
    rec, rec_note = torch_plan()
    cuda = _driver_cuda()
    cc = _gpu_cc()
    arch = _arch_name(cc)
    rec_key = rec.rstrip("/").rsplit("/", 1)[-1] or "cpu"   # cu126 / cu130 / cpu
    target = "" if rec == TORCH_INDEX_CPU else rec_key

    # 当前实际装的是哪一档：读 torch 自己的编译版本，比"猜用户点了什么"可靠
    installed = installed_version("torch") or ""
    cur = ""
    if installed:
        m = re.search(r"\+(cu\d+|cpu)$", installed)
        cur = m.group(1) if m else ("cpu" if "+cpu" in installed else "")

    if rec == TORCH_INDEX_CPU:
        return {"status": "cpu", "arch": arch, "target": "", "current": cur,
                "reason": rec_note or "没有可用的 CUDA 版，走 CPU 版"}
    # 版本不符(mismatch)时也说一句：装的档位对、但版本号不是清单里那个，
    # 同样值得让人看见，属于"该动手"而不是"一切正常"。
    if cur and cur == target:
        ver_state = ""
        if installed and not _satisfied(f"torch=={_spec_version('torch')}", installed):
            ver_state = "版本与清单不一致，建议重装"
        if ver_state:
            return {"status": "switch", "arch": arch, "target": target,
                    "current": cur, "reason": ver_state}
        return {"status": "ok", "arch": arch, "target": target,
                "current": cur, "reason": ""}
    reason = rec_note or (f"本机建议装 {target} 版" if target else "")
    if cur:
        reason = f"当前装的是 {cur}，" + (reason or f"建议换成 {target}")
    return {"status": "switch", "arch": arch, "target": target,
            "current": cur, "reason": reason}


def _spec_version(name: str) -> str:
    """从清单里取"钉死的那个版本号"（`torch==2.14.1` → `2.14.1`）。"""
    for it in load_items():
        if it.name.lower() == name.lower():
            m = re.search(r"==\s*([\w.\-+]+)", it.spec)
            if m:
                return m.group(1)
    return ""

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
    # 用户显式选的 torch 档位（cu130 / cu126 / cpu）。空 = 按本机推荐走。
    # 只对 torch_index=True 的条目有意义，其余包不看它。
    torch_variant: str = ""
    # 与它**必须同版本配对**的包（torch ↔ torchvision）。点其中一个的安装/
    # 重装，另一个会被自动带上——见 `_with_torch_siblings` 的理由。
    torch_peer: str = ""

    @property
    def peers_with(self) -> str:
        """自动连带处理的另一个包名；空 = 无从属关系。只对 torch 两行有意义。"""
        return self.torch_peer if self.torch_index else ""

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
            torch_peer=_TORCH_PEER.get(name.lower(), ""),
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
                torch_peer=_TORCH_PEER.get(name.lower(), ""),
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
        # 必须同版本配对的那个包（torch ↔ torchvision）。前端据此在行内写一句
        # "与 torch 配套，点重装会一起装"，避免用户只点一个之后以为没生效。
        "torch_peer": item.peers_with,
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
        # 下一轮要写进日志开头的一句话（"torch 与 torchvision 已一并处理"）。
        # 用"挂起"而不是直接 _emit：_start_many 会重置 log，直接写会被冲掉。
        self._pending_note = ""

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
            # 可选档位（含"本机推荐"那一档）。只推荐不强制：页面可以让人
            # 换一档再装，装的时候按他选的走。
            "torch_options": torch_options(),
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

    def install(self, name: str, *, torch_variant: str = "") -> dict[str, Any]:
        item = self._find(name)
        if not item.supported:
            raise ValueError(
                f"{item.name} 只支持 {' / '.join(item.platforms)}，"
                f"当前系统（{sys.platform}）装上也是坏的")
        return self._start_many("install", self._with_torch_siblings([item]),
                                torch_variant=torch_variant)

    def uninstall(self, name: str) -> dict[str, Any]:
        # 卸载不设平台限制：装错了的（比如 Windows 上的 vllm）本来就该能卸掉。
        # 也**不**连带 torchvision ↔ torch：拆环境时能单独卸一个是有用的，
        # 而且卸完再装一遍本来就是修 ABI 不匹配的常规手段。
        return self._start_many("uninstall", [self._find(name)])

    def reinstall(self, name: str, *, torch_variant: str = "") -> dict[str, Any]:
        item = self._find(name)
        if not item.supported:
            raise ValueError(
                f"{item.name} 只支持 {' / '.join(item.platforms)}，"
                f"当前系统（{sys.platform}）装上也是坏的")
        return self._start_many("reinstall", self._with_torch_siblings([item]),
                                torch_variant=torch_variant)

    def _with_torch_siblings(self, items: list[Item]) -> list[Item]:
        """给 torch / torchvision 补上它必须配对的那一个，返回处理顺序。

        为什么必须成对：torch 与 torchvision 是同一套 ABI 编出来的，版本和
        CUDA 档位都得一致。只重装一个会——pip 为了让 torchvision 的依赖成立而
        把 torch 换回它自己挑的版本、或留下档位不一致的组合，后者 import 时报
        "undefined symbol: _ZN3c10..."，字面上完全看不出是"档位选错"。详见
        `_TORCH_PEER` 的注释。

        **顺序有讲究：先 torchvision、后 torch。** 反过来的话，装 torch 的
        那一步 pip 会去满足 torchvision 的 `torch==x.y.z` 约束，可能把刚下的
        cu126 轮子换成别的档位；先装 torchvision 时它依赖的 torch 还没装，
        pip 会按 --index-url 一并装好，随后再装 torch 就是同一档位的覆盖安装，
        结果是确定的。

        顺序是**固定的**，不随"用户点的是哪一个"变化：点 torchvision 时才去
        拉 torch，若按"配对的那个放前面"排，就成了先 torch 后 torchvision，
        恰好踩上面那个坑。

        只在调用方**没有**同时点名两者时才补：用户自己勾了两个包（批量动作）
        就按他给的顺序来，不重复插入。
        """
        pool = {it.name.lower(): it for it in load_items()}
        named = {it.name.lower() for it in items}
        # 按固定的"先 torchvision 后 torch"排：_TORCH_ORDER 里的下标，其余保持
        # 用户给的相对顺序（用大偏移，避免和它们混在一起）。
        def rank(it: Item) -> int:
            key = it.name.lower()
            return _TORCH_ORDER.index(key) if key in _TORCH_ORDER else len(_TORCH_ORDER)

        wanted: list[Item] = []
        seen: set[str] = set()
        for it in items:
            peer = it.peers_with
            if not peer or peer in named:
                continue                    # 用户自己点了两个，别再插
            other = pool.get(peer)
            if other is not None and other.name.lower() not in seen:
                wanted.append(other)
                seen.add(other.name.lower())
        for it in items:
            if it.name.lower() not in seen:
                wanted.append(it)
                seen.add(it.name.lower())
        if len(wanted) > len(items):
            added = [it.name for it in wanted if it.name.lower() not in named]
            self._note_peer([x.name for x in wanted])
            wanted.sort(key=rank)
        return wanted

    def _note_peer(self, names: list[str]) -> None:
        """在日志开头写清"为什么多装了一个"，别让用户以为是页面抽风。

        要把**两个包名都写全**。之前只拼了"被补上的那个"，于是点 torchvision
        时文案成了"torch 与 torch 必须配对"，反而更让人糊涂。
        """
        if not names:
            return
        # 文案里的名字按展示习惯排（torch 在前），与**执行顺序**（torchvision
        # 在前）是两回事：执行顺序是 pip 约束决定的，这句话是给人读的。
        _show = ("torch", "torchvision")
        shown = sorted(names, key=lambda n: _show.index(n.lower())
                       if n.lower() in _show else len(_show))
        self._pending_note = (
            f"{' 与 '.join(shown)} 必须同版本配对，已一并处理"
            f"（单独重装其中一个会把另一个换掉，或留下档位不一致导致 import 报"
            f" undefined symbol）。只卸不装时可单独操作。")

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

    def _start_many(self, action: str, items: list[Item], *,
                    torch_variant: str = "") -> dict[str, Any]:
        """所有动作的唯一入口：一个包和一批包走同一条路。

        分开写两份循环只会让"单包装好了、批量却没落日志"这类偏差活下来。
        """
        # 选定的 torch 档位钉进 Item 上挂着的那份副本里：线程跑的时候用的
        # 就是这一份，不再去读全局状态（否则轮询一刷新就可能换档）。
        key = str(torch_variant or "").strip().lower()
        if key and key != "auto":
            if _variant(key) is None:
                raise ValueError(f"没有这个 torch 档位：{torch_variant}")
            items = [Item(**{**it.__dict__, "torch_variant": key}) for it in items]
        # 挂起的那句话属于本轮，取走后立刻清空：留着会被下一个不相关的动作
        # 再写一遍，变成误导。
        note, self._pending_note = self._pending_note, ""
        with self._lock:
            if self.status == "running":
                # 状态是 running 就直接拒，此时不能把 note 丢掉——它等着本轮
                # 真正开跑时用。放回去。
                self._pending_note = note
                raise RuntimeError("上一个任务还没结束，等它跑完或先取消")
            self.status = "running"
            self.action = action
            self.target = items[0].name if len(items) == 1 else f"{len(items)} 个包"
            self.started = time.time()
            self.finished = 0.0
            self.exit_code = None
            self.log = []
            self._cancel = False
            if note:
                # 写在 log 重置之后：之前写会被上一行清掉。
                self.log.append(note)
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
            # 用户选了档位就按他选的装（只推荐、不强制）；没选才走本机推荐。
            index = torch_index_for(item.torch_variant)
            note = "" if item.torch_variant else torch_plan()[1]
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
