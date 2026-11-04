"""命令行入口。

三条命令就是当前阶段该做的三件事：
    doctor  每台机器跑一次，确认硬件与分档匹配，输出可以直接贴到群里
    serve   起 vLLM 服务
    smoke   跑几张图，验证端到端能出 caption，并给出实测吞吐
    bench   扫一遍并发，找出这台机器真正饱和的点
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import (ConfigError, DEFAULT_TIERS, load_ops, load_tier_table,
                     resolve_profile)
from .hardware import describe, query_gpus
from .netutil import trust_env_for

# smoke 用的最小 prompt。正式的 caption 规格（四档输出、tag 注入方式）
# 要等 S1 实验定了再写进 prompts/ 目录，不要在这里堆。
SMOKE_PROMPT = (
    "Describe this image in one paragraph for AI training data. "
    "Cover in this order: main subject; its appearance (color, material, texture); "
    "the setting and background; composition and camera angle; lighting; any visible text "
    "(transcribe it verbatim). Do not speculate about things you cannot see. "
    "Answer in English."
)


def _setup_console() -> None:
    """Windows 控制台默认 GBK，打印中文会炸。stdout / stderr 都要处理。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass


def _load_profile(args: argparse.Namespace):
    gpus = query_gpus()
    if not gpus:
        raise ConfigError(
            "没有检测到 NVIDIA 显卡（nvidia-smi 不可用）。\n"
            "Mac 不支持 CUDA，无法参与打标；Windows 请确认驱动已安装。"
        )
    gpu = gpus[0]
    print(describe(gpu))
    if len(gpus) > 1:
        print(f"（检测到 {len(gpus)} 张卡，只用第 0 张）")

    table = load_tier_table(args.tiers)
    return resolve_profile(
        table,
        gpu,
        model_override=args.model,
        tier_override=args.tier,
        max_pixels_override=args.max_pixels,
        max_num_seqs_override=getattr(args, "max_num_seqs", None),
        max_num_batched_tokens_override=getattr(args, "max_num_batched_tokens", None),
        gpu_mem_override=getattr(args, "gpu_mem", None),
    )


# ---------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    profile = _load_profile(args)
    print()
    print(profile.summary())
    print()
    print("把上面这段原样贴到群里，用于核对全池参数是否一致。")
    return 0


def cmd_llama(args: argparse.Namespace) -> int:
    """llama.cpp 运行时的查看与安装。

    命令行这一份是"网页以外的那条路"：没有浏览器、或者 GitHub 要走代理时，
    用 `--zip` 直接喂一个已经下好的包。安装本身复用 llama_runtime，与网页
    按钮是同一套逻辑、同一个落点，不会出现"网页装好了 CLI 仍说没装"。
    """
    from . import llama_runtime

    if (getattr(args, "llama_cmd", "") or "status") == "install":
        started = llama_runtime.install(url=args.zip or "", force=args.force)
        if not started.get("started"):
            print(f"[llama] {started.get('reason') or '没有开始'}")
        # 同步等：命令行用户要的是"跑完给结果"，不是"再去看另一个命令"。
        last_pct = -1
        while True:
            st = llama_runtime.status()
            if st["busy"]:
                if st["total"]:
                    pct = int(100 * st["done"] / st["total"])
                    if pct != last_pct:
                        mib = 1 << 20
                        print(f"\r[llama] {st['stage']} {pct}% "
                              f"({st['done'] // mib}/{st['total'] // mib} MB)",
                              end="", flush=True)
                        last_pct = pct
                time.sleep(0.3)
                continue
            print()
            if st["stage"] == llama_runtime.STAGE_DONE:
                print(f"[llama] 装好了：{st['binary']}")
                return 0
            if st["stage"] == llama_runtime.STAGE_CANCELLED:
                print("[llama] 已取消")
                return 1
            print(f"[llama] 失败：{st['error'] or '未知原因'}")
            return 1

    st = llama_runtime.status()
    if st["installed"]:
        print(f"[llama] 已安装：{st['binary']}"
              f"（版本 {st['version'] or '未知'}）")
        return 0
    print("[llama] 没找到 llama-server。安装：tagger llama install")
    print(f"[llama] 会装到 {st['install_root']}；"
          f"已有本地包就 `tagger llama install --zip <路径>`。")
    return 1


# ---------------------------------------------------------------- serve


def cmd_serve(args: argparse.Namespace) -> int:
    from .model_loader import fingerprint_env, start_server

    profile = _load_profile(args)
    print()
    print(profile.summary())
    print()

    log_path = Path(args.log) if args.log else None
    handle = start_server(
        profile,
        port=args.port,
        data_root=args.data_root,
        log_path=log_path,
        wait=not args.no_wait,
        timeout_s=args.timeout,
        engine=getattr(args, "engine", "vllm"),
    )

    if args.no_wait:
        print(f"[tagger] 已在后台启动 pid={handle.process.pid if handle.process else '?'}")
        print(f"[tagger] 就绪后访问 {handle.base_url}/health")
        return 0

    fp = fingerprint_env(handle)
    print()
    print("环境指纹（写入每条标注的 provenance）：")
    print(json.dumps(fp, ensure_ascii=False, indent=2))

    fp_out = Path(args.fingerprint_out or "run/fingerprint.json")
    fp_out.parent.mkdir(parents=True, exist_ok=True)
    fp_out.write_text(json.dumps(fp, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[tagger] 指纹已写入 {fp_out}")

    try:
        print()
        print(f"[tagger] 服务运行中，Ctrl+C 停止")
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[tagger] 正在停止...")
    finally:
        handle.stop()
    return 0


# ---------------------------------------------------------------- smoke


def _image_payload(path: Path, use_file_url: bool) -> dict:
    if use_file_url:
        return {"type": "image_url", "image_url": {"url": f"file://{path.resolve()}"}}
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    suffix = path.suffix.lower().lstrip(".") or "jpeg"
    if suffix == "jpg":
        suffix = "jpeg"
    return {"type": "image_url", "image_url": {"url": f"data:image/{suffix};base64,{b64}"}}


def _load_test(
    images: list[Path],
    *,
    base_url: str,
    model_name: str,
    prompt: str,
    max_tokens: int,
    timeout_s: float,
    concurrency: int,
    repeats: int = 1,
    use_file_url: bool = False,
    system_prompt: str | None = None,
) -> tuple[list[tuple[Path, str, int, float]], float]:
    """按并发跑一轮，返回 (每次请求的结果, 墙钟总耗时)。

    图片只编码一次：真实链路里每张图也只编码一次，重复编码会把
    CPU 上的 base64 开销算进吞吐，测出来的数偏低。
    """
    import httpx

    payloads: list[tuple[Path, dict]] = []
    for path in images:
        part = _image_payload(path, use_file_url)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user",
                         "content": [part, {"type": "text", "text": prompt}]})
        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        payloads.extend([(path, payload)] * repeats)

    timeout = httpx.Timeout(timeout_s, connect=10.0)
    # httpx.Client 是线程安全的，共用一个连接池才能真正复用 keep-alive；
    # 每个线程各建一个的话，连接建立开销会被算进延迟。
    with httpx.Client(timeout=timeout,
                      limits=httpx.Limits(max_connections=max(8, concurrency * 2),
                                          max_keepalive_connections=concurrency),
                      trust_env=trust_env_for(base_url)) as client:

        def one(item: tuple[Path, dict]) -> tuple[Path, str, int, float]:
            path, payload = item
            t0 = time.time()
            try:
                r = client.post(f"{base_url}/v1/chat/completions", json=payload)
                r.raise_for_status()
                data = r.json()
                text = data["choices"][0]["message"]["content"].strip()
                usage = data.get("usage") or {}
                return path, text, int(usage.get("completion_tokens") or 0), time.time() - t0
            except httpx.HTTPStatusError as exc:
                return path, f"[HTTP {exc.response.status_code}] {exc.response.text[:300]}", 0, time.time() - t0
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                return path, f"[ERROR] {type(exc).__name__}: {exc}", 0, time.time() - t0

        started = time.time()
        if concurrency <= 1:
            results = [one(item) for item in payloads]
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                results = list(pool.map(one, payloads))
        return results, time.time() - started


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


# 一行 = (并发, img/s, tok/s, p50, p95, 失败数)
BenchRow = tuple[int, float, float, float, float, int]


def _pick_concurrency(rows: list[BenchRow]) -> tuple[BenchRow, BenchRow]:
    """从压测结果里挑出峰值点与建议点。

    建议点刻意不是峰值：吞吐差不到 10% 时选并发更小的那个，延迟更低、
    显存余量更大，连跑几天更稳。峰值往往是"刚好塞满"的状态，
    再遇上温度或磁盘波动就会掉到线下。
    """
    if not rows:
        raise ValueError("没有压测结果")
    best = max(rows, key=lambda r: r[1])
    stable = [r for r in rows if r[1] >= best[1] * 0.9]
    pick = min(stable, key=lambda r: r[0])
    return best, pick


def cmd_smoke(args: argparse.Namespace) -> int:
    images = [Path(p) for p in args.images]
    missing = [str(p) for p in images if not p.is_file()]
    if missing:
        raise ConfigError(f"图片不存在: {', '.join(missing)}")

    base_url = args.url.rstrip("/")
    print(f"[tagger] 目标服务: {base_url}  模型名: {args.model_name}")
    print(f"[tagger] {len(images)} 张图，并发 {args.concurrency}")
    print()

    results, total = _load_test(
        images,
        base_url=base_url,
        model_name=args.model_name,
        prompt=args.prompt or SMOKE_PROMPT,
        max_tokens=args.max_tokens,
        timeout_s=args.timeout,
        concurrency=max(1, int(args.concurrency or 1)),
        use_file_url=bool(args.use_file_url),
    )

    for path, text, tokens, elapsed in results:
        preview = text.replace("\n", " ")[:160]
        print(f"  {path.name}  {elapsed:6.2f}s  {tokens:4d} tok  {preview}")

    ok = sum(1 for _, t, _, _ in results if not t.startswith("["))
    rates = [tokens / elapsed for _, _, tokens, elapsed in results
             if tokens and elapsed > 0]
    print()
    print(f"[tagger] 完成 {ok}/{len(results)} 张，总耗时 {total:.1f}s，"
          f"吞吐 {len(results) / total:.2f} img/s（并发 {args.concurrency}）")
    if rates:
        print(f"[tagger] 单条解码约 {sum(rates) / len(rates):.1f} tok/s；"
              f"p50 延迟 {_pct([e for *_, e in results], 0.5):.2f}s，"
              f"p95 {_pct([e for *_, e in results], 0.95):.2f}s")
    print("[tagger] 提示：并发只有一两张时量化不出真实吞吐，"
          "要定机器配置请跑 `tagger bench`。")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for path, text, tokens, elapsed in results:
                f.write(json.dumps(
                    {"image": str(path), "caption": text, "tokens": tokens,
                     "wall_s": round(elapsed, 3)},
                    ensure_ascii=False,
                ) + "\n")
        print(f"[tagger] 结果写入 {out}")

    return 0 if ok == len(results) else 1


