"""包级并行：把「包」当调度单位，N 张卡各吃一个包。

和批内切分（`cpu_tagger.MultiDeviceTagBackend`）是两件不同的事，不能互相替代：

    批内切分：**一个**包在跑，每凑一批就等分给各卡 —— 适合"包少图多"。
    包级并行：**N 个**包同时跑，一个包完整归一张卡 —— 适合"包多"。

批内切分放在后端层（它只回答"一批怎么算"）；包级并行是调度（它回答"谁先拿
下一个包"），要跨 NL / tag 两条线，所以单独一层。

★lane 之间**不共享任何可变状态**：各自一个后端、一个 WorkIndex、一个 sink、
一份 stats。协调全靠盘上已有的跨进程机制——WorkIndex 的文件锁 + 每实例独立
sqlite 连接、failures.jsonl 的文件锁、jsonl 按"包名 + 进程号"分片。那套东西
本来就在支撑"几十个分包跑同一个产物目录"，这里只是把进程换成线程，所以
**不需要新造一套协调协议**，也就不需要担心"漏图/重复落盘"这类新失败模式。

为什么用线程而不是进程：模型对象不可 pickle（transformers pipeline），Windows
只能 spawn（每个进程都要重付一次 import torch + 读权重）。
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Iterable, Sequence


# ------------------------------------------------------------------ 分组

def pack_key(item: Any) -> str:
    """一条任务属于哪个包。

    zip/tar 成员靠 `source`（包文件路径）；散图没有包，按所在目录归堆——这样
    散图目录在包级并行下会退化成一个"包"，由调用方判断要不要退回批内切分。
    """
    source = str(getattr(item, "source", "") or "")
    if source:
        return source
    path = getattr(item, "path", None)
    parent = getattr(path, "parent", None)
    return str(parent or "")


def group_by_pack(items: Iterable) -> list[tuple[str, list]]:
    """按包分组，**保持首次出现顺序**（续做时人的直觉对得上）。"""
    order: list[str] = []
    buckets: dict[str, list] = {}
    for item in items:
        key = pack_key(item)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(item)
    return [(key, buckets[key]) for key in order]


def split_by_weight(packs: Sequence, n: int,
                    weight: Callable[[Any], int] | None = None) -> list[list]:
    """按重量贪心分配（LPT）：重的先放，每次丢给当前最轻的那条 lane。

    为什么不用轮转：包与包的体积差 10 倍很常见（一包 200 张、一包 2000 张）。
    轮转会让某条 lane 收尾时还剩一个大包，其余 lane 全部空转等它——墙钟由最慢
    那条决定，等于把 N 卡退回 1 卡。LPT 的近似下界是"最长那个包"，已经够好。

    包数少于 lane 数时后面的 lane 拿到空列表，调用方据此可以把 lane 数压到
    `min(n, len(packs))`，避免白加载权重。
    """
    if n <= 0:
        return []
    buckets: list[list] = [[] for _ in range(n)]
    loads = [0] * n
    w = weight or (lambda _p: 1)
    for pack in sorted(packs, key=w, reverse=True):
        i = loads.index(min(loads))
        buckets[i].append(pack)
        loads[i] += int(w(pack))
    return buckets


# ------------------------------------------------------------------ 并发

def run_lanes(count: int, fn: Callable[[int, threading.Event], None]) -> None:
    """并行跑 count 条 lane。

    `fn(i, halt)`：第 i 条 lane 干活；每个包边界检查 `halt`，被置位就收手。

    任一条抛异常（含 `Stopped` 这类控制流异常）就置 halt 并记下来，其余 lane
    在**下一个包边界**停手，已算完的照常落盘。不硬杀线程——Python 没有安全的
    杀线程手段，而"算完当前包再停"与 guard / 停止按钮的既有语义一致。

    最后把第一条异常原样抛出去：`Stopped` 要能被 run_client 捕获，否则 Web 上
    点停止会变成"任务失败"。
    """
    halt = threading.Event()
    errors: list[tuple[int, BaseException]] = []
    lock = threading.Lock()

    def _run(i: int) -> None:
        try:
            fn(i, halt)
        except BaseException as exc:      # noqa: BLE001 - 原样上抛，见 docstring
            with lock:
                errors.append((i, exc))
            halt.set()

    threads = [threading.Thread(target=_run, args=(i,), name=f"pack-lane-{i}",
                                daemon=True)
               for i in range(max(1, count))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0][1]


# ------------------------------------------------------------------ 统计合并

_TAG_INT_FIELDS = ("total", "done", "skipped", "busy", "failed",
                   "decode_ms", "forward_ms")


def merge_tag_stats(target: Any, parts: Sequence[Any]) -> None:
    """把多份 `TagStats` **绝对求和**写进 target。

    "绝对求和"而不是"累加"：这个函数可能被调用多次（中途刷新进度、最后结账），
    累加会让计数随刷新次数翻倍——界面上表现为"进度越跑越多"。
    `wall_ms` 取 max：几条 lane 是并行跑的，加起来不是墙钟。
    """
    if not parts:
        return
    for name in _TAG_INT_FIELDS:
        setattr(target, name, sum(int(getattr(p, name, 0) or 0) for p in parts))
    target.wall_ms = max(int(getattr(p, "wall_ms", 0) or 0) for p in parts)
    target.stopped = any(bool(getattr(p, "stopped", False)) for p in parts)
    reasons: dict[str, int] = {}
    for part in parts:
        for key, n in (getattr(part, "by_reason", None) or {}).items():
            reasons[key] = reasons.get(key, 0) + int(n)
    target.by_reason = reasons


_CLIENT_INT_FIELDS = ("total", "done", "skipped", "failed", "busy",
                      "blacklisted", "cached", "rate_limited", "tokens",
                      "prompt_tokens", "artist_skipped")


def merge_client_stats(target: Any, parts: Sequence[Any]) -> None:
    """把多份 `ClientStats` 求和写进 target。字段只取调用方真正会读的那些：
    Web 层用 `.busy` 判"有图被别的进程占着"，用 `.failed` 判要不要封包。"""
    if not parts:
        return
    for name in _CLIENT_INT_FIELDS:
        if not hasattr(target, name):
            continue
        setattr(target, name, sum(int(getattr(p, name, 0) or 0) for p in parts))
    # wall_ms 是"每张耗时的和"，并行下加起来没有意义，取总耗时更接近直觉。
    if hasattr(target, "wall_ms"):
        target.wall_ms = max(int(getattr(p, "wall_ms", 0) or 0) for p in parts)
    if hasattr(target, "by_reason"):
        reasons: dict[str, int] = {}
        for part in parts:
            for key, n in (getattr(part, "by_reason", None) or {}).items():
                reasons[key] = reasons.get(key, 0) + int(n)
        target.by_reason = reasons
