"""打标链路的 GPU 侧最小基准：解码 / 预处理 / 前向各占多少，各档位能省多少。

为什么要先做基准而不是直接改代码
--------------------------------
"把 nvJPEG 接进来能省 10%" 这类判断，一旦建立在猜测上，后面所有决策都会
跟着错。这个脚本的唯一目的就是产出可重复的数字：同一台机器、同一批图、
固定迭代次数，把下面四件事分别量出来。

    1. 解码：PIL(CPU) / torchvision CPU / torchvision CUDA(nvJPEG) 各多少 ms
    2. 预处理：resize + normalize 在 CPU 还是 GPU 上做
    3. 前向：fp32(默认) / fp32+TF32 / bf16 / autocast，batch 1..N
    4. CPU 入队 vs GPU 执行：判断当前是 CPU 跟不上（launch 太多）还是 GPU 打满

第四项直接回答"launch 数高不高、有没有被 CPU 拖住"：前向调用是异步的，
`model(x)` 返回时 GPU 可能才刚开始算。所以分别记
    enqueue = 调用 model(x) 花掉的墙钟（CPU 侧，含 Python 逐层 dispatch）
    total   = synchronize 之后的墙钟（GPU 真正算完）
二者比值就是 CPU 占比。enqueue ≈ total 说明 CPU 是瓶颈，加 batch 会明显变好；
enqueue << total 说明 GPU 打满，只有换模型/降精度才有用。

用法
----
    env\\python\\python.exe devtools\\bench_tag_gpu.py                # 全部跑一遍
    env\\python\\python.exe devtools\\bench_tag_gpu.py --quick        # 少迭代，冒烟用
    env\\python\\python.exe devtools\\bench_tag_gpu.py --images 64 --batches 1 4 8
    env\\python\\python.exe devtools\\bench_tag_gpu.py --skip-decode  # 只测前向

依赖：torch + torchvision（CUDA 版）+ timm + Pillow。缺哪个会跳过对应段落，
不会整个脚本失败——但那样得到的结论也不完整，看输出里的 "skip" 提示。
"""

from __future__ import annotations

import argparse
import io
import statistics
import sys
import time
from pathlib import Path

try:
    import torch
except ImportError:  # pragma: no cover - 提示比 traceback 有用
    sys.exit("需要 torch：env\\python\\python.exe -m pip install torch torchvision")

from PIL import Image

# 控制台可能是 GBK（中文 Windows 默认）。一个 µ 之类的字符就能让整段输出
# 抛 UnicodeEncodeError，把测好的数字全吞掉——宁可替换成 ? 也不要崩。
try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass


# ------------------------------------------------------------------ 工具


def _sync() -> None:
    if torch.cuda.is_available():
        torch.cuda.synchronize()