def cmd_bench(args: argparse.Namespace) -> int:
    """扫并发找饱和点。

    为什么必须实测：vLLM 的吞吐不是随并发线性涨的，到某个点后每条请求
    的 prefill 开始互相抢 batch 预算，吞吐走平甚至下降，而延迟继续涨。
    分档表里的 max_num_seqs 是按保守估计写的，30M 量级下这个点选错
    就是多跑几天。
    """
    import glob as globmod

    paths: list[Path] = []
    for pattern in args.images:
        matched = [Path(p) for p in globmod.glob(pattern, recursive=True)]
        if matched:
            paths.extend(p for p in matched if p.is_file())
        elif Path(pattern).is_file():
            paths.append(Path(pattern))
    if not paths:
        raise ConfigError(f"没有匹配到图片: {' '.join(args.images)}")

    levels = [int(c) for c in str(args.concurrency).split(",") if c.strip()]
    levels = sorted({max(1, c) for c in levels})
    repeats = max(1, int(args.repeat))

    # 默认按**真实负载**打：system prompt + 产出规格生成的完整指令。
    # 换成 smoke 那段小 prompt 的话，prompt token 少一半，prefill 成本
    # 被低估，测出来的并发上限会偏高——照着它配 30M 会跑不完。
    prompt, system_prompt = args.prompt or SMOKE_PROMPT, None
    if args.real_prompt:
        from .client import SYSTEM_PROMPT, _prompt_for

        table = load_tier_table(args.tiers)
        ops = load_ops(table)
        prompt, system_prompt = _prompt_for(ops.output_spec()), SYSTEM_PROMPT
        spec_max = int((table.get("defaults") or {}).get("max_tokens", 800))
        max_tokens = max(int(args.max_tokens), spec_max)
    else:
        max_tokens = int(args.max_tokens)

    print(f"[tagger] 目标服务: {args.url.rstrip('/')}  模型名: {args.model_name}")
    print(f"[tagger] {len(paths)} 张图 × {repeats} 轮，并发梯度 {levels}")
    print(f"[tagger] prompt {'按产出规格生成' if args.real_prompt else 'smoke 短指令'}，"
          f"max_tokens={max_tokens}"
          f"（不跟真实产出对齐的话，测出来的是另一个负载）")
    print()
    print(f"{'并发':>4}  {'img/s':>8}  {'tok/s':>9}  {'p50':>7}  {'p95':>7}  {'失败':>4}")
    print("-" * 52)

    rows: list[BenchRow] = []
    for level in levels:
        results, total = _load_test(
            paths,
            base_url=args.url.rstrip("/"),
            model_name=args.model_name,
            prompt=prompt,
            max_tokens=max_tokens,
            timeout_s=args.timeout,
            concurrency=level,
            repeats=repeats,
            system_prompt=system_prompt,
        )
        ok = [r for r in results if not r[1].startswith("[")]
        failed = len(results) - len(ok)
        rate = len(ok) / total if total > 0 else 0.0
        tok_rate = sum(r[2] for r in ok) / total if total > 0 else 0.0
        lat = [r[3] for r in ok]
        row = (level, rate, tok_rate, _pct(lat, 0.5), _pct(lat, 0.95), failed)
        rows.append(row)
        print(f"{level:>4}  {rate:>8.2f}  {tok_rate:>9.1f}  "
              f"{row[3]:>7.2f}  {row[4]:>7.2f}  {failed:>4}")

    if not rows:
        return 1
    best, pick = _pick_concurrency(rows)
    print()
    if best[1] <= 0:
        # 全失败时打出来的"最高点 0 img/s"毫无意义，还会让人以为测了个寂寞。
        # 把第一条错误原样贴出来：连不上 / HTTP 400 模型名错 / 超时，处理方式完全不同。
        sample = next((r[1] for r in results if r[1].startswith("[")), "")
        print(f"[tagger] 所有请求都失败了，没有吞吐可言。")
        if sample:
            print(f"[tagger] 第一条错误：{sample[:300]}")
        print("[tagger] 先 `tagger smoke` 确认服务地址、模型名与超时。")
        return 1
    print(f"[tagger] 吞吐最高点：并发 {best[0]}，{best[1]:.2f} img/s")
    print(f"[tagger] 建议并发：{pick[0]}（峰值 90% 以上里最小的那个；"
          f"再往上延迟涨而吞吐不涨）")
    if args.images_total:
        total_imgs = int(args.images_total)
        # 压测直接打 HTTP，不经过硬件保护；真实任务在任务边界按 duty 让步，
        # 所以这里要把占空比折进去，否则估出来的天数会乐观 30%。
        try:
            duty = float(load_ops(load_tier_table(args.tiers))
                         .guard_policy().duty_cycle)
        except Exception:            # noqa: BLE001 - 配置坏了不拦压测结论
            duty = 1.0
        duty = min(max(duty, 0.05), 1.0)
        machines = max(1, int(getattr(args, "machines", 1) or 1))
        per_day = pick[1] * 86400 * duty * machines
        print()
        print(f"[tagger] 按 {total_imgs:,} 张、并发 {pick[0]}、"
              f"占空比 {duty:.2f}、{machines} 台估算："
              f"约 {total_imgs / per_day:.1f} 天"
              f"（{per_day:,.0f} 张/天）")
        if total_imgs / per_day > 30:
            print("[tagger] 超过一个月：先看能不能减少每张的 token"
                  "（max_pixels / NL 长度 / 只要 tag），再加机器")
    return 0


# ---------------------------------------------------------------- guard


def cmd_guard(args: argparse.Namespace) -> int:
    """看这台机器现在的状态，以及该不该停。

    成员动手前先跑一次确认阈值合理；跑任务时守护在独立线程里自动生效，
    这里只是让人能亲眼看到读数与阈值的关系。
    """
    from .guard import ResourceGuard, power_hint

    table = load_tier_table(args.tiers)
    ops = load_ops(table)
    gpus = query_gpus()
    device = gpus[0].name if gpus else ""
    if gpus:
        print(describe(gpus[0]))

    guard = ResourceGuard(
        ops.guard_policy(),
        data_dir=args.data_dir,
        device_name=device,
        verbose=False,
        start_watchdog=False,
    )
    policy = guard.policy
    print()
    print(f"阈值: duty={policy.duty_cycle}  熔断={policy.temp_cutoff_c:.0f}°C  "
          f"恢复={policy.temp_resume_c:.0f}°C  磁盘下限={policy.disk_min_free_gb:.0f}G/"
          f"{policy.disk_min_free_pct * 100:.0f}%")

    if args.watch:
        print(f"\n每 {args.interval}s 采样一次，Ctrl+C 退出")
        try:
            while True:
                state = guard.watchdog.sample_once()
                print(state.describe(), flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print()
            return 0

    state = guard.watchdog.sample_once()
    print()
    print(state.describe())
    print()
    print(power_hint(device))
    return 0 if state.can_run else 1


# ---------------------------------------------------------------- 输入发现


def cmd_discover(args: argparse.Namespace) -> int:
    """扫描输入目录：能看到多少张图、来自哪些分包、平铺后叫什么。"""
    from .pack import discover_inputs

    items = discover_inputs(
        args.input_dir,
        recursive=not args.no_recursive,
        include_zips=not args.no_zips,
        include_loose=not args.no_loose,
        limit=args.limit,
    )
    if not items:
        print(f"[tagger] {args.input_dir} 下没有发现图片")
        return 1

    total_bytes = sum(i.size for i in items)
    by_source: dict[str, int] = {}
    for it in items:
        by_source[it.source] = by_source.get(it.source, 0) + 1

    print(f"[tagger] 共 {len(items)} 张，{total_bytes / 1048576:.1f} MB，"
          f"来自 {len(by_source)} 个来源")
    for src, n in sorted(by_source.items())[: args.top]:
        print(f"  {n:6d}  {Path(src).name}")

    print("\n平铺示例：")
    for it in items[: args.samples]:
        print(f"  {it.rel_path}  →  {it.flat_name}")
    return 0


# ---------------------------------------------------------------- 分包


def cmd_manifest(args: argparse.Namespace) -> int:
    """扫描输入分包目录，生成全局索引（每包一行，含内部条目与宽高）。"""
    from .archive import build_manifest

    out = build_manifest(args.shard_dir, args.out)
    print(f"[tagger] 索引写入 {out}")
    print("[tagger] 下游可以只读这个文件做分配和预过滤，不必打开每个分包。")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    """把已封口的分片推到 FTP。失败的留在本地，下次可重传。

    上传是一个「启用项」：upload.enabled 为 false 时（默认）本命令不发起任何
    网络连接，只告诉你产物在哪。这样没有 FTP 服务器的人也能把流程完整跑完。
    """
    from .upload import archive_dir_to_ftp

    table = load_tier_table(args.tiers)
    ops = load_ops(table)
    enabled = bool((ops.upload or {}).get("enabled", False))

    if not enabled and not args.force:
        print("[tagger] 上传未启用（upload.enabled: false），产物全部留在本地：")
        print(f"        {Path(args.out_dir).resolve()}")
        print("        需要上传时：把分档表的 upload.enabled 改成 true，"
              "或本次加 --force。")
        return 0

    if not args.dry_run:
        cfg = ops.ftp_config()
        if not cfg.host:
            raise ConfigError(
                "upload.host 未配置。在分档表的 upload 段填 host/user/remote_dir，"
                "密码建议放环境变量 TAGGER_FTP_PASSWORD。"
            )
    else:
        cfg = ops.ftp_config()

    if cfg.host:
        print(f"[ftp] 目标 {cfg.user}@{cfg.host}:{cfg.port}{cfg.remote_dir}")
    ok, fail = archive_dir_to_ftp(
        cfg,
        args.out_dir,
        enabled=True,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(f"[tagger] dry-run 完成：{ok} 个分片待上传")
        return 0
    print(f"[tagger] 上传完成：成功 {ok}，失败 {fail}")
    if fail:
        print("[tagger] 失败的分片仍保留在本地，可再次执行本命令重传。")
    return 0 if fail == 0 else 1


# ---------------------------------------------------------------- 分桶


def cmd_buckets(args: argparse.Namespace) -> int:
    """预览分桶表：每个宽高比在总预算下落成什么尺寸。

    这个命令不需要显卡，任何一台机器都能跑——因为分桶是纯几何计算，
    它的结果必须全池一致，而"先跑起来看看"是最不可靠的对齐方式。
    """
    from .bucket import assign_bucket
    from .config import resolve_buckets

    table = load_tier_table(args.tiers)
    defaults = table.get("defaults") or {}
    max_pixels = int(args.max_pixels or defaults.get("max_pixels", 589824))
    buckets = resolve_buckets(defaults.get("buckets", True), max_pixels)

    if not buckets:
        print("[tagger] 分桶已关闭（buckets: false），图片按 max_pixels 等比缩放。")
        return 0

    side = int(round(max_pixels ** 0.5))
    print(f"[tagger] 总预算 {max_pixels} px（等效正方形 {side}×{side}）")
    print(f"[tagger] {len(buckets)} 个桶，尺寸对齐 32（= patch 16 × merge 2）")
    print()
    print("  桶          目标尺寸        面积      token   预算占用")
    for b in buckets:
        print("  " + b.describe(max_pixels))

    tok = [b.tokens for b in buckets]
    print()
    print(f"  token 区间 {min(tok)}～{max(tok)}，跨 {max(tok) / min(tok):.2f} 倍。")
    print("  非方形桶填不满预算属于几何限制（32 对齐），不是配置写错。")

    if args.images:
        print()
        print("  图片分配：")
        from PIL import Image

        for p in args.images:
            path = Path(p)
            try:
                with Image.open(path) as im:
                    w, h = im.size
            except OSError as exc:
                print(f"    {path.name}: 读取失败（{exc}）")
                continue
            b = assign_bucket(w, h, buckets, max_pixels=max_pixels)
            ratio = w / h if h else 0
            if b is not None:
                print(f"    {path.name:<28} {w}x{h} ({ratio:.2f})  →  "
                      f"{b.name}  {b.width}x{b.height}")
            else:
                print(f"    {path.name:<28} {w}x{h} ({ratio:.2f})  →  "
                      f"超出设计范围，按原比例处理")
    return 0


# ---------------------------------------------------------------- 画师标签


def cmd_artists(args: argparse.Namespace) -> int:
    """预览画师名提取：每个目录名对应多少张图、哪些还缺英文别名。

    在正式打标前跑一次，比打完几百万张才发现第一层目录其实是分类名要好。
    """
    from .artist import AliasTable, extract_artist, needs_translation
    from .pack import discover_inputs

    table = load_tier_table(args.tiers)
    ops = load_ops(table)
    rule = ops.artist_rule()
    if args.enable:
        rule.enabled = True
    if not rule.enabled:
        print("[tagger] 画师标签未启用（artist.enabled: false）。")
        print("        加 --enable 可先行预览，不必改配置。")
        return 0

    if args.aliases:
        rule.aliases_path = args.aliases

    table_obj = AliasTable(rule.aliases_path)
    known = table_obj.as_dict()

    items = discover_inputs(args.input_dir, limit=args.limit)
    if not items:
        print(f"[tagger] {args.input_dir} 下没有发现图片")
        return 1

    counts: dict[str, int] = {}
    reasons: dict[str, int] = {}
    mapped_count = 0
    for it in items:
        tag = extract_artist(it.logical_path, rule, known)
        if tag.raw:
            counts[tag.raw] = counts.get(tag.raw, 0) + 1
            mapped_count += int(tag.mapped)
        else:
            reasons[tag.reason] = reasons.get(tag.reason, 0) + 1

    total = len(items)
    found = sum(counts.values())
    print(f"[tagger] 扫描 {total} 张，depth={rule.depth}，前缀 {rule.prefix!r}")
    print(f"[tagger] 识别出 {len(counts)} 个画师名，覆盖 {found} 张"
          f"（{found / total * 100:.1f}%），{total - found} 张取不到")
    if table_obj.path is not None:
        print(f"[tagger] 翻译表 {table_obj.path}"
              f"（{len(table_obj)} 条，{len(table_obj.missing)} 条待翻译）")
    print()

    pending: list[str] = []
    for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: args.top]:
        note = ""
        english = table_obj.get(name)
        if english:
            shown = extract_artist(f"{name}/x.jpg", rule, known).tag
            note = f"  → {rule.prefix}{shown}" if shown != name else ""
        elif needs_translation(name):
            note = "  ← 待翻译"
            pending.append(name)
        print(f"  {n:6d}  {rule.prefix}{name}{note}")

    if len(counts) > args.top:
        print(f"  … 其余 {len(counts) - args.top} 个未显示（--top 可调）")

    if reasons and found == 0:
        print()
        print("  取不到的原因：")
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {n:6d}  {reason}")

    if pending:
        print()
        print(f"  {len(pending)} 个非英文名还没有译文：")
        for name in pending[:10]:
            print(f"    {name}")
        if len(pending) > 10:
            print(f"    … 其余 {len(pending) - 10} 个")
        print("  没有自动音译是故意的：同一名字被翻成两种写法，标签就废了。")

    if mapped_count:
        print(f"\n  已命中翻译表 {mapped_count} 张。")

    # 累积：遇到没登记过的名字先记进表（译文留空），下次不必再判断。
    # 这一步只登记不翻译，翻译仍然必须由人填。
    if not args.no_record:
        added = table_obj.add_pending(counts.keys())
        if added:
            table_obj.save()
            print(f"\n[tagger] 新登记 {len(added)} 个名字到 {table_obj.path}"
                  f"（译文留空，待人工填写）")
            print("        填好后下次运行会自动命中。加 --no-record 可关闭登记。")
        elif table_obj.dirty:
            table_obj.save()

    if args.write_aliases:
        out = Path(args.write_aliases)
        out.parent.mkdir(parents=True, exist_ok=True)
        template = {name: table_obj.get(name) or "" for name in sorted(counts)}
        out.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"\n[tagger] 翻译表已写出到 {out}（值为空 = 沿用原名）")
    return 0


