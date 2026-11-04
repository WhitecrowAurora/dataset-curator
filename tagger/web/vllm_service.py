"""本机 vLLM 服务的启停与体检（WebUI 侧的薄封装）。

命令行里的 `tagger serve` 是"起服务、等就绪、写指纹、守着 Ctrl+C"一整条；
网页只关心其中三件事，所以这里单独包一层，而不是把 cli.cmd_serve 塞进线程：

1. **状态**。首次加载要下权重 + 编译 kernel，几分钟起步。没有状态页的话，
   页面上只有一个"卡住"的按钮，人只能去翻终端。
2. **启动/停止**。启动放在后台线程里，HTTP 立刻返回；等就绪期间轮询能看见
   日志尾巴。停止必须能把"正在等就绪"的循环掐断——否则进程杀了，循环还在
   傻等 900 秒。
3. **指纹**。就绪后写 `run/fingerprint.json`，客户端靠它拿 max_pixels 与
   model_id。缺这份文件时"重试提分辨率"不会生效，provenance 也会是空的，
   而这种缺失在页面上完全看不出来——所以状态里要显式报告。

本机只允许一个服务：单卡上两个 vLLM 抢显存，而且客户端只认一个 URL。
远程服务（别人机器上跑的 serve）不在这一层的管辖范围，用 probe 看连通性即可。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

import httpx

from ..config import ConfigError
from ..model_loader import DEFAULT_PORT
from ..netutil import trust_env_for
from ..nl_engine import normalize_engine

# 与 cli `tagger serve` 的默认落点一致。Web 侧拉起的服务写这里，任务的
# run_client 也读这里——两边必须是同一个路径，否则指纹永远"缺失"。
FINGERPRINT_PATH = Path("run") / "fingerprint.json"

# 日志尾巴保留多少行。够看清 OOM / 参数错 / 路径不存在，又不至于把页面撑爆。
TAIL_LINES = 40

# 本机能不能跑 vLLM 的判定要调 nvidia-smi，别每 4 秒查一次。
CAP_TTL_S = 30.0


class _Cancelled(Exception):
    """等就绪期间收到停止请求。内部用，不落到页面上。"""


class VllmService:
    """进程级单例（app.py 持有）。状态字段读写跨线程，过锁。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # stopped | starting | ready | stopping | failed
        self.status = "stopped"
        self.error = ""
        self.port = int(DEFAULT_PORT)
        # 没起来时也给个地址：面板上"socket 会绑到哪"是有用信息，
        # 空着只会让人以为"地址还没定"。
        self.url = f"http://127.0.0.1:{self.port}"
        self.started = 0.0
        self.ready_at = 0.0
        self.profile = ""
        self.log_path = str(FINGERPRINT_PATH.parent / f"vllm-{self.port}.log")
        self._fingerprint: dict = {}
        # 启动时传的参数（None = 用分档表）。放进 snapshot 是为了让页面
        # 能显示"现在跑的到底是默认值还是我调过的值"——改了没重启最难查。
        # model 决定加载哪套权重；served_name 是请求里的路由名，两者不是一回事。
        self._overrides: dict = {"engine": None,
                                 "model": None,
                                 "tier": None,
                                 "precision": None,
                                 "served_name": None,
                                 "max_num_seqs": None,
                                 "max_num_batched_tokens": None,
                                 "gpu_mem": None,
                                 "local_media_root": None}
        self._handle = None
        self._thread: threading.Thread | None = None
        # 当前这一轮启动的令牌；stop()/新 start() 会让旧轮次失效（见 start）。
        self._token: object | None = None
        self._stop_flag = threading.Event()
        # 能力探测缓存：(时间, 引擎, 能不能, 原因)。
        # 带上引擎是因为两个引擎的结论不同；换引擎必须重探，
        # 否则切过去还会读到上一个引擎的"能用"。
        self._cap: tuple[float, str, bool, str] | None = None
        # 分档解析结果缓存：snapshot 每 4 秒拉一次，而 query_gpus 要调
        # nvidia-smi，不能跟着页面轮询走。
        self._plan_cache: tuple[float, tuple, dict] | None = None
        self._notes: deque[str] = deque(maxlen=20)

    # ---------------------------------------------------------------- 查询

    def local_capability(self, engine: str = "vllm") -> tuple[bool, str]:
        """能不能在本机拉起这个引擎。不能时给一句人能读的原因。

        判定走 nl_engine.check_engine：vLLM 那一支还是原来的口径
        （Windows 原生、缺 vllm 命令、CUDA MPS 都在那里拦住）；
        llamacpp 那一支只要求找得到 llama-server，且不需要 MPS 检查——
        它是 C++ 进程，不涉及 CUDA Graph capture。

        两个引擎的结论分开缓存：页面上同时展示"哪条能用"，共用一格缓存
        会让先探到的那个把另一个的答案盖掉。
        """
        from ..nl_engine import normalize_engine

        engine = normalize_engine(engine)
        now = time.time()
        cached = self._cap
        if (cached is not None and cached[1] == engine
                and now - cached[0] < CAP_TTL_S):
            return cached[2], cached[3]
        ok, reason = self._probe_capability(engine)
        self._cap = (now, engine, ok, reason)
        return ok, reason

    def invalidate_capability(self) -> None:
        """让下一次 local_capability 重新判定。

        刚装完 llama-server 的那一刻，缓存里多半还记着"找不到 llama-server"
        （TTL 内），页面会继续显示"这机器起不了服务"——装完就看到这句最打击人。
        只清"判过不能用"的那一格：能用的结论没必要每次轮询都作废，否则
        会变成每几秒跑一次 nvidia-smi。
        """
        cap = self._cap
        if cap is not None and not cap[2]:
            self._cap = None

    @staticmethod
    def _probe_capability(engine: str) -> tuple[bool, str]:
        try:
            from ..hardware import query_gpus
            from ..nl_engine import check_engine

            check_engine(engine)
            if not query_gpus():
                return False, "没有检测到 NVIDIA 显卡（nvidia-smi 不可用）"
        except ConfigError as exc:
            return False, str(exc)
        except Exception as exc:          # noqa: BLE001 - 体检失败不该让页面报错
            return False, f"环境检查失败：{type(exc).__name__}: {exc}"
        return True, ""

    def _reap(self) -> None:
        """服务进程自己挂了时把状态翻成 failed。不翻的话页面会一直显示就绪，
        任务请求全部超时，而人以为服务还在。"""
        with self._lock:
            handle, status = self._handle, self.status
        if status != "ready" or handle is None or handle.process is None:
            return
        code = handle.process.poll()
        if code is None:
            return
        with self._lock:
            if self.status == "ready":
                self.status = "failed"
                self.error = f"vllm 进程已退出（code={code}）"

    def plan(self, *, model: str | None = None, tier: str | None = None,
             precision: str | None = None,
             max_num_seqs: int | None = None,
             max_num_batched_tokens: int | None = None,
             gpu_mem: float | None = None) -> dict:
        """当前这组参数会解析成什么（分档 / 权重 / 批调度 / 显存）。

        页面在点启动**之前**就要能看见选了 8B 会发生什么：16G 卡上仍落在
        low-16g，max_model_len 只有 4096，粗估可能 OOM。这些等启动之后才
        发现，代价是几分钟的等待和一段看不懂的 CUDA 日志。

        解析失败（模型 key 写错、卡不支持该精度）也在这里变成一句人话。
        """
        key = (model or None, tier or None, precision or None, max_num_seqs,
               max_num_batched_tokens, gpu_mem)
        now = time.time()
        cached = self._plan_cache
        if cached is not None and cached[1] == key and now - cached[0] < CAP_TTL_S:
            return dict(cached[2])
        out = self._resolve_plan(*key)
        self._plan_cache = (now, key, out)
        return dict(out)

    @staticmethod
    def _resolve_plan(model: str | None, tier: str | None,
                      precision: str | None,
                      max_num_seqs: int | None,
                      max_num_batched_tokens: int | None,
                      gpu_mem: float | None) -> dict:
        try:
            from ..config import DEFAULT_TIERS, load_tier_table, resolve_profile
            from ..hardware import query_gpus

            gpus = query_gpus()
            if not gpus:
                return {"ok": False,
                        "error": "没有检测到 NVIDIA 显卡（nvidia-smi 不可用）"}
            p = resolve_profile(
                load_tier_table(DEFAULT_TIERS), gpus[0],
                model_override=model or None,
                tier_override=tier or None,
                precision_override=precision or None,
                max_num_seqs_override=max_num_seqs,
                max_num_batched_tokens_override=max_num_batched_tokens,
                gpu_mem_override=gpu_mem,
            )
            return {
                "ok": True,
                "tier": p.tier,
                "model": p.model.key,
                "model_path": p.weights_path,
                "precision": p.precision,
                # 实际会传给 vLLM 的量化方式。null = 按权重 dtype 加载，
                # 与 precision 不一致就说明这次是回退（比如 w4a16 没权重）。
                "quantization": getattr(p, "quantization", None),
                # 真 4bit 权重 vs 选了 W4A16 却回退 BF16：页面靠它决定要不要
                # 提示"先量化"。不能用 quantization 代替（compressed-tensors
                # 产物不传 --quantization，值为 null）。
                "weight_quantized": getattr(p, "weight_quantized", True),
                "max_pixels": p.max_pixels,
                "max_model_len": p.max_model_len,
                "max_num_seqs": p.max_num_seqs,
                "max_num_batched_tokens": p.max_num_batched_tokens,
                "gpu_memory_utilization": p.gpu_memory_utilization,
                "vram_gb": p.vram_gb,
                "device": p.device_name,
                "warnings": list(p.warnings),
                "summary": p.summary(),
            }
        except ConfigError as exc:
            # ConfigError 的 message 本来就是给人看的，带个类名只会让它更像 traceback
            return {"ok": False, "error": str(exc)}
        except Exception as exc:          # noqa: BLE001 - 预览失败只该显示原因
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def snapshot(self) -> dict:
        self._reap()
        with self._lock:
            status, error = self.status, self.error
            port, url = self.port, self.url
            started, ready_at = self.started, self.ready_at
            profile = self.profile
            log_path = self.log_path
            fp = dict(self._fingerprint)
            handle = self._handle
            overrides = dict(self._overrides)
        pid = None
        if handle is not None and handle.process is not None:
            try:
                pid = handle.process.pid
            except Exception:             # noqa: BLE001 - 进程对象已被回收
                pid = None
        # 空 = 跟后端默认（现在是 llama.cpp，见 nl_engine.normalize_engine）。
        # 这里以前写死 "vllm"，等于在快照里绕开了那个默认值，页面上"引擎"
        # 一格显示的和实际会起的东西可以是两回事。
        engine = normalize_engine(overrides.get("engine"))
        local_ok, local_reason = self.local_capability(engine)
        # 盘上有指纹就用盘上的：可能是命令行 `tagger serve` 写的，也可能来自
        # 上一次网页启动。客户端读的是同一个文件，页面上展示的就该是它。
        disk_fp = read_fingerprint()
        # 这一组参数（含"还没启动时的默认档"）解析出来是什么。页面靠它
        # 在点启动之前显示"会起哪个档、哪套权重、可能有什么问题"。
        plan = self.plan(model=overrides.get("model"),
                         tier=overrides.get("tier"),
                         precision=overrides.get("precision"),
                         max_num_seqs=overrides.get("max_num_seqs"),
                         max_num_batched_tokens=overrides.get("max_num_batched_tokens"),
                         gpu_mem=overrides.get("gpu_mem"))
        return {
            "status": status,
            "error": error,
            "engine": engine,
            "port": port,
            "url": url,
            "pid": pid,
            "started": started,
            "ready_at": ready_at,
            "profile": profile,
            "log_path": log_path,
            "log_tail": self._tail(),
            "fingerprint": fp or disk_fp or None,
            "fingerprint_path": str(FINGERPRINT_PATH),
            "local_ok": local_ok,
            "local_reason": local_reason,
            "overrides": overrides,
            "plan": plan,
        }

    # ---------------------------------------------------------------- 启停

    def start(self, *, port: int = DEFAULT_PORT, timeout_s: int = 900,
              engine: str | None = None,
              model: str | None = None,
              tier: str | None = None,
              precision: str | None = None,
              served_name: str | None = None,
              max_num_seqs: int | None = None,
              max_num_batched_tokens: int | None = None,
              gpu_mem: float | None = None,
              local_media_root: str | None = None) -> dict:
        """起服务。立刻返回快照，等就绪在后台线程里做。

        model 选哪套权重（None = 按显存落档）；tier 指定分档（决定
        max_model_len / 默认并发）；served_name 是接口路由名，必须与任务
        表单里的"模型名"一致。并发三项留空就用分档表的值。它们必须在
        **启动时**定，改完要重启：分档表是按最保守的卡写的，而 30M 量级下
        这几个数直接决定跑几天。

        local_media_root 非空时加 `--allowed-local-media-path`，服务端就能
        收 file:// 地址；客户端据此用 `--use-file-url` 省掉 base64 的 33%
        体积与 CPU。放行范围必须覆盖客户端的 spool 目录（产物目录下的
        `.spool`），否则请求会因 400 被拒。
        """
        from ..nl_engine import normalize_engine

        engine = normalize_engine(engine)
        port = int(port or DEFAULT_PORT)
        if not 1024 <= port <= 65535:
            raise ValueError(f"端口 {port} 不合法：vLLM 需要 1024~65535（<1024 需要 root 权限）")
        timeout_s = max(30, int(timeout_s or 900))
        model = (model or "").strip() or None
        tier = (tier or "").strip() or None
        if model is not None or tier is not None:
            # 写错 key 时 resolve_profile 会在启动线程里抛，页面只看到
            # "启动失败" + 一段 traceback。在这里当场拒绝更省几分钟。
            from ..config import DEFAULT_TIERS, load_tier_table

            table = load_tier_table(DEFAULT_TIERS)
            known = table.get("models") or {}
            if model is not None and model not in known:
                raise ValueError(
                    f"未知模型 {model!r}，可选：{', '.join(map(str, known)) or '（分档表 models 段为空）'}")
            tiers = [str(t.get("name")) for t in (table.get("tiers") or [])
                     if isinstance(t, dict) and t.get("name")]
            if tier is not None and tier not in tiers:
                raise ValueError(
                    f"未知分档 {tier!r}，可选：{', '.join(tiers) or '（分档表 tiers 段为空）'}")
        # 精度只接受 bf16 / fp8 / w4a16（int4/awq/gptq 当同义写法）。在这里
        # 归一化而不是留到启动线程：写错的档位要当场回一句人话，而不是几分钟
        # 后页面上一个"启动失败 + traceback"。
        from ..config import normalize_quant_precision

        precision = normalize_quant_precision(precision)
        served_name = (served_name or "").strip() or "tagger"
        seqs = None if max_num_seqs is None else int(max_num_seqs)
        if seqs is not None and not 1 <= seqs <= 512:
            raise ValueError(f"max_num_seqs={seqs} 不合理：取 1~512")
        batched = (None if max_num_batched_tokens is None
                   else int(max_num_batched_tokens))
        if batched is not None and batched not in (0,) and batched < 2048:
            # 小于 2048 时一步放不下一张图的 prefill（576 图像 token + prompt）
            # 加解码，多请求会互相饿死。这个是硬底线，不是建议值。
            raise ValueError(f"max_num_batched_tokens={batched} 太小：至少 2048")
        # 留一点余量给 KV：0.98 以上 vLLM 自己就会因为拿不到启动显存而失败，
        # 与其让它报一段难懂的 CUDA OOM，不如在这里拦住。
        mem = None if gpu_mem is None else float(gpu_mem)
        if mem is not None and not 0.1 <= mem <= 0.98:
            raise ValueError(f"gpu_mem={mem} 不合理：取 0.1~0.98")
        media = (local_media_root or "").strip() or None
        if media is not None:
            root = Path(media)
            if not root.is_dir():
                # 放行一个不存在的目录，vLLM 起得来但每张 file:// 都被拒，
                # 报出来是 400 而不是"目录不存在"，排查要绕一大圈。
                raise ValueError(f"file:// 放行目录不存在：{root}")
            media = str(root.resolve())
        ok, reason = self.local_capability(engine)
        if not ok:
            # 环境不满足时当场拒绝：先回 202、两秒后翻成 failed 的话，
            # 用户看到的是一个"启动失败"的服务，得点开日志才知道是根本没显卡。
            raise ConfigError(reason)
        with self._lock:
            if self.status in ("starting", "ready"):
                raise RuntimeError("本机 vLLM 服务已在运行或启动中；先停止再换配置")
            self.status = "starting"
            self.error = ""
            self.port = port
            self.url = f"http://127.0.0.1:{port}"
            self.started = time.time()
            self.ready_at = 0.0
            self.profile = ""
            self._fingerprint = {}
            self._overrides = {"engine": engine,
                               "model": model,
                               "tier": tier,
                               "precision": precision,
                               "served_name": served_name,
                               "max_num_seqs": seqs,
                               "max_num_batched_tokens": batched,
                               "gpu_mem": mem,
                               "local_media_root": media}
            self.log_path = str(FINGERPRINT_PATH.parent / f"vllm-{port}.log")
            self._notes.clear()
            self._stop_flag.clear()
            # 每次启动一个令牌。上一个线程收尾慢（比如还在等就绪）时，
            # 它晚到的状态写入会被令牌挡掉，不会把新一轮的状态改回去。
            token = self._token = object()
        thread = threading.Thread(target=self._run,
                                  args=(port, timeout_s, token, dict(self._overrides)),
                                  name=f"vllm-{port}", daemon=True)
        self._thread = thread
        thread.start()
        return self.snapshot()

    def stop(self) -> dict:
        """停服务。等就绪的循环靠 _stop_flag 掐断，先置标志再收进程。"""
        with self._lock:
            handle = self._handle
            active = self.status in ("starting", "ready", "stopping")
            already = self.status == "stopped" and handle is None
            if active:
                self.status = "stopping"
        if already:
            return self.snapshot()
        self._stop_flag.set()
        if handle is not None:
            try:
                handle.stop()
            except Exception as exc:       # noqa: BLE001 - 回收失败也要落到状态里
                self._append_log(f"停止时出错：{type(exc).__name__}: {exc}")
        with self._lock:
            self._handle = None
            self.status = "stopped"
            self.error = ""
            self.ready_at = 0.0
            # 作废当前令牌：正在等就绪的线程收尾时不再写状态。
            self._token = None
        if active:
            self._append_log("服务已停止")
        return self.snapshot()

    # ---------------------------------------------------------------- 内部

    def _run(self, port: int, timeout_s: int, token: object,
             overrides: dict | None = None) -> None:
        handle = None
        overrides = overrides or {}

        def current() -> bool:
            """这一轮还作数吗。stop() 或新一轮 start() 会把令牌换掉。"""
            return self._token is token

        try:
            from ..config import DEFAULT_TIERS, load_tier_table, resolve_profile
            from ..hardware import query_gpus
            from ..model_loader import (fingerprint_env, probe_version,
                                        start_server, wait_ready)
            from ..nl_engine import check_engine

            engine = normalize_engine(overrides.get("engine"))
            check_engine(engine)
            gpus = query_gpus()
            if not gpus:
                raise ConfigError("没有检测到 NVIDIA 显卡（nvidia-smi 不可用）")
            profile = resolve_profile(
                load_tier_table(DEFAULT_TIERS), gpus[0],
                model_override=overrides.get("model"),
                tier_override=overrides.get("tier"),
                precision_override=overrides.get("precision"),
                max_num_seqs_override=overrides.get("max_num_seqs"),
                max_num_batched_tokens_override=overrides.get("max_num_batched_tokens"),
                gpu_mem_override=overrides.get("gpu_mem"),
            )
            log_path = Path(self.log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self.profile = profile.summary()

            # wait=False：不等就绪就返回，等就绪放到本线程里自己轮询，
            # 这样停止请求能在两次轮询之间生效。
            handle = start_server(profile, port=port, log_path=log_path, wait=False,
                                  served_name=overrides.get("served_name") or "tagger",
                                  data_root=overrides.get("local_media_root"),
                                  engine=engine)
            if not current():
                # 起进程的这几秒里已经被叫停：status 已是 stopped，但进程刚建出来。
                raise _Cancelled()
            with self._lock:
                self._handle = handle

            def tick(_remaining: int) -> None:
                if self._stop_flag.is_set() or not current():
                    raise _Cancelled()
                proc = handle.process
                if proc is not None and proc.poll() is not None:
                    # vLLM 起动就崩时 wait_ready 会傻等到超时；早发现早报错。
                    raise RuntimeError(f"vllm 进程已退出（code={proc.returncode}）")

            wait_ready(handle.base_url, timeout_s=timeout_s, on_tick=tick)
            # 就绪与"被停止"可能同时发生：晚到的 ready 会把 stopped 覆盖回去，
            # 页面上就成了"已停止但显示运行中"。
            if self._stop_flag.is_set() or not current():
                raise _Cancelled()
            # wait=False 的分支不填版本号，这里补一次，指纹里才有 vllm_version。
            handle.vllm_version = probe_version(handle.base_url, engine)
            fp = fingerprint_env(handle)
            FINGERPRINT_PATH.parent.mkdir(parents=True, exist_ok=True)
            FINGERPRINT_PATH.write_text(
                json.dumps(fp, ensure_ascii=False, indent=2), encoding="utf-8")
            with self._lock:
                if not current():
                    raise _Cancelled()
                self._fingerprint = fp
                self.ready_at = time.time()
                self.status = "ready"
        except _Cancelled:
            if handle is not None:
                try:
                    handle.stop()
                except Exception:          # noqa: BLE001 - 已经在收尾了
                    pass
            with self._lock:
                if current():
                    self._handle = None
                    self.status = "stopped"
                    self.error = ""
        except Exception as exc:           # noqa: BLE001 - 启动失败要能看见原因
            if handle is not None:
                try:
                    handle.stop()
                except Exception:          # noqa: BLE001 - 已经要报错了，别覆盖
                    pass
            with self._lock:
                if current():
                    self._handle = None
                    self.status = "failed"
                    self.error = f"{type(exc).__name__}: {exc}"
            if current():
                self._append_log(f"启动失败：{self.error}")

    def _tail(self, lines: int = TAIL_LINES) -> list[str]:
        try:
            raw = Path(self.log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return list(self._notes)[-lines:]
        return [ln for ln in raw.splitlines() if ln.strip()][-lines:]

    def _append_log(self, msg: str) -> None:
        """把 Web 侧的动作记进同一份服务日志。启动失败的原因常常不在 vllm
        的输出里（比如"没有显卡"），单独存一份内存消息又会在刷新后丢掉。"""
        self._notes.append(f"[web] {msg}")
        try:
            path = Path(self.log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"[web] {msg}\n")
        except OSError:
            pass

    # ---------------------------------------------------------------- 连通性

    @staticmethod
    def probe(url: str) -> dict:
        """探一个 OpenAI 兼容地址是否可用。远程服务只能靠这个。

        只打 /health 和 /v1/models：前者说明进程活着，后者说明模型名对不对。
        "地址填错"与"服务挂了"在页面上长得一样，分开报出来能省一次排查。
        """
        base = (url or "").strip().rstrip("/")
        out: dict = {"url": base, "ok": False, "version": "", "models": [],
                     "error": ""}
        if not base:
            out["error"] = "没有填服务地址"
            return out
        # 本机地址直连：开着代理软件时 /health 会被代理回 502，
        # 于是"服务没起"和"服务起着但代理拦了"在页面上长得一模一样。
        trust = trust_env_for(base)
        try:
            r = httpx.get(f"{base}/health", timeout=3, trust_env=trust)
        except httpx.HTTPError as exc:
            out["error"] = f"连不上：{type(exc).__name__}: {exc}"
            return out
        if r.status_code != 200:
            out["error"] = f"/health 返回 HTTP {r.status_code}"
            return out
        out["ok"] = True
        try:
            v = httpx.get(f"{base}/version", timeout=3, trust_env=trust)
            if v.status_code == 200:
                out["version"] = str(v.json().get("version") or "")
        except (httpx.HTTPError, ValueError):
            pass      # 版本号只是锦上添花，拿不到不影响"在线"这个结论
        try:
            m = httpx.get(f"{base}/v1/models", timeout=3, trust_env=trust)
            if m.status_code == 200:
                out["models"] = [str(x.get("id")) for x in (m.json().get("data") or [])]
        except (httpx.HTTPError, ValueError):
            pass
        return out


def read_fingerprint() -> dict:
    """盘上的环境指纹（`tagger serve` 写的）。没有就返回空字典。"""
    try:
        data = json.loads(FINGERPRINT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# app.py 持有的进程级单例。与 MANAGER / UPLOADS / DEDUPS 同一模式。
VLLM = VllmService()
