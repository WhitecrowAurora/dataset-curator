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
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import ConfigError, RunProfile
from .netutil import trust_env_for
from .nl_engine import normalize_engine


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
    # 只有 vLLM 有这个开关，llama.cpp 下恒为空。
    local_media_root: str = ""
    # 起的是哪个引擎。两套引擎的服务端并发不是同一个数（vLLM 是
    # max_num_seqs，llama.cpp 是 -np），指纹里必须分开记：客户端照哪个数
    # 推在途请求数，取错就会"服务端排队而 GPU 空转"或反过来。
    engine: str = "vllm"
    # llama.cpp 实际生效的槽位数；vLLM 下为 None（它没有这个概念）。
    slots: int | None = None

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
        profile.model.resolved_path,
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

    # 精度三档落到命令行的唯一出口。RunProfile.quantization 是"能力"而不是意图：
    # 选了 w4a16 但 quantized_path 为空时它返回 None，这里就不传——
    # 传了反而会让 vLLM 去找不存在的量化配置，报错离真正原因很远。
    if profile.quantization:
        cmd += ["--quantization", profile.quantization]

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


def _probe_version(base_url: str) -> str | None:
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


def probe_version(base_url: str) -> str | None:
    """公开的版本探测。`start_server(wait=False)` 不填 vllm_version，
    Web 侧要自己补这一次，指纹里才有版本号。"""
    return _probe_version(base_url)


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


