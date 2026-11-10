"""GGUF 量化（Q4_K_M / Q6_K）：**我们自己的 numpy 实现**。

和 quantize.py 的 W4A16 是两条独立的链路：

    W4A16  → llm-compressor（Python + CUDA）→ safetensors 目录 → vLLM 加载
    Q4/Q6  → 本文件 + kquant/gguf_file      → 单个 .gguf 文件 → llama.cpp 加载

第一版是调 llama.cpp 的 `llama-quantize.exe`，但那意味着用户为了压一个模型，
得先下 200MB 的运行时。K-quant 本身只是"256 个权重一组、算 scale/min、
按固定字节布局打包"，用 numpy 完全能自己做，所以改成内置实现：

    gguf_file.py  读原 GGUF（元数据原样搬运）、按新类型写回去
    kquant.py     Q4_K / Q6_K 的量化与反量化

代价是速度：numpy 逐张量跑，8B 模型大约几分钟（llama-quantize 是几十秒），
而且没有做 K-quant 的迭代最优 scale（见 kquant.py 顶部说明），同 bit 下误差
略大。换来的是不需要任何外部程序，装完就有。

**输入仍必须是 GGUF**。safetensors 转 GGUF 要处理 tokenizer 词表、chat
template、视觉塔的层名映射，那是"格式转换"，不是量化——两件事分开，
免得把一个大坑伪装成一个按钮。
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .gguf_file import (
    GGML_TYPE_BF16,
    GGML_TYPE_F16,
    GGML_TYPE_F32,
    GGML_TYPE_Q4_K,
    GGML_TYPE_Q6_K,
    Reader,
    TensorInfo,
    write_gguf,
)
from .kquant import QK_K, quantize_q4k, quantize_q6k
from .quantize import MODELS_ROOT, _guard_out

try:                                    # 内核加载失败也别让整个模块 import 崩
    from .kquant import rms_error  # noqa: F401
    HAS_KQUANT = True
except Exception:                       # noqa: BLE001
    HAS_KQUANT = False

# 我们自己实现了的档位（不是 llama-quantize 的全集）
_IMPLEMENTED = {"Q4_K_M", "Q4_K", "Q6_K"}

# 层名形如 `blk.17.ffn_down.weight`，用来实现"首尾层用更高精度"的规则
_LAYER_RE = re.compile(r"blk\.(\d+)\.")

_LOG_LINES = 200
_LINE_MAX = 400

# 量化阶段在进度里占多少（剩下的给落盘）。量化要走几百个张量的 matrix
# 运算，落盘只是 I/O，90/10 大致对得上两者的耗时比。
_QUANT_SPAN = 90.0


def _pair(done: float, total: float) -> str:
    """进度文案里的「已写/总长」。不到 1 GB 用 MB，免得小文件显示成 0.00 GB。"""
    if total >= 1024 ** 3:
        return f"{done / 1024 ** 3:.2f}/{total / 1024 ** 3:.2f} GB"
    return f"{done / 1024 ** 2:.1f}/{total / 1024 ** 2:.1f} MB"

# 档位 → quant-type 与界面说明。
# 数字来自 devtools/nl-quant-ab.md（126 张真实素材、同 prompt 的 A/B），
# 不是凭印象写的：Q4 的 caption 系统性短 12%，Q6 的长度反而和 Q8 齐平。
TARGETS: dict[str, dict[str, str]] = {
    "q4_k_m": {
        "flag": "Q4_K_M",
        "label": "Q4_K_M",
        "note": "最小最快（8B 约 4.8 GB）。实测描述比 Q8 短约 12%，"
                "材质/配色这类细节提得少——只要粗描述就选它。",
    },
    "q6_k": {
        "flag": "Q6_K",
        "label": "Q6_K",
        "note": "8B 约 6.3 GB。长度与 Q8 齐平，细节覆盖率反而略高，"
                "机时只要 Q8 的八成——下游要细节就选它。",
    },
    "q4_k": {
        "flag": "Q4_K",
        "label": "Q4_K（纯 4bit）",
        "note": "所有张量一律 Q4_K，不做 v 投影的 Q6_K 混合。"
                "只有在显存实在紧张、又想让体积和 Q4_K_M 相当时才选它。",
    },
}

def resolve_flag(target: str) -> str:
    """把页面上的档位换成内核认的类型名。

    预设档（q4_k_m / q6_k）从 TARGETS 取，顺带拿到说明文案；其余只放行
    我们真写了的 `_IMPLEMENTED`。**不能再像调外部工具那样转发任意类型名**——
    现在是自己的代码在算，不认识的档位没有实现，报错比产出坏文件好。
    """
    key = (target or "").strip().lower()
    if key in TARGETS:
        return TARGETS[key]["flag"]
    raw = (target or "").strip().upper()
    if raw in _IMPLEMENTED:
        return raw
    raise ValueError(
        f"暂不支持 {target!r}。自己实现的量化内核目前只有 "
        f"{' / '.join(sorted(_IMPLEMENTED))}（还没做 K-quant 的其他档）。")


def capability() -> tuple[bool, str]:
    """能不能跑 GGUF 量化。

    现在是自己算的（numpy），不需要 GPU、不需要 torch、更不需要装 llama.cpp。
    唯一的前提是源文件得是 F16/BF16 的 GGUF——safetensors 转换是另一件事。
    """
    try:
        import numpy  # noqa: F401
    except Exception:                       # noqa: BLE001 - 缺 numpy 基本跑不起来
        return False, "缺少 numpy，GGUF 量化跑不了"
    if not HAS_KQUANT:
        return False, "量化内核（tagger.kquant）没能加载"
    return True, ""


@dataclass
class GgufTask:
    source: Path
    out_file: Path
    target: str                      # q4_k_m / q6_k
    status: str = "running"          # running / done / error / canceled
    percent: float = 0.0
    stage: str = "准备"
    message: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancel: bool = False
    log: deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_LINES))

    def state(self) -> dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "source": str(self.source),
            "out_file": str(self.out_file),
            "target": self.target,
            "status": self.status,
            "percent": round(self.percent, 1),
            "stage": self.stage,
            "message": self.message,
            "elapsed": round(elapsed, 1),
            "log_tail": list(self.log),
        }


def default_out(source: Path, target: str) -> Path:
    """默认落在 models/nl/ 下，文件名带上档位。

    把档位写进文件名不是装饰：两份 GGUF 摆在同一目录、名字只差一个后缀位，
    换错文件跑一天才发现的事故成本太高。
    """
    return MODELS_ROOT / "nl" / f"{source.stem}-{target.upper()}.gguf"


class GgufQuant:
    """进程级单例。一次只跑一个——量化会吃满 CPU，并行没有收益。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: GgufTask | None = None

    def snapshot(self) -> dict[str, Any]:
        ok, reason = capability()
        with self._lock:
            task = self._task
        return {
            "available": ok,
            "reason": reason,
            # 不再有外部可执行文件；这个字段留给页面显示"谁在算"
            "engine": "内置 numpy（Q4_K / Q6_K）",
            "targets": [
                {"key": key, "flag": spec["flag"], "label": spec["label"],
                 "note": spec["note"]}
                for key, spec in TARGETS.items()
            ],
            "task": task.state() if task is not None else None,
        }

    def start(self, source: str, target: str,
              out_file: str | None = None) -> dict[str, Any]:
        # 参数错误先报：填错了路径却被告知"没装 llama-quantize"，
        # 会让人去装一个装了也用不上的东西。
        flag = resolve_flag(target)

        raw = (source or "").strip().strip('"')
        if not raw:
            raise ValueError("请先选择要量化的 GGUF 源文件（F16/BF16 档）")
        src = Path(raw)
        if not src.is_absolute():
            src = MODELS_ROOT.parent / src
        src = src.resolve()
        if not src.is_file():
            raise ValueError(f"源文件不存在：{src}")
        if src.suffix.lower() != ".gguf":
            raise ValueError("源文件必须是 .gguf；safetensors 要先转成 GGUF 才能量化")

        raw_out = (out_file or "").strip().strip('"')
        out = _guard_out(Path(raw_out) if raw_out else default_out(src, flag))
        if out.suffix.lower() != ".gguf":
            out = out.with_suffix(".gguf")
        if out == src:
            raise ValueError("输出文件不能和源文件相同，那会把 F16 权重覆盖掉")

        # 参数都合法了，才轮到"这台机器能不能跑"
        ok, reason = capability()
        if not ok:
            raise RuntimeError(reason)

        with self._lock:
            if self._task is not None and self._task.status == "running":
                return self._task.state()
            task = GgufTask(source=src, out_file=out, target=flag)
            self._task = task

        out.parent.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._run, args=(task,), daemon=True,
                         name=f"gguf-{out.stem}").start()
        return task.state()

    def cancel(self) -> dict[str, Any] | None:
        with self._lock:
            task = self._task
        if task is None:
            return None
        if task.status == "running":
            # 只是置标志：量化循环每处理完一个张量检查一次，自己退出并删掉
            # .part 文件。没有子进程可杀——taskkill 那套已经不需要了。
            task.cancel = True
            task.status = "canceled"
            task.stage = "已取消"
            task.message = "已取消；半成品文件已丢弃"
            task.finished_at = time.time()
        return task.state()

    def _run(self, task: GgufTask) -> None:
        try:
            ok, reason = capability()
            if not ok:
                raise RuntimeError(reason)
            task.stage = "读取 GGUF"
            stats = quantize_file(
                task.source, task.out_file, task.target,
                log=task.log.append,
                progress=lambda pct, stage: _set_progress(task, pct, stage),
                should_stop=lambda: task.cancel,
            )
            if task.cancel:
                task.finished_at = task.finished_at or time.time()
                return

            task.status = "done"
            task.percent = 100.0
            task.stage = "完成"
            size = task.out_file.stat().st_size / (1024 ** 3)
            skipped = stats.get("skipped", 0)
            extra = f"，{skipped} 个小张量保持原精度" if skipped else ""
            task.message = f"已生成 {task.out_file.name}（{size:.2f} GB{extra}）"
            task.log.append(task.message)
        except CancelledError:
            task.finished_at = task.finished_at or time.time()
        except Exception as exc:              # noqa: BLE001 - 失败必须让页面看见
            task.status = "error"
            task.stage = "失败"
            task.message = f"{type(exc).__name__}: {exc}"
            task.log.append(task.message)
        finally:
            task.finished_at = time.time()