# ---------------------------------------------------------------- 分发


def cmd_split(args: argparse.Namespace) -> int:
    """生成分发包与分包表。

    image_id 在这里一次性分配完毕，之后本地产出只照着填，不再生成。
    这是多人合并能对上账的前提——见 naming.py 里的说明。
    """
    from .artist import AliasTable, extract_artist
    from .distribute import (DistItem, assign_ids, plan_shards, read_shard_manifest,
                             write_shard, write_table)
    from .pack import discover_inputs

    table = load_tier_table(args.tiers)
    ops = load_ops(table)
    rule = ops.artist_rule()
    rule.enabled = True  # 分发必须知道画师，否则 image_id 里没有归属
    if args.aliases:
        rule.aliases_path = args.aliases

    cfg = ops.distribute or {}
    layout = str(args.layout or cfg.get("layout", "flat")).lower()
    mode = str(args.mode or cfg.get("mode", "count")).lower()
    per_shard = int(args.per_shard or cfg.get("per_shard", 20000))
    max_bytes = int(args.max_bytes or cfg.get("max_bytes_per_shard", 0))
    seq_width = int(cfg.get("seq_width", 6))

    # 分包表是"哪个序号给了哪张图"的唯一凭证。对同一张表再跑一次 split
    # 会从 1 开始重新分配，把旧批次已发出的 image_id 静默指给另一批图——
    # 成员照着旧表做的产物在 merge 时全部变成内容冲突。所以宁可报错。
    table_exists = Path(args.table).is_file()
    if table_exists:
        raise ConfigError(
            f"分包表已存在: {args.table}。继续写会用新序号覆盖旧批次的分配记录。\n"
            f"  分第二批素材请换一个 --table 文件名；确定要丢弃旧记录就先删掉它。")

    aliases = AliasTable(rule.aliases_path)
    items = discover_inputs(args.input_dir, limit=args.limit,
                            recursive=not args.no_recursive)
    if not items:
        print(f"[tagger] {args.input_dir} 下没有发现图片")
        return 1

    # 去重排除：清单里的 key（源::成员）不进包、不占 image_id。
    # 集合差即可——split 本来就不读图片字节，这个性质保持不变。
    if getattr(args, "exclude", None):
        from .dedup import load_exclude

        exclude = load_exclude(args.exclude)
        before = len(items)
        items = [it for it in items if it.key not in exclude]
        print(f"[tagger] 去重清单：排除 {before - len(items)} 张（{args.exclude}）")
        if not items:
            print("[tagger] 排除后没有剩余图片")
            return 1

    dist_items: list[DistItem] = []
    no_artist = 0
    for it in items:
        a = extract_artist(it.logical_path, rule, aliases.as_dict())
        if not a.tag:
            no_artist += 1
        dist_items.append(DistItem(
            source=it.source,
            member=it.member,
            rel_path=it.rel_path,
            artist=a.tag or "",
            size=it.size,
            ext="",
        ))

    assign_ids(dist_items, seq_width=seq_width)
    plans = plan_shards(dist_items, mode=mode, per_shard=per_shard,
                        max_bytes=max_bytes)

    print(f"[tagger] 共 {len(dist_items)} 张，切成 {len(plans)} 个包")
    print(f"[tagger] 布局 {layout}，模式 {mode}"
          + (f"，每包 {per_shard} 张" if mode == "count"
             else f"，每包 ≤ {max_bytes / 1073741824:.1f} GB"))
    if no_artist:
        print(f"[tagger][警告] {no_artist} 张取不到画师名，image_id 将只有序号")
    print()

    for plan in plans:
        write_shard(plan, args.out_dir, layout=layout, compress="stored")

    table_path = write_table(plans, args.table, next_seq=1 + len(dist_items))
    print()
    print(f"[tagger] 分包表写入 {table_path}")
    print(f"         总计 {sum(p.count for p in plans)} 张，"
          f"{sum(p.bytes for p in plans) / 1073741824:.2f} GB")

    if args.verify_pack:
        print()
        print("[tagger] 回读校验每个包的 manifest：")
        bad = 0
        for plan in plans:
            try:
                rows = read_shard_manifest(Path(args.out_dir) / plan.name)
            except (KeyError, OSError) as exc:
                print(f"  ✗ {plan.name}: {exc}")
                bad += 1
                continue
            if len(rows) != plan.count:
                print(f"  ✗ {plan.name}: manifest {len(rows)} 条，计划 {plan.count} 条")
                bad += 1
            elif rows[0]["image_id"] != plan.first_id or rows[-1]["image_id"] != plan.last_id:
                print(f"  ✗ {plan.name}: 首尾 id 与分包表不符")
                bad += 1
        print(f"  {'全部一致' if not bad else f'{bad} 个包有问题'}")
        return 1 if bad else 0
    return 0


def cmd_dedup(args: argparse.Namespace) -> int:
    """图片去重：发分包前扫重复图，产出排除清单与报告，不动素材文件。

    几百万张的哈希要按小时算，所以支持断点续跑：哈希落在 <输出>/hashes.db，
    中断或改阈值重跑时，文件没变的部分直接复用，不用从头来。
    """
    from .dedup import DedupParams, DedupStopped, run_dedup

    params = DedupParams(
        input_dir=args.input_dir,
        out_dir=args.out_dir or "",
        mode=args.mode,
        threshold=args.threshold,
        keep=args.keep,
        recursive=not args.no_recursive,
        workers=args.workers,
    )

    def on_log(msg: str) -> None:
        print(f"[tagger] {msg}")

    last = {"done": -1}

    def on_progress(done: int, total: int, cur: str) -> None:
        # 进度按 500 张步进打印，避免每张图刷一行
        if total and (done == total or done - last["done"] >= 500):
            last["done"] = done
            print(f"[tagger] 已结算 {done}/{total}（{done * 100 // total}%）"
                  + (f" {cur}" if cur else ""))

    try:
        summary = run_dedup(params, on_progress=on_progress, on_log=on_log)
    except DedupStopped:
        print("[tagger] 已停止：扫过的哈希都在缓存里，重跑会接着来")
        return 1
    except KeyboardInterrupt:
        print("\n[tagger] 中断：扫过的哈希都在缓存里，重跑会接着来")
        return 1

    print(f"[tagger] 报告 {summary['report_file']}")
    print(f"[tagger] 发分包时加 --exclude {summary['exclude_file']} 可排除这些重复图")
    return 0


# ---------------------------------------------------------------- 客户端


def cmd_client(args: argparse.Namespace) -> int:
    """成员端：照分发包的 manifest 顺序打标，产出 -tag.json / -nl.json 交回总机。

    这条命令不查显卡、不做分档匹配：它连的是**已经把模型跑起来的那个服务**
    （自己的机器起、或者用 --url 连别人的都可以）。硬件体检是 serve 的事，
    客户端只需要知道服务在哪、叫什么名字。
    """
    from .client import run_client

    table = load_tier_table(args.tiers)
    ops = load_ops(table)
    return run_client(args, ops, table)


# ---------------------------------------------------------------- 合并