def _timeit(fn, *, warmup: int, iters: int) -> tuple[float, float]:
    """跑 warmup 次预热，再计 iters 次，返回 (中位数 ms, 均值 ms)。"""
    for _ in range(warmup):
        fn()
    _sync()

    samples: list[float] = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        _sync()
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    return samples[len(samples) // 2], sum(samples) / len(samples)


def _make_images(count: int, size: int, quality: int = 95) -> list[bytes]:
    """造一批"看起来像真实素材"的 JPEG：随机噪点 + 渐变。

    纯色图会让 JPEG 解码异常快（RLE 友好），测出来偏乐观；
    噪点图更接近实际素材，属于保守估计。
    """
    import random

    rng = random.Random(20261102)
    out: list[bytes] = []
    for _ in range(count):
        buf = io.BytesIO()
        im = Image.new("RGB", (size, size))
        px = im.load()
        # 分块填色 + 噪声：比逐像素快，又保证足够的熵
        for by in range(0, size, 8):
            for bx in range(0, size, 8):
                base = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
                for y in range(by, min(by + 8, size)):
                    for x in range(bx, min(bx + 8, size)):
                        n = rng.randrange(-12, 12)
                        px[x, y] = (max(0, min(255, base[0] + n)),
                                    max(0, min(255, base[1] + n)),
                                    max(0, min(255, base[2] + n)))
        im.save(buf, "JPEG", quality=quality)
        out.append(buf.getvalue())
    return out


def _fmt(ms: float) -> str:
    return f"{ms:8.2f}"


def _row(label: str, ms: float, ref: float | None = None) -> str:
    if ref and ref > 0:
        speed = f"{ref / ms:5.2f}x"
        save = f"{(1 - ms / ref) * 100:+6.1f}%"
    else:
        speed, save = "    -", "     -"
    return f"  {label:<34}{_fmt(ms)} ms   {speed}   {save}"


# ------------------------------------------------------------------ 1. 解码


def bench_decode(images: list[bytes], size: int, *, warmup: int, iters: int) -> dict:
    """CPU 解码 vs GPU 解码（nvJPEG）。这是"预处理上不上 GPU"的唯一依据。"""
    from PIL import Image as PILImage

    results: dict[str, float] = {}
    try:
        import torchvision.io as tvio
    except ImportError:
        print("  [skip] 没有 torchvision，跳过解码对比")
        return results

    def pil_decode_one(idx: int) -> None:
        raw = images[idx % len(images)]
        im = PILImage.open(io.BytesIO(raw)).convert("RGB")
        im.resize((size, size), PILImage.BILINEAR)

    results["PIL 解码+缩放 (CPU)"] = _timeit(
        lambda: pil_decode_one(0), warmup=warmup, iters=iters)[0]

    cpu_batch = torch.frombuffer(bytearray(images[0]), dtype=torch.uint8)
    try:
        results["torchvision 解码 (CPU)"] = _timeit(
            lambda: tvio.decode_jpeg(cpu_batch), warmup=warmup, iters=iters)[0]
    except Exception as exc:  # noqa: BLE001
        print(f"  [skip] torchvision CPU 解码不可用: {type(exc).__name__}: {exc}")

    if torch.cuda.is_available():
        try:
            # 注意：输入必须是 **CPU** 上的 uint8 张量，解码由 device="cuda"
            # 决定在卡上做（内部走 nvJPEG）。传 CUDA 张量会直接报
            # "Input tensor must be a CPU tensor" —— 这是 torchvision 的接口约定。
            results["nvJPEG 解码 (CUDA)"] = _timeit(
                lambda: tvio.decode_jpeg(cpu_batch, device="cuda"),
                warmup=warmup, iters=iters)[0]

            def full_gpu(idx: int) -> None:
                raw = torch.frombuffer(bytearray(images[idx % len(images)]),
                                       dtype=torch.uint8)
                img = tvio.decode_jpeg(raw, device="cuda")
                # GPU 侧 resize + 归一化，模拟"预处理全在卡上"
                img = img.permute(2, 0, 1).unsqueeze(0).float().div_(255.0)
                torch.nn.functional.interpolate(img, size=(size, size),
                                                mode="bilinear", align_corners=False)

            results["nvJPEG+缩放 全 GPU"] = _timeit(
                lambda: full_gpu(0), warmup=warmup, iters=iters)[0]
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] CUDA 解码不可用: {type(exc).__name__}: {exc}")

    return results


# ------------------------------------------------------------------ 2. 前向


def _build_model(arch: str, img_size: int, classes: int, device: str, dtype,
                 checkpoint: str | None = None):
    import timm

    model = timm.create_model(arch, pretrained=False, num_classes=classes,
                              img_size=img_size)
    if checkpoint:
        # 速度只取决于结构与形状，与权重数值无关；给 checkpoint 是为了
        # 让"测速度"和"测精度"能用同一份权重，省得对不上号。
        from safetensors.torch import load_file

        weights = load_file(str(Path(checkpoint) / "model.safetensors"))
        missing, unexpected = model.load_state_dict(weights, strict=False)
        non_head = [k for k in missing if not k.startswith("head")]
        if non_head:
            raise SystemExit(f"权重与结构对不上：缺 {len(non_head)} 个非 head 键")
    return model.eval().to(device=device, dtype=dtype)