def _set_progress(task: GgufTask, pct: float, stage: str) -> None:
    task.percent = max(task.percent, min(100.0, float(pct)))
    if stage:
        task.stage = stage


class CancelledError(RuntimeError):
    """用户在页面上按了取消。用异常而不是返回值，是为了少写层层 if。"""


def _plan(name: str, flag: str, n_layer: int | None = None) -> str:
    """这个张量该用哪种块格式；空串 = 原样保留。

    Q4_K_M 的规则照抄 llama.cpp（对着官方 Q4_K_M 文件逐个张量验过）：

    - `attn_v` 全部 Q6_K —— v 投影直接决定 attention 输出，4 bit 掉得最明显；
    - `ffn_down` 只在 `i < n/8`、`i >= 7n/8`、`i % 3 == 0` 时用 Q6_K ——
      首尾层离输入/输出最近，误差影响最大；
    - 其余 Q4_K。

    `n_layer` 为 None 表示不按层分配（单测里用），全部走 Q4_K。
    """
    if flag == "Q6_K":
        return "q6k"
    if flag == "Q4_K":
        return "q4k"
    if "v_proj" in name or "attn_v" in name:
        return "q6k"
    if name.endswith("output.weight"):
        # lm_head 直接产出 logits，4 bit 的噪声会原封不动体现在 token
        # 分布上。官方 Q4_K_M 把它单独留在 Q6_K，这一档必须跟上。
        return "q6k"
    if "ffn_down" in name and n_layer:
        layer = _layer_of(name)
        if layer >= 0 and (layer < n_layer // 8
                           or layer >= 7 * n_layer // 8
                           or layer % 3 == 0):
            return "q6k"
    return "q4k"


