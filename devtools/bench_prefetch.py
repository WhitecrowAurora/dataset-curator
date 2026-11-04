"""解码预取值不值：同一批图，`prefetch=False` 与 `True` 各跑一遍。

为什么需要这个脚本
------------------
预取的收益不是"解码快了多少"，而是"解码有没有被前向盖住"。单看端到端
ms/张 只能得到一个总数，看不出到底是重叠生效了，还是恰好今天机器空闲。
所以这个脚本同时打印两样东西：

    ms/张          A/B 交替多轮取的均值（交替是为了抵消显存/主机的状态漂移）
    last_timings   total / decode / forward 三段累计

判据（写进 perf-baseline.md §13 的那条）
----------------------------------------
`_predict_many` 预取时，解码在前向进行中并行跑，所以

    decode + forward  >  total       ← 重叠生效
    decode + forward ≈  total        ← 还是串行（多半是条件没满足）

看到前者不要以为计时器坏了，那正是要的结果。

另外每次都做一次**逐位一致**比较：两档跑出来的 tag 列表（名字 + 概率）
直接 `==`。像素路径没变，只挪了时间位置，任何不等都说明有东西被共享了。

用法
----
    env\\python\\python.exe devtools\\bench_prefetch.py
    env\\python\\python.exe devtools\\bench_prefetch.py --images 24 --batch 4
    env\\python\\python.exe devtools\\bench_prefetch.py --dir H:\\some\\pngs --precision bf16
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger.cpu_tagger import build_backend, resolve_device  # noqa: E402

# 默认拿仓库外的一批真实素材；不存在就退回到调用者指定的 --dir（见 main）。
DEFAULT_DIR = Path(r"H:\lulynx-trainer\sucai\3_amu")
EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def collect(folder: Path, limit: int) -> list[str]:
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in EXTS)
    return [str(p) for p in files[:limit]]


def run_once(model_dir, device, precision, batch, prefetch, images) -> tuple:
    backend = build_backend(
        "wd-eva02", model_dir=model_dir, device=device, precision=precision,
        batch_size=batch, prefetch=prefetch, verbose=False,
    )
    backend.load()
    # warmup 必须跑满一批：CUDA Graph 是在第一次前向时捕获的，用单张热身会让
    # 抓图那一次的开销落进正式计时里。单批的解码缓存也在这里建好。
    backend.predict_batch(images[:max(1, batch)])
    t0 = time.perf_counter()
    preds = backend.predict_batch(images)
    ms = (time.perf_counter() - t0) * 1000 / len(images)
    return preds, ms, dict(backend.last_timings)


def main() -> int:
    ap = argparse.ArgumentParser(description="解码预取 A/B 基准")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="图片目录")
    ap.add_argument("--images", type=int, default=24, help="用前 N 张")
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--precision", default="bf16", choices=["fp32", "tf32", "bf16"],
                    help="bf16 下前向最短，预取收益最容易看出来")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--model-dir", default=str(REPO / "models" / "wd-eva02"),
                    dest="model_dir")
    ap.add_argument("--rounds", type=int, default=2, help="A/B 交替轮数，取均值")
    args = ap.parse_args()

    folder = Path(args.dir)
    if not folder.is_dir():
        print(f"没有目录 {folder}，用 --dir 指一个图片目录", file=sys.stderr)
        return 2
    images = collect(folder, args.images)
    if len(images) < 2:
        print(f"{folder} 里可用图片不足 2 张", file=sys.stderr)
        return 2

    device = resolve_device(args.device)
    print(f"设备={device} 精度={args.precision} batch={args.batch} "
          f"图片={len(images)} 轮数={args.rounds}", flush=True)

    res: dict[bool, list] = {False: [], True: []}
    # 每轮的比值也要打：整机负载漂移会把两档一起抬起来，只看均值会以为没收益。
    ratios: list[float] = []
    for _ in range(args.rounds):
        pair: dict[bool, float] = {}
        for prefetch in (False, True):
            preds, ms, tim = run_once(args.model_dir, device, args.precision,
                                      args.batch, prefetch, images)
            res[prefetch].append((preds, ms, tim))
            pair[prefetch] = ms
            print(f"  prefetch={prefetch!s:<5} {ms:7.1f} ms/张", flush=True)
        ratios.append(pair[False] / pair[True])
        print(f"  → 本轮 {ratios[-1]:.2f}×", flush=True)

    print()
    for prefetch in (False, True):
        ms = sum(r[1] for r in res[prefetch]) / len(res[prefetch])
        print(f"prefetch={prefetch!s:<5} {ms:7.1f} ms/张   "
              f"last_timings={res[prefetch][-1][2]}", flush=True)

    base = sum(r[1] for r in res[False]) / len(res[False])
    fast = sum(r[1] for r in res[True]) / len(res[True])
    print(f"提速: {base / fast:.2f}×（均值口径）  "
          f"逐轮 {min(ratios):.2f}~{max(ratios):.2f}×", flush=True)

    a = [p.tags for p in res[False][-1][0]]
    b = [p.tags for p in res[True][-1][0]]
    print("输出逐位一致:", a == b, flush=True)
    return 0 if a == b else 1


if __name__ == "__main__":
    raise SystemExit(main())