def _merge_rounds(rounds: list[list[dict]]) -> list[dict]:
    """多轮结果按 label 取中位数。

    为什么必须多轮：GPU 的时钟和功耗墙会让同一配置在不同时刻差出 2 倍，
    不做多轮的话很容易把"这一轮刚好降频了"当成"这个精度更慢"。
    """
    by_label: dict[str, list[dict]] = {}
    for rows in rounds:
        for row in rows:
            by_label.setdefault(row["label"], []).append(row)
    out: list[dict] = []
    for label, group in by_label.items():
        out.append({
            **group[0],
            "ms": statistics.median(g["ms"] for g in group),
            "enqueue": statistics.median(g["enqueue"] for g in group),
        })
    return out


def bench_forward(arch: str, img_size: int, classes: int, batches: list[int],
                  *, warmup: int, iters: int,
                  checkpoint: str | None = None) -> list[dict]:
    """前向：精度档位 × batch。同时记录 CPU 入队时间来判断是否 launch-bound。"""
    if not torch.cuda.is_available():
        print("  [skip] 没有 CUDA，跳过前向基准")
        return []

    device = "cuda"
    rows: list[dict] = []

    def run(label: str, batch: int, dtype, tf32: bool, graph: bool) -> None:
        torch.backends.cuda.matmul.allow_tf32 = tf32
        torch.backends.cudnn.allow_tf32 = tf32
        try:
            model = _build_model(arch, img_size, classes, device, dtype,
                                 checkpoint=checkpoint)
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] {label}: {type(exc).__name__}: {exc}")
            return

        x = torch.randn(batch, 3, img_size, img_size, device=device, dtype=dtype)
        enqueue: list[float] = []

        if graph:
            # CUDA Graph：固定形状 + 静态输入，把 N 次 kernel launch 压成 1 次图的回放。
            # 只有在 batch/尺寸固定、且没有动态控制流时成立——我们的打标正是这种。
            try:
                s = torch.cuda.Stream()
                s.wait_stream(torch.cuda.current_stream())
                with torch.cuda.stream(s):
                    for _ in range(3):
                        model(x)
                torch.cuda.current_stream().wait_stream(s)
                g = torch.cuda.CUDAGraph()
                with torch.cuda.graph(g):
                    static_out = model(x)

                def call() -> None:
                    g.replay()

                _timeit(call, warmup=warmup, iters=iters)  # 预热图
                ms_median, _ = _timeit(call, warmup=0, iters=iters)
                rows.append({"label": f"{label} batch={batch} [cudagraph]",
                             "ms": ms_median, "batch": batch,
                             "enqueue": ms_median, "graph": True})
                del static_out, g
                torch.cuda.empty_cache()
                return
            except Exception as exc:  # noqa: BLE001
                print(f"  [skip] cudagraph {label} batch={batch}: "
                      f"{type(exc).__name__}: {exc}")
                torch.cuda.empty_cache()
                return

        def call() -> None:
            t0 = time.perf_counter()
            with torch.no_grad():
                model(x)
            enqueue.append((time.perf_counter() - t0) * 1000.0)

        try:
            ms_median, _ = _timeit(call, warmup=warmup, iters=iters)
        except torch.cuda.OutOfMemoryError:
            # 16G 卡上 fp32 batch=8 + 高分辨率很容易撞顶。跳过而不是中断整个基准，
            # 否则一台小卡机器什么都测不出来。
            print(f"  [skip] {label} batch={batch}: 显存不足")
            del model, x
            torch.cuda.empty_cache()
            return
        rows.append({
            "label": f"{label} batch={batch}",
            "ms": ms_median,
            "batch": batch,
            "enqueue": statistics.median(enqueue[-iters:]) if enqueue else 0.0,
            "graph": False,
        })
        del model, x
        torch.cuda.empty_cache()

    for batch in batches:
        run("fp32 (tf32 off)", batch, torch.float32, False, False)
        run("fp32 (tf32 on) ", batch, torch.float32, True, False)
        run("bf16           ", batch, torch.bfloat16, True, False)
    # CUDA Graph 只值得在 batch=1 与最大 batch 上各测一次
    for batch in (1, max(batches)):
        run("bf16           ", batch, torch.bfloat16, True, True)

    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = True
    return rows


# ------------------------------------------------------------------ 2b. 发射次数


