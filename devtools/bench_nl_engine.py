"""NL 推理引擎对比基准：同一批图、同一份 prompt，量吞吐与延迟。

要回答的问题
------------
"本地 NL 该用哪个引擎"不能靠搜到的 benchmark 猜。别人的数字是别人的卡、
别人的 prompt、别人的输出长度。这里只做一件事：**把引擎当成黑盒，
只换 URL，量出这条链路上真实的张/秒**。

五个必须守住的规则
------------------
1. **prompt 必须来自生产代码**。指令换一个字，输出长度就变，吞吐跟着变——
   那样测出来的差异归因不清。这里直接 import `tagger.client._prompt_for`。
2. **准备耗时单列**。`_resize_to_budget` + base64 是 CPU 侧固定成本
   （HANDOFF 实测单线程 53 ms/张、8 线程 14 ms/张）。它不进推理时间，
   但决定端到端天花板——混在一起就看不见瓶颈在谁身上。
3. **并发走梯度，不打单点**。单点只能说明"这一档多快"，梯度才能说明
   "再加并发还涨不涨"，也就是该开几个 worker、会不会白占显存。
4. **看 p95，不只平均**。0.1% 抽检是人眼在看，队头延迟比均值重要。
5. **正确性单独判**。吞吐再高，如果 `<CAPTION>` 块缺了或长度崩到几十字，
   那批数据是废的。解析率与长度分布跟吞吐一起出表。

用法
----
    # llama.cpp（llama-server 已起在 8080）
    env\\python\\python.exe devtools\\bench_nl_engine.py ^
        --images D:\\nlbench\\imgs --engine llamacpp-q4km8b ^
        --url http://127.0.0.1:8080 --model qwen3vl

    # 只量准备成本，不连服务端
    env\\python\\python.exe devtools\\bench_nl_engine.py --images D:\\nlbench\\imgs --prep-only

输出：控制台表 + `--json` 指定的文件（不给就写 devtools/bench_nl_engine.<engine>.json）。
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:                                   # 中文 Windows 控制台是 GBK
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:                      # noqa: BLE001
    pass

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import httpx                          # noqa: E402

from tagger.client import SYSTEM_PROMPT, _prompt_for, _resize_to_budget  # noqa: E402
from tagger.config import OutputSpec  # noqa: E402

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


# ------------------------------------------------------------------ 取图


def collect_images(paths: list[str], limit: int) -> list[Path]:
    """目录/文件混着收，排序后再截断。

    排序是刻意的：换引擎要能重跑同一批图，靠 os 的目录顺序不可靠。
    截断放在排序之后，两次运行的样本才一致。
    """
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found.extend(sorted(q for q in p.iterdir()
                                if q.is_file() and q.suffix.lower() in IMG_EXT))
        elif p.is_file():
            found.append(p)
        else:
            raise SystemExit(f"图片路径不存在: {p}")
    if not found:
        raise SystemExit("没有找到图片")
    return found[:limit] if limit > 0 else found


# ------------------------------------------------------------------ 准备成本（CPU 侧）


def prep_one(data: bytes, max_pixels: int) -> int:
    """返回 base64 后的长度，顺便把缩放与编码都跑掉（纯 CPU 侧成本）。"""
    small = _resize_to_budget(data, max_pixels) if max_pixels else data
    return len(base64.b64encode(small))


def bench_prep(images: list[bytes], max_pixels: int, threads: list[int]) -> list[dict]:
    """单线程 vs 多线程的准备成本。

    为什么要多线程：请求在等 GPU 时 CPU 是空的，准备可以和推理重叠。
    如果多线程加速比接近核数，说明"加并发"几乎不要钱；如果线性度差，
    那就是内存带宽/IO 到了顶，加并发只是排队。
    """
    rows: list[dict] = []
    for n in threads:
        t0 = time.perf_counter()
        if n <= 1:
            for d in images:
                prep_one(d, max_pixels)
        else:
            with ThreadPoolExecutor(max_workers=n) as pool:
                list(pool.map(lambda d: prep_one(d, max_pixels), images))
        wall = time.perf_counter() - t0
        rows.append({"threads": n, "wall_s": wall,
                     "ms_per_img": wall / len(images) * 1000,
                     "img_per_s": len(images) / wall})
    return rows


# ------------------------------------------------------------------ 推理


def build_prompt(mode: str, caption_range: list[int] | None = None) -> str:
    spec = OutputSpec(mode=mode)
    prompt = _prompt_for(spec)
    if caption_range:
        import re

        lo, hi = caption_range
        prompt = re.sub(r"\d+-\d+ characters", f"{lo}-{hi} characters", prompt)
        if f"{lo}-{hi} characters" not in prompt:
            raise SystemExit("prompt 里没找到长度引导，--caption-range 无效")
    return prompt


def post_one(client: httpx.Client, url: str, model: str, image_part: dict,
             prompt: str, max_tokens: int, temperature: float,
             top_p: float) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [image_part,
                                         {"type": "text", "text": prompt}]},
        ],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
    }
    t0 = time.perf_counter()
    r = client.post(f"{url}/v1/chat/completions", json=payload)
    dt = time.perf_counter() - t0
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    text = (body["choices"][0]["message"]["content"] or "")
    usage = body.get("usage") or {}
    return {
        "latency": dt,
        "text": text,
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
    }


def judge(text: str, mode: str) -> dict:
    """只判"这条产物能不能用"，不判"写得好不好"。

    与生产闸门口径一致：块标记在、正文非空、长度落在引导区间附近。
    写得好不好要靠 0.1% 人眼抽检，基准脚本管不了那一段。
    """
    import re

    cap = re.search(r"<CAPTION>(.*?)(?:</CAPTION>|(?=<TAGS>)|\Z)", text, re.S | re.I)
    body = (cap.group(1).strip() if cap else "")
    has_block = cap is not None
    leak = "<TAGS>" in text.upper() and mode == "nl"
    return {
        "has_block": has_block,
        "chars": len(body),
        "chars_per_token": (len(body) / max(1, len(body.split()))) if body else 0.0,
        "leak_tag_block": leak,
        "usable": bool(has_block and len(body) >= 40 and not leak),
    }


def run_level(images: list[bytes], url: str, model: str, prompt: str, *,
              concurrency: int, max_pixels: int, max_tokens: int,
              temperature: float, top_p: float, timeout: float) -> dict:
    """在一个并发档位上跑完整批，返回吞吐与延迟分布。"""
    limits = httpx.Limits(max_connections=concurrency + 4,
                          max_keepalive_connections=concurrency + 4)
    client = httpx.Client(timeout=timeout, limits=limits,
                          headers={"User-Agent": "tagger-bench"})
    n = len(images)
    lat: list[float] = []
    toks: list[int] = []
    judges: list[dict] = []
    errors: list[str] = []
    lock = threading.Lock()
    cursor = {"i": 0}

    def worker() -> None:
        while True:
            with lock:
                if cursor["i"] >= n:
                    return
                idx = cursor["i"]
                cursor["i"] += 1
            data = images[idx]
            small = _resize_to_budget(data, max_pixels) if max_pixels else data
            b64 = base64.b64encode(small).decode("ascii")
            part = {"type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            try:
                res = post_one(client, url, model, part, prompt,
                               max_tokens, temperature, top_p)
            except Exception as exc:      # noqa: BLE001 - 基准脚本不该因一条失败而中断
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")
                continue
            with lock:
                lat.append(res["latency"])
                toks.append(res["completion_tokens"])
                judges.append(judge(res["text"], "nl"))

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(lambda _: worker(), range(concurrency)))
    wall = time.perf_counter() - t0
    client.close()

    lat_sorted = sorted(lat)
    ok = [j for j in judges if j["usable"]]
    return {
        "concurrency": concurrency,
        "images": n,
        "ok": len(lat),
        "errors": len(errors),
        "error_sample": errors[:3],
        "wall_s": wall,
        "img_per_s": len(lat) / wall if wall else 0.0,
        "lat_p50": statistics.median(lat_sorted) if lat_sorted else 0.0,
        "lat_p95": (lat_sorted[int(len(lat_sorted) * 0.95) - 1]
                    if lat_sorted else 0.0),
        "lat_max": lat_sorted[-1] if lat_sorted else 0.0,
        "completion_tokens_avg": (sum(toks) / len(toks)) if toks else 0.0,
        "tok_per_s": (sum(toks) / wall) if wall else 0.0,
        "usable_rate": len(ok) / len(judges) if judges else 0.0,
        "caption_chars_avg": (sum(j["chars"] for j in judges) / len(judges)
                              if judges else 0.0),
        "leak_rate": (sum(1 for j in judges if j["leak_tag_block"]) / len(judges)
                      if judges else 0.0),
    }


def warmup(url: str, model: str, prompt: str, image: bytes, max_pixels: int,
           max_tokens: int, temperature: float, top_p: float, timeout: float,
           rounds: int = 1) -> float:
    """预热：第一次请求含 CUDA 上下文建立与 kernel 编译（Triton 可到分钟级）。

    不预热就直接测，第一张会把整批的均值拖歪——尤其是 llama.cpp 首次
    加载视觉塔、mistral.rs 首次编译 kernel 这两处。
    """
    client = httpx.Client(timeout=timeout)
    small = _resize_to_budget(image, max_pixels) if max_pixels else image
    b64 = base64.b64encode(small).decode("ascii")
    part = {"type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
    t0 = time.perf_counter()
    for _ in range(max(1, rounds)):
        post_one(client, url, model, part, prompt, max_tokens, temperature, top_p)
    dt = time.perf_counter() - t0
    client.close()
    return dt


# ------------------------------------------------------------------ 机时推算


def fleet_math(img_per_s: float, total_images: int) -> dict:
    """把张/秒换算成人能看懂的机时。

    3000 万张这个量级，单机吞吐差 2 倍就是"11 天 vs 22 天"的差别——
    这也是为什么引擎选择必须用实测数字来定，不能凭印象。
    """
    if img_per_s <= 0:
        return {}
    seconds = total_images / img_per_s
    return {
        "total_images": total_images,
        "img_per_s": img_per_s,
        "seconds": seconds,
        "hours": seconds / 3600,
        "days": seconds / 86400,
    }


# ------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description="NL 引擎对比基准")
    ap.add_argument("--images", nargs="+", required=True, help="图片目录或文件")
    ap.add_argument("--limit", type=int, default=24, help="取多少张（0=全部）")
    ap.add_argument("--engine", default="unknown", help="引擎标签，写进结果")
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--model", default="qwen3vl")
    ap.add_argument("--mode", default="nl", choices=["nl", "tag", "both"])
    ap.add_argument("--max-pixels", type=int, default=589824,
                    dest="max_pixels", help="准备阶段的像素预算，与生产一致")
    ap.add_argument("--max-tokens", type=int, default=800, dest="max_tokens")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--top-p", type=float, default=0.9, dest="top_p")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--prep-threads", type=int, nargs="+", default=[1, 4, 8],
                    dest="prep_threads")
    ap.add_argument("--caption-range", type=int, nargs=2, default=None,
                    dest="caption_range", metavar=("LO", "HI"))
    ap.add_argument("--prep-only", action="store_true", dest="prep_only")
    ap.add_argument("--no-warmup", action="store_true", dest="no_warmup")
    ap.add_argument("--json", default="", help="结果写到哪个 json")
    args = ap.parse_args()

    files = collect_images(args.images, args.limit)
    blobs = [p.read_bytes() for p in files]
    prompt = build_prompt(args.mode, args.caption_range)

    print("=" * 78)
    print(f" NL 引擎基准   engine={args.engine}  mode={args.mode}")
    print("=" * 78)
    print(f" 图片   : {len(files)} 张，源字节 {sum(map(len, blobs)) / 1e6:.1f} MB")
    print(f" 预算   : max_pixels={args.max_pixels}  max_tokens={args.max_tokens}")
    print(f" prompt : {len(prompt)} 字符（生产同源）")
    print()

    prep_rows = bench_prep(blobs, args.max_pixels, args.prep_threads)
    print("-" * 78)
    print(" 1) 准备成本（缩放 + base64，纯 CPU）")
    print("-" * 78)
    base_ms = prep_rows[0]["ms_per_img"]
    print(f"  {'线程':>4}  {'ms/张':>9}  {'张/秒':>9}  {'加速比':>7}")
    for r in prep_rows:
        print(f"  {r['threads']:>4}  {r['ms_per_img']:>9.2f}  "
              f"{r['img_per_s']:>9.1f}  {base_ms / r['ms_per_img']:>6.2f}x")
    print()
    print(f"  读法：单线程 {base_ms:.1f} ms/张 → 天花板只有 "
          f"{1000 / base_ms:.0f} 张/秒。推理再快也超不过这一行。")
    print()

    result: dict = {
        "engine": args.engine,
        "url": args.url,
        "model": args.model,
        "mode": args.mode,
        "images": len(files),
        "max_pixels": args.max_pixels,
        "max_tokens": args.max_tokens,
        "prep": prep_rows,
    }

    if args.prep_only:
        print("（--prep-only：不连服务端）")
    else:
        if not args.no_warmup:
            dt = warmup(args.url, args.model, prompt, blobs[0], args.max_pixels,
                        args.max_tokens, args.temperature, args.top_p, args.timeout)
            print(f" 预热 1 张：{dt:.2f}s（含首次 CUDA/编译开销，不计入统计）")
            print()

        print("-" * 78)
        print(" 2) 推理吞吐（并发梯度）")
        print("-" * 78)
        print(f"  {'并发':>4}  {'墙钟':>8}  {'张/秒':>9}  {'p50':>8}  {'p95':>8}  "
              f"{'tok/秒':>8}  {'可用率':>7}  {'均字符':>7}")
        rows: list[dict] = []
        for c in args.concurrency:
            r = run_level(blobs, args.url, args.model, prompt,
                          concurrency=c, max_pixels=args.max_pixels,
                          max_tokens=args.max_tokens,
                          temperature=args.temperature, top_p=args.top_p,
                          timeout=args.timeout)
            rows.append(r)
            print(f"  {r['concurrency']:>4}  {r['wall_s']:>7.1f}s  "
                  f"{r['img_per_s']:>9.2f}  {r['lat_p50']:>7.2f}s  "
                  f"{r['lat_p95']:>7.2f}s  {r['tok_per_s']:>8.1f}  "
                  f"{r['usable_rate'] * 100:>6.1f}%  "
                  f"{r['caption_chars_avg']:>7.0f}")
            if r["errors"]:
                print(f"        !! 失败 {r['errors']} 条，例：{r['error_sample']}")
        result["levels"] = rows

        best = max(rows, key=lambda r: r["img_per_s"])
        result["best"] = best
        print()
        print(f" 最佳档位：并发 {best['concurrency']}，{best['img_per_s']:.2f} 张/秒，"
              f"p95 {best['lat_p95']:.2f}s，可用率 {best['usable_rate'] * 100:.1f}%")
        print()
        print("-" * 78)
        print(" 3) 机时推算（按最佳档位）")
        print("-" * 78)
        for total, label in ((3_000_000, "每人 300 万张"),
                             (30_000_000, "全量 3000 万张")):
            m = fleet_math(best["img_per_s"], total)
            print(f"  {label:<16} 单机 {m['hours']:.1f} 小时 = {m['days']:.1f} 天")
            for k in (4, 8, 16, 32):
                print(f"    {k:>2} 台并行 → {m['days'] / k:>6.1f} 天")
        result["fleet"] = {str(t): fleet_math(best["img_per_s"], t)
                           for t in (3_000_000, 30_000_000)}

    out = Path(args.json) if args.json else (
        REPO / "devtools" / f"bench_nl_engine.{args.engine}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f" 结果已写入：{out}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
