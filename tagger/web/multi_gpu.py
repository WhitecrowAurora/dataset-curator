"""多卡（数据并行）推理服务组：一张卡一个进程。

为什么是数据并行而不是张量并行
------------------------------
每张卡装一份完整权重，各自处理自己的请求，每张图算完就直接写它自己的
json。代价是每卡一份模型（8B-Q4 约 6GB），换来的是：

* 扩展是线性的——4 张卡约等于 4 倍吞吐，不需要任何模型侧支持；
* 产物天然是同一批文件，不需要事后按卡合并；
* 单卡和 N 卡的产出逐字节相同（参数完全一致，只是谁算的问题）。

张量并行是另一回事：要把权重按层/按头切开，llama.cpp 只能靠
`--split-mode`，且默认按 1:1 切——卡显存不等时会直接 OOM。对一个
"把 30M 张图打完"的批处理任务，数据并行的收益/风险比明显更好。

与 VllmService 的关系
---------------------
VllmService 管"一个服务"，这里管"一组服务"，两边不共享状态。
同一张卡上不能同时有单卡服务和多卡 worker——显存会互相抢，所以
调用方（app.py）在启动前负责拦住，这里只做本组内的互斥。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..config import (DEFAULT_TIERS, ConfigError, load_tier_table,
                      normalize_quant_precision, resolve_profile)
from ..hardware import GPUInfo, query_gpus
from ..model_loader import ServerHandle, start_server
from ..nl_engine import normalize_engine

# 多卡组的默认起始端口。和单卡服务的 8000 错开，方便两者并存观察，
# 也避免"停掉多卡、单卡起不来"这种端口占用引起的误判。
BASE_PORT = 8010

# 页面每 4 秒轮询一次快照，而每次快照都要调 nvidia-smi。缓 2 秒：
# 既不拖慢 nvidia-smi，也不至于让数字看起来是冻住的。
METRICS_TTL_S = 2.0

TAIL_LINES = 30


def read_metrics() -> dict[int, dict]:
    """每张卡的利用率与显存占用。

    查不到就返回空表——监控面板不该成为一个新的故障点，
    没有 nvidia-smi 的机器上其余功能照常。
    """
    from ..hardware import _find_nvidia_smi

    exe = _find_nvidia_smi()
    if not exe:
        return {}
    import subprocess

    try:
        out = subprocess.run(
            [exe, "--query-gpu=index,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    rows: dict[int, dict] = {}
    for line in out.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            idx = int(parts[0])
            util = float(parts[1])
            used = float(parts[2])
            total = float(parts[3])
        except ValueError:
            continue
        rows[idx] = {
            "util": util,
            "mem_used_mb": int(used),
            "mem_total_mb": int(total),
            "mem_pct": round(used / total * 100, 1) if total else 0.0,
        }
    return rows


@dataclass
class GpuWorker:
    """一张卡上的一个服务进程。字段读写跨线程，全部在 service 的锁里动。"""

    index: int                          # 物理卡号（nvidia-smi 的 index）
    name: str
    vram_gb: float
    port: int
    # stopped | starting | ready | failed
    status: str = "stopped"
    error: str = ""
    pid: int | None = None
    started: float = 0.0
    ready_at: float = 0.0
    log_path: str = ""
    handle: ServerHandle | None = field(default=None, repr=False)
    # 打标侧回调累加：这张卡打过多少张、失败多少。不是进程自己的指标，
    # 而是"这一轮任务里它实际分担了多少活"。
    processed: int = 0
    failed: int = 0
    last_ts: float = 0.0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class MultiGpuService:
    """进程级单例（app.py 持有）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status = "stopped"         # stopped | starting | ready | partial | failed | stopping
        self.engine = "llamacpp"
        self.error = ""
        self.base_port = BASE_PORT
        self.started = 0.0
        self.ready_at = 0.0
        self.workers: list[GpuWorker] = []
        self._overrides: dict = {}
        self._thread: threading.Thread | None = None
        self._token: object | None = None
        self._stop_flag = threading.Event()
        self._metrics: tuple[float, dict] = (0.0, {})
        self._notes: deque[str] = deque(maxlen=20)

    # ---------------------------------------------------------------- 查询

    @staticmethod
    def capability(engine: str = "llamacpp") -> tuple[bool, str, list[GPUInfo]]:
        """这组服务能不能起：引擎在不在、有没有卡、卡够不够。"""
        engine = normalize_engine(engine)
        # 先查卡再查引擎：引擎没装时页面仍要能列出"这台机器有几张卡"，
        # 否则用户看到的是"多卡不可用"，会先去查显卡而不是装 llama-server。
        gpus = query_gpus()
        if not gpus:
            return False, "没有检测到 NVIDIA 显卡（nvidia-smi 不可用）", []
        try:
            from ..nl_engine import check_engine

            check_engine(engine)
        except ConfigError as exc:
            return False, str(exc), gpus
        except Exception as exc:            # noqa: BLE001 - 体检失败不该让页面报错
            return False, f"环境检查失败：{type(exc).__name__}: {exc}", gpus
        return True, "", gpus

    def metrics(self) -> dict[int, dict]:
        now = time.time()
        ts, cached = self._metrics
        if now - ts < METRICS_TTL_S:
            return cached
        fresh = read_metrics()
        self._metrics = (now, fresh)
        return fresh

    def urls(self) -> list[str]:
        """就绪的地址，按卡号顺序。打标侧直接拿去当多槽。"""
        with self._lock:
            return [w.url for w in self.workers if w.status == "ready"]

    def note_item(self, slot: int, ok: bool) -> None:
        """打标完成一张，累到对应卡上。slot 越界就忽略——换服务/停任务时
        旧回调会晚到一两条，不该为此抛异常把整批打标打断。"""
        with self._lock:
            if 0 <= slot < len(self.workers):
                w = self.workers[slot]
                if ok:
                    w.processed += 1
                else:
                    w.failed += 1
                w.last_ts = time.time()
                self._notes.append(f"#{slot} {'完成' if ok else '失败'}")

    def _tail(self, log_path: str) -> list[str]:
        if not log_path:
            return []
        p = Path(log_path)
        if not p.is_file():
            return []
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                return [ln.rstrip() for ln in deque(f, maxlen=TAIL_LINES)]
        except OSError:
            return []

    def snapshot(self) -> dict:
        self._reap()
        metrics = self.metrics()
        with self._lock:
            status, error = self.status, self.error
            engine = self.engine
            base_port = self.base_port
            started, ready_at = self.started, self.ready_at
            overrides = dict(self._overrides)
            workers = [
                {
                    "index": w.index,
                    "name": w.name,
                    "vram_gb": w.vram_gb,
                    "port": w.port,
                    "url": w.url,
                    "status": w.status,
                    "error": w.error,
                    "pid": w.pid,
                    "started": w.started,
                    "ready_at": w.ready_at,
                    "log_path": w.log_path,
                    "processed": w.processed,
                    "failed": w.failed,
                    "last_ts": w.last_ts,
                    "gpu": metrics.get(w.index) or {},
                    "log_tail": self._tail(w.log_path) if w.status == "failed" else [],
                }
                for w in self.workers
            ]
            notes = list(self._notes)
        return {
            "status": status,
            "error": error,
            "engine": engine,
            "base_port": base_port,
            "started": started,
            "ready_at": ready_at,
            "overrides": overrides,
            "workers": workers,
            "notes": notes,
            # 打标表单要的：每卡并发（= 每卡 slot 数）与总并发。
            "per_card_seqs": int(overrides.get("per_card_seqs") or 0),
            "total_seqs": sum(1 for _ in workers) * int(overrides.get("per_card_seqs") or 0),
            "urls": [w["url"] for w in workers if w["status"] == "ready"],
        }

    def _reap(self) -> None:
        """进程自己挂了要把状态翻成 failed，否则页面一直显示就绪、
        任务全超时，而人以为卡还在跑。"""
        with self._lock:
            pairs = [(w, w.handle) for w in self.workers
                     if w.status == "ready" and w.handle and w.handle.process]
        for w, handle in pairs:
            code = handle.process.poll()
            if code is None:
                continue
            with self._lock:
                if w.status == "ready":
                    w.status = "failed"
                    w.error = f"服务进程已退出（code={code}）"
                    if self.status == "ready":
                        self.status = "partial"

    # ---------------------------------------------------------------- 启停

    def start(self, *, engine: str = "llamacpp",
              gpus: list[int] | None = None,
              base_port: int = BASE_PORT,
              model: str | None = None,
              tier: str | None = None,
              precision: str | None = None,
              served_name: str = "tagger",
              per_card_seqs: int | None = None,
              timeout_s: int = 1800) -> dict:
        """起一组服务。立刻返回快照，逐个加载在后台线程里做。

        逐个而不并发加载：几个进程同时把几 GB 权重读进显存，磁盘和 PCIe
        会互相踩，总时间反而更长，而且日志交叠后 OOM 到底是谁的看不出来。
        """
        engine = normalize_engine(engine)
        base_port = int(base_port or BASE_PORT)
        if not 1024 <= base_port <= 65000:
            raise ValueError(f"基端口 {base_port} 不合法：取 1024~65000")
        seqs = None if per_card_seqs in (None, 0, "") else int(per_card_seqs)
        if seqs is not None and not 1 <= seqs <= 64:
            # 每卡 64 条在途已经远超一张卡能同时 prefill 的量，
            # 再大只会把请求堆在服务端排队，延迟涨、吞吐不动。
            raise ValueError(f"每卡并发={seqs} 不合理：取 1~64")
        timeout_s = max(60, int(timeout_s or 1800))
        served_name = (served_name or "").strip() or "tagger"

        ok, reason, available = self.capability(engine)
        if not ok:
            raise ConfigError(reason)
        if len(available) < 2:
            raise ConfigError(
                f"检测到 {len(available)} 张卡，多卡模式至少需要 2 张。"
                f"单卡请用普通启动。")
        wanted = [int(x) for x in (gpus or [g.index for g in available])]
        known = {g.index: g for g in available}
        missing = [i for i in wanted if i not in known]
        if missing:
            raise ConfigError(
                f"这些卡号不存在：{missing}。可用：{sorted(known)}")
        if len(wanted) < 2:
            raise ConfigError("多卡模式至少要选 2 张卡。")
        if len(wanted) != len(set(wanted)):
            raise ConfigError(f"卡号重复：{wanted}")
        ports = [base_port + i for i in range(len(wanted))]
        if ports[-1] > 65535:
            raise ConfigError(f"端口会越界：{ports[0]}~{ports[-1]}")

        with self._lock:
            if self.status in ("starting", "ready", "stopping"):
                raise RuntimeError("多卡服务已在运行或启动中；先停止再换配置")
            self.status = "starting"
            self.error = ""
            self.engine = engine
            self.base_port = base_port
            self.started = time.time()
            self.ready_at = 0.0
            self._overrides = {
                "engine": engine, "gpus": wanted, "base_port": base_port,
                "model": (model or "").strip() or None,
                "tier": (tier or "").strip() or None,
                "precision": normalize_quant_precision(precision),
                "served_name": served_name,
                "per_card_seqs": seqs,
            }
            # 重建工人表：卡号变了以后旧记录没有意义。
            self.workers = []
            for i, idx in enumerate(wanted):
                gpu = known[idx]
                self.workers.append(GpuWorker(
                    index=idx, name=gpu.name, vram_gb=gpu.vram_gb,
                    port=ports[i],
                    log_path=str(Path("run") / f"multi-{idx}-{ports[i]}.log"),
                ))
            self._notes.clear()
            self._stop_flag.clear()
            token = self._token = object()
            cfg = dict(self._overrides)
            timeout = timeout_s

        thread = threading.Thread(target=self._run, args=(token, cfg, timeout),
                                  name="multi-gpu", daemon=True)
        self._thread = thread
        thread.start()
        return self.snapshot()

    def stop(self) -> dict:
        with self._lock:
            active = self.status in ("starting", "ready", "partial", "stopping")
            if active:
                self.status = "stopping"
        self._stop_flag.set()
        with self._lock:
            handles = [(w, w.handle) for w in self.workers if w.handle]
        for w, handle in handles:
            try:
                handle.stop()
            except Exception as exc:        # noqa: BLE001 - 回收失败也要落到状态里
                self._notes.append(f"#{w.index} 停止时出错：{exc}")
        with self._lock:
            for w in self.workers:
                w.handle = None
                w.pid = None
                if w.status in ("starting", "ready"):
                    w.status = "stopped"
            self.status = "stopped"
            self.error = ""
            self.ready_at = 0.0
            self._token = None
        return self.snapshot()

    # ---------------------------------------------------------------- 内部

    def _alive(self, token: object) -> bool:
        return self._token is token and not self._stop_flag.is_set()

    def _run(self, token: object, cfg: dict, timeout_s: int) -> None:
        table = None
        try:
            table = load_tier_table(DEFAULT_TIERS)
        except ConfigError as exc:
            with self._lock:
                self.status = "failed"
                self.error = str(exc)
            return

        engine = cfg["engine"]
        seqs = cfg["per_card_seqs"]
        ready = 0
        for w in list(self.workers):
            if not self._alive(token):
                return
            with self._lock:
                w.status = "starting"
                w.started = time.time()
                w.error = ""
            gpu = GPUInfo(index=w.index, name=w.name, vram_gb=w.vram_gb,
                          compute_cap=None)
            try:
                profile = resolve_profile(
                    table, gpu,
                    model_override=cfg["model"],
                    tier_override=cfg["tier"],
                    precision_override=cfg["precision"],
                    max_num_seqs_override=seqs,
                )
                Path(w.log_path).parent.mkdir(parents=True, exist_ok=True)
                handle = start_server(
                    profile,
                    port=w.port,
                    engine=engine,
                    served_name=cfg["served_name"],
                    device_index=w.index,
                    slots_override=seqs,
                    log_path=w.log_path,
                    wait=True,
                    timeout_s=timeout_s,
                )
            except Exception as exc:        # noqa: BLE001 - 一张卡失败不该拖垮整组
                with self._lock:
                    w.status = "failed"
                    w.error = f"{type(exc).__name__}: {exc}"
                continue
            if not self._alive(token):
                # 加载过程中被停了：把刚起来的这份收掉，否则会留一个
                # 谁也管不到的进程占着整张卡。
                try:
                    handle.stop()
                except Exception:           # noqa: BLE001
                    pass
                return
            with self._lock:
                w.handle = handle
                w.status = "ready"
                w.ready_at = time.time()
                w.pid = handle.process.pid if handle.process else None
                ready += 1
        with self._lock:
            if self._token is not token:
                return
            total = len(self.workers)
            if ready == total and total:
                self.status = "ready"
                self.ready_at = time.time()
            elif ready:
                self.status = "partial"
                self.error = f"{total - ready}/{total} 张卡启动失败，失败的卡不参与打标"
            else:
                self.status = "failed"
                self.error = "所有卡都启动失败，展开每张卡的日志看原因"


# 进程级单例：路由、任务、快照读的是同一份状态。
MULTI = MultiGpuService()