def bench_launch_count(arch: str, img_size: int, classes: int,
                       batches: list[int]) -> None:
    """数一次前向到底发了多少 kernel —— 直接回答 launch 数高不高。

    GPU 上每次 kernel 发射有固定成本（约 2~5 µs，含驱动/硬件队列）。
    当单个 kernel 的 GPU 时间只有几十 µs 时，发射成本就占掉可观比例；
    更麻烦的是 CPU 侧要一口气把上千个算子排进去，Python 逐层 dispatch
    的开销会直接变成 GPU 的空转等待。
    """
    if not torch.cuda.is_available():
        return
    from torch.profiler import ProfilerActivity, profile

    for batch in batches:
        try:
            model = _build_model(arch, img_size, classes, "cuda", torch.bfloat16)
            x = torch.randn(batch, 3, img_size, img_size, device="cuda",
                            dtype=torch.bfloat16)
            with torch.no_grad():
                model(x)
            torch.cuda.synchronize()

            with profile(activities=[ProfilerActivity.CPU,
                                     ProfilerActivity.CUDA]) as prof:
                with torch.no_grad():
                    model(x)
                torch.cuda.synchronize()

            events = prof.key_averages()
            kernels = sum(e.count for e in events if e.self_device_time_total > 0)
            cuda_us = sum(e.self_device_time_total for e in events)
            cpu_us = sum(e.self_cpu_time_total for e in events)
            print(f"  batch={batch}: 算子 dispatch {len(events)} 次 → "
                  f"kernel 发射 {kernels} 次")
            print(f"           GPU 忙 {cuda_us / 1000:6.2f} ms   "
                  f"CPU 忙 {cpu_us / 1000:6.2f} ms   "
                  f"平均每 kernel {cuda_us / max(kernels, 1):5.1f} us GPU 时间")
            print(f"           纯发射成本（按 3us/次）约 "
                  f"{kernels * 3 / 1000:5.1f} ms —— 这就是 CUDA Graph 能压掉的部分")
            del model, x
            torch.cuda.empty_cache()
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] batch={batch}: {type(exc).__name__}: {exc}")
            torch.cuda.empty_cache()
    print()


# ------------------------------------------------------------------ 3. 端到端


def bench_end_to_end(images: list[bytes], decode_ms: float, fwd_ms_per_img: float,
                     size: int) -> None:
    """把两段拼起来的算术：解码在 CPU 上时，它和前向是串行的。"""
    if decode_ms <= 0 or fwd_ms_per_img <= 0:
        return
    total = decode_ms + fwd_ms_per_img
    print(f"  当前串行链路（解码在 CPU）        {total:8.2f} ms/张"
          f"   解码占 {decode_ms / total * 100:5.1f}%")
    for gpu_decode in (2.0, 4.0, 6.0):
        print(f"  假如 GPU 解码只要 {gpu_decode:>3.1f} ms         "
              f"{fwd_ms_per_img + gpu_decode:8.2f} ms/张"
              f"   省 {(1 - (fwd_ms_per_img + gpu_decode) / total) * 100:5.1f}%")


