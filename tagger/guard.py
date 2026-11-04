"""资源守护：占空比让步、温度闭环、磁盘熔断。

机制沿用 lulynx-trainer 里验证过的做法（`GpuDutyCycleThrottler`）：

    「GPU 利用率」的本质就是 kernel 之间的空转时间，
    所以制造空闲最干净的位置是任务边界，而不是去抢驱动层。

因此这里不做驱动级限流，只在每处理完一个单元后 sleep
`sleep = busy * (1 - duty) / duty`，让平均功耗贴着 duty 走。
好处是不干扰推理框架自己的批处理与 CUDA graph。

温度用 PI 闭环动态压 duty（和训练器同一套参数）：
误差超死区就调积分项，自动把占空比压到能维持目标温度的水平。

三个流是分开的：
    采样线程 watchdog（daemon，只读 GPU 状态，永不阻塞主流程）
    主流程       打标循环，只在单元边界读一次最新状态并决定让不让步
    熔断逻辑     温度/磁盘超限时置位 gate，主流程自然停下

所以打标不会被监控拖慢，监控也不会因为打标卡住而失效。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


# ------------------------------------------------------------------ 策略


@dataclass(slots=True)
class GuardPolicy:
    """所有阈值都可配。默认值保守，但用户可以整体调松/调紧。

    训练器那边的经验是：`gpu_duty_cycle` 取 0.2~1.0，1.0 = 不插入空闲。
    这里沿用同一语义，默认 0.7（留三成空）。
    """

    # 占空比：1.0 = 全速，0.7 = 每忙 7 份睡 3 份
    duty_cycle: float = 0.70

    # 温度闭环：0 = 关闭；设成 68 表示自动把占空比压到维持 68°C
    target_temp_c: float = 0.0
    temp_deadband_c: float = 2.0
    kp: float = 0.04
    ki: float = 0.004
    min_duty: float = 0.2
    max_sleep_s: float = 30.0

    # 硬熔断（闭环之外的保护线）
    temp_warn_c: float = 72.0
    temp_cutoff_c: float = 82.0
    temp_resume_c: float = 70.0

    # 磁盘
    disk_min_free_gb: float = 30.0
    disk_min_free_pct: float = 0.15

    # 连续运行多久强制休息
    continuous_minutes: float = 0.0
    rest_minutes: float = 5.0

    sample_interval_s: float = 5.0
    temp_poll_interval_s: float = 5.0

    # 笔记本散热余量小，自动再收紧一档
    auto_tune_laptop: bool = True

    def tuned_for(self, name: str) -> "GuardPolicy":
        if not self.auto_tune_laptop:
            return self
        lowered = name.lower()
        if not any(k in lowered for k in ("laptop", "mobile", "max-q")):
            return self
        return GuardPolicy(
            duty_cycle=min(self.duty_cycle, 0.55),
            target_temp_c=self.target_temp_c or 70.0,
            temp_deadband_c=self.temp_deadband_c,
            kp=self.kp, ki=self.ki,
            min_duty=self.min_duty, max_sleep_s=self.max_sleep_s,
            temp_warn_c=min(self.temp_warn_c, 68.0),
            temp_cutoff_c=min(self.temp_cutoff_c, 76.0),
            temp_resume_c=min(self.temp_resume_c, 65.0),
            disk_min_free_gb=self.disk_min_free_gb,
            disk_min_free_pct=self.disk_min_free_pct,
            continuous_minutes=self.continuous_minutes or 120.0,
            rest_minutes=self.rest_minutes,
            sample_interval_s=self.sample_interval_s,
            temp_poll_interval_s=self.temp_poll_interval_s,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "GuardPolicy":
        """从配置字典构造，允许只写要改的字段。"""
        allowed = {f for f in cls.__slots__}
        kwargs = {k: v for k, v in (d or {}).items() if k in allowed}
        return cls(**kwargs)


# ------------------------------------------------------------------ 状态


@dataclass(slots=True)
class GuardState:
    level: str = "ok"            # ok | warn | pause | stop
    reasons: list[str] = field(default_factory=list)
    temp_c: float = 0.0
    gpu_util: float = 0.0
    vram_used_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_free_pct: float = 1.0
    duty: float = 1.0
    session_minutes: float = 0.0
    ts: float = field(default_factory=time.time)

    @property
    def can_run(self) -> bool:
        return self.level in ("ok", "warn")

    def describe(self) -> str:
        return (
            f"temp {self.temp_c:.0f}°C  util {self.gpu_util * 100:.0f}%  "
            f"duty {self.duty:.2f}  disk {self.disk_free_gb:.0f}G/"
            f"{self.disk_free_pct * 100:.0f}%  [{self.level}]"
            + ("  " + "; ".join(self.reasons) if self.reasons else "")
        )


def _read_gpu() -> tuple[float, float, float]:
    """(利用率0-1, 温度°C, 显存已用GB)。读不到返回 0。"""
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    try:
        out = subprocess.run(
            [exe, "--query-gpu=utilization.gpu,temperature.gpu,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=8,
        )
    except (subprocess.SubprocessError, OSError):
        return 0.0, 0.0, 0.0
    if out.returncode != 0 or not out.stdout.strip():
        return 0.0, 0.0, 0.0
    parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
    if len(parts) < 3:
        return 0.0, 0.0, 0.0
    try:
        # memory.used 的单位是 MiB（nounits 只是不带百分号/后缀），
        # 不除以 1024 的话 GuardState.vram_used_gb 会虚大 1024 倍。
        return float(parts[0]) / 100.0, float(parts[1]), float(parts[2]) / 1024.0
    except ValueError:
        return 0.0, 0.0, 0.0


# ------------------------------------------------------------------ 占空比


class DutyCycleGovernor:
    """在任务边界插入空闲，把平均功耗压到 duty 附近。

    和训练器的 step 边界同一个思路，只是这里的"单元"是一张图 / 一批图。
    """

    def __init__(self, policy: GuardPolicy, *, clock=time.monotonic,
                 sleeper=time.sleep, verbose: bool = True):
        self.policy = policy
        self._clock = clock
        self._sleep = sleeper
        self.verbose = verbose
        self._last_boundary: float | None = None
        self._auto_duty = 1.0
        self._integral = 0.0
        self._last_temp = 0.0
        self._last_poll = 0.0
        self.last_report: dict = {}

    def update_temperature(self, temp_c: float) -> None:
        """温度闭环（PI）。由 watchdog 线程周期性喂进来。"""
        p = self.policy
        if p.target_temp_c <= 0 or temp_c <= 0:
            return
        now = self._clock()
        if now - self._last_poll < p.temp_poll_interval_s:
            return
        self._last_poll = now
        self._last_temp = temp_c

        error = temp_c - p.target_temp_c
        if abs(error) > p.temp_deadband_c:
            self._integral = min(max(self._integral + error, -200.0), 200.0)
            self._auto_duty = 1.0 - p.kp * error - p.ki * self._integral
        self._auto_duty = min(max(self._auto_duty, p.min_duty), 1.0)

    @property
    def effective_duty(self) -> float:
        """手动 duty 与温控 duty 取更小者——谁更保守听谁的。"""
        return min(self.policy.duty_cycle, self._auto_duty)

    def on_unit_done(self) -> dict:
        """每处理完一个单元调用一次。返回本次让步的记录。"""
        now = self._clock()
        if self._last_boundary is None:
            self._last_boundary = now
            return {}

        busy_s = max(now - self._last_boundary, 0.0)
        self._last_boundary = now

        duty = self.effective_duty
        slept = 0.0
        if duty < 1.0 and busy_s > 0.0:
            sleep_s = min(busy_s * (1.0 - duty) / max(duty, 1e-6),
                          self.policy.max_sleep_s)
            if sleep_s >= 0.005:
                self._sleep(sleep_s)
                slept = sleep_s

        self.last_report = {
            "busy_s": round(busy_s, 3),
            "slept_s": round(slept, 3),
            "duty": round(duty, 3),
            "auto_duty": round(self._auto_duty, 3),
            "temp_c": round(self._last_temp, 1),
        }
        return self.last_report


# ------------------------------------------------------------------ 看门狗


class GuardWatchdog(threading.Thread):
    """独立守护流：只采样，不改主流程节奏。

    打标主循环通过 `state` 属性读取最新快照，不会因为磁盘/温度查询而阻塞。
    """

    def __init__(self, policy: GuardPolicy, *, data_dir: str | Path = ".",
                 device_name: str = "", governor: DutyCycleGovernor | None = None,
                 verbose: bool = True,
                 on_state: "callable | None" = None):
        super().__init__(name="tagger-guard", daemon=True)
        self.policy = policy.tuned_for(device_name)
        self.data_dir = Path(data_dir)
        self.device_name = device_name
        self.governor = governor
        self.verbose = verbose
        # 状态回调（Web 层订阅）：级别变为 warn/pause/stop，或从高级别恢复
        # 时通知一次。回调在 watchdog 线程里执行，必须快、不许抛。
        self.on_state = on_state
        self._last_notified = "ok"

        self.state = GuardState(level="ok")
        self._stop = threading.Event()
        self._started_at = time.time()
        self._resume_after = 0.0
        # GPU 读数连续为 0 的次数 / 是否读到过非零读数。用来区分"没有
        # NVIDIA 卡的机器"（从来读不到，不拦）和"传感器/驱动中途死了"
        # （读到过、后来全是 0，必须拦——不然温度熔断从此失效，等于
        # 带着坏仪表继续跑）。
        self._gpu_zero = 0
        self._gpu_seen = False

    @staticmethod
    def _rank(level: str) -> int:
        """级别高度：stop > pause > warn > ok。赋值前先比高度，防止降级。"""
        return {"ok": 0, "warn": 1, "pause": 2, "stop": 3}.get(level, 0)

    def _disk(self) -> tuple[float, float]:
        try:
            u = shutil.disk_usage(self.data_dir)
        except OSError:
            return 999.0, 1.0
        return u.free / 1024 ** 3, (u.free / u.total if u.total else 1.0)

    def sample_once(self) -> GuardState:
        util, temp, vram = _read_gpu()
        free_gb, free_pct = self._disk()
        p = self.policy
        st = GuardState(
            temp_c=temp, gpu_util=util, vram_used_gb=vram,
            disk_free_gb=free_gb, disk_free_pct=free_pct,
            duty=self.governor.effective_duty if self.governor else p.duty_cycle,
            session_minutes=(time.time() - self._started_at) / 60.0,
        )

        if free_gb < p.disk_min_free_gb or free_pct < p.disk_min_free_pct:
            st.level = "stop"
            st.reasons.append(
                f"磁盘 {free_gb:.0f}G/{free_pct * 100:.0f}% 低于 "
                f"{p.disk_min_free_gb:.0f}G/{p.disk_min_free_pct * 100:.0f}%")

        now = time.time()
        if temp >= p.temp_cutoff_c:
            if self._rank(st.level) < self._rank("pause"):
                st.level = "pause"
            st.reasons.append(f"温度 {temp:.0f}°C ≥ 熔断线 {p.temp_cutoff_c:.0f}°C")
            self._resume_after = max(self._resume_after, now + p.sample_interval_s * 3)
        elif temp >= p.temp_warn_c and st.level == "ok":
            st.level = "warn"
            st.reasons.append(f"温度 {temp:.0f}°C ≥ 警告线 {p.temp_warn_c:.0f}°C")

        if now < self._resume_after or (self._resume_after and temp > p.temp_resume_c):
            if self._rank(st.level) < self._rank("pause"):
                st.level = "pause"
            if not st.reasons:
                st.reasons.append(f"冷却中 {temp:.0f}°C > 恢复线 {p.temp_resume_c:.0f}°C")

        if p.continuous_minutes > 0 and st.session_minutes >= p.continuous_minutes:
            # 到点就重置计时并进入强制休息。不重置的话 session_minutes 只增
            # 不减，一旦触发就永远 pause——"连续 N 分钟休 M 分钟"变成
            # "跑满 N 分钟后整个进程停摆"。
            self._started_at = now
            self._resume_after = max(self._resume_after,
                                     now + p.rest_minutes * 60.0)
            if self._rank(st.level) < self._rank("pause"):
                st.level = "pause"
            st.reasons.append(f"已连续 {st.session_minutes:.0f} 分钟，"
                              f"休息 {p.rest_minutes:.0f} 分钟")

        # 温度熔断之外的第二道保护：GPU 读数曾经正常、现在连续读不到，
        # 说明 nvidia-smi/驱动出了问题，所有温度保护都成了摆设。停下来
        # 让人看，比"感觉不到热就继续跑"诚实。
        if temp <= 0 and util <= 0:
            if self._gpu_seen:
                self._gpu_zero += 1
                if self._gpu_zero >= 3:
                    if self._rank(st.level) < self._rank("stop"):
                        st.level = "stop"
                    st.reasons.append(
                        f"GPU 状态连续 {self._gpu_zero} 次读不到"
                        f"（nvidia-smi 失效？温度保护已失效）")
            # 从没读到过数的机器（无卡 / 无驱动）不算异常，不拦。
        else:
            self._gpu_zero = 0
            self._gpu_seen = True

        if self.governor:
            self.governor.update_temperature(temp)
            st.duty = self.governor.effective_duty
        return st

    def _notify(self, st: GuardState) -> None:
        """级别变化时通知订阅方（Web 层）。恢复到 ok 也通知——界面上的
        "已暂停"需要有人来解除。回调抛异常就摘掉：守护线程绝不能陪葬。"""
        if self.on_state is None or st.level == self._last_notified:
            return
        self._last_notified = st.level
        try:
            self.on_state(st)
        except Exception:
            self.on_state = None

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.state = self.sample_once()
                self._notify(self.state)
                if self.verbose and self.state.level in ("pause", "stop"):
                    print(f"[guard] {self.state.describe()}", flush=True)
            except Exception as exc:  # 守护线程绝不能因为异常死掉
                if self.verbose:
                    print(f"[guard] 采样异常: {type(exc).__name__}: {exc}", flush=True)
            self._stop.wait(self.policy.sample_interval_s)

    def stop(self) -> None:
        self._stop.set()

    def wait_can_run(self, *, max_wait_min: float = 60.0,
                     poll_s: float = 2.0, on_wait=None) -> GuardState:
        """主流程在需要时调用：阻塞到允许继续为止。"""
        deadline = time.time() + max_wait_min * 60
        while True:
            st = self.state
            if st.can_run:
                return st
            if st.level == "stop":
                return st
            if time.time() > deadline:
                st.reasons.append(f"等待超过 {max_wait_min:.0f} 分钟")
                return st
            if on_wait:
                on_wait(st)
            time.sleep(poll_s)


# ------------------------------------------------------------------ 组合入口


class ResourceGuard:
    """主流程用的门面：一条命令起线程，之后每单元问一次。"""

    def __init__(self, policy: GuardPolicy | None = None, *,
                 data_dir: str | Path = ".", device_name: str = "",
                 verbose: bool = True, start_watchdog: bool = True,
                 on_state=None):
        self.policy = (policy or GuardPolicy()).tuned_for(device_name)
        self.device_name = device_name
        self.verbose = verbose
        self.governor = DutyCycleGovernor(self.policy, verbose=verbose)
        self.watchdog = GuardWatchdog(
            self.policy, data_dir=data_dir, device_name=device_name,
            governor=self.governor, verbose=verbose, on_state=on_state,
        )
        self._autostart = start_watchdog
        self._started = False

    @property
    def state(self) -> GuardState:
        return self.watchdog.state

    def start(self) -> "ResourceGuard":
        if self._autostart and not self._started:
            self.watchdog.start()
            self._started = True
        return self

    def stop(self) -> None:
        if self._started:
            self.watchdog.stop()
            self._started = False

    def before_unit(self, *, wait: bool = True) -> GuardState:
        """每个单元开始前调用。必要时阻塞。"""
        if not self._started:
            self.start()
        st = self.state
        if not st.can_run and wait:
            st = self.watchdog.wait_can_run()
        return st

    def after_unit(self) -> dict:
        """每个单元结束后调用，按占空比让步。"""
        return self.governor.on_unit_done()

    def __enter__(self) -> "ResourceGuard":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def power_hint(device_name: str = "") -> str:
    """功耗墙/锁频建议。比占空比更有效的手段，但要管理员权限。"""
    return (
        "比占空比更划算的两种做法（需要管理员权限，影响全局设置）：\n"
        "  限功耗 : nvidia-smi -pl <瓦数>     例：4090 450W→320W，性能约掉 5-8%\n"
        "  锁频率 : nvidia-smi -lgc <MHz>     例：1500\n"
        "  复位   : nvidia-smi -rgc && nvidia-smi -pl <原瓦数>\n"
        "原理：降频同时降压，同样损失 1 个 FLOP 换来的散热收益高于占空比空转。\n"
        "对打标这种 batch 推理（受显存带宽限制、不吃满算力）尤其划算。"
    )
