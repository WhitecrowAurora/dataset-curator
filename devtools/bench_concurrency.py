"""并发与并行的最小原型：客户端线程数、以及 pack 转码要不要开线程池。

两个问题，一个脚本
------------------
1. **客户端并发**：`--concurrency` 到底能不能把一条推理链路的吞吐拉满？
   用一个"只 sleep 不推理"的假服务端来测——这样测出来的全部是客户端开销，
   和 GPU 快慢无关。假服务端的延迟设成真实量级（默认 300ms/张）。

   关键指标不是吞吐，而是 **每张图的客户端开销** = 实际耗时 − 服务端延迟。
   这个数就是"Python 那部分"的真身。它如果只有几毫秒，那么重写成 Rust
   最多也只能省下那几毫秒的几分之一。

2. **pack 转码并行**：`tagger pack` 现在是单线程（全仓库只有 client 有线程池）。
   Pillow 的 JPEG 解码/编码在 C 层里会释放 GIL，所以线程池对它是有效的。
   这里直接拿 `transcode_to_jpeg` 测单线程 vs N 线程的加速比。

用法
----
    env\\python\\python.exe devtools\\bench_concurrency.py
    env\\python\\python.exe devtools\\bench_concurrency.py --images 48 --latency 0.3
    env\\python\\python.exe devtools\\bench_concurrency.py --skip-pack
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 中文 Windows 控制台是 GBK，输出里一个 ² / µ 就能让整段崩掉。
try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "env" / "python" / "python.exe"
if not PY.is_file():
    PY = Path(sys.executable)

# 假产物必须能过质检闸门，否则每张都会重试 3 次，测出来的是"重试耗时"而不是
# "干净跑完的耗时"。长度要过 min_chars(140)，tag 覆盖率要过 60%。
FAKE_TEXT = ("<TAGS>1girl, long_hair, red_dress, smile, looking_at_viewer, "
             "outdoors, day, blue_sky, tree, standing, full_body, white_shirt</TAGS>\n"
             "<CAPTION>A girl with long hair stands outdoors in a red dress and a "
             "white shirt, smiling at the viewer. It is daytime and the blue sky is "
             "clear, with a tree standing behind her. Her full body is in frame as she "
             "keeps smiling, looking at the viewer with her long hair falling over the "
             "red dress under the open sky of a bright day.</CAPTION>")


# ------------------------------------------------------------------ 假服务端


class _Handler(BaseHTTPRequestHandler):
    latency = 0.3
    hits = 0
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)          # 必须读完，否则连接会卡住
        time.sleep(self.latency)         # 模拟推理耗时
        body = json.dumps({
            "choices": [{"message": {"content": FAKE_TEXT}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with _Handler.lock:
            _Handler.hits += 1

    def log_message(self, *args) -> None:   # 关掉逐请求日志，否则输出全是噪音
        return


def start_server(latency: float) -> tuple[ThreadingHTTPServer, str]:
    _Handler.latency = latency
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ------------------------------------------------------------------ 造包


def build_shard(root: Path, count: int, size: int = 384) -> Path:
    """造一个最小可用的分发包：N 张图 + manifest.jsonl。"""
    from PIL import Image

    root.mkdir(parents=True, exist_ok=True)
    shard = root / "dist-0000.zip"
    rows: list[str] = []
    with zipfile.ZipFile(shard, "w", compression=zipfile.ZIP_STORED) as zf:
        for i in range(1, count + 1):
            image_id = f"bench-{i:06d}"
            name = f"{image_id}.jpg"
            import io

            buf = io.BytesIO()
            Image.new("RGB", (size, size), (30 + i * 7 % 200, 90, 140)).save(
                buf, "JPEG", quality=92)
            zf.writestr(name, buf.getvalue())
            rows.append(json.dumps({
                "image_id": image_id, "file": name, "artist": "bench",
                "src": "", "member": "", "rel_path": name, "bytes": buf.tell(),
            }))
        zf.writestr("manifest.jsonl", "\n".join(rows) + "\n")
    return shard


# ------------------------------------------------------------------ 客户端并发


def run_client_once(shard: Path, url: str, out: Path, concurrency: int,
                    limit: int) -> float:
    """跑一次真正的 tagger client，返回墙钟秒数。"""
    cmd = [
        str(PY), "-m", "tagger.cli", "client",
        "--shard", str(shard),
        "--url", url,
        "--model-name", "bench",
        "--out-dir", str(out),
        "--mode", "both",
        "--tag-backend", "none",
        "--concurrency", str(concurrency),
        "--limit", str(limit),
        "--no-guard",
        "--quiet",
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"client 退出码 {proc.returncode}")
    return wall


def measure_startup(shard: Path, url: str, workdir: Path) -> float:
    """量一次进程启动 + 配置加载的固定成本（跑 1 张图，减去它的推理延迟）。

    不把这部分扣掉，32 张的基准里会混进 1.4 秒启动时间，被摊成每张 44ms，
    看起来像"Python 开销很大"——实际那是 argparse + 读配置 + 建连接池。
    """
    out = workdir / "out-startup"
    if out.exists():
        shutil.rmtree(out)
    wall = run_client_once(shard, url, out, 1, 1)
    return max(0.0, wall - _Handler.latency)


def bench_concurrency(shard: Path, url: str, workdir: Path, *, count: int,
                      latency: float, levels: list[int],
                      startup: float) -> list[dict]:
    rows: list[dict] = []
    for level in levels:
        out = workdir / f"out-c{level}"
        if out.exists():
            shutil.rmtree(out)
        _Handler.hits = 0
        wall = run_client_once(shard, url, out, level, count)
        produced = len(list(out.rglob("*-nl.json")))
        # 理论下界：启动一次 + count/c 轮 × 每轮 latency。
        ideal = startup + (count / level) * latency
        overhead = max(0.0, wall - ideal)
        rows.append({
            "concurrency": level,
            "wall": wall,
            "ideal": ideal,
            "produced": produced,
            "per_img": wall / max(produced, 1),
            "overhead": overhead / max(produced, 1),
            "throughput": produced / wall if wall else 0.0,
        })
    return rows


# ------------------------------------------------------------------ pack 并行


def bench_pack(images: list[bytes], threads: list[int]) -> list[dict]:
    from tagger.pack import transcode_to_jpeg

    def one(data: bytes) -> None:
        transcode_to_jpeg(data, quality=95, subsampling=2)

    rows: list[dict] = []
    for n in [1] + threads:
        t0 = time.perf_counter()
        if n <= 1:
            for data in images:
                one(data)
        else:
            with ThreadPoolExecutor(max_workers=n) as pool:
                list(pool.map(one, images))
        wall = time.perf_counter() - t0
        rows.append({"threads": n, "wall": wall,
                     "per_img": wall / len(images) * 1000,
                     "throughput": len(images) / wall})
    return rows


# ------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description="并发/并行最小原型")
    ap.add_argument("--images", type=int, default=32, help="并发基准的图片数")
    ap.add_argument("--latency", type=float, default=0.3, help="假服务端每张耗时（秒）")
    ap.add_argument("--levels", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--pack-images", type=int, default=60, dest="pack_images")
    ap.add_argument("--pack-size", type=int, default=1024, dest="pack_size")
    ap.add_argument("--pack-threads", type=int, nargs="+", default=[2, 4, 8],
                    dest="pack_threads")
    ap.add_argument("--skip-client", action="store_true", dest="skip_client")
    ap.add_argument("--skip-pack", action="store_true", dest="skip_pack")
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="tagger-bench-"))
    print("=" * 78)
    print(" 并发 / 并行最小原型")
    print("=" * 78)
    print(f" 工作目录 : {workdir}")
    print(f" python   : {PY}")
    print(f" CPU 核数 : {os.cpu_count()}")
    print()

    try:
        if not args.skip_client:
            print("-" * 78)
            print(f" 1) 客户端并发（假服务端，延迟 {args.latency * 1000:.0f} ms/张）")
            print("-" * 78)
            shard = build_shard(workdir, args.images)
            srv, url = start_server(args.latency)
            try:
                startup = measure_startup(shard, url, workdir)
                print(f"  进程启动 + 配置加载（固定成本）：{startup * 1000:.0f} ms")
                rows = bench_concurrency(shard, url, workdir, count=args.images,
                                         latency=args.latency, levels=args.levels,
                                         startup=startup)
            finally:
                srv.shutdown()

            print(f"  {'并发':>4}  {'墙钟':>8}  {'理论下界':>9}  {'产出':>6}  "
                  f"{'每张':>8}  {'客户端开销/张':>13}  {'吞吐':>9}")
            for r in rows:
                print(f"  {r['concurrency']:>4}  {r['wall']:>7.2f}s  "
                      f"{r['ideal']:>8.2f}s  {r['produced']:>6}  "
                      f"{r['per_img'] * 1000:>7.1f}ms  "
                      f"{r['overhead'] * 1000:>11.1f}ms  "
                      f"{r['throughput']:>6.1f} 张/s")

            saturated = [r for r in rows if r["concurrency"] >= 4] or rows
            best = min(r["overhead"] for r in saturated)
            print()
            print(f"  客户端自身开销（饱和后最佳档位）：约 {best * 1000:.2f} ms/张")
            print("  读法：这个数远小于服务端延迟时，说明加线程/换语言都没有意义——")
            print("        瓶颈在等 GPU，不在跑 Python。")
            print()

        if not args.skip_pack:
            print("-" * 78)
            print(f" 2) pack 转码并行（{args.pack_images} 张 "
                  f"{args.pack_size}x{args.pack_size}）")
            print("-" * 78)
            from PIL import Image
            import io

            images: list[bytes] = []
            for i in range(args.pack_images):
                buf = io.BytesIO()
                im = Image.new("RGB", (args.pack_size, args.pack_size))
                px = im.load()
                for y in range(0, args.pack_size, 4):
                    for x in range(0, args.pack_size, 4):
                        px[x, y] = ((x * 7 + i) % 256, (y * 5) % 256, (x + y) % 256)
                im.save(buf, "JPEG", quality=92)
                images.append(buf.getvalue())

            rows = bench_pack(images, args.pack_threads)
            base = rows[0]["wall"]
            print(f"  {'线程':>4}  {'墙钟':>8}  {'每张':>8}  {'吞吐':>10}  {'加速比':>7}")
            for r in rows:
                print(f"  {r['threads']:>4}  {r['wall']:>7.2f}s  "
                      f"{r['per_img']:>7.1f}ms  {r['throughput']:>7.1f} 张/s  "
                      f"{base / r['wall']:>6.2f}x")
            print()
            print("  读法：加速比接近核数就说明 Pillow 释放了 GIL，开线程是白捡的；")
            print("        线性度差说明受内存带宽或磁盘限制，加线程没用。")
            print()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
