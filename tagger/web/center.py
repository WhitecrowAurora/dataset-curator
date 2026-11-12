"""调度中心：把 fox-label v3 客户端托管成一个可启停的后台进程。

边界
----
协调（握手 / 派工 / 心跳 / 预锁 / 回收 / 归还）是同事那套 `worker.py`，我们只内置
一份原样副本 `tagger/fox_worker.py`，**这段逻辑一行都不重写**。这里只做三件事：

1. 起 / 停那个子进程，把它的 stdout 收进环形缓冲给页面看；
2. 身份（worker.json）与本页配置落在 `work/center/` 下，不脏用户主目录；
3. 拿同一套凭据打一次 `/api/v1/status`，给页面一份**只读**看板（不派工、不续约）。

为什么开子进程而不是在 web 进程里开线程
--------------------------------------
worker 自己会起孙进程（`--cmd` 打标）、自己管信号与退出码。放进 web 线程里，
一次崩栈就把整个后端带走；作为子进程，它死了页面能看见，重启也只是再点一次。
这也和 `vllm_service.py` 管 llama-server 的方式一致。

用户只需要填两样
----------------
服务器地址 + 姓名。`--cmd` 由这里自动拼好（打标服务地址、模式、进度文件都在里面），
页面高级项才允许改——普通人不需要知道"要跑什么命令"。
"""

from __future__ import annotations

import collections
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
CENTER_DIR = ROOT / "work" / "center"

DEFAULTS = {
    "server": "",
    "user_id": "",
    # 打标服务地址（本机 serve 或外部 API 的中转都行）。
    "tag_url": "http://127.0.0.1:8000",
    "mode": "both",                 # both / tag / nl（能产哪几侧，硬约束）
    # 派工倾向：只影响服务端把哪种片排在前面，不改变本机产出什么。
    # "" = 不挑（老口径）；tag/nl = 只接缺那侧的片；pref-tag/pref-nl = 优先那侧。
    "parts_pref": "",
    "out_dir": str(ROOT / "work" / "fox-work"),
    "interval": 120,                # 心跳秒；0 = 用服务端建议值
    "extra_args": "",               # 追加给 tagger client 的参数（高级）
    "cmd_template": "",             # 非空则完全覆盖自动拼的命令（高级）
    # 长驻等待：池子空了不退出，隔一会儿再问。开着才像"挂在线上等人派活"；
    # 关着就是"把手上的干完就停"，页面上会直接从运行中变成已停止。
    "wait": True,
    "idle_timeout": 0,              # 配 wait：连续这么多秒没活就自己收工（0 = 一直等）
    "max_packages": 0,              # 本次最多交付多少片，够了就停（0 = 不限）
    # 分片下载：并发连接数 + 开工前测速选最快 CDN 边缘。
    # 国内到 HF 单条 TCP 常被限速，多连接才是把带宽吃满的那一半；
    # 选优那一半是靠 2MiB 的探测把 v4/v6 里最快的那条路钉住。
    "download_conns": 4,            # 1 = 单流；4~8 条足够
    "cdn_probe": True,              # 关掉只省几秒探测，多连接照旧
    # 看门狗：子进程非主动退出（崩溃、被杀内存、网络断到进程自己抛死）时自动
    # 按退避重新拉起。关掉就回到"崩了就停在已停止，等人再点开始"的老行为。
    "watchdog": True,
}

# 看门狗策略。FATAL_RC 是"重启也救不了"的退出码：2 配置/用法、3 认证、
# 7 连续失败到上限（worker 侧主动停手，等环境修好）。其余一律当偶发处理。
FATAL_RC = {2, 3, 7}
RESTART_BASE = 3.0        # 首次退避 3s，之后翻倍：3/6/12/24/48/60
RESTART_MAX = 60.0
HEALTHY_SECONDS = 600.0   # 跑满 10 分钟就算这次是健康的，退避归零
WATCHDOG_TICK = 1.0       # 看门狗巡一圈的间隔（测试会把它调小）

LOG_LINES = 400

# 这几个键清空没有意义（清掉只会让下一次启动必然失败），空串一律当"没填"。
_NON_EMPTY = {"server", "user_id", "tag_url", "out_dir", "mode"}

MODES = ("both", "tag", "nl")