def cmd_merge(args: argparse.Namespace) -> int:
    """回收成员产出，对账后写总表，并生成抽检清单。"""
    from .distribute import read_shard_manifest
    from .merge import (MergeStats, check, collect, collect_provenance,
                        load_shard_plans, provenance_warnings, sample_records,
                        write_merged, write_sample_sheet)

    table = load_tier_table(args.tiers)
    ops = load_ops(table)
    spec = ops.output_spec()
    cfg = ops.merge or {}

    dataset_name = args.dataset or str(cfg.get("dataset_name", ""))
    image_root = str(cfg.get("image_root", "/image"))
    image_ext = str(cfg.get("image_ext", ".jpg"))
    sample_n = int(args.sample_n or cfg.get("sample_n", 20))
    sample_seed = int(cfg.get("sample_seed", 42))

    require = args.require or spec.mode
    if require not in ("nl", "tag", "both"):
        raise ConfigError(f"--require 只能是 nl / tag / both，收到 {require!r}")

    # 应产出的 id 从分发包 manifest 汇总。没有它就只能做内容检查，
    # 发现不了"整条没交"。
    expected: list[str] | None = None
    if args.shard_dir:
        ids: list[str] = []
        for shard in sorted(Path(args.shard_dir).glob("dist-*.zip")):
            try:
                ids.extend(r["image_id"] for r in read_shard_manifest(shard))
            except (KeyError, OSError) as exc:
                print(f"[merge][警告] 读不动 {shard.name}: {exc}")
        expected = ids
        print(f"[merge] 分发表列出 {len(expected)} 个 image_id")

    # 分包总表：只有区间，没有逐条 id。它的用处是回答"这一包交了没有"——
    # 分发包已经不在了（成员删了、没传上来）时，逐条对账就会静默退化成
    # "自己跟自己比"，整包缺失完全看不见。
    plans: list[dict] | None = None
    explicit_table = bool(args.table)
    table_path = args.table or str((ops.merge or {}).get("shard_table") or "")
    if table_path:
        p = Path(table_path)
        if not p.is_file():
            # 配置里的默认路径不存在是常态（还没跑过 split），只在显式指定时报。
            if explicit_table:
                print(f"[merge][警告] 分包总表不存在: {p}（整包缺失将无法检测）")
        else:
            try:
                plans = load_shard_plans(p)
            except (ValueError, OSError) as exc:
                print(f"[merge][警告] 分包总表读不动 {p}: {exc}")
            else:
                total_planned = sum(int(x.get("count") or 0) for x in plans)
                print(f"[merge] 分包总表 {p}：{len(plans)} 个包、{total_planned} 条")
                if expected is not None and len(expected) != total_planned:
                    # 两个来源应当一致。不一致意味着分发包少了几份——
                    # 那正是"整包没交"在分发侧的形态。
                    print(f"[merge][警告] 分发包 {len(expected)} 条 ≠ 分发表 "
                          f"{total_planned} 条（差 {abs(total_planned - len(expected))}，"
                          f"可能整包没拿到或没上传）")

    print(f"[merge] 回收目录：{', '.join(args.roots)}")
    stats = MergeStats()
    records = collect(args.roots, stats=stats)
    check(records, expected_ids=expected, require=require, stats=stats,
          shard_plans=plans)

    # 溯源一致性：混进来几代 prompt / 几个 tag 后端，正文是看不出来的
    prov = collect_provenance(records, stats=stats)
    prov_warn = provenance_warnings(prov)

    print()
    for line in stats.summary_lines():
        print("  " + line)

    if prov_warn:
        print()
        print("  溯源不一致（这批数据来自不同配置，混进总表会污染分布）：")
        for msg in prov_warn:
            print(f"    {msg}")
        print("    → 要么重跑统一配置，要么按来源拆成两批分别训练。")
    else:
        dims = "、".join(f"{k}={next(iter(v), '-')}" for k, v in prov.items() if v)
        if dims:
            print(f"  溯源        : {dims}")

    if stats.by_artist:
        empty_artists = [a for a, r in stats.by_artist.items() if r["got"] == 0]
        if empty_artists:
            print()
            print(f"  整片没有产出（疑似空文件夹或画师名没识别）{len(empty_artists)} 个：")
            for a in sorted(empty_artists)[: args.top]:
                print(f"    {a}")

    if stats.empty_roots:
        print()
        print(f"  整份没交（回收目录里一份产出都没有）{len(stats.empty_roots)} 个：")
        for r in stats.empty_roots[: args.top]:
            print(f"    {r}")
        print("    → 先确认这个人是否真的跑了、目录是否传对了；"
              "空目录和没交对最终结果没有区别。")

    whole = stats.whole_missing_shards
    if whole:
        lack = sum(c.expected for c in whole)
        print()
        print(f"  整包没交 {len(whole)} 个（分发表里有、回收 0 条，共 {lack} 条）：")
        for c in whole[: args.top]:
            print(f"    {c.name}  {c.first_id} ~ {c.last_id}  {c.expected} 条")
        if len(whole) > args.top:
            print(f"    … 其余 {len(whole) - args.top} 个略")

    short = stats.short_shards
    if short:
        print()
        print(f"  分包不足 {len(short)} 个（共缺 {sum(c.short for c in short)} 条）：")
        for c in short[: args.top]:
            print(f"    {c.name}  回来 {c.got}/{c.expected}  缺 {c.short}")

    if stats.unassigned_ids:
        print()
        print(f"  不在分发表内 {len(stats.unassigned_ids)} 个 id"
              f"（分配记录与产物对不上）：")
        for i in stats.unassigned_ids[: args.top]:
            print(f"    {i}")

    if stats.extra_ids:
        print()
        print(f"  多出（不在分发表内）{len(stats.extra_ids)} 个 id"
              f"（不算错，但分配记录与实际不符，前 {min(len(stats.extra_ids), args.top)} 个）：")
        for i in stats.extra_ids[: args.top]:
            print(f"    {i}")

    if stats.missing_ids:
        print()
        print(f"  整条缺失（前 {min(len(stats.missing_ids), args.top)} 个）：")
        for i in stats.missing_ids[: args.top]:
            print(f"    {i}")

    if stats.idempotent_repeats:
        print()
        print(f"  重复提交 {len(stats.idempotent_repeats)} 次，内容一致已取一份（不阻断）：")
        for item in stats.idempotent_repeats[: args.top]:
            print(f"    {item}")
        if len(stats.idempotent_repeats) > args.top:
            print(f"    … 其余 {len(stats.idempotent_repeats) - args.top} 处略")

    if stats.duplicates:
        print()
        print(f"  内容冲突 {len(stats.duplicates)} 个 id（同一 id 两份不同内容，必须人工裁决）：")
        for iid, files in list(stats.duplicates.items())[: args.top]:
            print(f"    {iid}")
            for f in files[:4]:
                print(f"      {f}")

    out, written = write_merged(
        records, args.out,
        dataset_name=dataset_name, image_root=image_root, image_ext=image_ext,
        fmt=args.format, require=require, skip_empty=not args.keep_empty,
    )
    print()
    print(f"[merge] 总表写入 {out}（{written} 条，格式 {args.format}）")
    print(f"        image_path 模板：{image_root}/{dataset_name or '<数据集名>'}/<image_id>{image_ext}")

    if sample_n:
        samples = sample_records(records, n=sample_n, seed=sample_seed)
        sheet = write_sample_sheet(
            samples, args.sample_out,
            dataset_name=dataset_name, image_root=image_root, image_ext=image_ext)
        print(f"[merge] 抽检清单写入 {sheet}（{len(samples)} 条，seed={sample_seed}）")

    if stats.failed:
        print()
        print(f"[merge] 不通过：缺失 {len(stats.missing_ids)}、"
              f"内容冲突 {len(stats.duplicates)}、不可读 {len(stats.unreadable)}、"
              f"整份没交 {len(stats.empty_roots)}、整包没交 {len(whole)}")
        return 1
    print()
    print(f"[merge] 通过：{stats.complete}/{stats.expected} 条完整")
    return 0


# ---------------------------------------------------------------- 复核


def cmd_verify(args: argparse.Namespace) -> int:
    """复核产物完整性：数量对账 + 内容非空 + 结构完整。

    这是打标流程的验收环节。跑完不报错不等于标对了，但至少要保证
    「输入多少张，输出多少条，每条都有内容」——这条底线失守比标得不准更致命。
    """
    from .pack import discover_inputs
    from .verify import verify_dir

    table = load_tier_table(args.tiers)
    ops = load_ops(table)
    spec = ops.output_spec()
    # 产物该有什么内容，取决于它是哪种模式产出的。tag 模式的批次若按 both
    # 复核，每一条都会被判成"缺 nl"——那不是产物有问题，是复核口径用错了。
    mode = str(getattr(args, "mode", None) or spec.mode).lower()

    expected = None
    if args.input_dir:
        items = discover_inputs(
            args.input_dir,
            recursive=not args.no_recursive,
            include_zips=not args.no_zips,
            limit=args.limit,
        )
        # 期望名要跟着转码规则走：pack.prepare 重编码后把扩展名改成 .jpg，
        # 产物里的 name 也是改过的。对账还拿原始名，非 JPEG 输入会被
        # 全部判成"缺失"——输入 PNG 打出来的包永远验不过，且没人知道为什么。
        # non_jpeg 模式下 pack 按字节魔数决定是否重编码，这里只能按扩展名
        # 估算（读每个成员验魔数太贵）；极少数"扩展名与编码不符"的输入
        # 会在这条路径上误报缺失，人工看一眼就能识别。
        reencode = ops.pack_options().reencode_mode

        def _expected_name(flat: str) -> str:
            if reencode == "always":
                return f"{Path(flat).stem}.jpg"
            if reencode == "non_jpeg" and not flat.lower().endswith((".jpg", ".jpeg")):
                return f"{Path(flat).stem}.jpg"
            return flat

        expected = {_expected_name(it.flat_name) for it in items}
        print(f"[verify] 输入清单 {len(expected)} 张（{args.input_dir}，"
              f"转码 {reencode}）")

    print(f"[verify] 复核 {args.out_dir}")
    print(f"         模式 mode={mode}，"
          f"NL 预算 {spec.nl_min_tokens}~{spec.nl_max_tokens} token")
    print()

    try:
        report = verify_dir(
            args.out_dir,
            expected=expected,
            mode=mode,
            nl_min_tokens=spec.nl_min_tokens,
            nl_max_tokens=spec.nl_max_tokens,
            max_samples=args.max_samples,
        )
    except RuntimeError as exc:
        # 典型情形：目录里是 parquet 分片但没装 pyarrow。
        # 这不该打成一屏 traceback —— 它是一句"请装依赖"就能解决的事。
        print(f"\n[verify][错误] {exc}", file=sys.stderr)
        return 2

    if report.shards == 0:
        print("  [错误] 没有找到任何 .tar / .zip / .parquet 分片，"
              "检查 --out-dir 是否正确")
        return 2

    for line in report.summary_lines():
        print("  " + line)

    errors = [i for i in report.issues if i.level == "error"]
    warns = [i for i in report.issues if i.level != "error"]

    if errors:
        print()
        print(f"  错误明细（显示前 {min(len(errors), args.top)} 条，共 {len(errors)}）：")
        for i in errors[: args.top]:
            print(f"    ✗ {i.kind}: {i.detail}")
        if len(errors) > args.top:
            print(f"    … 其余 {len(errors) - args.top} 条略")

    if warns:
        print()
        print(f"  提示（{len(warns)} 条，不阻断）：")
        for i in warns[: min(len(warns), args.top)]:
            print(f"    · {i.kind}: {i.detail}")

    if report.missing:
        print()
        print("  缺失样本（前 10 个）：")
        for name in report.missing[:10]:
            print(f"    {name}")
        if len(report.missing) > 10:
            print(f"    … 其余 {len(report.missing) - 10} 个")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "scanned": report.scanned,
            "ok": report.ok,
            "shards": report.shards,
            "counts": report.counts,
            "missing": report.missing,
            "extra": report.extra,
            "issues": [{"kind": i.kind, "level": i.level, "detail": i.detail}
                       for i in report.issues],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[verify] 报告写入 {out}")

    print()
    if report.failed:
        print(f"[verify] 不通过：{len(errors)} 处错误。这些样本不能直接用。")
        return 1
    print(f"[verify] 通过：{report.ok}/{report.scanned} 条完整。")
    return 0


# ---------------------------------------------------------------- 报告


def cmd_report(args: argparse.Namespace) -> int:
    """把产物折成批次，输出折线图能直接吃的数据。

    批次大小优先取命令行，其次取配置 report.batch_size。之所以两边都给：
    试跑时想按 100 条看，正式跑想看 5000 条——改命令行比改配置快，
    但配置得有个合理默认值，否则新人直接用会得到 3 万格的噪声图。
    """
    from .report import (build_report, collect_points,
                        collect_points_from_archives, summary_lines,
                        write_report)

    batch_size = int(args.batch_size or 0)
    order = str(args.order or "")
    require = "any"
    if not batch_size or not order:
        try:
            ops = load_ops(load_tier_table(args.tiers))
        except ConfigError:
            ops = None
            print(f"[report][警告] 读不到分档表 {args.tiers}，"
                  f"批次大小按 1000、顺序按 id 处理")
        else:
            cfg = ops.report or {}
            batch_size = batch_size or ops.report_batch_size()
            order = order or str(cfg.get("order") or "id")
        if ops is not None:
            # 完整率按配置声明的产出模式判定（both = tag 和 NL 都得有），
            # 与 merge 的 require 同源。读不到配置才退回"有任一内容就算"。
            require = ops.output_spec().mode
    batch_size = batch_size or 1000
    order = order or "id"

    unreadable: list[str] = []
    if args.archives:
        try:
            points = collect_points_from_archives(args.out_dir,
                                                  unreadable=unreadable,
                                                  require=require)
        except RuntimeError as exc:
            print(f"\n[report][错误] {exc}", file=sys.stderr)
            return 2
        source = f"archives:{args.out_dir}"
    else:
        roots = list(args.roots or [args.out_dir])
        points = collect_points(roots, unreadable=unreadable, require=require)
        source = "artifacts:" + ", ".join(str(r) for r in roots)

    print(f"[report] 来源   : {source}")
    print(f"[report] 批次   : 每 {batch_size} 条一批，按 {order} 排序"
          f"（完整率口径 {require}）")

    report = build_report(points, batch_size=batch_size, order=order,
                          source=source,
                          extra={"unreadable": unreadable[:200],
                                 "unreadable_count": len(unreadable)})

    print()
    for line in summary_lines(report):
        print("  " + line)
    if unreadable:
        print(f"  读不动     : {len(unreadable)} 项（前 3：{'；'.join(unreadable[:3])}）")

    written = write_report(report, json_out=args.json_out, md_out=args.md_out,
                           top=args.top)
    print()
    for p in written:
        print(f"[report] 写入 {p}")

    if not points:
        print()
        print("[report] 没有扫到任何产出：检查 --roots / --out-dir 是否指对了目录。")
        return 1
    return 0


