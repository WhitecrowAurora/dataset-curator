"""vLLM 服务启动器。

职责只有四件：拼参数、起进程、等就绪、把手柄交出去。
不做打标逻辑，也不做任务调度——那是 client / shards 的事。

为什么要包一层而不是直接敲 `vllm serve`：
全池 10 台机器必须用完全相同的参数（尤其 max_pixels、max_model_len、
gpu_memory_utilization），否则同一张图在不同机器上产出的 caption 密度不同，
数据集内部会出现按设备分组的伪特征。参数留给人记，一定会出错。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import ConfigError, RunProfile
from .netutil import trust_env_for


DEFAULT_PORT = 8000


@dataclass(slots=True)
class ServerHandle:
    process: subprocess.Popen | None
    base_url: str
    port: int
    profile: RunProfile
    vllm_version: str | None = None
    # --allowed-local-media-path 放行的根目录。客户端要决定能不能用
    # file:// 传图，必须知道服务端到底放行了哪里——猜错的表现是
    # "HTTP 400 图片路径不被允许"，而那时已经浪费了一张图的排队。
    local_media_root: str = ""
    # 引擎名（vllm / llamacpp）。写进指纹：换引擎等于换一代数据，
    # 两个引擎的 tokenizer 与视觉塔虽然同源，但量化格式与采样细节不同，
    # 混在一份数据集里事后无法区分。
    engine: str = "vllm"
    # 真正传给子进程的命令行。留一份是为了让指纹能回答"实际跑的是什么参数"——
    # profile 是意图，命令行才是事实，两者漂移过一次（8 vs 12 slot）。
    cmd: list[str] = field(default_factory=list)

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is not None:
            return
        if platform.system() == "Windows":
            self.process.terminate()
        else:
            # vLLM 会 spawn worker，整组发 SIGINT 让它优雅释放显存
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                self.process.terminate()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()


# CUDA MPS（Multi-Process Service）相关环境变量。只要出现任何一个，
# 就说明当前 shell 在一个 MPS 客户端上下文里。
_MPS_ENV_VARS = (
    "CUDA_MPS_PIPE_DIRECTORY",
    "CUDA_MPS_LOG_DIRECTORY",
    "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE",
)


def check_mps() -> None:
    """拦截 CUDA MPS。

    vLLM 与 MPS 不兼容，这是硬冲突不是性能问题：

    1. vLLM 按 gpu_memory_utilization 从整卡预分配显存池，并用 CUDA Graph
       复用小批量的 launch 开销。MPS 下多个进程共享同一个 CUDA 上下文，
       显存统计与隔离语义都变了，graph capture 会直接失败。
    2. MPS 的收益场景是「很多小进程抢一张卡」，而我们的并发本来就在
       vLLM 的连续批处理里（--max-num-seqs），进程数始终是 1。
    3. 真要同时跑多个模型，正确做法是多卡分工，不是 MPS 挤一张卡。

    所以这里选择直接报错而不是警告：一旦带着 MPS 跑起来，症状是启动阶段
    偶发 CUDA error、或吞吐莫名腰斩，排查成本远高于现在拦住。
    """
    active = [v for v in _MPS_ENV_VARS if os.environ.get(v)]
    if not active:
        return
    raise ConfigError(
        f"检测到 CUDA MPS 环境（{', '.join(active)}）。vLLM 不支持 MPS：\n"
        "  - 它按整卡比例预分配显存并用 CUDA Graph，MPS 下 graph capture 会失败；\n"
        "  - 我们的并发由 --max-num-seqs 的连续批处理负责，不需要多进程共享 GPU。\n"
        "请先退出 MPS 上下文（unset 上述环境变量并停掉 nvidia-cuda-mps-control -d），"
        "再重新启动。要跑多个模型请用多张卡，每卡一个实例。"
    )


def check_environment() -> None:
    """vLLM 只在 Linux 上可用。Windows 需要 WSL2，这里提前拦住并给出指引。"""
    if platform.system() == "Windows":
        raise ConfigError(
            "vLLM 无法在 Windows 原生运行。两个选择：\n"
            "  1) 在 WSL2 里执行同一条命令（推荐，GPU 直通会有 10-30% 性能损失）\n"
            "  2) 在别的 Linux 机器上起服务，本机用 `tagger smoke --url http://host:8000` 连过去\n"
            "     （远程模式下 args 里的本地数据路径需要换成共享挂载或 http 地址）"
        )
    if shutil.which("vllm") is None:
        raise ConfigError(
            "找不到 vllm 命令。安装：pip install 'tagger[serve]' 或 pip install vllm"
        )
    check_mps()


def build_command(
    profile: RunProfile,
    *,
    port: int = DEFAULT_PORT,
    data_root: str | None = None,
    served_name: str = "tagger",
    extra_args: list[str] | None = None,
) -> list[str]:
    """按 profile 拼出 vllm serve 的完整命令行。

    每个参数都对应一个会让输出口径漂移的变量，改动前请想清楚后果。
    """
    cmd: list[str] = [
        "vllm",
        "serve",
        profile.weights_path,
        "--served-model-name",
        served_name,
        "--port",
        str(port),
        "--host",
        "127.0.0.1",
        "--max-model-len",
        str(profile.max_model_len),
        "--max-num-seqs",
        str(profile.max_num_seqs),
        "--gpu-memory-utilization",
        f"{profile.gpu_memory_utilization:.2f}",
        # 同一套 prompt 模板，前缀基本相同，缓存能省掉大量重复 prefill
        "--enable-prefix-caching",
    ]

    # 单步 token 预算（prefill 分块 + decode 合计）。默认 8192 时，
    # 三四张图同时 prefill 就会把一步撑满，后面的请求只能等下一步，
    # 并发数再高也填不满——这是"并发调大了但吞吐没变"最常见的原因。
    if profile.max_num_batched_tokens:
        cmd += ["--max-num-batched-tokens",
                str(profile.max_num_batched_tokens)]

    if profile.model.revision:
        cmd += ["--revision", profile.model.revision]

    # 精度：bf16 不传（按权重自带 dtype 加载）；fp8 让 vLLM 在线量化；
    # w4a16 只在确实有量化权重时才传，格式从模型配置推断。
    #
    # 为什么"没有权重就不传"：给一个 bf16 权重加 --quantization awq，vLLM
    # 会按 4bit 去读，报出来的是形状/键名不匹配，离真正原因（权重没量化）
    # 很远。宁可显式回退 bf16 并在这里说一句。
    quant = profile.quantization
    if quant:
        cmd += ["--quantization", quant]
    elif (profile.precision.lower() in ("w4a16", "int4", "awq", "gptq")
          and not profile.model.quantized_path):
        print(
            f"[tagger][警告] precision={profile.precision} 但没有量化权重"
            f"（quantized_path 为空），本次实际按 bf16 加载。"
            f"要先跑一次 4bit 量化，或改用 fp8 / bf16。"
        )

    # 视觉 token 预算。这是单卡吞吐最大的杠杆，也是全池口径一致的关键。
    # 注意 vLLM 对 mm-processor-kwargs 的解析随版本变化，若启动报错，
    # 先确认 --help 里这个参数接受的格式。
    #
    # max_pixels 的换算口径是跟着架构走的：Qwen3-VL 是 patch 16 × merge 2，
    # 即 32×32 像素一个 token，589824 才等于 576 token。换到别的家族
    # （Kimi 的 MoonViT 等）这个换算不成立，同一个数字会得到不同的 token 数。
    # 不阻止启动，但必须让人看见——静默不一致正是这份配置最怕的事。
    if not profile.model.family.startswith("qwen3vl"):
        print(
            f"[tagger][警告] family={profile.model.family} 不是 Qwen3-VL，"
            f"max_pixels={profile.max_pixels} 按 32×32/token 换算的结论对它不成立。"
            f"请先用 `tagger smoke` 实测单图 token 数，不要沿用 Qwen 的吞吐估算。"
        )
    cmd += [
        "--mm-processor-kwargs",
        json.dumps({"max_pixels": profile.max_pixels}),
    ]

    # 一张图一条样本，限制多图可以省显存也能防误用
    cmd += ["--limit-mm-per-prompt", json.dumps({"image": 1})]

    if data_root:
        # 允许用 file:// 传本地图片，避免 base64 编码带来的 33% 体积膨胀和 CPU 开销
        cmd += ["--allowed-local-media-path", str(Path(data_root).resolve())]

    if extra_args:
        cmd += list(extra_args)

    return cmd


def _probe_version(base_url: str, engine: str = "vllm") -> str | None:
    if engine == "llamacpp":
        # llama-server 没有 /version，构建号在 /props 的 build_info 里。
        from . import nl_engine

        return nl_engine.probe_version(base_url)
    try:
        # 本机地址不吃系统代理：代理软件常把 127.0.0.1 也接管过去，
        # 结果"服务明明起着却探不到"（见 netutil 的说明）。
        r = httpx.get(f"{base_url}/version", timeout=5,
                      trust_env=trust_env_for(base_url))
        if r.status_code == 200:
            return r.json().get("version")
    except (httpx.HTTPError, ValueError):
        pass
    return None


def probe_version(base_url: str, engine: str = "vllm") -> str | None:
    """公开的版本探测。`start_server(wait=False)` 不填 vllm_version，
    Web 侧要自己补这一次，指纹里才有版本号。"""
    return _probe_version(base_url, engine)


def wait_ready(
    base_url: str,
    *,
    timeout_s: int = 900,
    poll_s: float = 2.0,
    on_tick: callable | None = None,
) -> None:
    """轮询 /health 直到就绪。

    首次加载要下模型 + 编译 kernel，慢是正常的；但超过 timeout 说明有问题，
    宁可失败也不要让成员干等着以为在跑。
    """
    deadline = time.time() + timeout_s
    last_err: str = ""
    # 轮询也要直连本机：被代理拦下时 /health 永远不通，
    # 表现成"服务起不来"，实际是请求根本没到 vLLM。
    trust = trust_env_for(base_url)
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=5, trust_env=trust)
            if r.status_code == 200:
                return
            last_err = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            last_err = type(exc).__name__
        if on_tick:
            on_tick(int(deadline - time.time()))
        time.sleep(poll_s)
    raise ConfigError(
        f"服务在 {timeout_s}s 内没有就绪（最后错误：{last_err}）。"
        f"检查显存是否够、模型路径是否正确。"
    )


def start_server(
    profile: RunProfile,
    *,
    port: int = DEFAULT_PORT,
    data_root: str | None = None,
    log_path: str | Path | None = None,
    wait: bool = True,
    timeout_s: int = 900,
    served_name: str = "tagger",
    engine: str = "vllm",
) -> ServerHandle:
    """起一个服务并等待就绪。

    served_name 是 OpenAI 接口里的路由名（--served-model-name / --alias），
    必须与客户端请求里的 model 字段一字不差。网页端把它一起传进来，就是
    为了让表单里那个"模型名"真的能决定服务端叫什么，而不是两边各写各的。

    engine=llamacpp 时走 nl_engine：Windows 原生唯一能上 GPU 的路。
    两条路只共用"起进程 / 等就绪 / 交手柄"，命令行构造完全分开——
    参数没有一一对应关系（见 nl_engine 开头的换算说明）。
    """
    engine = engine or "vllm"
    from . import nl_engine

    nl_engine.check_engine(engine)

    if engine == "llamacpp":
        gguf, mmproj = nl_engine.resolve_weights(profile)
        # 显存决定开几个 slot：查不到就当未知，按最保守的 2 个起（只影响吞吐）
        try:
            from .hardware import query_gpus

            gpus = query_gpus()
            vram_gb = max((g.vram_gb for g in gpus), default=None)
        except Exception:                   # noqa: BLE001 - 探测失败不该拦住起服务
            vram_gb = None
        cmd = nl_engine.build_command(profile, port=port,
                                      served_name=served_name or "tagger",
                                      gguf=gguf, mmproj=mmproj,
                                      vram_gb=vram_gb)
        # llama-server 没有 --allowed-local-media-path，file:// 一律别用。
        data_root = None
    else:
        cmd = build_command(profile, port=port, data_root=data_root,
                            served_name=served_name or "tagger")
    base_url = f"http://127.0.0.1:{port}"

    # 日志必须落文件。stdout=PIPE 又从不读的话，管道缓冲（约 64KB）被
    # vLLM 的启动日志写满后子进程会被自己的输出卡死——症状是"服务起着
    # 起着就冻结了"，而且排查时一条日志都拿不到。默认写 run/ 下，
    # 等待失败时把尾巴打出来。
    if log_path is None:
        log_path = Path("run") / f"vllm-{port}.log"
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    print(f"[tagger] 服务日志: {Path(log_path).resolve()}")

    print(f"[tagger] 启动: {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        # 独立进程组，便于整组回收
        start_new_session=(platform.system() != "Windows"),
    )

    handle = ServerHandle(process=process, base_url=base_url, port=port,
                          profile=profile,
                          local_media_root=(str(Path(data_root).resolve())
                                            if data_root else ""),
                          engine=engine, cmd=list(cmd))

    if not wait:
        log_file.close()
        return handle

    try:
        print(f"[tagger] 等待就绪（最多 {timeout_s}s，首次加载会慢）...")
        wait_ready(base_url, timeout_s=timeout_s)
    except ConfigError:
        handle.stop()
        _print_log_tail(log_path)
        raise
    finally:
        # 子进程持有自己那份 dup 出去的句柄，这里关掉父进程的副本不影响它写。
        log_file.close()

    handle.vllm_version = _probe_version(base_url, engine)
    print(f"[tagger] 就绪: {base_url}  engine={engine}  "
          f"version={handle.vllm_version or 'unknown'}")
    return handle


def _print_log_tail(log_path: str | Path, *, lines: int = 40) -> None:
    """等待失败时把服务日志最后几行打出来。启动失败的根因（OOM、参数不认、
    路径不存在）几乎都在最后几十行里，只抛"没就绪"等于让人干猜。"""
    try:
        raw = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    tail = [ln for ln in raw.splitlines() if ln.strip()][-lines:]
    if tail:
        print(f"[tagger] ---- 服务日志末尾（{Path(log_path)}）----")
        for ln in tail:
            print(f"  {ln}")


def _llamacpp_runtime_shape(handle: ServerHandle) -> tuple[int | None, str]:
    """从真的传给 llama-server 的命令行里取 (slot 数, KV 类型)。

    为什么不从 profile 取：profile 是"想要的"，命令行是"实际发生的"。
    两者漂移过一次（profile 写 8、进程开 12），代价是每台机器白丢 18% 吞吐，
    而且指纹会谎报运行参数。取命令行就不会再漂。
    """
    if getattr(handle, "engine", "vllm") != "llamacpp":
        return None, ""
    cmd = list(getattr(handle, "cmd", None) or [])
    slots: int | None = None
    kv = ""
    for i, tok in enumerate(cmd):
        if tok in ("-np", "--parallel") and i + 1 < len(cmd):
            try:
                slots = int(cmd[i + 1])
            except (TypeError, ValueError):
                slots = None
        elif tok in ("-ctk", "--cache-type-k") and i + 1 < len(cmd):
            kv = str(cmd[i + 1])
    return slots, kv


def fingerprint_env(handle: ServerHandle) -> dict:
    """把决定输出口径的变量打包成一个指纹，写进 selftest 结果和 provenance。

    这张字典就是"这批数据是在什么环境下产的"的完整答案。
    """
    p = handle.profile
    # llama.cpp 的并发不是 max_num_seqs，而是命令行里的 -np（slot 数）。
    # 客户端的 resolve_concurrency 就是读这个字段来决定"同时送几条在途"，
    # 记成 profile 里的 8 而服务端实际开 12，等于每台机器白丢 ~18% 吞吐。
    # 所以这里从**真正传给进程的命令行**里取，而不是从配置里抄。
    slots, kv_type = _llamacpp_runtime_shape(handle)
    return {
        "model_id": p.model_id,
        "model_path": p.weights_path,
        "model_revision": p.model.revision,
        "precision": p.precision,
        # 实际传给 vLLM 的量化方式（None = 按权重自带 dtype 加载）。它与
        # precision 可能不一致：w4a16 缺量化权重时会回退 bf16，事后对账
        # 必须看这个字段，否则两批数据会被当成同一代。
        "quantization": p.quantization,
        # 权重本身是否真被压成 4bit。compressed-tensors 的产物不需要显式
        # --quantization，只看上一个字段会把它误判成回退。
        "weight_quantized": p.weight_quantized,
        "max_pixels": p.max_pixels,
        # 换桶等于换一套输入尺寸分布，必须进指纹，否则两批数据无法判断是否同源
        "bucket_spec": p.bucket_spec,
        "max_model_len": p.max_model_len,
        # llamacpp 记进程实际开的 slot 数（-np），vLLM 记 profile 的值。
        # 客户端按这个字段决定在途请求数，两边不一致就等于白丢吞吐。
        "max_num_seqs": slots or p.max_num_seqs,
        "kv_type": kv_type or None,
        "max_num_batched_tokens": p.max_num_batched_tokens,
        "gpu_memory_utilization": p.gpu_memory_utilization,
        "temperature": p.temperature,
        "top_p": p.top_p,
        "max_tokens": p.max_tokens,
        # 引擎本身就是口径的一部分：同一个模型在 vLLM(bf16/int4) 与
        # llama.cpp(GGUF) 下产出的 caption 长度分布不同，混进一份数据集
        # 就无法事后区分。llamacpp 还要记权重档位（Q4_K_M / Q8_0）。
        "engine": getattr(handle, "engine", "vllm"),
        "gguf": str(getattr(p.model, "gguf", "") or ""),
        "mmproj": str(getattr(p.model, "mmproj", "") or ""),
        "engine_version": handle.vllm_version,
        # vllm_version 保留旧键：已有的对账脚本与 report 在读它。
        "vllm_version": handle.vllm_version,
        "python": sys.version.split()[0],
        "device": p.device_name,
        # 空串 = 服务端不接受 file://。客户端据此决定能不能开 --use-file-url：
        # 放行目录与实际产物目录不匹配时报的是 400，而不是"路径不对"。
        "local_media_root": handle.local_media_root,
    }