def _spawn_error_text(binary: str, exc: OSError) -> str:
    """Popen 失败时说清"是哪一层出的错"。

    Windows 上三种典型混在一起：
    - WinError 2：exe 或它依赖的 dll 找不到（路径看着没错，就是缺 dll）
    - WinError 193 / 0xc1：下的是别的架构的包（arm64 包在 x64 上）
    - 没有报错但 0.2 秒退出：日志里通常什么都没有
    这个函数只负责把原文和现场（exe 路径、同目录 dll 数、日志位置）拼上，
    怎么修交给用户或 AI 判断。
    """
    exe = Path(str(binary))
    lines = [f"启动失败：{type(exc).__name__}: {exc}",
             f"可执行文件：{binary}（存在={exe.is_file()}）"]
    # Linux 上是 .so，而且缺库的报错不是"路径写错"而是
    # "error while loading shared libraries"——同目录列出 .so 才看得出缺哪一个。
    lib_glob = "*.dll" if os.name == "nt" else "*.so*"
    try:
        if exe.parent.is_dir():
            libs = sorted(p.name for p in exe.parent.glob(lib_glob))
            lines.append(f"同目录库({len(libs)})：{', '.join(libs[:20]) or '无'}")
    except OSError:
        pass
    low = str(exc).lower()
    if "winerror 2" in low or "cannot find the file" in low:
        lines.append(
            "这个错误码在 Windows 上**也可能是缺 dll**，不是路径写错。"
            "CUDA 版 llama.cpp 要配同版本的 cudart：到「NL 服务」页重装一次，"
            "或改用 CPU/Vulkan 版。")
    elif "winerror 193" in low or "不是有效的 win32" in low or "not a valid win32" in low:
        lines.append("这是架构不符（下成了 arm64 / 32 位的包），重下对应 x64 的压缩包。")
    return "\n".join(lines)


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
    device_index: int = 0,
    slots_override: int | None = None,
) -> ServerHandle:
    """起一个推理服务并等待就绪（vLLM 或 llama.cpp）。

    served_name 是 OpenAI 接口里的路由名（--served-model-name / -a），必须与
    客户端请求里的 model 字段一字不差。网页端把它一起传进来，就是为了
    让表单里那个"模型名"真的能决定服务端叫什么，而不是两边各写各的。

    engine 决定拼哪套命令行：两者参数完全不通用，所以命令必须在这里分流，
    而不是让调用方各自 remember。`check_environment()` 只管 vLLM（Windows
    原生跑不了、MPS 冲突），llama.cpp 那一支由 `nl_engine.check_engine` 判。

    slots_override 是 llama.cpp 的 `-np`（并发槽位数）。它是**显存开关**：
    每个 slot 启起来就按 CTX_PER_SLOT 预留 KV，想让出显存给别的程序就把
    这个数调小——调小任务并发是没用的，那些 slot 照样占着。

    device_index 只对 llama.cpp 有效（CUDA_VISIBLE_DEVICES），多卡时一张卡
    一份服务；vLLM 走自己的张量并行，不看这个。
    """
    resolved = normalize_engine(engine)
    base_url = f"http://127.0.0.1:{port}"
    slots_used: int | None = None

    if resolved == "llamacpp":
        from . import nl_engine

        nl_engine.check_engine(resolved)
        gguf, mmproj = nl_engine.resolve_weights(profile)
        cmd = nl_engine.build_command(
            profile, port=port, served_name=served_name or "tagger",
            gguf=gguf, mmproj=mmproj,
            vram_gb=profile.vram_gb, slots_override=slots_override,
        )
        # 从刚拼好的命令里回读 -np，而不是再算一遍 slot_plan：指纹要记的是
        # "实际启动的那条命令"，另算一份就有了两个真相源。
        slots_used = int(cmd[cmd.index("-np") + 1])
        # 只能用一张卡：CUDA_VISIBLE_DEVICES 必须在进程启动前定，-ngl 99
        # 会把整份权重放到此时唯一可见的那张卡上。
        child_env = dict(os.environ)
        # ★两个坑，都在真机上踩得到：
        #   1) `if device_index:` 把 **0 号卡**当成了"没指定"——父进程自己带着
        #      CUDA_VISIBLE_DEVICES=2 时，本该用物理 0 号卡的 worker 会继承 2 号，
        #      于是多卡面板按 0 号记端口/显存，实际跑在 2 号上。
        #   2) device_index 是**物理卡号**（multi_gpu 传的是 nvidia-smi 的 index），
        #      但子进程的 CUDA_VISIBLE_DEVICES 也按物理号解释，所以直接写是对的；
        #      要防的是"本进程已经被限过卡"——那时子进程里物理号 + 继承的可见集
        #      会二次过滤，最终那张卡可能根本不可见。
        # 做法：先清掉继承值，再写目标卡，保证子进程只看得到这一张。
        if device_index is not None:
            child_env["CUDA_VISIBLE_DEVICES"] = str(device_index)
        # Linux 动态链接器不看 exe 同目录的 .so；验活（llama_runtime.smoke_test）
        # 和这里必须共用同一份环境，否则装完那次 --version 先报缺 libcudart.so.*。
        from . import llama_runtime
        child_env = llama_runtime.server_env(cmd[0], child_env)
    else:
        check_environment()
        cmd = build_command(profile, port=port, data_root=data_root,
                            served_name=served_name or "tagger")
        child_env = None

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
    if resolved == "llamacpp":
        # 显式回显"实际生效的槽位数"和"实际用哪张卡"。以前只有命令行里那串
        # `-np N` 能看出来，用户传了 --max-num-seqs 12 却在日志里找不到它，
        # 就以为参数没生效（Bug 1 的报告正是这么来的）。
        print(f"[tagger] 槽位: {slots_used}（-np）  "
              f"卡: {(child_env or {}).get('CUDA_VISIBLE_DEVICES', '继承环境')}")
    try:
        process = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            # 独立进程组，便于整组回收
            start_new_session=(platform.system() != "Windows"),
            env=child_env,
        )
    except OSError as exc:
        # ★"exe 存在"和"exe 能起"是两件事。dll 缺失时 Windows 抛的是
        # WinError 2「系统找不到指定的文件」——同一个错误码，指向的却是**依赖
        # 的 dll**，不是 exe 本身。以前这里直接漏出去（或被上层翻成"找不到
        # llama-server"），用户按字面去核对路径，怎么都对。
        log_file.close()
        raise ConfigError(_spawn_error_text(cmd[0], exc)) from exc

    handle = ServerHandle(process=process, base_url=base_url, port=port,
                          profile=profile,
                          local_media_root=(str(Path(data_root).resolve())
                                            if data_root else ""),
                          engine=resolved, slots=slots_used)

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

    # llama-server 没有 /version，_probe_version 会返回 None；版本号由
    # 调用方（网页/CLI）用 nl_engine.probe_version 走 /props 补，口径不同。
    handle.vllm_version = _probe_version(base_url)
    print(f"[tagger] 就绪: {base_url}  engine={resolved}  "
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


def fingerprint_env(handle: ServerHandle) -> dict:
    """把决定输出口径的变量打包成一个指纹，写进 selftest 结果和 provenance。

    这张字典就是"这批数据是在什么环境下产的"的完整答案。
    """
    p = handle.profile
    return {
        "model_id": p.model_id,
        "model_path": p.model.resolved_path,
        "model_revision": p.model.revision,
        # 引擎决定这套参数怎么落地（两套命令行完全不通用），也决定下面
        # 哪个字段才是"服务端并发"。
        "engine": handle.engine,
        "precision": p.precision,
        "max_pixels": p.max_pixels,
        # 换桶等于换一套输入尺寸分布，必须进指纹，否则两批数据无法判断是否同源
        "bucket_spec": p.bucket_spec,
        "max_model_len": p.max_model_len,
        "max_num_seqs": p.max_num_seqs,
        # llama.cpp 的服务端并发是 -np（slots），上面那行对 llama-server
        # 一个字节都不生效——它是分档表按 vLLM 口径写的数。客户端要按
        # engine 选字段，否则 16G 卡 12 槽会按 8 条在途跑（白丢约三成吞吐）。
        "slots": handle.slots,
        "max_num_batched_tokens": p.max_num_batched_tokens,
        "gpu_memory_utilization": p.gpu_memory_utilization,
        "temperature": p.temperature,
        "top_p": p.top_p,
        "max_tokens": p.max_tokens,
        "vllm_version": handle.vllm_version,
        "python": sys.version.split()[0],
        "device": p.device_name,
        # 空串 = 服务端不接受 file://。客户端据此决定能不能开 --use-file-url：
        # 放行目录与实际产物目录不匹配时报的是 400，而不是"路径不对"。
        "local_media_root": handle.local_media_root,
    }