# ---------------------------------------------------------------- AB 对比


def cmd_abtest(args: argparse.Namespace) -> int:
    """同一批图跑多套 prompt，按变体分目录，最后出对比表。

    为什么复用 run_client 而不是另写一条流程：AB 的价值在于"只有 prompt
    不同"。另写一条流程，早晚会有一处参数（并发/重试/超时/跳过判定）跟
    正式跑不一致，那时差异就归因不清了。这里只是给每个变体换一份
    out_dir 与 prompt，其余全部同源。
    """
    from . import abtest
    from .client import run_client

    table = load_tier_table(args.tiers)
    ops = load_ops(table)
    variants = abtest.resolve_variants(ops.variants, args.variants)

    root = Path(args.out_dir)
    root.mkdir(parents=True, exist_ok=True)
    print(f"[abtest] 变体 {len(variants)} 套：{', '.join(v.name for v in variants)}")
    print(f"[abtest] 输出根目录：{root.resolve()}")

    code = 0
    names: list[str] = []
    for i, v in enumerate(variants, 1):
        print()
        print(f"===== [{i}/{len(variants)}] 变体 {v.name} =====")
        sub = argparse.Namespace(**vars(args))
        sub.out_dir = str(root / v.name)
        sub.variant = v.name
        try:
            code |= run_client(sub, ops, table)
        finally:
            names.append(v.name)

    result = abtest.compare_variants(root, names)
    md_out = args.compare_out or str(root / "compare.md")
    json_out = args.compare_json or str(root / "compare.json")
    abtest.write_compare(result, json_out=json_out, md_out=md_out)

    print()
    print("[abtest] 对比：")
    for line in abtest.summary_lines(result):
        print("  " + line)
    print()
    print(f"[abtest] 对比表 {md_out}")
    print(f"[abtest] 对比数据 {json_out}")
    if result.get("unreadable"):
        print(f"[abtest][警告] {len(result['unreadable'])} 项读不动，详见对比表")
    return code


# ---------------------------------------------------------------- 入口


def cmd_tag(args: argparse.Namespace) -> int:
    """本地打标：一个图片目录进，`<image_id>-tag.json` 出，全程不连 vLLM。

    与 `client` 唯一的分野是输入形态：那边要求 split 出来的分发包（里面才有
    manifest），这边直接吃散图。产物字段与 client 的 tag 产物对齐，将来的
    merge / report 不用改就能读。
    """
    from .cpu_tagger import TaggerUnavailable, build_backend, resolve_device
    from .local_tag import scan_images, tag_items

    items = scan_images(args.input_dir, recursive=not args.no_recursive,
                        limit=args.limit)
    if not items:
        raise ConfigError(f"{args.input_dir} 里没有找到图片（png/jpg/webp/bmp/gif/tif）")

    backend = build_backend(
        args.backend, model=args.model, model_dir=args.model_dir,
        device=resolve_device(args.device), precision=args.precision,
        batch_size=max(1, args.batch),
        preprocess=getattr(args, "preprocess", "pil"),
        thresholds=({"default": args.threshold} if args.threshold is not None else None),
        verbose=True,
    )
    if backend is None:                       # choices 里没有 none，这里是纯保险
        raise ConfigError("本地打标必须有一个 tag 后端")
    try:
        backend.load()
    except TaggerUnavailable as exc:
        raise ConfigError(f"tag 后端加载失败：{exc}") from exc

    out_dir = Path(args.out_dir) if args.out_dir else (
        Path(args.input_dir).resolve().parent / f"{Path(args.input_dir).resolve().name}-tags")
    print(f"[tag] {len(items)} 张 → {out_dir}")

    seen = {"n": 0}
    stop = {"flag": False}

    def progress(kind: str, item, extra, stats) -> None:
        seen["n"] += 1
        if seen["n"] % 20 == 0 or kind == "failed":
            mark = "!" if kind == "failed" else " "
            print(f"  {mark}{stats.done + stats.failed}/{stats.total}  "
                  f"{item.image_id}  {kind}", flush=True)

    # Ctrl+C 第一次只置标记：交给引擎在块边界收尾，正在算的那一块算完、产物
    # 原子落盘。硬中断（默认行为）会把这一块丢在半空，而"续跑"靠的是
    # "产物存在即跳过"——半截状态既不报错也不会被重算，只会显示成莫名少几张。
    # 第二次按才按原样抛出，留一个"我就是要立刻停"的出口。
    import signal

    def _on_sigint(signum, frame):        # noqa: ANN001 - signal 的固定签名
        if stop["flag"]:
            raise KeyboardInterrupt
        stop["flag"] = True
        print("\n[tag] 收到 Ctrl+C，当前块算完就收尾（再按一次立即退出）", flush=True)

    previous = signal.signal(signal.SIGINT, _on_sigint)
    try:
        stats = tag_items(backend, items, out_dir=out_dir, force=args.force,
                          on_progress=progress,
                          should_stop=lambda: stop["flag"])
    finally:
        signal.signal(signal.SIGINT, previous)
    print()
    for line in stats.summary_lines():
        print(line)
    if stats.decode_ms or stats.forward_ms:
        print(f"  其中解码 {stats.decode_ms} ms / 前向 {stats.forward_ms} ms")
    return 0 if stats.failed == 0 else 1


def cmd_pack(args: argparse.Namespace) -> int:
    """打标 + 封包一条龙：目录或单个 zip 进，parquet/tar/zip 分片出。

    和 `tag` 的区别只有一个：走 `pipeline.plan_inputs` 拿全局序号
    （`画师-000123`），打完再调 `pack_items` 封包。凡是"要给训练侧的成品"，
    id 必须是全局的——相对路径在多个包之间会撞名，撞了不报错，只是覆盖。

    两阶段都在同一个命令里，但产物是分开的：
        <out>/tags/<id>-tag.json    中间产物（断点续跑靠它）
        <out>/pack-*.parquet|tar|zip 成品
    所以改质量/采样重跑一次，打标那一步会全部跳过，只重封。
    """
    from .artist import AliasTable
    from .pack import PackOptions
    from .pipeline import (PackReport, PackSpaceError, pack_items, plan_inputs,
                           plan_stats)

    table = load_tier_table(args.tiers)
    ops = load_ops(table)
    cfg = ops.archive or {}
    rule = ops.artist_rule()
    # None = 跟随配置（artist.enabled），True/False = 命令行显式覆盖
    if args.artist is not None:
        rule.enabled = bool(args.artist)
    if args.artist_depth is not None:
        rule.depth = max(0, args.artist_depth)
    if args.no_artist_inject:
        rule.inject_caption = False

    formats = args.formats or cfg.get("formats") or ["parquet"]
    if isinstance(formats, str):
        formats = [formats]
    options = PackOptions.from_dict({
        **cfg,
        "formats": formats,
        "jpeg_quality": args.quality if args.quality is not None
        else int(cfg.get("jpeg_quality", 95)),
        "jpeg_subsampling": args.subsampling if args.subsampling is not None
        else int(cfg.get("jpeg_subsampling", 0)),
        "compress": args.compress or cfg.get("compress", "stored"),
        "output_max_records": args.max_records if args.max_records is not None
        else int(cfg.get("output_max_records", 5000)),
        "output_max_bytes": args.max_bytes if args.max_bytes is not None
        else int(cfg.get("output_max_bytes", 0)),
    })

    layout = (args.layout or "flat").lower()
    aliases = AliasTable(rule.aliases_path).as_dict() if rule.aliases_path else {}
    plan = plan_inputs(
        args.input_dir,
        recursive=not args.no_recursive,
        include_zips=not args.no_zips,
        include_loose=not args.no_loose,
        limit=args.limit or 0,
        assign_ids=True,
        layout=layout,
        seq_width=int(args.seq_width or 6),
        artist_rule=rule,
        aliases=aliases,
    )
    if not plan.items:
        print(f"[pack] {args.input_dir} 下没有发现图片")
        return 1

    info = plan_stats(plan)
    out_dir = Path(args.out_dir) if args.out_dir else (
        Path(args.input_dir).resolve().parent /
        f"{(Path(args.input_dir).stem if Path(args.input_dir).is_file() else Path(args.input_dir).name)}-tags")

    print(f"[pack] 输入 {plan.input_path}")
    print(f"[pack] 共 {info['total']} 张，{plan.total_bytes / 1048576:.1f} MB，"
          f"来自 {len(plan.sources)} 个来源")
    for row in plan.sources[: args.top]:
        print(f"        {row['count']:7d}  {row['name']}")
    if rule.enabled:
        print(f"[pack] 画师名 {info['artists']['count']} 个，"
              f"覆盖 {info['artists']['covered']} 张"
              + (f"，{info['artists']['missing']} 张取不到" if info["artists"]["missing"] else ""))
    print(f"[pack] 布局 {layout}，输出 {out_dir}")
    print(f"[pack] 格式 {'/'.join(options.formats)}，质量 q{options.jpeg_quality} / "
          f"采样 {_subsampling_label(options.jpeg_subsampling)}，"
          f"每包 {options.max_records} 条")
    print()
    print("[pack] 命名示例：")
    for sample in plan.sample(args.samples):
        print(f"        {sample['rel']}  →  {sample['name']}")

    # ---- 打标
    if not args.skip_tag:
        from .cpu_tagger import TaggerUnavailable, build_backend, resolve_device
        from .local_tag import tag_items

        backend = build_backend(
            args.backend, model=args.model, model_dir=args.model_dir,
            device=resolve_device(args.device), precision=args.precision,
            batch_size=max(1, args.batch),
            preprocess=getattr(args, "preprocess", "pil"),
            thresholds=({"default": args.threshold} if args.threshold is not None else None),
            verbose=True,
        )
        if backend is None:
            raise ConfigError("本地打标必须有一个 tag 后端")
        try:
            backend.load()
        except TaggerUnavailable as exc:
            raise ConfigError(f"tag 后端加载失败：{exc}") from exc

        print()
        print(f"[pack] 打标 {len(plan.items)} 张 → {out_dir / 'tags'}")
        seen = {"n": 0}

        def progress(kind: str, item, extra, stats) -> None:
            seen["n"] += 1
            if seen["n"] % 20 == 0 or kind == "failed":
                mark = "!" if kind == "failed" else " "
                print(f"  {mark}{stats.done + stats.failed}/{stats.total}  "
                      f"{item.image_id}  {kind}", flush=True)

        stats = tag_items(backend, plan.items, out_dir=out_dir, force=args.force,
                          on_progress=progress)
        del backend
        print()
        for line in stats.summary_lines():
            print(line)
    else:
        print()
        print("[pack] 跳过打标（--skip-tag），直接按已有产物封包")

    # ---- 封包
    print()
    print("[pack] 封包…")

    def on_seal(row: dict) -> None:
        print(f"  ✓ {row['name']}  {row['count']} 条  "
              f"{row['bytes'] / 1048576:.1f} MB", flush=True)

    # 命名口径进 packstate 指纹：改了画师层级/开关就不能拿旧记忆冒充已完成
    naming = {"artist": rule.enabled, "artist_depth": rule.depth,
              "seq_width": int(args.seq_width or 6),
              "artist_inject": bool(rule.enabled and rule.inject_caption)}

    try:
        report: PackReport = pack_items(
            plan.items, out_dir, options, layout=layout,
            artist_prefix=rule.prefix,
            inject_artist=bool(rule.enabled and rule.inject_caption),
            naming=naming,
            on_seal=on_seal,
            on_log=lambda message: print(f"  · {message}"),
            check_space=not args.no_space_check,
        )
    except PackSpaceError as exc:
        # 盘位不够是"换个盘再来"的事，不该抛成一堆 traceback。
        print()
        print(f"[pack][磁盘不足] {exc}", file=sys.stderr)
        print("[pack][提示] 换输出盘 / 少选格式 / 调低质量后再跑；"
              "确认估算偏保守可以加 --no-space-check。")
        return 1
    print()
    print(f"[pack] 写入 {report.packed} 条，跳过（已封过）{report.skipped_done} 条，"
          f"缺产物 {report.skipped_missing} 条，失败 {report.failed} 条")
    print(f"[pack] 产出 {len(report.sealed)} 个分片，共 "
          f"{sum(s['bytes'] for s in report.sealed) / 1048576:.1f} MB → {out_dir}")
    if report.skipped_missing:
        print(f"[pack][提示] {report.skipped_missing} 条没有打标产物（打标失败或未跑到），"
              f"它们不会进包；重跑一次会补齐。")
    return 0 if report.failed == 0 and report.packed > 0 else (
        0 if report.skipped_done > 0 else 1)