# 派工倾向（服务端 dispatch.PARTS_CHOICES 去掉 both 之后的写法）：
#   ""        不挑，接任何还欠东西的片（老口径，两侧都做）
#   "tag" / "nl"            **只接**缺这一侧的片，没有就空转等
#   "pref-tag" / "pref-nl"  优先缺这一侧的片，另一侧也能做（兜底接）
# 空串是有效值（= 不传 --parts），所以不能加进 _NON_EMPTY。
PARTS_PREFS = ("", "tag", "nl", "pref-tag", "pref-nl")


def _num(val, default: float = 0.0) -> float:
    """配置里的数字可能被手改成垃圾值，转不动就当 0，别让点「开始」变成 500。"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class CenterError(RuntimeError):
    """消息直接给页面显示。"""


def _kill_tree(pid: int) -> None:
    """终止 pid 及其全部子孙进程。

    Windows 用 `taskkill /T` 按父子链收（没有进程组语义）；POSIX 靠
    `start_new_session` 建出来的进程组整组 SIGTERM，worker 才有机会归还租约。
    两边的失败都咽掉：进程可能已经自己退了，那不是错误。
    """
    if not pid:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, AttributeError):
        pass


class Center:
    def __init__(self, work_dir: Path | None = None):
        self.dir = Path(work_dir or CENTER_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.dir / "center.json"
        # worker 自己的身份文件（它写，我们只读）。用环境变量指过来，
        # 否则它会写到 ~/.config/fox-label/worker.json，卸载留垃圾。
        self.worker_cfg = self.dir / "worker.json"
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._log: collections.deque[str] = collections.deque(maxlen=LOG_LINES)
        self._started_at = 0.0
        self._last_rc: int | None = None
        # 看板缓存。snapshot() 是每个接口的返回值，而取看板要打一次 HTTP
        # （连不上时最长 6s）——不缓存的话点「开始」「停止」都得先陪等一轮超时。
        self._board_cache: tuple[float, dict] | None = None
        # 看门狗状态：_wanted 是"用户希望它在跑"，和 running（此刻真在跑）分开。
        # 用户点停止 → _wanted=False，进程被我们杀掉也不会被拉起；进程自己崩了 →
        # _wanted 仍为 True，到点自动重新拉起。
        self._wanted = False
        self._next_spawn = 0.0
        self._restarts = 0
        self._spawn_at = 0.0
        self._argv: list[str] = []
        threading.Thread(target=self._watchdog, name="center-watchdog",
                         daemon=True).start()

    # ------------------------------------------------------------ 配置
    def config(self) -> dict:
        saved = {}
        try:
            saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
        return cfg

    def save_config(self, **kw) -> dict:
        cfg = self.config()
        for key in DEFAULTS:
            if key not in kw:
                continue
            val = kw[key]
            if val is None:
                continue
            # 空串当"没填"。页面把输入框清空（或表单还没灌进草稿）时不该把
            # 配置清成空——`tag_url` 变成空串，build_cmd 会拼出 `--url ""`，
            # worker 跑起来必然失败，而报错里只看得到"请求失败"。
            # extra_args / cmd_template 的空是有意义的（清空），不在此列。
            if key in _NON_EMPTY and isinstance(val, str) and not val.strip():
                continue
            cfg[key] = val
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        return cfg

    # ------------------------------------------------------------ 身份
    def identity(self) -> dict:
        try:
            data = json.loads(self.worker_cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        # secret 不回给页面：页面上没有一处需要它，回显只是多一个泄漏点。
        return {k: data.get(k) for k in
                ("user_id", "device_id", "server", "server_id", "beat_interval")
                if data.get(k) not in (None, "")}

    def _env(self) -> dict:
        env = dict(os.environ)
        env["FOXLABEL_WORKER_CONFIG"] = str(self.worker_cfg)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        # 子进程的 print 走 locale 编码（Windows 上是 cp936），而管道那头我们
        # 按 UTF-8 解 —— 不钉死这一项，页面日志里的中文全是"�"。
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    # ------------------------------------------------------------ 登录
    def login(self, server: str, user_id: str, secret: str = "") -> dict:
        server = (server or "").strip().rstrip("/")
        user_id = (user_id or "").strip()
        if not server:
            raise CenterError("要填调度中心地址（例：http://1.2.3.4:31831）")
        if not user_id:
            raise CenterError("要填姓名/ID：服务端按它记账，也是你取回工位的唯一依据")
        if self.running:
            raise CenterError("正在跑的时候不能换身份，先停止")

        argv = [sys.executable, "-m", "tagger.fox_worker", "login",
                "--server", server, "--user-id", user_id]
        if secret.strip():
            argv += ["--secret", secret.strip()]
        try:
            # encoding 必须和 _env 里的 PYTHONIOENCODING 对齐：子进程按 UTF-8 写，
            # 这边却按 locale(cp936) 读，中文会解码失败或变成乱码。
            proc = subprocess.run(argv, cwd=str(ROOT), env=self._env(),
                                  capture_output=True, text=True, timeout=60,
                                  encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            # 不捕获的话这里冒到 HTTP 层就是 500，页面只说"服务器错误"，
            # 而真实原因（地址填错、网络黑洞）谁都看不出来。
            raise CenterError("登录超时（60s 没有应答）：检查地址和网络") from exc
        except OSError as exc:
            raise CenterError(f"起不了 worker：{exc}") from exc
        for line in (proc.stdout or "").splitlines():
            self._push(f"[login] {line}")
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise CenterError(detail[-1] if detail else f"登录失败（退出码 {proc.returncode}）")

        self.save_config(server=server, user_id=user_id)
        # 身份换了，旧看板已经不是这台设备的事，别让缓存继续显示上一个人的租约。
        self._board_cache = None
        return self.identity()

    def logout(self) -> dict:
        """断开：忘掉本机的设备身份，地址和姓名留着。

        删的是 worker.json（device_id + secret）。center.json 里那份配置不动，
        这样重连只要再点一次「连接」，不用把地址重新抄一遍。
        """
        with self._lock:
            if self.running:
                raise CenterError("正在跑，先停止再断开")
            try:
                self.worker_cfg.unlink(missing_ok=True)
            except OSError as exc:
                raise CenterError(f"删不掉身份文件：{self.worker_cfg}（{exc}）") from exc
            self._board_cache = None
        return self.snapshot()

    # ------------------------------------------------------------ 跑
    def build_cmd(self, cfg: dict | None = None) -> str:
        """worker 的 `--cmd`。占位符 {shard} {out} 由 worker 替换。

        引号是必须的：worker 用 `str.format` + `shell=True` 起子进程，路径里有空格
        （Windows 用户名很常见）不加引号就会断成两条命令。
        """
        cfg = cfg or self.config()
        if cfg.get("cmd_template"):
            return str(cfg["cmd_template"])
        extra = str(cfg.get("extra_args") or "").strip()
        return (
            f'"{sys.executable}" -m tagger.cli client '
            f'--shard "{{shard}}" --out-dir "{{out}}" '
            f'--url "{cfg["tag_url"]}" --mode {cfg["mode"]} '
            f'--progress-file "{{out}}/.fox-progress"'
            + (f" {extra}" if extra else "")
        )

    def start(self, **kw) -> dict:
        # 快照放在锁外：snapshot 会去问一次调度中心（连不上最长 6s），
        # 持锁做这件事会把收日志的线程一起堵住，日志全堆在管道里。
        with self._lock:
            if not self.running:
                cfg = self.save_config(**kw) if kw else self.config()
                ident = self.identity()
                if not ident.get("server"):
                    raise CenterError("还没连过：先填地址和姓名点「连接」")
                # 先把"想跑"这件事置上，拉起失败再撤回：否则 _spawn 抛错之后
                # 看门狗会以为这是一次崩溃，过几秒又去拉一个注定失败的进程。
                self._wanted = True
                self._restarts = 0
                self._next_spawn = 0.0
                try:
                    self._spawn(cfg)
                except CenterError:
                    self._wanted = False
                    raise
        return self.snapshot()

    def _spawn(self, cfg: dict) -> None:
        """按配置把一个 worker 子进程拉起来。**调用方必须已持有 self._lock**。

        抽出来是为了让看门狗复用：手动点和自动拉起走同一条路径，就不会出现
        "点开始能跑、自动拉起少传一个参数"这种只在半夜发作的差异。
        """
        mode = str(cfg.get("mode") or "")
        if mode not in MODES:
            # mode 是直接拼进命令行的，白名单是唯一的把关处。
            raise CenterError(f"产出模式只能是 {' / '.join(MODES)}，现在是 {mode!r}")
        try:
            Path(cfg["out_dir"]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 盘符不存在 / 没权限是最常见的操作错误，得说清是哪个目录。
            raise CenterError(f"产物目录建不出来：{cfg['out_dir']}（{exc}）") from exc
        argv = [sys.executable, "-m", "tagger.fox_worker", "run",
                "--out", cfg["out_dir"], "--cmd", self.build_cmd(cfg)]
        interval = _num(cfg.get("interval"))
        if interval > 0:
            argv += ["--interval", f"{interval:g}"]
        # 长驻等待：池子空了不退出，隔一会儿再问。页面上点一次「开始」就
        # 一直挂着等派工，而不是干完手上的就变成「已停止」。
        if cfg.get("wait"):
            argv.append("--wait")
        idle = _num(cfg.get("idle_timeout"))
        if idle > 0:
            argv += ["--idle-timeout", f"{idle:g}"]
        max_pkgs = int(_num(cfg.get("max_packages")))
        if max_pkgs > 0:
            argv += ["--max-packages", str(max_pkgs)]
        # 下载口径：worker 自己按这两个参数决定分段并发与 CDN 选优。
        # 不在命令行里传时 worker 有同样的默认值（4 连接 + 选优），这里传是因为
        # 页面允许改；clamp 在 worker 里也有一份，两边各兜一次。
        conns = int(_num(cfg.get("download_conns")))
        if conns >= 1:
            argv += ["--download-conns", str(max(1, min(16, conns)))]
        if not cfg.get("cdn_probe", True):
            argv.append("--no-cdn-probe")
        # 分项意愿（2026-11-12）。服务端认五值：both|tag|nl|pref-tag|pref-nl。
        # 两个概念必须分开，否则会申报一个"我做不出"的意愿：
        #   产出模式 mode  = **能力**，硬约束（client 按它决定跑哪套模型）
        #   派工倾向 pref  = **优先级**，软约束（只影响服务端排序）
        # 组合规则：
        #   mode=both → 直接采用 pref（"" = 不传，老口径"都做"）
        #   mode=tag  → 只能是 tag；pref 若指向 nl 那侧则**报错**，不静默改写
        #   mode=nl   → 只能是 nl；同上
        pref = str(cfg.get("parts_pref") or "").strip().lower()
        if pref not in PARTS_PREFS:
            raise CenterError(
                f"派工倾向只能是 {'/'.join(p or '不挑' for p in PARTS_PREFS)}，现在是 {pref!r}")
        if mode in ("tag", "nl"):
            # 与能力相反的一侧：接了也产不出内容，只会白下载 1.8G。
            # 页面会在 mode≠both 时禁掉这个下拉框，走到这里说明是手改了
            # center.json —— 报错比悄悄改成 mode 更好查。
            if pref and not pref.endswith(mode):
                raise CenterError(
                    f"产出模式是「只出 {mode}」，派工倾向却指向 {pref}；"
                    f"这样接到的片本侧产不出内容。把倾向改成「不挑」或 {mode}。")
            parts_arg = mode
        else:
            parts_arg = pref
        if parts_arg:
            argv += ["--parts", parts_arg]
        self._argv = argv
        self._push(f"[center] 启动：{' '.join(argv)}")
        # 独立进程组：停止时要连 worker 起的孙进程（真正在打标、写产物的那个）
        # 一起收，否则"已停止"只是杀掉了父进程，见 _kill_tree。
        extra = {} if os.name == "nt" else {"start_new_session": True}
        try:
            self._proc = subprocess.Popen(
                argv, cwd=str(ROOT), env=self._env(), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, **extra)
        except OSError as exc:
            self._proc = None
            raise CenterError(f"worker 起不来：{exc}") from exc
        self._started_at = time.time()
        self._spawn_at = time.time()
        self._last_rc = None
        threading.Thread(target=self._pump, args=(self._proc,),
                         name="center-log", daemon=True).start()

    def stop(self, report: bool = True) -> dict:
        """终止子进程。report=False 用在进程退出的路径上：那条路上没人看快照，
        而取快照要多等一次远端 HTTP。"""
        with self._lock:
            # 用户明确要停：先撤"想跑"，_pump 收尾时就不会把它当成崩溃再拉起来。
            self._wanted = False
            self._next_spawn = 0.0
            proc = self._proc
            if proc is not None:
                # 先把引用摘掉：收日志的线程随后收尾时不再认领它，退出码由这里记。
                self._proc = None
                self._push("[center] 停止：发 SIGTERM，worker 会归还手上的租约")
        if proc is not None:
            # 终止过程**不持锁**：wait 期间收日志的线程要继续读管道，否则子进程
            # 输出一多就会写满管道缓冲区，双双卡到超时。
            rc = self._terminate(proc)
            with self._lock:
                self._last_rc = rc
                # 租约已经还回去了，别让看板继续显示上一轮的派工。
                self._board_cache = None
        return self.snapshot() if report else {}

    def _terminate(self, proc: subprocess.Popen) -> int | None:
        """收掉 worker **和它的整棵进程树**。

        为什么不能只 terminate 直接子进程：worker 用 `shell=True` 起 `--cmd`，
        真正在打标、往产物目录写文件的是**孙进程**。杀掉 worker 之后孙进程
        还活着，页面上写着"已停止"、盘上却在继续长文件——两边都是实话，
        只是看的不是同一个东西。所以这里按进程组/进程树一起收。
        """
        _kill_tree(proc.pid)
        try:
            return proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._push("[center] 15s 没退出，强杀（包会在 TTL 到期后自动回池）")
            proc.kill()
            try:
                # kill 之后必须再 wait：否则句柄挂着（Windows 上尤其），
                # 而且这一轮的退出码就没人记了。
                return proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return None

    def _pump(self, proc: subprocess.Popen) -> None:
        """收 stdout。页面靠它看进度，别在这里做任何会抛的事。"""
        try:
            for line in proc.stdout or []:
                self._push(line.rstrip("\n"))
        finally:
            # 用 wait() 而不是 poll()：管道读到头时进程往往还没被回收，
            # poll() 会返回 None —— 退出码就丢了，页面「上次退出码」永远空白，
            # worker 崩了也看不出来。
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rc = proc.poll()
            with self._lock:
                if self._proc is proc:
                    self._last_rc = rc
                    self._proc = None
                    self._push(f"[center] 进程结束，退出码 {rc}")
                    self._schedule_restart(rc)

    def _schedule_restart(self, rc: int | None) -> None:
        """子进程结束了：判断要不要自动拉起，并排下一次的时间。**调用方持锁**。

        三种情况按原样停：
          * 用户点过停止（_wanted 已撤）；
          * 配置里关了看门狗；
          * 退出码属于"重启也救不了"的那几类（配置错、认证失效、连续失败停手）。
        其余（崩溃、被系统杀掉、网络异常退出）一律按退避重拉，退避 3→60s。
        """
        if not self._wanted:
            return
        if not self.config().get("watchdog", True):
            return
        if rc in FATAL_RC:
            self._wanted = False
            self._push(f"[center] 看门狗：退出码 {rc} 需要人工处理，不再自动拉起")
            return
        # 上一个进程跑够久就说明它是健康的，退避归零——偶发崩溃不该越滚越慢。
        if self._spawn_at and time.time() - self._spawn_at > HEALTHY_SECONDS:
            self._restarts = 0
        delay = min(RESTART_MAX, RESTART_BASE * (2 ** min(self._restarts, 5)))
        self._restarts += 1
        self._next_spawn = time.time() + delay
        self._push(f"[center] 看门狗：{delay:.0f}s 后自动拉起（第 {self._restarts} 次）")

    def _watchdog(self) -> None:
        """后台每秒看一眼：该在跑却不在跑、且已到点，就重新拉起。

        只在"用户希望它在跑"时才动手，所以点过停止的机器不会被反复拉起
        （那会变成怎么点都停不下来的怪物）。
        """
        while True:
            time.sleep(WATCHDOG_TICK)
            with self._lock:
                if not self._wanted or not self._next_spawn:
                    continue
                if self.running or time.time() < self._next_spawn:
                    continue
                self._next_spawn = 0.0
                try:
                    self._spawn(self.config())
                except CenterError as exc:
                    # 拉不起来（盘没了、配置被改坏）：别一秒一次刷屏，半分钟后再试
                    self._push(f"[center] 看门狗：拉不起来（{exc}），30s 后再试")
                    self._next_spawn = time.time() + 30.0

    def _push(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self._log.append(f"[{stamp}] {line}" if not line.startswith("[") else line)

    # ------------------------------------------------------------ 状态
    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def brief(self) -> dict:
        """顶栏那盏灯：**此刻能不能连上调度中心**，不是"以前登上过没有"。

        linked 只看本地身份文件（存过就一直为真，总机关了也一样），所以灯不能
        用它——那样总机停了灯还绿着，等于没提示。online 才是灯要的那个答案。

        超时压到 2.5s：这个请求每 8 秒来一发，等 6 秒会把顶栏的"没连上"延后
        好一会儿才显示出来，而人在等着看灯。
        """
        ident = self.identity()
        linked = bool(ident.get("server") and ident.get("user_id")
                      and ident.get("device_id"))
        online = False
        reason = ""
        if linked:
            got = self._status_call(timeout=2.5)
            if got is None:
                online, reason = False, "连不上调度中心：检查网络或总机是否在跑"
            elif got[0] == 401:
                online, reason = False, "凭据失效：让管理员重签 secret"
            elif got[0] != 200:
                online, reason = False, f"调度中心返回 HTTP {got[0]}"
            else:
                online = True
        return {
            "linked": linked,
            "online": online,
            "running": self.running,
            "user_id": ident.get("user_id") or "",
            "reason": reason,
        }

    def board(self) -> dict:
        """只读看板：直接问调度中心要这台设备的租约。**不触发派工**。

        3 秒内复用上一次结果：这个函数被每个接口的返回值带着走，而它要打一次
        远端 HTTP。缓存只是"别让点按钮时陪等等"，永远不会用来做决定。
        """
        cached = self._board_cache
        if cached and time.time() - cached[0] < 3.0:
            return cached[1]
        data = self._board_fetch()
        self._board_cache = (time.time(), data)
        return data

    def _status_call(self, timeout: float) -> tuple[int, dict] | None:
        """拿本机凭据打一次只读的 `/api/v1/status`。

        返回 (状态码, 内容)；连都没连上（网络层）返回 None。
        没登录过也返回 None —— 调用方自己用 identity 区分"没配"与"连不上"。
        """
        ident = self.identity()
        server = ident.get("server") or self.config().get("server")
        if not (server and ident.get("user_id") and ident.get("device_id")):
            return None
        try:
            body = json.loads(self.worker_cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            body = {}
        try:
            resp = httpx.post(f"{server.rstrip('/')}/api/v1/status",
                              json={"user_id": body.get("user_id"),
                                    "device_id": body.get("device_id"),
                                    "secret": body.get("secret")},
                              timeout=timeout)
        except httpx.HTTPError:
            return None
        return resp.status_code, (resp.json() if resp.content else {})

    def _board_fetch(self) -> dict:
        ident = self.identity()
        if not (ident.get("server") and ident.get("user_id") and ident.get("device_id")):
            return {"ok": False, "reason": "还没连接"}
        got = self._status_call(timeout=6.0)
        if got is None:
            return {"ok": False, "reason": "连不上调度中心：检查网络或总机是否在跑"}
        code, data = got
        if code == 401:
            return {"ok": False, "reason": "凭据失效：让管理员重签 secret，再填一次"}
        if code != 200:
            return {"ok": False, "reason": f"HTTP {code}"}
        data["ok"] = True
        # 池子余量（SPEC-parts §7）：新服务端在 status 里直接给了 parts；
        # 老服务端没有，就单独问一次 pool。两次任一失败都不影响看板本身——
        # 顶部那三个数字是"要不要去补"的参考，不能因为它连不上就把租约也藏了。
        if not isinstance(data.get("parts"), dict):
            data["parts"] = self._pool_fetch() or {}
        return data

    def _pool_fetch(self) -> dict | None:
        """问服务端「池子还剩多少」。不需要身份，也不需要包名。"""
        ident = self.identity()
        server = ident.get("server") or self.config().get("server")
        if not server:
            return None
        try:
            resp = httpx.post(f"{server.rstrip('/')}/api/v1/pool", json={}, timeout=6.0)
            if resp.status_code != 200:
                return None
            got = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        return got.get("parts") if isinstance(got, dict) else None

    def snapshot(self) -> dict:
        cfg = self.config()
        return {
            "running": self.running,
            "last_rc": self._last_rc,
            # 看门狗状态：页面拿它区分"被停了"和"崩了正在等重启"
            "watchdog": {
                "on": bool(cfg.get("watchdog", True)),
                "wanted": self._wanted,
                "restarts": self._restarts,
                "next_in": (round(max(0.0, self._next_spawn - time.time()))
                            if self._next_spawn else None),
            },
            "uptime_s": round(time.time() - self._started_at, 1) if self.running else 0,
            "config": {k: cfg[k] for k in ("server", "user_id", "tag_url", "mode",
                                           "out_dir", "interval", "extra_args",
                                           "download_conns", "cdn_probe")},
            "cmd": self.build_cmd(cfg),
            "identity": self.identity(),
            "board": self.board(),
            "log": list(self._log)[-120:],
        }

    def shutdown(self) -> None:
        try:
            self.stop(report=False)   # 退出路径上没人看快照，别再等一次远端
        except Exception:      # noqa: BLE001 - 退出路径上不许再抛
            pass


CENTER = Center()
