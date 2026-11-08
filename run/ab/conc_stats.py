"""并发档位的单张延迟分布。

★不要用"延迟之和"折算吞吐：并发跑的时候多张是重叠的，加起来远大于墙钟。
吞吐直接看 nl_ab collect 打印的那一行（它用的是真实墙钟秒数）。
"""
from __future__ import annotations

import json
import statistics
import sys


def stats(path: str) -> None:
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    lat = sorted(r["latency"] for r in rows if r.get("latency"))
    errs = sum(1 for r in rows if r.get("error"))
    p50 = lat[len(lat) // 2]
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
    print(f"{path:<22} n={len(rows):<3} 错误={errs} "
          f"p50={p50:.2f}s p95={p95:.2f}s 最大={lat[-1]:.2f}s "
          f"均值={statistics.mean(lat):.2f}s")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        stats(p)