def _subsampling_label(value: int) -> str:
    return {0: "4:4:4", 1: "4:2:2", 2: "4:2:0"}.get(int(value), "4:4:4")


def cmd_web(args: argparse.Namespace) -> int:
    """起本地 UI 服务。

    默认 `127.0.0.1` 而不是 `0.0.0.0`：这是本机工具，带上 `--host 0.0.0.0`
    就等于把"能读你整块硬盘上任意图片目录"的接口挂到局域网上了，必须显式选择。
    """
    import threading
    import webbrowser

    try:
        import uvicorn
    except ImportError as exc:                # pragma: no cover - 环境问题
        raise ConfigError(
            "缺 web 依赖，装一次即可：\n"
            "  env\\python\\python.exe -m pip install fastapi uvicorn websockets"
        ) from exc

    from .web.app import create_app

    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/"
    print(f"[web] 本地打标 UI : {url}")
    print("[web] 关闭窗口或 Ctrl+C 结束服务")
    if not (Path(__file__).resolve().parent / "web" / "ui" / "dist" / "index.html").is_file():
        print("[web][提示] 前端还没构建，页面会显示构建步骤；"
              "改前端时用 npm run dev（3010 已代理到本端口）")
    if not args.no_open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # 单 worker：任务表在进程内存里，多 worker 会各存一份，页面就会"看不到任务"。
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tagger", description="NL+tag 打标框架")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--tiers", default=DEFAULT_TIERS, help="分档表路径")
        sp.add_argument("--model", default=None, help="强制指定模型 key（覆盖分档表）")
        sp.add_argument("--tier", default=None, help="强制指定分档名")
        sp.add_argument("--max-pixels", type=int, default=None, dest="max_pixels")
        # 并发三件套。默认值在分档表里按"最保守的卡"写死，
        # 而 30M 量级下这几项直接决定要跑几天，必须能在起服务时当场调整。
        sp.add_argument("--max-num-seqs", type=int, default=None,
                        dest="max_num_seqs",
                        help="服务端同时处理多少条请求（默认按分档表）")
        sp.add_argument("--max-num-batched-tokens", type=int, default=None,
                        dest="max_num_batched_tokens",
                        help="单步 token 预算；并发调大而它不跟上，吞吐不会涨")
        sp.add_argument("--gpu-mem", type=float, default=None, dest="gpu_mem",
                        help="显存占比 0~1（默认配置里的 gpu_memory_utilization）")

    sp_doctor = sub.add_parser("doctor", help="硬件体检 + 分档匹配")
    add_common(sp_doctor)
    sp_doctor.set_defaults(func=cmd_doctor)

    # llama.cpp 是默认引擎，但 llama-server 不是 pip 包：没有它连服务都起不来。
    # 单独一条命令，和网页上的安装按钮走同一份实现。
    sp_llama = sub.add_parser("llama", help="llama.cpp 运行时（查看/安装 llama-server）")
    sp_llama.add_argument("llama_cmd", nargs="?", default="status",
                          choices=["status", "install"],
                          help="status（默认）| install")
    sp_llama.add_argument("--zip", default="",
                          help="install 时直接下这个 zip（离线包/镜像/内网）")
    sp_llama.add_argument("--force", action="store_true",
                          help="已经装过也重装（换构建时用）")
    sp_llama.set_defaults(func=cmd_llama)

    sp_serve = sub.add_parser("serve", help="启动 NL 推理服务（vLLM / llama.cpp）")
    add_common(sp_serve)
    sp_serve.add_argument("--engine", choices=["vllm", "llamacpp"], default="llamacpp",
                          help="推理引擎，默认 llamacpp（Windows 原生唯一能上 GPU 的路）"
                               "；vLLM 只有 Linux 版，需 WSL2")
    sp_serve.add_argument("--port", type=int, default=8000)
    sp_serve.add_argument("--data-root", default=None, dest="data_root",
                          help="允许 file:// 访问的图片根目录")
    sp_serve.add_argument("--log", default=None, help="服务日志输出文件")
    sp_serve.add_argument("--timeout", type=int, default=900, help="等待就绪的秒数")
    sp_serve.add_argument("--no-wait", action="store_true", dest="no_wait")
    sp_serve.add_argument("--fingerprint-out", default="run/fingerprint.json",
                          dest="fingerprint_out")
    sp_serve.set_defaults(func=cmd_serve)

    sp_smoke = sub.add_parser("smoke", help="跑几张图验证端到端")
    sp_smoke.add_argument("--url", default="http://127.0.0.1:8000")
    sp_smoke.add_argument("--model-name", default="tagger", dest="model_name")
    sp_smoke.add_argument("--images", nargs="+", required=True)
    sp_smoke.add_argument("--prompt", default=None)
    sp_smoke.add_argument("--max-tokens", type=int, default=400, dest="max_tokens")
    sp_smoke.add_argument("--timeout", type=int, default=90)
    sp_smoke.add_argument("--concurrency", type=int, default=1,
                          help="并发请求数")
    sp_smoke.add_argument("--use-file-url", action="store_true", dest="use_file_url",
                          help="用 file:// 传图（服务端需 --allowed-local-media-path）")
    sp_smoke.add_argument("--out", default="run/smoke.jsonl")
    sp_smoke.set_defaults(func=cmd_smoke)

    sp_bench = sub.add_parser("bench", help="扫并发梯度，找这台机器的饱和点")
    sp_bench.add_argument("--tiers", default=DEFAULT_TIERS)
    sp_bench.add_argument("--url", default="http://127.0.0.1:8000")
    sp_bench.add_argument("--model-name", default="tagger", dest="model_name")
    sp_bench.add_argument("--images", nargs="+", required=True,
                          help="图片路径或 glob（如 'work/imgs/*.jpg'）")
    sp_bench.add_argument("--concurrency", default="1,2,4,8,16",
                          help="并发梯度，逗号分隔")
    sp_bench.add_argument("--repeat", type=int, default=1,
                          help="每张图重复几次（样本少时靠这个凑量）")
    sp_bench.add_argument("--prompt", default=None)
    sp_bench.add_argument("--real-prompt", action="store_true", dest="real_prompt",
                          default=True,
                          help="用产出规格生成的真实指令（默认开；"
                               "关掉才是 smoke 短指令）")
    sp_bench.add_argument("--no-real-prompt", action="store_false", dest="real_prompt",
                          help="用 smoke 短指令（只测纯解码能力时用）")
    sp_bench.add_argument("--max-tokens", type=int, default=800, dest="max_tokens")
    sp_bench.add_argument("--timeout", type=int, default=120)
    sp_bench.add_argument("--images-total", type=int, default=0, dest="images_total",
                          help="总图片数，给了就按建议并发估算跑完要几天")
    sp_bench.add_argument("--machines", type=int, default=1,
                          help="按几台同配置的机器一起算（3000 万这个量级必须看这个）")
    sp_bench.set_defaults(func=cmd_bench)

    sp_guard = sub.add_parser("guard", help="硬件保护状态：温度 / GPU 占用 / 磁盘")
    sp_guard.add_argument("--tiers", default=DEFAULT_TIERS)
    sp_guard.add_argument("--data-dir", default=".", dest="data_dir",
                          help="检查磁盘余量的目录（通常是图片缓存所在盘）")
    sp_guard.add_argument("--watch", action="store_true", help="持续采样")
    sp_guard.add_argument("--interval", type=float, default=5.0)
    sp_guard.set_defaults(func=cmd_guard)

    sp_discover = sub.add_parser("discover", help="扫描输入目录，预览发现与平铺结果")
    sp_discover.add_argument("--input-dir", required=True, dest="input_dir")
    sp_discover.add_argument("--no-recursive", action="store_true", dest="no_recursive",
                             help="只看顶层，不进子目录")
    sp_discover.add_argument("--no-zips", action="store_true", dest="no_zips",
                             help="不展开 zip 分包")
    sp_discover.add_argument("--no-loose", action="store_true", dest="no_loose",
                             help="忽略散图")
    sp_discover.add_argument("--limit", type=int, default=None)
    sp_discover.add_argument("--top", type=int, default=10, help="按来源列前 N 个")
    sp_discover.add_argument("--samples", type=int, default=10, help="展示 N 条平铺示例")
    sp_discover.set_defaults(func=cmd_discover)

    sp_manifest = sub.add_parser("manifest", help="为输入分包生成全局索引")
    sp_manifest.add_argument("--shard-dir", required=True, dest="shard_dir")
    sp_manifest.add_argument("--out", default=None)
    sp_manifest.set_defaults(func=cmd_manifest)

    sp_upload = sub.add_parser("upload", help="把封口的分片上传到 FTP（默认关闭）")
    sp_upload.add_argument("--tiers", default=DEFAULT_TIERS)
    sp_upload.add_argument("--out-dir", default="work/out", dest="out_dir")
    sp_upload.add_argument("--dry-run", action="store_true", dest="dry_run",
                           help="只列出待传分片，不连接服务器")
    sp_upload.add_argument("--force", action="store_true",
                           help="即使 upload.enabled 为 false 也执行上传")
    sp_upload.set_defaults(func=cmd_upload)

    sp_buckets = sub.add_parser("buckets", help="预览分桶表：每个宽高比落在什么尺寸")
    sp_buckets.add_argument("--tiers", default=DEFAULT_TIERS)
    sp_buckets.add_argument("--max-pixels", type=int, default=None, dest="max_pixels")
    sp_buckets.add_argument("--images", nargs="*", default=None,
                            help="可选：把若干张图分到桶里看看")
    sp_buckets.set_defaults(func=cmd_buckets)

    sp_artists = sub.add_parser("artists", help="预览画师名提取与英文别名缺口")
    sp_artists.add_argument("--tiers", default=DEFAULT_TIERS)
    sp_artists.add_argument("--input-dir", required=True, dest="input_dir")
    sp_artists.add_argument("--enable", action="store_true",
                            help="临时启用画师标签（不改配置文件）")
    sp_artists.add_argument("--limit", type=int, default=None)
    sp_artists.add_argument("--top", type=int, default=20, help="显示前 N 个画师")
    sp_artists.add_argument("--aliases", default=None,
                            help="翻译表路径（覆盖配置里的 artist.aliases_path）")
    sp_artists.add_argument("--no-record", action="store_true", dest="no_record",
                            help="不把新名字登记进翻译表")
    sp_artists.add_argument("--write-aliases", default=None, dest="write_aliases",
                            help="把当前识别到的名字导出成翻译表")
    sp_artists.set_defaults(func=cmd_artists)

    sp_split = sub.add_parser("split", help="生成分发包与分包表（分配全局 image_id）")
    sp_split.add_argument("--tiers", default=DEFAULT_TIERS)
    sp_split.add_argument("--input-dir", required=True, dest="input_dir")
    sp_split.add_argument("--out-dir", default="work/dist", dest="out_dir")
    sp_split.add_argument("--table", default="work/shards.json")
    sp_split.add_argument("--mode", default=None, choices=["count", "bytes"])
    sp_split.add_argument("--per-shard", type=int, default=None, dest="per_shard",
                          help="count 模式：每包多少张")
    sp_split.add_argument("--max-bytes", type=int, default=None, dest="max_bytes",
                          help="bytes 模式：每包多少字节，如 8589934592 = 8GB")
    sp_split.add_argument("--layout", default=None, choices=["flat", "mirror"])
    sp_split.add_argument("--aliases", default=None, help="画师翻译表路径")
    sp_split.add_argument("--limit", type=int, default=None)
    sp_split.add_argument("--no-recursive", action="store_true", dest="no_recursive")
    sp_split.add_argument("--verify-pack", action="store_true", dest="verify_pack",
                          help="写完后回读每个包，核对条数与首尾 id")
    sp_split.add_argument("--exclude", default=None,
                          help="排除清单路径（tagger dedup 产出的 dedup-exclude.txt），"
                               "命中的图不进包、不占 image_id")
    sp_split.set_defaults(func=cmd_split)

    sp_dedup = sub.add_parser("dedup", help="图片去重：发分包前扫重复图，产出排除清单")
    sp_dedup.add_argument("--input-dir", required=True, dest="input_dir")
    sp_dedup.add_argument("--out-dir", default=None, dest="out_dir",
                          help="输出目录（默认 <输入名>-dedup，哈希缓存也放这里）")
    sp_dedup.add_argument("--mode", default="similar", choices=["exact", "similar"],
                          help="exact=只查字节级精确重复；similar=加感知相似（默认）")
    sp_dedup.add_argument("--threshold", type=int, default=8,
                          help="相似汉明距离阈值 0~32（默认 8 ≈ 允许轻度重压缩）")
    sp_dedup.add_argument("--keep", default="first", choices=["first", "largest"],
                          help="组内保留哪张：first=编号序最先（默认），largest=文件最大")
    sp_dedup.add_argument("--workers", type=int, default=8, help="并行扫描线程数")
    sp_dedup.add_argument("--no-recursive", action="store_true", dest="no_recursive")
    sp_dedup.set_defaults(func=cmd_dedup)

    sp_client = sub.add_parser("client", help="按分发包打标，产出 -tag.json / -nl.json")
    sp_client.add_argument("--tiers", default=DEFAULT_TIERS)
    sp_client.add_argument("--shard", nargs="+", default=None,
                           help="分发包 zip（或装着 zip 的目录）；用 --hf-repo 时可省略")
    sp_client.add_argument("--hf-repo", default=None, dest="hf_repo",
                           help="HF 私有仓库里的 webdataset（org/name），"
                                "按分片流式处理、下完即删")
    sp_client.add_argument("--hf-pattern", default="*.tar", dest="hf_pattern",
                           help="分片文件名匹配，默认 *.tar")
    sp_client.add_argument("--hf-revision", default="main", dest="hf_revision")
    # 代理：列清单 / 下分片 / parquet 流式共用一份。空 = 直连（但仍信任
    # HTTP(S)_PROXY 环境变量，很多机器本来就靠它出网）。
    sp_client.add_argument("--hf-proxy", default="", dest="hf_proxy",
                           help="HF 请求走的代理，如 http://127.0.0.1:7890 或 "
                                "socks5://127.0.0.1:7891（需 socksio）。空 = 直连/"
                                "沿用环境变量")
    sp_client.add_argument("--hf-cache", default=None, dest="hf_cache",
                           help="分片缓存目录，默认 run/hf-cache/<org--name>"
                                "（跨任务共享；UI 的下载页往同一个目录备片）")
    sp_client.add_argument("--hf-shards", type=int, default=0, dest="hf_shards",
                           help="只跑前 N 个分片（试跑用）")
    sp_client.add_argument("--hf-keep", action="store_true", dest="hf_keep",
                           help="跑完不删分片（默认删，为了省 SSD）；parquet 模式不落盘")
    # 预取：下载与打标并行。串行时每片要空等下载完（0.5~2 GB / 几十秒）。
    sp_client.add_argument("--hf-prefetch", type=int, default=1, dest="hf_prefetch",
                           help="预取几个分片（默认 1；0 = 严格串行）。"
                                "磁盘剩余空间不够时会自动暂停预取")
    # 单文件多连接：国内单条 TCP 常被压到几百 KB/s，靠并发才能跑满带宽。
    sp_client.add_argument("--hf-conns", type=int, default=4, dest="hf_conns",
                           help="下载单个分片用几条并发连接（默认 4，1 = 单连接；"
                                "上限 16）。文件小于 16 MB 或服务端不支持 Range "
                                "时自动退回单连接")
    sp_client.add_argument("--hf-reserve-gb", type=float, default=0.0,
                           dest="hf_reserve_gb",
                           help="给产物留的磁盘余量（GB）；0 = 自动"
                                "（最大分片的 30%%，且不少于 2 GB）")
    sp_client.add_argument("--hf-format", default="auto", dest="hf_format",
                           choices=["auto", "parquet", "webdataset"],
                           help="仓库格式，默认自动探测（先看 parquet 再看 tar）")
    sp_client.add_argument("--hf-image-column", default="image", dest="hf_image_column",
                           help="parquet 里放图片的列名")
    sp_client.add_argument("--hf-path-template", default="{image_id}.jpg",
                           dest="hf_path_template",
                           help="写进总表的 image_path 模板，可用 {image_id} {index} {shard} {repo}")
    sp_client.add_argument("--out-dir", default="work/out", dest="out_dir",
                           help="产物目录，最终整目录交回总机")
    sp_client.add_argument("--url", default="http://127.0.0.1:8000",
                           help="本机/自建服务地址（API 模式请改用 --api-base）")
    sp_client.add_argument("--urls", nargs="*", default=[], dest="urls",
                           help="多卡：其余推理服务的地址。--url 是第一个，"
                                "这里放剩下的；任务按轮询分发，产物照旧写"
                                "--out-dir，不需要事后合并")
    sp_client.add_argument("--model-name", default="tagger", dest="model_name",
                           help="本机：serve 的 --served-model-name；"
                                "API 模式：API 侧的模型 id（如 gpt-4o / claude-sonnet-4-5）")
    # ---- 外部推理 API。三个参数一组，缺 --api-provider 就还是本机引擎。
    sp_client.add_argument("--api-provider", default="", dest="api_provider",
                           choices=["", "local", "openai", "anthropic"],
                           help="推理来源：留空/local = 本机自建；openai = OpenAI 兼容接口"
                                "（也覆盖 LM Studio、one-api）；anthropic = Messages API")
    sp_client.add_argument("--api-base", default="", dest="api_base",
                           help="API 地址，例如 https://api.openai.com/v1 或 "
                                "https://api.anthropic.com；留空按来源取默认域名")
    sp_client.add_argument("--api-key", default="", dest="api_key",
                           help="API 密钥；留空则读 OPENAI_API_KEY / ANTHROPIC_API_KEY")
    sp_client.add_argument("--mode", default=None, choices=["nl", "tag", "both"],
                           help="覆盖配置里的 output.mode")
    sp_client.add_argument("--temperature", type=float, default=None)
    sp_client.add_argument("--top-p", type=float, default=None, dest="top_p")
    sp_client.add_argument("--max-tokens", type=int, default=None, dest="max_tokens")
    sp_client.add_argument("--timeout", type=int, default=None, help="单请求超时秒数")
    sp_client.add_argument("--concurrency", type=int, default=None,
                           help="在途请求数（默认跟服务端 max_num_seqs 走）")
    sp_client.add_argument("--limit", type=int, default=None)
    sp_client.add_argument("--force", action="store_true",
                           help="忽略已有产物，全部重打")
    # 结果缓存。默认关：首轮命中率为 0，而算图哈希本身要 1~2 ms/张。
    sp_client.add_argument("--cache", action="store_true",
                           help="结果缓存：同图同参数直接复用上次产出，不送模型。"
                                "重跑 / 换输出目录 / 多机处理有重叠素材时才划算")
    sp_client.add_argument("--cache-db", default=None, dest="cache_db",
                           help="缓存库路径，默认 run/result-cache.sqlite3（跨任务共享）")
    sp_client.add_argument("--cache-max-gb", type=float, default=8.0,
                           dest="cache_max_gb",
                           help="缓存库上限（GB），超了按最久未用淘汰，默认 8")
    sp_client.add_argument("--layout", default=None, choices=["flat", "mirror"],
                           help="产物目录布局，默认跟配置 distribute.layout")
    sp_client.add_argument("--fingerprint", default="run/fingerprint.json",
                           help="serve 写下的环境指纹，用来填 provenance")
    sp_client.add_argument("--model-id", default=None, dest="model_id",
                           help="强制指定 provenance 里的 model_id")
    sp_client.add_argument("--max-pixels", type=int, default=None, dest="max_pixels")
    # 描述长度引导（字符区间）。只决定"往多长写"，不移动 nl_min/max_tokens
    # 那两道合格线；改它会改变产出分布，所以会写进 provenance。
    sp_client.add_argument("--caption-chars", type=int, nargs=2, default=None,
                           dest="caption_chars", metavar=("LO", "HI"),
                           help="描述长度引导，例如 250 500（默认按 token 区间派生 400-1024）")
    sp_client.add_argument("--tier", default=None, help="写进 provenance 的分档名")
    sp_client.add_argument("--seed", type=int, default=None, help="固定采样种子（可复现）")
    sp_client.add_argument("--tag-backend", default=None, dest="tag_backend",
                           choices=["pixai", "wd-eva02", "cl-tagger-v2", "onnx", "none"],
                           help="tag 反推后端（CPU 跑，与 GPU 的 NL 并行）；"
                                "none = 让 VLM 自己出 tag")
    sp_client.add_argument("--tag-model", default=None, dest="tag_model",
                           help="覆盖后端的 HF 仓库名")
    sp_client.add_argument("--tag-model-dir", default=None, dest="tag_model_dir",
                           help="本地模型目录（离线时用）")
    sp_client.add_argument("--tag-device", default=None, dest="tag_device",
                           help="cpu / cuda:0，默认 auto（有卡就用卡）")
    sp_client.add_argument("--tag-precision", default=None, dest="tag_precision",
                           choices=["fp32", "tf32", "bf16"],
                           help="tag 前向精度：tf32 白拿 1.12~1.47× 且实测 0 tag 改判（默认）；"
                                "bf16 到 2.36×（配 CUDA Graph 3.8×），"
                                "但 79 张里约有 2 个 tag 会真改判")
    sp_client.add_argument("--tag-batch", type=int, default=None, dest="tag_batch",
                           help="tag 攒批张数（仅 wd-eva02 生效）；"
                                "批处理主要在 bf16 下兑现收益，fp32 单给 batch 只有 1.15×")
    sp_client.add_argument("--tag-preprocess", default=None, dest="tag_preprocess",
                           choices=["pil", "tensor"],
                           help="tag 预处理：pil = 现状（CPU 缩放/归一化）；"
                                "tensor = 缩放/归一化上卡（解码仍在 CPU）。"
                                "1024² 大 PNG 实测 1.47×，JPEG/小图只有 1.06~1.08×，"
                                "且重采样换了实现（线稿类图约 1 成 tag 会变）")
    sp_client.add_argument("--hf-token", default=None, dest="hf_token",
                           help="HF token，私有仓库/受限模型需要（也可用环境变量 HF_TOKEN）")
    sp_client.add_argument("--use-file-url", action="store_true", dest="use_file_url",
                           help="用 file:// 传图，需服务端 --allowed-local-media-path 放行产物目录")
    sp_client.add_argument("--failures", default=None,
                           help="失败明细 jsonl，默认 <out-dir>/failures.jsonl")
    sp_client.add_argument("--max-failures", type=int, default=3, dest="max_failures",
                           help="同一 id 累计失败多少次后跳过（跨运行累计，0=不限制）")
    sp_client.add_argument("--no-guard", action="store_true", dest="no_guard",
                           help="关闭资源守护（不建议：这是唯一能防跑坏卡的一层）")
    sp_client.add_argument("--variant", default="",
                           help="AB 变体名（在 tiers.yaml 的 variants 段定义）；"
                                "只跑单套时用，跑多套请用 abtest")
    sp_client.add_argument("--stats-json", default=None, dest="stats_json",
                           help="把分段计时写到这个文件：read / tag / tag_decode / "
                                "tag_fwd / b64 / http / parse / gate / write 各自的"
                                "耗时与次数（tag_decode+tag_fwd ⊂ tag）")
    sp_client.add_argument("--artifact-format", default="json", dest="artifact_format",
                           choices=["json", "parquet"],
                           help="产物形态：json=每图两个小文件（默认）；"
                                "parquet=攒成片，千万级时文件数降 4 个数量级")
    sp_client.add_argument("--parquet-rows", type=int, default=2000,
                           dest="parquet_rows",
                           help="每个 parquet 分片的行数（默认 2000）")
    sp_client.add_argument("--quiet", action="store_true")
    sp_client.set_defaults(func=cmd_client)

    sp_merge = sub.add_parser("merge", help="回收成员产出，对账后合并总表")
    sp_merge.add_argument("--tiers", default=DEFAULT_TIERS)
    sp_merge.add_argument("--roots", nargs="+", required=True,
                          help="各成员的回收目录，可给多个")
    sp_merge.add_argument("--shard-dir", default=None, dest="shard_dir",
                          help="分发包目录，用它汇总应产出的 image_id 做对账")
    sp_merge.add_argument("--table", default=None,
                          help="分包总表 shards.json，用区间检测「整包没交」"
                               "（分发包已不在时也能查）")
    sp_merge.add_argument("--out", default="work/merged.jsonl")
    sp_merge.add_argument("--format", default="jsonl", choices=["jsonl", "json"])
    sp_merge.add_argument("--dataset", default=None,
                          help="数据集类型名，拼进 image_path（如 fanbox）")
    sp_merge.add_argument("--require", default=None, choices=["nl", "tag", "both"])
    sp_merge.add_argument("--sample-n", type=int, default=None, dest="sample_n")
    sp_merge.add_argument("--sample-out", default="work/sample.md", dest="sample_out")
    sp_merge.add_argument("--keep-empty", action="store_true", dest="keep_empty",
                          help="空内容也写进总表（默认丢弃并计入统计）")
    sp_merge.add_argument("--top", type=int, default=20)
    sp_merge.set_defaults(func=cmd_merge)

    sp_verify = sub.add_parser("verify", help="复核产物完整性：有无丢标、空 json")
    sp_verify.add_argument("--tiers", default=DEFAULT_TIERS)
    sp_verify.add_argument("--mode", default=None, choices=["nl", "tag", "both"],
                           help="按哪种产出模式复核（默认用配置的 output.mode）。"
                                "tag 模式的批次按 both 复核会把每条都判成缺 nl")
    sp_verify.add_argument("--out-dir", default="work/out", dest="out_dir")
    sp_verify.add_argument("--input-dir", default=None, dest="input_dir",
                           help="给输入目录即可做数量对账（推荐）")
    sp_verify.add_argument("--no-recursive", action="store_true", dest="no_recursive")
    sp_verify.add_argument("--no-zips", action="store_true", dest="no_zips")
    sp_verify.add_argument("--limit", type=int, default=None)
    sp_verify.add_argument("--max-samples", type=int, default=0, dest="max_samples",
                           help="最多抽查多少条，0 = 全量")
    sp_verify.add_argument("--top", type=int, default=20, help="明细显示条数")
    sp_verify.add_argument("--json-out", default=None, dest="json_out",
                           help="把报告写成 json")
    sp_verify.set_defaults(func=cmd_verify)

    sp_report = sub.add_parser("report", help="产出批次报告（供折线图看趋势）")
    sp_report.add_argument("--tiers", default=DEFAULT_TIERS)
    sp_report.add_argument("--roots", nargs="+", default=None,
                           help="成员产物目录（可多个）；不给则用 --out-dir")
    sp_report.add_argument("--out-dir", default="work/out", dest="out_dir",
                           help="产物目录；配 --archives 时按归档读")
    sp_report.add_argument("--archives", action="store_true",
                           help="读封口后的 .tar/.zip（验收视角），而不是中间态 json")
    sp_report.add_argument("--batch-size", type=int, default=0, dest="batch_size",
                           help="每批条数；不给则取配置 report.batch_size")
    sp_report.add_argument("--order", default=None, choices=["id", "time"],
                           help="批次排序：id 可复现，time 反映服务状态变化")
    sp_report.add_argument("--json-out", default="work/report.json", dest="json_out")
    sp_report.add_argument("--md-out", default="work/report.md", dest="md_out")
    sp_report.add_argument("--top", type=int, default=20, help="markdown 列前 N 批")
    sp_report.set_defaults(func=cmd_report)

    # abtest 继承 client 的全部参数：AB 的前提就是"只有 prompt 不同"，
    # 参数面不一致会让差异归因不清。额外多出来的只有变体选择与产出路径。
    # conflict_handler="resolve" 只为了解决 -h/--help 这一个冲突（父子解析器
    # 各带一份），其余参数没有重名。
    sp_abtest = sub.add_parser("abtest", parents=[sp_client],
                               conflict_handler="resolve",
                               help="同一批图跑多套 prompt，按变体分目录并出对比表")
    sp_abtest.add_argument("--variants", nargs="+", required=True,
                           help="要跑的变体名（tiers.yaml 的 variants 段）")
    sp_abtest.add_argument("--compare-out", default=None, dest="compare_out",
                           help="对比表 markdown，默认 <out-dir>/compare.md")
    sp_abtest.add_argument("--compare-json", default=None, dest="compare_json",
                           help="对比数据 json，默认 <out-dir>/compare.json")
    sp_abtest.set_defaults(func=cmd_abtest, out_dir="work/ab")

    sp_tag = sub.add_parser("tag", help="本地打标：图片目录 → tag 产物（不连 vLLM）")
    sp_tag.add_argument("--input-dir", required=True, dest="input_dir")
    sp_tag.add_argument("--out-dir", default=None, dest="out_dir",
                        help="产物目录，默认 <输入目录名>-tags（落在其旁边）")
    sp_tag.add_argument("--backend", default="wd-eva02",
                        choices=["pixai", "wd-eva02", "cl-tagger-v2", "onnx"])
    sp_tag.add_argument("--model", default=None, help="覆盖后端的 HF 仓库名")
    sp_tag.add_argument("--model-dir", default=None, dest="model_dir",
                        help="本地权重目录（wd-eva02 认这个）")
    sp_tag.add_argument("--device", default="auto")
    sp_tag.add_argument("--precision", default="bf16",
                        choices=["fp32", "tf32", "bf16"])
    sp_tag.add_argument("--batch", type=int, default=4)
    sp_tag.add_argument("--preprocess", default="pil", choices=["pil", "tensor"],
                        help="预处理位置：pil = CPU 缩放/归一化（默认）；"
                             "tensor = 缩放/归一化上卡，大 PNG 约 1.5×")
    sp_tag.add_argument("--threshold", type=float, default=None,
                        help="覆盖 tagging.thresholds.default（默认 0.35）")
    sp_tag.add_argument("--limit", type=int, default=0, help="只跑前 N 张")
    sp_tag.add_argument("--force", action="store_true", help="忽略已有产物全部重打")
    sp_tag.add_argument("--no-recursive", action="store_true", dest="no_recursive")
    sp_tag.set_defaults(func=cmd_tag)

    sp_pack = sub.add_parser(
        "pack", help="打标 + 封包：目录/zip → parquet / webdataset / zip 分片")
    sp_pack.add_argument("--tiers", default=DEFAULT_TIERS)
    sp_pack.add_argument("--input-dir", required=True, dest="input_dir",
                         help="图片目录，或单个 .zip 素材包")
    sp_pack.add_argument("--out-dir", default=None, dest="out_dir",
                         help="产物目录，默认 <输入名>-tags（旁边）")
    sp_pack.add_argument("--formats", nargs="+", default=None,
                         choices=["parquet", "webdataset", "zip"],
                         help="输出格式，可多选；默认跟配置 archive.formats")
    sp_pack.add_argument("--layout", default=None, choices=["flat", "mirror"],
                         help="flat=画师-000123.jpg 平铺；mirror=保留原目录结构")
    sp_pack.add_argument("--seq-width", type=int, default=None, dest="seq_width",
                         help="序号宽度，默认 6（支持 999999 张）")
    sp_pack.add_argument("--quality", type=int, default=None,
                         help="JPEG 重编码质量 90~100，默认取配置")
    sp_pack.add_argument("--subsampling", type=int, default=None,
                         choices=[0, 1, 2],
                         help="色度采样 0=4:4:4（默认）1=4:2:2 2=4:2:0")
    sp_pack.add_argument("--compress", default=None, choices=["stored", "deflate"],
                         help="zip 分片的压缩方式（tar/parquet 不受影响）")
    sp_pack.add_argument("--max-records", type=int, default=None, dest="max_records",
                         help="每包最多多少条，默认取配置")
    sp_pack.add_argument("--max-bytes", type=int, default=None, dest="max_bytes",
                         help="每包最多多少字节，0=不限")
    # 两个开关互斥：同时给会变成"后写的那个赢"，用户根本看不出自己选了什么。
    artist_group = sp_pack.add_mutually_exclusive_group()
    artist_group.add_argument("--artist", action="store_true", dest="artist",
                              default=None,
                              help="从路径第一层取画师名（进 image_id / meta）")
    artist_group.add_argument("--no-artist", action="store_false", dest="artist",
                              help="不取画师名（id 只有序号）")
    sp_pack.add_argument("--artist-depth", type=int, default=None, dest="artist_depth",
                         help="画师名取路径第几层目录，0=第一层（默认跟配置）")
    sp_pack.add_argument("--no-artist-inject", action="store_true", dest="no_artist_inject",
                         help="画师名只留在 JSON，不写进 .tag/.nl 文本")
    sp_pack.add_argument("--no-space-check", action="store_true", dest="no_space_check",
                         help="跳过盘位预算（预计产出 + 20%% 冗余）")
    sp_pack.add_argument("--backend", default="wd-eva02",
                         choices=["pixai", "wd-eva02", "cl-tagger-v2", "onnx"])
    sp_pack.add_argument("--model", default=None, help="覆盖后端的 HF 仓库名")
    sp_pack.add_argument("--model-dir", default=None, dest="model_dir",
                         help="本地权重目录（wd-eva02 认这个）")
    sp_pack.add_argument("--device", default="auto")
    sp_pack.add_argument("--precision", default="bf16",
                         choices=["fp32", "tf32", "bf16"])
    sp_pack.add_argument("--batch", type=int, default=4)
    sp_pack.add_argument("--preprocess", default="pil", choices=["pil", "tensor"],
                         help="预处理位置：pil = CPU 缩放/归一化（默认）；"
                              "tensor = 缩放/归一化上卡，大 PNG 约 1.5×")
    sp_pack.add_argument("--threshold", type=float, default=None)
    sp_pack.add_argument("--limit", type=int, default=0, help="只跑前 N 张")
    sp_pack.add_argument("--force", action="store_true", help="忽略已有产物全部重打")
    sp_pack.add_argument("--no-recursive", action="store_true", dest="no_recursive")
    sp_pack.add_argument("--no-zips", action="store_true", dest="no_zips",
                         help="不展开 zip 分包")
    sp_pack.add_argument("--no-loose", action="store_true", dest="no_loose",
                         help="忽略散图")
    sp_pack.add_argument("--skip-tag", action="store_true", dest="skip_tag",
                         help="不跑打标，直接按已有产物封包（改质量/格式时用）")
    sp_pack.add_argument("--top", type=int, default=10, help="列前 N 个来源")
    sp_pack.add_argument("--samples", type=int, default=8, help="展示 N 条命名示例")
    sp_pack.set_defaults(func=cmd_pack)

    sp_web = sub.add_parser("web", help="起本地打标 UI（浏览器界面）")
    sp_web.add_argument("--host", default="127.0.0.1",
                        help="监听地址。默认只听本机——这是本机工具，别暴露到局域网")
    sp_web.add_argument("--port", type=int, default=27100)
    sp_web.add_argument("--no-open", action="store_true", dest="no_open",
                        help="不自动打开浏览器")
    sp_web.set_defaults(func=cmd_web)

    return p


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"\n[配置错误] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
