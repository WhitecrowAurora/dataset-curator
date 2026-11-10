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
    "mode": "both",                 # both / tag / nl
    "out_dir": str(ROOT / "work" / "fox-work"),
    "interval": 120,                # 心跳秒；0 = 用服务端建议值
    "extra_args": "",               # 追加给 tagger client 的参数（高级）
    "cmd_template": "",             # 非空则完全覆盖自动拼的命令（高级）
    # 长驻等待：池子空了不退出，隔一会儿再问。开着才像"挂在线上等人派活"；
    # 关着就是"把手上的干完就停"，页面上会直接从运行中变成已停止。
    "wait": True,
    "idle_timeout": 0,              # 配 wait：连续这么多秒没活就自己收工（0 = 一直等）
    "max_packages": 0,              # 本次最多交付多少片，够了就停（0 = 不限）
}

LOG_LINES = 400

# 这几个键清空没有意义（清掉只会让下一次启动必然失败），空串一律当"没填"。
_NON_EMPTY = {"server", "user_id", "tag_url", "out_dir", "mode"}

MODES = ("both", "tag", "nl")


def _num(val, default: float = 0.0) -> float:
    """配置里的数字可能被手改成垃圾值，转不动就当 0，别让点「开始」变成 500。"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class CenterError(RuntimeError):
    """消息直接给页面显示。"""


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
                self._push(f"[center] 启动：{' '.join(argv)}")
                try:
                    self._proc = subprocess.Popen(
                        argv, cwd=str(ROOT), env=self._env(), stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                        errors="replace", bufsize=1)
                except OSError as exc:
                    raise CenterError(f"worker 起不来：{exc}") from exc
                self._started_at = time.time()
                self._last_rc = None
                threading.Thread(target=self._pump, args=(self._proc,),
                                 name="center-log", daemon=True).start()
        return self.snapshot()

    def stop(self, report: bool = True) -> dict:
        """终止子进程。report=False 用在进程退出的路径上：那条路上没人看快照，
        而取快照要多等一次远端 HTTP。"""
        with self._lock:
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
        try:
            proc.terminate()
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
        return data

    def snapshot(self) -> dict:
        cfg = self.config()
        return {
            "running": self.running,
            "last_rc": self._last_rc,
            "uptime_s": round(time.time() - self._started_at, 1) if self.running else 0,
            "config": {k: cfg[k] for k in ("server", "user_id", "tag_url", "mode",
                                           "out_dir", "interval", "extra_args")},
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
