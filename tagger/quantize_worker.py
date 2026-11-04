"""W4A16 量化子进程（由 tagger.quantize 拉起，一般不直接调用）。

只做一件事：把 bf16 权重压成 compressed-tensors 的 W4A16，写进 --out。
父进程负责状态、日志、取消与覆盖清单，这里只管算。

进度契约：往 stdout 打 `@@PROGRESS <百分比> <阶段>`，其余输出都是日志。
llm-compressor 没有结构化回调，中段（校准 + 逐层更新）是最长也最安静的
一段，所以那条阶段文案要写人话——页面上同时显示已用时间，让人知道没死。

为什么只量化 Linear 且忽略视觉塔：
1. 视觉塔的参数占比不小，但打标时它的作用是把图变成 token，4bit 化对
   细微纹理（而这批素材正是要训色感与材质的）损失最大；
2. AWQ/GPTQ 的校准数据是纯文本，视觉塔没有对应的校准信号，量化它等于
   用错分布去拟合。
于是忽略 visual/vision/merger，只压语言部分，显存仍能砍掉一大半。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from pathlib import Path


def _emit(percent: float, stage: str) -> None:
    print(f"@@PROGRESS {percent:.0f} {stage}", flush=True)


def _stdout_utf8() -> None:
    """管道是父进程按 utf-8 读的，这里必须统一，否则 Windows 上中文日志会
    让父进程解码失败（readline 抛 UnicodeDecodeError，整个任务被当成崩溃）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _ignore_patterns() -> list[str]:
    return ["lm_head", "re:.*visual.*", "re:.*vision.*", "re:.*merger.*"]


def _import_oneshot():
    """llm-compressor 换过两次导入路径，按新旧顺序探。"""
    try:
        from llmcompressor import oneshot          # >= 0.5
        return oneshot
    except ImportError:
        from llmcompressor.transformers import oneshot   # <= 0.4
        return oneshot


def _load_model_class(is_vlm: bool):
    """多模态模型的 Auto 类在不同 transformers 版本里名字不同。"""
    import transformers

    if not is_vlm:
        return transformers.AutoModelForCausalLM
    for name in ("AutoModelForImageTextToText", "AutoModelForVision2Seq"):
        cls = getattr(transformers, name, None)
        if cls is not None:
            return cls
    raise RuntimeError(
        "当前 transformers 版本没有多模态 Auto 类（需要 >= 4.45）："
        "pip install -U transformers")


def _calibration_set(samples: int):
    """官方示例用的通用文本校准集。

    为什么不用素材本身：量化发生在打标之前，那时还没有产物；而校准要的是
    "语言分布的多样性"，不是"这批图长什么样"。ultrachat 覆盖长句、列表、
    对话，比几百条 tag 更能代表模型平时的输入。
    """
    from datasets import load_dataset

    ds = load_dataset("HuggingFaceH4/ultrachat_200k", split=f"train_sft[:{samples}]")

    def first_turn(row):
        messages = row.get("messages") or []
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                text = str(msg.get("content") or "").strip()
                if text:
                    return {"text": text}
        return {"text": ""}

    ds = ds.map(first_turn, remove_columns=ds.column_names)
    ds = ds.filter(lambda row: bool(row["text"]))
    return ds