# ------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description="打标链路 GPU 侧基准")
    ap.add_argument("--images", type=int, default=32, help="基准图数量")
    ap.add_argument("--image-size", type=int, default=1024, dest="image_size",
                    help="基准图原始边长（默认 1024，接近真实素材）")
    ap.add_argument("--size", type=int, default=448, help="预处理后的输入边长")
    ap.add_argument("--arch", default="eva02_large_patch14_448",
                    help="timm 架构名（默认与 wd-eva02 一致）")
    ap.add_argument("--classes", type=int, default=16473, help="分类头数量")
    ap.add_argument("--batches", type=int, nargs="+", default=[1, 4, 8])
    ap.add_argument("--checkpoint", default=None,
                    help="WD 权重目录（含 model.safetensors）；不给就用随机权重——\n速度与权重无关，给它是为了让速度和精度用同一份权重")
    ap.add_argument("--rounds", type=int, default=1,
                    help="前向整套配置重复几轮取中位（抗降频，建议 3）")
    ap.add_argument("--quick", action="store_true", help="少迭代，冒烟用")
    ap.add_argument("--skip-decode", action="store_true", dest="skip_decode")
    ap.add_argument("--skip-forward", action="store_true", dest="skip_forward")
    args = ap.parse_args()

    warmup = 2 if args.quick else 5
    iters = 5 if args.quick else 20
    if args.rounds > 1:
        # 多轮的意义在抗降频，单轮迭代次数可以少一点，总时长才不至于爆掉
        warmup, iters = max(warmup, 8), max(iters, 30)

    print("=" * 78)
    print(" 打标链路 GPU 基准")
    print("=" * 78)
    print(f" torch      : {torch.__version__}")
    print(f" cuda       : {torch.version.cuda}")
    print(f" available  : {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        print(f" device     : {props.name}  {props.total_memory / 1024**3:.1f} GB"
              f"  cc={props.major}.{props.minor}")
    print(f" tf32 默认  : matmul={torch.backends.cuda.matmul.allow_tf32}"
          f"  cudnn={torch.backends.cudnn.allow_tf32}")
    print(f" 迭代       : warmup={warmup}  iters={iters}")
    print()

    decode_ms = 0.0
    decode_results: dict[str, float] = {}
    if not args.skip_decode:
        print("-" * 78)
        print(" 1) 解码与预处理（每张，含缩放）")
        print("-" * 78)
        images = _make_images(args.images, args.image_size)
        decode_results = bench_decode(images, args.size, warmup=warmup, iters=iters)
        ref = decode_results.get("PIL 解码+缩放 (CPU)")
        for label, ms in decode_results.items():
            print(_row(label, ms, ref if label != "PIL 解码+缩放 (CPU)" else None))
        decode_ms = ref or 0.0
        print()

    fwd_rows: list[dict] = []
    if not args.skip_forward:
        print("-" * 78)
        print(" 2) 前向（每张 ms = 单次前向 / batch）")
        print("-" * 78)
        fwd_rounds = [
            bench_forward(args.arch, args.size, args.classes, args.batches,
                          warmup=warmup, iters=iters, checkpoint=args.checkpoint)
            for _ in range(max(1, args.rounds))
        ]
        fwd_rows = _merge_rounds(fwd_rounds) if len(fwd_rounds) > 1 else fwd_rounds[0]
        base = next((r["ms"] / r["batch"] for r in fwd_rows
                     if r["label"].startswith("fp32 (tf32 off) batch=1")), 0.0)
        for r in fwd_rows:
            per_img = r["ms"] / r["batch"]
            print(_row(r["label"], per_img, base))
        print()
        print("   CPU 入队 vs GPU 执行（判断是不是 launch-bound）：")
        for r in fwd_rows:
            if r["graph"]:
                continue
            share = (r["enqueue"] / r["ms"] * 100) if r["ms"] else 0.0
            verdict = ("CPU 拖住（值得上 CUDA Graph / 加 batch）" if share > 60
                       else "GPU 打满（只能靠降精度/换模型）" if share < 20
                       else "两者接近（加 batch 会有效）")
            print(f"     {r['label']:<32} 入队 {r['enqueue']:6.2f} ms / "
                  f"总 {r['ms']:6.2f} ms  → {share:4.1f}%  {verdict}")
        print()

        print("-" * 78)
        print(" 2b) 一次前向的 kernel 发射次数（launch 开销有多大）")
        print("-" * 78)
        bench_launch_count(args.arch, args.size, args.classes,
                           [1, max(args.batches)])

        if decode_ms > 0:
            best = min((r for r in fwd_rows if not r["graph"]),
                       key=lambda r: r["ms"] / r["batch"], default=None)
            if best:
                print("-" * 78)
                print(" 3) 端到端估算")
                print("-" * 78)
                bench_end_to_end(images if not args.skip_decode else [],
                                 decode_ms, best["ms"] / best["batch"], args.size)
                print()

    print("=" * 78)
    print(" 结论怎么读")
    print("=" * 78)
    print("  · 解码那一节的差值就是 nvJPEG 的收益上限；除非它占总时长 >20%，")
    print("    否则接 GPU 解码不值得（要处理 fallback、显存、图像一致性）。")
    print("  · 前向那节：tf32 on 一般 1.5~3x，bf16 再 1~1.5x，但都会小幅改变 tag 概率，")
    print("    上生产前必须用同一批图对比 fp32 的 tag 集合差异。")
    print("  · CPU 入队占比 >60% 才考虑 CUDA Graph；GPU 打满时它一点用都没有。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