def _layer_of(name: str) -> int:
    match = _LAYER_RE.search(name)
    return int(match.group(1)) if match else -1


def _raw_nbytes(info: Any) -> int:
    """按原始类型算一个张量占多少字节（用于原样拷贝）。"""
    n = info.n_elements
    if info.type == GGML_TYPE_F32:
        return n * 4
    if info.type in (GGML_TYPE_F16, GGML_TYPE_BF16):
        return n * 2
    raise ValueError(f"张量 {info.name} 的类型 {info.type} 不支持（源要 F16/BF16/F32）")


def quantize_file(src: Path, out: Path, flag: str,
                  log: Callable[[str], None],
                  progress: Callable[[float, str], None],
                  should_stop: Callable[[], bool]) -> dict[str, int]:
    """把 F16/BF16 的 GGUF 压成目标档位，写到 out。

    先写 `.part` 再改名：中途失败/取消时，目录里不会出现一个看起来能用、
    实际半截的 .gguf——那种文件拿去加载，报的错会离真正原因很远。

    `progress` 的口径是整个文件：0~90 是逐张量量化，90~100 是落盘，
    落盘阶段的 stage 里带"已写/总长"，调用方直接透给界面即可。
    """
    reader = Reader(src)
    if not reader.tensors:
        raise ValueError("这个 GGUF 里没有张量，可能选错了文件")

    tmp = out.with_suffix(out.suffix + ".part")
    total = len(reader.tensors)
    out_tensors: list[tuple[Any, bytes]] = []
    skipped = 0
    layers = [_layer_of(t.name) for t in reader.tensors]
    n_layer = max(layers) + 1 if layers and max(layers) >= 0 else 0
    try:
        for index, info in enumerate(reader.tensors, start=1):
            if should_stop():
                raise CancelledError()
            kind = _plan(info.name, flag, n_layer)
            if not kind or info.n_elements % QK_K != 0 or len(info.dims) == 1:
                # **1D 张量一律不量化**：norm/bias 是逐层的缩放/偏移量，
                # 误差会顺着残差流一路累积。llama.cpp 也这么做——官方
                # Q6_K 里 145 个 F32 全是 1D 张量（含 4096 元素的 norm，
                # 它虽然能被 256 整除，但不该压）。
                out_tensors.append((info, reader.tensor_bytes(info, _raw_nbytes(info))))
                skipped += 1
                log(f"跳过 {info.name}（{info.n_elements} 个元素，保持原精度）")
            else:
                x = reader.read_float(info)
                if kind == "q6k":
                    _, body = quantize_q6k(x)
                    ttype = GGML_TYPE_Q6_K
                else:
                    _, _, body = quantize_q4k(x)
                    ttype = GGML_TYPE_Q4_K
                out_tensors.append((TensorInfo(info.name, info.dims, ttype, 0), body))
                log(f"{info.name} → {'Q6_K' if kind == 'q6k' else 'Q4_K'}")
            progress(index * _QUANT_SPAN / total, f"量化中 {index}/{total}")

        # 落盘单独占 10%：量化阶段先走到 90，剩下的由 write_gguf 每写完一个
        # 张量回调一次。以前两段共用 100%，几个 GB 写下去时进度条一动不动。
        progress(_QUANT_SPAN, "写入文件")

        def _report_write(done: int, total_bytes: int) -> None:
            # 落盘也能取消：按完取消还要再写几 GB 才停，等于按钮失灵。
            if should_stop():
                raise CancelledError()
            frac = done / total_bytes if total_bytes else 1.0
            progress(_QUANT_SPAN + (100.0 - _QUANT_SPAN) * frac,
                     f"写入文件 {_pair(done, total_bytes)}")

        write_gguf(tmp, reader.metadata_bytes(), reader.kv_count,
                   out_tensors, reader.alignment, on_write=_report_write)
        tmp.replace(out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        reader.close()      # mmap 要主动放，否则 Windows 上句柄会一直挂着

    return {"tensors": len(out_tensors), "skipped": skipped}


GGUF = GgufQuant()