def _heartbeat(stop: threading.Event, every_s: int = 60) -> None:
    """长阶段的"我还活着"信号。

    只打日志不打进度：假造百分比比不动更糟——用户会以为快好了。
    """
    started = time.time()
    while not stop.wait(every_s):
        minutes = int((time.time() - started) // 60)
        print(f"[quantize] 仍在进行（已 {minutes} 分钟），这一段不会输出逐条进度",
              flush=True)


def _verify(out: Path) -> None:
    """产物自检：没写进 quantization_config 就说明没量化成功。

    漏这一步的表现是"量化跑完了、vLLM 仍按 bf16 加载"，显存一点没省，
    而页面上一切正常。
    """
    cfg_path = out / "config.json"
    if not cfg_path.is_file():
        raise RuntimeError(f"量化产物不完整：{cfg_path} 不存在")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    quant = cfg.get("quantization_config") or {}
    method = str(quant.get("quant_method") or "")
    if not method:
        raise RuntimeError(
            "量化产物里没有 quantization_config：这份权重仍会被当成 bf16 加载")
    print(f"[quantize] 产物量化方式：{method}", flush=True)


def main(argv: list[str] | None = None) -> int:
    _stdout_utf8()
    parser = argparse.ArgumentParser(description="W4A16 量化（供 tagger 调用）")
    parser.add_argument("--model", required=True, help="原始 bf16 权重路径或 HF 仓库")
    parser.add_argument("--out", required=True, help="产物目录（必须在 models/ 之内）")
    parser.add_argument("--method", default="gptq", choices=("gptq", "awq"))
    parser.add_argument("--samples", type=int, default=256)
    parser.add_argument("--max-seq-len", type=int, default=2048, dest="max_seq_len")
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat, args=(stop,), daemon=True)
    beat.start()
    try:
        _emit(2, "检查运行环境")
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError(
                "量化需要 CUDA：当前 torch 看不到 GPU。先确认 nvidia-smi 正常，"
                "再确认装的是 CUDA 版 torch（CPU 版会走到这里）。")
        print(f"[quantize] GPU: {torch.cuda.get_device_name(0)}", flush=True)

        _emit(6, "加载 transformers / llm-compressor")
        from transformers import AutoConfig, AutoProcessor

        _emit(10, "读取模型配置")
        config = AutoConfig.from_pretrained(args.model, trust_remote_code=True)
        is_vlm = bool(getattr(config, "vision_config", None))
        model_cls = _load_model_class(is_vlm)
        processor = None
        if is_vlm:
            processor = AutoProcessor.from_pretrained(
                args.model, trust_remote_code=True)

        _emit(15, "加载 bf16 权重（最吃显存的一步）")
        model = model_cls.from_pretrained(
            args.model,
            torch_dtype=torch.bfloat16,
            device_map="auto",
            trust_remote_code=True,
            low_cpu_mem_usage=True,
        )

        _emit(40, "准备校准数据")
        ds = _calibration_set(args.samples)
        if len(ds) == 0:
            raise RuntimeError("校准数据集为空，检查网络或 HF 缓存")
        print(f"[quantize] 校准样本 {len(ds)} 条", flush=True)

        # 方案名与修饰器按方法分。都输出 compressed-tensors 的 W4A16：
        # 权重 4bit、激活 16bit，不需要 4bit Tensor Core。
        _emit(48, "开始量化（校准 + 逐层更新，最久的一段）")
        if args.method == "awq":
            try:
                from llmcompressor.modifiers.awq import AWQModifier as Modifier
            except ImportError:                 # 老版本路径
                from llmcompressor.modifiers.quantization import AWQModifier as Modifier
        else:
            from llmcompressor.modifiers.quantization import GPTQModifier as Modifier

        recipe = Modifier(scheme="W4A16", targets="Linear",
                          ignore=_ignore_patterns())

        oneshot = _import_oneshot()
        kwargs = {
            "model": model,
            "dataset": ds,
            "recipe": recipe,
            "output_dir": str(out),
            "max_seq_length": args.max_seq_len,
            "num_calibration_samples": len(ds),
        }
        if processor is not None:
            kwargs["processor"] = processor
        try:
            oneshot(**kwargs)
        except TypeError as exc:
            # 旧版 oneshot 没有 processor 参数：去掉再跑一次。
            if "processor" not in kwargs or "processor" not in str(exc):
                raise
            kwargs.pop("processor")
            oneshot(**kwargs)

        _emit(92, "保存 tokenizer / processor")
        if processor is not None:
            processor.save_pretrained(str(out))
        else:
            try:
                from transformers import AutoTokenizer
                AutoTokenizer.from_pretrained(args.model, trust_remote_code=True) \
                    .save_pretrained(str(out))
            except Exception as exc:        # noqa: BLE001 - 只有纯文本模型走这里
                print(f"[quantize] tokenizer 保存失败（vLLM 可能用不上）：{exc}",
                      flush=True)

        _emit(96, "校验产物")
        _verify(out)
        marker = {
            "base_model": args.model,
            "method": args.method,
            "format": "compressed-tensors",
            "scheme": "W4A16",
            "samples": len(ds),
            "created_at": int(time.time()),
        }
        (out / "quantized.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        _emit(100, "完成")
        return 0
    except Exception as exc:                  # noqa: BLE001 - 原因要原样给页面
        print(f"[quantize] 失败：{type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        return 1
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
