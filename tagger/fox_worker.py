#!/usr/bin/env python3
"""fox-label v3.3 客户端（申报制 + 自愈）：等派工、跑活、finish 申报、失败自动续跑。

和 v3.0 的区别（申报制）
-----------------------
租约没有 TTL 了：open 租约永久有效，只有两条关闭路径——worker 主动 finish
（done/fail 申报）或管理员在面板 release/unassign。因此：

  * 打标期间**零定时网络活动**：Beater（心跳续约线程）整体拆除，
    progress 不再周期上报；最终进度在 finish 时一次性带上。
  * 打标期间完全离线也能工作：下载（HF）之外不依赖服务端；
    最后 finish 申报时若网络不通，本地退避重试（1/2/4…上限 300s）
    直到成功——租约不会过期，重试总是安全的。
  * Ctrl-C / 崩溃后重启：interrupt 不再归还租约；重新 handshake 时
    服务端发现同设备已有 open 租约会原样返回（幂等续接），
    本地已校验通过的分片直接复用，不重复下载。

保留
----
IdlePinger（60s idle ping）：语义收窄为"我在，空闲"的面板三态展示信号，
不再是续约保活。404（旧服务端）静默降级逻辑不变。

身份
----
user_id 手填（打标同学自己填），device_id 首次运行自动生成并持久化。
secret 自 3.2 起**由本机自持**：本地没有就生成一把 `flw_...` 存在配置里，握手时
带给服务端登记。服务端不签发、不作废，只做两件事——登记、查重（一把 secret 只能
挂一个 user_id）。所以换机/重装不需要任何人转交密钥：管理员在面板「清空该用户身份」
（或 `server.py forget --user <名字>`）之后，直接重跑 login 即可。
（3.2 以前的老客户端本地没 secret 时仍由服务端签发一次，兼容保留。）

一次完整循环
------------
    handshake ──► active(+prelock)
       │
       ├── 下载分片（CDN 选优 + 多连接分段 + 断点续传 + sha256 校验）
       ├── 后台预取 prelock 那一片（趁跑活时下好，下一轮零等待）
       ├── 跑外部命令（tagger / bridge，由 --cmd 指定；期间完全离线）
       ├── 成功 → finish(done) 申报（带最终 progress）
       └── 失败 → finish(fail) 申报（带 reason 与最终 progress）

退出码
------
0 正常（含"池子空了"与 --once 完成）   2 用法/配置错误
3 认证失败（secret 冲突/被占用）        4 下载或校验失败
5 租约已关闭（管理员释放/已申报过，需重新握手） 6 任务命令失败（fail 已申报）
7 连续失败到上限已停手（需人工看一眼；长驻模式下 5/6 都不会退出，只有 7 会）
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

VERSION = "3.4.0"      # 3.4：CDN 选优 + 多连接分段下载 + 预取 prelock；3.3：本机重试 + 长驻不退；3.2：本机自持 secret
DEFAULT_SERVER = "http://127.0.0.1:31831"
CONFIG = Path(os.environ.get("FOXLABEL_WORKER_CONFIG")
              or Path.home() / ".config" / "fox-label" / "worker.json")

EXIT_OK, EXIT_USAGE, EXIT_AUTH, EXIT_DOWNLOAD, EXIT_LEASE, EXIT_TASK = 0, 2, 3, 4, 5, 6
# 7 = 连续失败到上限，主动停手。刻意和"任务失败"(6) 分开：6 现在不会再让长驻
# 会话退出（见 cmd_run），只有"连着挂 N 片"才停，而且这种状态重启也救不回来，
# 所以看门狗见到 7 就不再拉起，留给人看一眼。
EXIT_STUCK = 7

# 连续多少片失败就停手。偶发失败（显存被别的进程抢了、网络抖一下）很常见，
# 但同一台机器连着挂 5 片基本是环境坏了（模型路径错、盘满、驱动版本不对）。
MAX_CONSEC_FAILS = 5

# --wait 长驻时，池子空了隔多久再问一次。30s 是"人能接受的反应延迟"和
# "不把服务端当轮询靶子"之间的折中：派人的人一般不会在 10 秒内就急着看结果。
WAIT_POLL = 30

# 空转 ping（SPEC-declare §7.2，语义收窄）：手上没租约时每 60s 发一次
# /api/v1/ping，仅作"我在，空闲"的面板三态展示信号，**不是续约**——open
# 租约永久有效，无需保活。失败指数退避 1/2/4/8…上限 300s；成功后立即补发
# 一次并恢复 60s 周期。
IDLE_PING_INTERVAL = 60
IDLE_PING_BACKOFF_MAX = 300

# finish 申报重试（SPEC-declare §7.2）：申报制下 finish 是租约唯一的客户端
# 关闭路径，租约又不会过期，所以网络不通时本地退避重试直到成功是安全的。
FINISH_RETRY_MAX = 300.0

# 预取线程收尾时的等待上限（秒）。正常情况下它在这片活跑完之前早就下完了；
# 任务秒挂/被 Ctrl-C 时才会走到"等下不来就掐掉"，掐掉留 .part 给下一轮续传。
PREFETCH_JOIN_WAIT = 5.0
PREFETCH_STOP_WAIT = 30.0

# 下载：多连接 + CDN 选优（v3.4）。默认按**直连**口径拉满——国内到 HF 的
# 单连接常被长肥管道掐死在几百 KB/s，而 HF 的 CDN 对多连接是友好的
# （hf_transfer 就是 8~16 连接）。走代理时 --resolve 会被代理忽略（不是失败），
# 所以同一套默认值在挂梯子的机器上退化成普通下载，不会更糟。
DEFAULT_CONNS = 4           # 每片并发连接数；1 = 老的单流
SEG_MIN_BYTES = 8 * 1024 * 1024   # 每段至少这么大，太小的片不值得切
CDN_PROBE_BYTES = 2 * 1024 * 1024  # 每个候选 IP 只拉 2MiB 测速
CDN_PROBE_TTL = 600.0       # 选优结果复用 10 分钟（同一 out 目录）
CDN_PROBE_MAX_IPS = 6
CDN_PROBE_TIMEOUT = 8.0
CDN_CACHE = ".fox-cdn-pick.json"

DL_CONNS = DEFAULT_CONNS    # cmd_run 按 --download-conns 覆盖
DL_CDN_PROBE = True         # --no-cdn-probe 关闭
_RANGE_UNSUPPORTED = False  # 碰到不认 Range 的源就永久退回单流（本进程内）


def die(code: int, msg: str) -> None:
    print(f"[X] {msg}", file=sys.stderr)
    raise SystemExit(code)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ 配置

def load_cfg() -> dict:
    if not CONFIG.exists():
        die(EXIT_USAGE, f"还没有登录：{CONFIG} 不存在（先跑 worker.py login --server URL --user-id 名字）")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if not cfg.get("user_id"):
        die(EXIT_USAGE, "配置缺少 user_id，请重新 login")
    return cfg


def save_cfg(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG)


def new_local_secret() -> str:
    """本机自己生成的 secret（服务端只登记、不签发）。

    "secret 是我的机器是我的"的凭据，放谁手里该由机器决定：本机生成一次就存下来，
    服务端只是把你的名字和它绑定。丢了/换机都由管理员 `forget --user` 放掉旧绑定，
    下次握手按新机器上的这份重新登记——不需要谁在微信里传密钥。
    """
    return "flw_" + secrets.token_urlsafe(32)


def config_writable() -> str | None:
    """握手前先验一次"配置目录写不写得进去"。返回 None 表示可写。

    这一步必须放在 handshake 之前：首次握手服务端会登记用户并**只回一次**
    secret，本地写失败就等于把一个谁都不知道的 secret 锁在服务端——客户端
    之后每次握手都是 401「secret 不匹配」，只能等管理员 reset-secret。
    """
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        probe = CONFIG.parent / ".fox-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return f"{CONFIG.parent} 不可写（{e}）"
    return None


def save_cfg_after_handshake(cfg: dict, new_secret: bool) -> None:
    """握手成功后落盘。失败时绝不能只抛 traceback：新签发的 secret 只在内存里，
    进程一退就永久丢失。所以把 secret 明着打给用户/管理员抄走。"""
    try:
        save_cfg(cfg)
    except OSError as e:
        if new_secret and cfg.get("secret"):
            die(EXIT_USAGE,
                f"配置写不下去：{e}\n"
                f"    服务端已登记用户并签发 secret，它只出现这一次，请立刻抄下来：\n"
                f"    secret={cfg['secret']}\n"
                f"    修好 {CONFIG.parent} 的权限后重新执行：\n"
                f"    worker.py login --server {cfg['server']} --user-id {cfg['user_id']} "
                f"--secret {cfg['secret']}")
        die(EXIT_USAGE, f"配置写不下去：{CONFIG}（{e}）")


def save_cfg_soft(cfg: dict) -> None:
    """运行期的次要落盘（have 列表等）：写不下去只告警，不能中断打标。"""
    try:
        save_cfg(cfg)
    except OSError as e:
        log(f"[warn] 配置保存失败（不影响本次交付）：{CONFIG}（{e}）")


# ------------------------------------------------------------------ HTTP

class NetError(Exception):
    pass


def api(cfg: dict, path: str, body: dict, timeout: float = 15, retries: int = 2) -> dict:
    url = cfg["server"].rstrip("/") + path
    payload = dict(body)
    payload.setdefault("user_id", cfg.get("user_id"))
    payload.setdefault("device_id", cfg.get("device_id"))
    if cfg.get("secret"):
        payload.setdefault("secret", cfg["secret"])
    data = json.dumps(payload, ensure_ascii=False).encode()
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode(errors="replace")
            if e.code == 401:
                raise AuthError(_err(body_txt) or "secret 不匹配")
            if e.code in (502, 503, 504) and attempt < retries:
                last = ApiError(e.code, _err(body_txt) or f"HTTP {e.code}")
                time.sleep(1.5 * (attempt + 1))
                continue
            err = ApiError(e.code, _err(body_txt) or f"HTTP {e.code}")
            # 限流响应（SPEC-link §2.2）带 retry_after 秒数：客户端按它退避
            try:
                err.retry_after = float(json.loads(body_txt).get("retry_after") or 0)
            except Exception:
                err.retry_after = 0.0
            raise err
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise NetError(str(last))


def _err(text: str) -> str:
    try:
        return json.loads(text).get("error") or ""
    except Exception:
        return text[:120]


class ApiError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code, self.msg = code, msg
        self.retry_after = 0.0        # 429 时由 api() 填（服务端建议退避秒数）


class AuthError(Exception):
    pass


# ------------------------------------------------------------------ 硬件上报
# SPEC-link §1.1：handshake 追加可选 hw 对象（只增）。取数失败/无 GPU 不阻塞
# 握手——整个 hw 为 null，服务端落 NULL 列。

def collect_hw(cfg: dict) -> dict | None:
    """收集 {gpu_name, gpu_vram_mb, mode}；任何失败都降级为 None。

    gpu 取 tagger/hardware.visible_gpus() 主卡（GPUInfo.name/vram_gb）——
    必须是**可见**卡：worker 常被 CUDA_VISIBLE_DEVICES 限定在一张卡上，
    按物理 0 号上报会让服务端拿错的显存排工。
    vram 以 MB 计（服务端 0~1e6 整数）。mode 取当前 --tiers/--tier 配置名
    （FOXLABEL_TIERS / FOXLABEL_TIER 环境变量或 cfg["mode"]，最长 32 字符）。
    tagger 不在本机装包环境里时（独立 worker 部署）退化为 null GPU 字段，
    但 mode 照报——mode 来自配置，不依赖硬件。
    """
    gpu_name = gpu_vram_mb = None
    try:
        from tagger.hardware import visible_gpus   # noqa: 延迟导入，缺包不致命
        gpus = visible_gpus()
        if gpus:
            g0 = gpus[0]
            name = (getattr(g0, "name", "") or "").strip()[:64] or None
            vram = getattr(g0, "vram_gb", None)
            if vram is not None:
                mb = int(round(float(vram) * 1024.0))
                if 0 <= mb <= 1_000_000:
                    gpu_vram_mb = mb
            gpu_name = name
    except Exception as e:
        log(f"[warn] 硬件信息收集失败（GPU 字段按 null 处理，不阻塞）："
            f"{type(e).__name__}: {e}")
    mode = os.environ.get("FOXLABEL_TIERS") or os.environ.get("FOXLABEL_TIER") \
        or cfg.get("mode") or None
    if mode:
        mode = str(mode).strip()[:32] or None
    # 无 GPU（字段全 null）也照报 hw 对象：明确告诉服务端"这台机器没有卡"，
    # 只有连 mode 都没有的极端部署才整包降级为不报 hw。
    return {"gpu_name": gpu_name, "gpu_vram_mb": gpu_vram_mb, "mode": mode}


def sanitize_hw(hw: dict | None) -> dict | None:
    """客户端侧 hw 预清洗：与服务端 validate_hw 同规则，非法字段降为 null。

    服务端（dispatch.validate_hw / SPEC §1.1）对超限字段整包 400 拒绝；
    客户端自己先清洗，保证上报永远不会因 hw 触发握手失败。
    """
    if not hw:
        return hw
    out = dict(hw)
    name = out.get("gpu_name")
    if isinstance(name, str) and len(name) > 64:
        out["gpu_name"] = None
    vram = out.get("gpu_vram_mb")
    if vram is not None and (not isinstance(vram, int) or isinstance(vram, bool)
                             or not (0 <= vram <= 1_000_000)):
        out["gpu_vram_mb"] = None
    mode = out.get("mode")
    if isinstance(mode, str) and len(mode) > 32:
        out["mode"] = None
    return out


# ------------------------------------------------------------------ idle ping
# SPEC-declare §7.2：语义收窄——不再是"续约保活"，仅是"我在，空闲"的面板
# 三态展示信号（open 租约永久有效，无需保活）。404（旧服务端）每会话至多
# 告警一次，之后静默停发；401 走既有 AuthError 停机路径；网络失败指数退避，
# 成功后立即补发一次并恢复 60s 周期。daemon 线程，随主进程退出，不悬挂。

class IdlePinger(threading.Thread):
    def __init__(self, cfg: dict, interval: float = IDLE_PING_INTERVAL,
                 stop: threading.Event | None = None):
        super().__init__(daemon=True, name="idle-pinger")
        self.cfg = cfg
        self.interval = max(1.0, interval)
        self.stop = stop or threading.Event()
        self.backoff = 1.0
        self.auth_error: AuthError | None = None

    def run(self) -> None:
        warned_404 = False
        wait = 0.0                   # 0 = 首拍立即发（handshake 刚做完，60s 后再发太晚）
        failed = False
        while not self.stop.is_set():
            if wait > 0 and self.stop.wait(wait):
                return
            try:
                api(self.cfg, "/api/v1/ping", {}, retries=0)
                if failed:
                    # 退避期恢复：立即补发一次（消除退避期的观测空洞）
                    failed = False
                    self.backoff = 1.0
                    try:
                        api(self.cfg, "/api/v1/ping", {}, retries=0)
                    except (ApiError, NetError, AuthError):
                        pass          # 补发失败不打紧，下一周期照常
                wait = self.interval  # 成功：回到 60s 周期
            except AuthError as e:
                # secret 失效：走停机路径（main 循环读 stop+auth_error）
                log(f"[X] 认证失效：{e}")
                self.auth_error = e
                self.stop.set()
                return
            except ApiError as e:
                if e.code == 404:
                    if not warned_404:
                        warned_404 = True
                        log("[warn] 服务端版本较旧，空闲 ping 不可用（不影响打标）")
                    self.stop.set()   # 静默降级：之后彻底停发（连重试都不再发）
                    return
                failed = True
                if e.code == 429 and e.retry_after > 0:
                    # 限流：按服务端建议的 retry_after 退避（上限仍是 300s）
                    wait = min(max(e.retry_after, 1.0), IDLE_PING_BACKOFF_MAX)
                    self.backoff = min(max(self.backoff * 2, wait),
                                       IDLE_PING_BACKOFF_MAX)
                else:
                    wait = self.backoff
                    self.backoff = min(self.backoff * 2, IDLE_PING_BACKOFF_MAX)
            except NetError:
                # 网络不可达：指数退避 2/4/8…上限 300s，成功后立即补发并回 60s
                failed = True
                wait = self.backoff
                self.backoff = min(self.backoff * 2, IDLE_PING_BACKOFF_MAX)


# ------------------------------------------------------------------ 登录

def cmd_login(args) -> int:
    cfg = {"server": args.server.rstrip("/"), "user_id": args.user_id}
    if CONFIG.exists():
        old = json.loads(CONFIG.read_text(encoding="utf-8"))
        # 本地已有的 secret 一律先沿用，**不看 server / user_id 是否还对上**：
        # 改了地址、改了名字、地址末尾多个斜杠都不该让一台机器长出第二把钥匙
        # （换一把新的，服务端那边就可能不认了）。要换钥匙只有两个入口：显式
        # --secret 或 --new-secret。
        if old.get("secret") and not cfg.get("secret"):
            cfg["secret"] = old["secret"]
        if old.get("server", "").rstrip("/") == cfg["server"] and old.get("device_id"):
            cfg["device_id"] = old["device_id"]
    if getattr(args, "secret", None):
        cfg["secret"] = args.secret.strip()
    force_new = bool(getattr(args, "new_secret", False))
    if force_new:
        cfg.pop("secret", None)
    # 本机自持 secret：没有就当场生成一份，握手时带上让服务端登记
    minted = not cfg.get("secret")
    if minted:
        cfg["secret"] = new_local_secret()
    cfg.setdefault("device_id", uuid.uuid4().hex[:12])
    # 先验盘再握手：写不下去就不要让服务端登记这个用户（否则 secret 只回一次，
    # 丢了就锁死，见 config_writable 的注释）
    why = config_writable()
    if why:
        die(EXIT_USAGE,
            f"配置目录写不进去：{why}\n"
            f"    位置 {CONFIG}（可用环境变量 FOXLABEL_WORKER_CONFIG 换一个路径）\n"
            f"    这一次握手已经取消，服务端还没有登记 {cfg['user_id']}；"
            f"修好目录权限后重跑同一条命令即可。")
    try:
        res = api(cfg, "/api/v1/handshake", {"client_ver": VERSION}, retries=0)
    except AuthError as e:
        die(EXIT_AUTH, f"认证失败：{e}\n"
                       f"    secret 以本机为准，服务端只登记与查重（本机配置：{CONFIG}）。\n"
                       f"    ① 这个 user_id 已被别的机器占着 / 你想换机器：让管理员在面板点"
                       f"「清空该用户身份」（或 server.py forget --user {cfg['user_id']}），"
                       f"然后原样重跑本命令即可。\n"
                       f"    ② 提示「secret 已被用户 X 用了」：你的配置是从别人机器拷来的，"
                       f"删掉 {CONFIG} 再跑一次，本机会换一把新的。\n"
                       f"    ③ 名字打错了：换一个。")
    except (ApiError, NetError) as e:
        die(EXIT_USAGE, f"连不上服务端 {cfg['server']}：{e}")
    issued = res.get("secret")
    if issued and minted:
        # 极老的客户端协议：本地没有 secret 时由服务端签发一次，只能收下
        cfg["secret"] = issued
    elif issued and issued != cfg["secret"]:
        # 本地本来就有：**以本地为准，不覆写**。服务端会按 unique 口径采纳本地
        # 这把；万一对面还是老服务端，这里会留一条痕迹好排查。
        print(f"[注意] 服务端回了另一把 secret，本机保留原有的不覆写（{CONFIG}）")
    cfg["device_id"] = res.get("device_id") or cfg["device_id"]
    cfg["server_id"] = res.get("server_id")
    save_cfg_after_handshake(cfg, minted or bool(issued))
    print(f"已保存配置：{CONFIG}")
    print(f"服务端 : {cfg['server']}（server_id={cfg.get('server_id')}）")
    print(f"身份   : {cfg['user_id']} / 设备 {cfg['device_id']}")
    if issued and minted:
        print("密钥   : 服务端签发并保存（600）")
    elif force_new:
        print("密钥   : 已换成本机新生成的一把（旧的自此不用）")
    else:
        print("密钥   : 沿用本机原有的一把，已保存（600）")
    leases = res.get("leases") or []
    print(f"派工   : {len(leases)} 个" + (f"（{leases[0]['kind']} {leases[0]['package']}）"
                                         if leases else "（当前池子空）"))
    # 握手会顺带派工（服务端 assign 无条件执行），但 login 只是登记身份、不打算
    # 开工。不还回去的话这一片要闲置到 TTL 到期，这期间别的设备领不到它——
    # 表现就是"有人点了连接但没开炼，池子却少了一片"。
    if leases:
        release_unused(cfg, leases)
        print(f"（已归还刚派来的 {len(leases)} 片：只登记身份，不开工）")
    return EXIT_OK


def cmd_show(args) -> int:
    cfg = load_cfg()
    print(f"配置文件: {CONFIG}")
    print(f"服务端  : {cfg['server']}  server_id={cfg.get('server_id', '-')}")
    print(f"身份    : {cfg['user_id']} / 设备 {cfg.get('device_id')}")
    have = cfg.get("have") or []
    print(f"本地已有产物的包: {len(have)} 个" + (f"（{', '.join(have[:3])}…）" if have else ""))
    try:
        res = api(cfg, "/api/v1/status", {}, retries=0)
    except (ApiError, NetError, AuthError) as e:
        print(f"在线状态: 无法连接（{e}）")
        return EXIT_OK
    print(f"在线状态: 正常（{res.get('status', '?')}，档位 {res.get('tier', '?')}），"
          f"当前派工 {len(res.get('leases') or [])} 个")
    for l in res.get("leases") or []:
        print(f"  - {l['kind']:7s} {l['repo']}/{l['package']}  剩 {l['expires_in']}s")
    return EXIT_OK


# ------------------------------------------------------------------ 下载

def hf_endpoint() -> str:
    return os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")


def hf_token() -> str | None:
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if tok:
        return tok.strip()
    p = Path.home() / ".cache" / "huggingface" / "token"
    if p.exists():
        return p.read_text(encoding="utf-8").strip() or None
    return None


def hf_url(lease: dict) -> str:
    kind = "datasets" if (lease.get("repo_type") or "dataset") == "dataset" else "models"
    return f"{hf_endpoint()}/{kind}/{lease['repo']}/resolve/main/{lease['package']}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# 每片的下载旁证（只有下载耗时与是否命中预取），finish 时一次性上报。
# 连接数/出口 IP/协议族刻意不记：这些不该离开本机。
# 按 lease_id 存：预取线程写的是它自己那一片，不会与 active 串味。
_DL_STATS: dict[str, dict] = {}


def _note_dl(lease: dict, stats: dict) -> None:
    key = lease.get("lease_id") or ""
    _DL_STATS[key] = stats
    while len(_DL_STATS) > 64:              # 长驻进程别无限长
        _DL_STATS.pop(next(iter(_DL_STATS)), None)


def _dl_stats(lease: dict) -> dict:
    return dict(_DL_STATS.get(lease.get("lease_id") or "") or {})


def classify_fail(detail: str) -> str:
    """把失败描述归成服务端认识的枚举（net/data/disk/cmd/oom/other）。

    服务端要用它做统计（"这批里多少是坏数据、多少是显存炸了"），自由文本
    统计不了；但原文仍然照发，人看的还是那句具体的。
    """
    text = str(detail or "")
    low = text.lower()
    if "sha256 不符" in text:
        return "data"
    if "下载失败" in text:
        return "net"
    if "no space left" in low or "errno 28" in low or "磁盘空间" in text:
        return "disk"
    if "out of memory" in low or "oom" in low:
        return "oom"
    if "任务命令失败" in text:
        return "cmd"
    return "other"


def download(lease: dict, out_dir: Path, state: "RunState", cfg: dict) -> Path:
    """下载分片：CDN 选优 + 多连接分段 + 断点续传 + 校验。

    v3.4 起不再是"一个 curl 一条流到底"：
      1. 每片开工前先解析出真实 CDN 地址，从它的 A 记录里挑最快的边缘 IP
         （`cdn_pick`，结果按 out 目录缓存 10 分钟）；
      2. 按 range 把这片切成 DL_CONNS 段并发下，每段各自续传；
      3. 全部到齐按顺序拼回单文件，再走服务端 sha256 兜底——拼错位一定能发现。

    钉住的 IP 若中途失效（边缘下线/签名过期），自动清缓存、退回 DNS 再跑一次。
    已存在且哈希正确的分片直接复用，不重复下载。
    """
    dest = out_dir / lease["package"]
    want = lease.get("sha256")
    if dest.exists() and dest.stat().st_size > 0:
        if not want or sha256_file(dest) == want:
            log(f"本地已有校验通过的分片，跳过下载：{dest}")
            # prefetch_hit：这一片是预取那次下好的（本轮没花下载时间）
            _note_dl(lease, {"download_s": 0.0, "prefetch_hit": 1})
            return dest
        log(f"本地分片校验失败，重新下载：{dest}")
        dest.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    hdr_file = _write_hdr(lease, out_dir)
    t0 = time.time()
    global _RANGE_UNSUPPORTED
    try:
        pick = cdn_pick(lease, out_dir, hdr_file) if DL_CDN_PROBE else None
        try:
            return _download_once(lease, dest, hdr_file, pick, state, t0)
        except RangeUnsupported as e:
            # 源不认 Range：分段这条路彻底走不通，本进程后续一律单流。
            _RANGE_UNSUPPORTED = True
            _drop_segments(dest)
            log(f"[warn] {e}")
            return _download_once(lease, dest, hdr_file, pick, state, t0)
        except DownloadError:
            if not pick:
                raise
            # 钉的出口挂了 / 签名过期：清缓存退回 DNS，重来一次
            # （段文件保留，续传接着下，不浪费已下好的部分）。
            _forget_cdn(out_dir)
            log("[warn] 钉住的 CDN 出口下载失败，清缓存回退 DNS 重试一次")
            return _download_once(lease, dest, hdr_file, None, state, t0)
    finally:
        if hdr_file:
            hdr_file.unlink(missing_ok=True)


def _write_hdr(lease: dict, out_dir: Path) -> Path | None:
    """HF token 走 header 文件（避免出现在进程命令行里被 ps 看到）。

    每个租约一个独立名字：预取线程和主线程会各跑一个 curl，共用同一个文件名
    会互相截断（同一 out 目录跑两台设备的同理）。
    """
    tok = hf_token()
    if not tok:
        return None
    hdr_file = out_dir / f".fox-hf-headers-{lease.get('lease_id') or os.getpid()}"
    fd = os.open(hdr_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, f"Authorization: Bearer {tok}\n".encode())
    os.close(fd)
    return hdr_file


def _download_once(lease: dict, dest: Path, hdr_file: Path | None,
                   pick: dict | None, state: "RunState", t0: float) -> Path:
    url = (pick or {}).get("url") or hf_url(lease)
    total = int(lease.get("size") or 0) or _remote_size(url, hdr_file)
    conns = 1
    if total > 0 and DL_CONNS > 1 and not _RANGE_UNSUPPORTED:
        conns = max(1, min(DL_CONNS, total // SEG_MIN_BYTES))
    part = dest.with_suffix(dest.suffix + ".part")
    state.phase = "download"
    state.done = 0
    log(f"下载 {lease['package']}（{total / 2**20:.0f} MiB，{conns} 连接"
        + (f"，出口 {_tag(pick['ip'])}" if (pick or {}).get("ip") else "") + "）…")
    if conns > 1:
        part.unlink(missing_ok=True)       # 单流遗留的半截文件不参与拼接
        _download_segments(url, dest, hdr_file, pick, total, conns, state)
    else:
        _download_stream(url, part, hdr_file, pick, total, state)
    if not part.exists():
        raise DownloadError("没有下载到任何数据")
    part.replace(dest)
    dt = time.time() - t0
    speed = f"（{total / 2**20 / dt:.1f} MiB/s）" if dt > 0 and total else ""
    log(f"下载完成 {dest.name}，用时 {dt:.1f}s{speed}")
    want = lease.get("sha256")
    if want:
        got = sha256_file(dest)
        if got != want:
            dest.unlink(missing_ok=True)
            _drop_segments(dest)
            raise DownloadError(f"sha256 不符：\n  服务端 {want}\n  下载后 {got}")
        log("sha256 校验通过")
    # 只报耗时。连接数/出口 IP/协议族一律不上报：出口 IP 能定位网络位置，
    # 属于不该离开本机的元数据（本地日志里照旧能看）。
    _note_dl(lease, {"download_s": round(dt, 1), "prefetch_hit": 0})
    return dest


def _download_stream(url: str, part: Path, hdr_file: Path | None,
                     pick: dict | None, total: int, state: "RunState") -> None:
    """单连接（DL_CONNS=1 或片太小）：老逻辑，断点续传靠 curl -C -。"""
    cmd = ["curl", "-sS", "-L", "--fail", "--retry", "3", "--retry-delay", "2",
           "-C", "-", "-o", str(part)]
    cmd += _resolve_args(pick)
    if hdr_file:
        cmd += ["-H", f"@{hdr_file}"]
    cmd.append(url)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    while proc.poll() is None:
        if state.stop.is_set():
            proc.terminate()
            break
        if part.exists() and total:
            state.done = min(99, int(part.stat().st_size / total * 100))
        time.sleep(0.3)
    rc = proc.wait()
    if rc != 0 or not part.exists():
        err = (proc.stderr.read().decode(errors="replace") if proc.stderr else "")[-200:]
        raise DownloadError(f"curl 退出码 {rc} {err}")


def _download_segments(url: str, dest: Path, hdr_file: Path | None,
                       pick: dict | None, total: int, conns: int,
                       state: "RunState") -> None:
    """把这片切成 conns 段并发下，各段独立续传，最后按序拼回 <pkg>.part。

    段文件用 `.<pkg>.segNNN`（隐藏名，不会跟单流的 .part 混）。每段续传靠
    "已有长度 + curl -r 绝对区间"，不用 -C -（-C - 自己算区间，和 -r 语义冲突）。
    """
    seg_len = -(-total // conns)
    bounds = [(s, min(s + seg_len, total) - 1) for s in range(0, total, seg_len)]
    paths = [dest.parent / f".{dest.name}.seg{i:03d}" for i in range(len(bounds))]
    procs: list[subprocess.Popen | None] = [None] * len(bounds)
    errors: list[Exception | None] = [None] * len(bounds)
    stop = threading.Event()

    def worker(i: int, start: int, end: int, path: Path) -> None:
        want_len = end - start + 1
        have = path.stat().st_size if path.exists() else 0
        if have > want_len:                 # 上次的残渣越界了，重来
            path.unlink(missing_ok=True)
            have = 0
        if have == want_len:                # 这段已经齐了
            return
        cmd = ["curl", "-sS", "-L", "--fail", "--retry", "3", "--retry-delay", "2",
               "-r", f"{start + have}-{end}"]
        cmd += _resolve_args(pick)
        if hdr_file:
            cmd += ["-H", f"@{hdr_file}"]
        cmd.append(url)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            errors[i] = DownloadError("找不到 curl")
            stop.set()
            return
        procs[i] = proc
        try:
            written = have
            with path.open("ab") as fh:
                while True:
                    chunk = proc.stdout.read1(1 << 20)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > want_len:
                        # 服务端根本没理 Range，把整个文件吐给每段了——不掐掉
                        # 的话 4 段就能把盘写满。立刻停，让上层退回单流。
                        proc.terminate()
                        errors[i] = RangeUnsupported(
                            "服务端忽略 Range（返回了整文件），退回单流下载")
                        stop.set()
                        return
                    fh.write(chunk)
                    if state.stop.is_set() or stop.is_set():
                        break
            stopped = state.stop.is_set() or stop.is_set()
            if stopped:
                # 别人先停了（停止信号/别的段出错）：这里必须先把 curl 掐掉再
                # wait，否则它正往没人读的管道里写，wait 会永远等不到。
                proc.terminate()
            rc = proc.wait()
        except Exception as e:              # noqa: BLE001 —— 任何异常都算这段失败
            proc.kill()
            errors[i] = e
            stop.set()
            return
        if stopped:
            errors[i] = DownloadError("下载已中断")
            return
        if rc != 0:
            err = (proc.stderr.read().decode(errors="replace") if proc.stderr else "")[-200:]
            errors[i] = DownloadError(f"curl 退出码 {rc} {err}")
            stop.set()

    threads = [threading.Thread(target=worker, args=(i, s, e, paths[i]),
                                daemon=True, name=f"seg{i}")
               for i, (s, e) in enumerate(bounds)]
    for t in threads:
        t.start()
    killed = False
    while any(t.is_alive() for t in threads):
        if not killed and (state.stop.is_set() or stop.is_set()):
            # stop 可能是某段自己发现出错设的（Range 不支持/curl 失败）：
            # 同样要把它和其余段的 curl 一起掐掉，不然它们卡在阻塞写里。
            killed = True
            for p in procs:
                if p is not None and p.poll() is None:
                    p.terminate()
        got = sum(p.stat().st_size for p in paths if p.exists())
        state.done = min(99, int(got / total * 100))
        time.sleep(0.3)
    for t in threads:
        t.join()

    errs = [e for e in errors if e is not None]
    # RangeUnsupported 优先抛出：它是"这条路根本走不通"的信号，别的段大概率
    # 只是被连带中断（"下载已中断"），拿后者当结论会让上层白白重试一遍。
    err = next((e for e in errs if isinstance(e, RangeUnsupported)),
               errs[0] if errs else None)
    if err is not None:
        raise err
    part = dest.with_suffix(dest.suffix + ".part")
    with part.open("wb") as out:
        for path in paths:
            with path.open("rb") as fh:
                shutil.copyfileobj(fh, out, 1 << 20)
    size = part.stat().st_size
    if size != total:
        part.unlink(missing_ok=True)
        _drop_segments(dest)
        raise DownloadError(f"拼接后大小不符：期望 {total}，实得 {size}")
    _drop_segments(dest)


def _drop_segments(dest: Path) -> None:
    for p in dest.parent.glob(f".{dest.name}.seg*"):
        p.unlink(missing_ok=True)


def _bracket(ip: str) -> str:
    """curl 的 --resolve 里 IPv6 地址必须带方括号，否则它按冒号切不开。"""
    return f"[{ip}]" if ":" in ip else ip


def _resolve_args(pick: dict | None) -> list[str]:
    """把某台设备当前选的 CDN 边缘钉死给 curl（保留 SNI，证书照常校验）。"""
    if not (pick or {}).get("ip"):
        return []
    return ["--resolve", f"{pick['host']}:{pick['port']}:{_bracket(pick['ip'])}"]


# ---------------------------------------------------------------- CDN 选优

def cdn_pick(lease: dict, out_dir: Path, hdr_file: Path | None) -> dict | None:
    """每片开工前探一次：解析出真实 CDN 地址，从它的 A/AAAA 里挑最快的边缘。

    为什么值得探：HF 的 resolve 会 302 到具体 CDN 域名，而一个域名常有多个
    A/AAAA 记录，DNS 给的那个未必是到你这台机器最快的（跨境链路上差异极大，
    而且现在 v6 常常比 v4 快）。只拉 2MiB 就能分出高下，代价几秒，换来整片
    （十几个 G）的走线。

    直连口径（默认）：`--resolve` 只影响 curl 自己的 DNS；走代理时被代理忽略，
    所以挂梯子的机器不会因此失败，只是拿不到这部分收益。
    """
    final = _final_url(hf_url(lease), hdr_file)
    parts = urllib.parse.urlsplit(final) if final else None
    if not parts or not parts.hostname:
        return None
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    cached = _read_cdn_cache(out_dir, host)
    if cached:
        return {**cached, "url": final}
    ips = _edge_ips(host, port)
    if len(ips) < 2:                       # 只有一个候选，没必要花那 2MiB
        return {"host": host, "port": port, "ip": "", "url": final}
    scored = [(_probe_ip(final, host, port, ip), ip)
              for ip in _candidates(ips, CDN_PROBE_MAX_IPS)]
    scored.sort(reverse=True)
    speed, best = scored[0]
    if speed <= 0:
        log(f"[warn] CDN 测速全部失败（{host}），按 DNS 默认走")
        return {"host": host, "port": port, "ip": "", "url": final}
    pick = {"host": host, "port": port, "ip": best, "at": time.time(), "speed": speed}
    _write_cdn_cache(out_dir, pick)
    rest = "、".join(f"{_tag(ip)} {sp / 2**20:.1f}MB/s" for sp, ip in scored[1:]) or "无"
    log(f"CDN 选优 {host} → {_tag(best)}（{speed / 2**20:.1f}MB/s；其余 {rest}）")
    return {**pick, "url": final}


def _tag(ip: str) -> str:
    return f"[v6] {_bracket(ip)}" if ":" in ip else f"[v4] {ip}"


def _final_url(url: str, hdr_file: Path | None) -> str:
    """跟随 302 拿到最终 CDN 地址（这才是该钉 IP 的那个 host）。"""
    for head in (True, False):
        cmd = ["curl", "-sS", "-L", "-o", os.devnull, "-w", "%{url_effective}",
               "--max-time", "20"]
        cmd += ["-I"] if head else ["-r", "0-0"]
        if hdr_file:
            cmd += ["-H", f"@{hdr_file}"]
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ""
        if r.returncode == 0:
            out = r.stdout.decode(errors="replace").strip()
            if out.startswith("http"):
                return out
    return ""


def _edge_ips(host: str, port: int) -> list[str]:
    """A 和 AAAA 都要：IPv6 直连常常比 IPv4 快（v4 跨境链路更容易被掐）。

    v6 不通的机器不必担心——候选连不上时探测会立刻返回 0，成本几乎为零。
    """
    ips: list[str] = []
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
        except OSError:
            continue
        for info in infos:
            ip = info[4][0]
            if "%" in ip:                      # 链路本地地址带 scope id，没意义
                continue
            if ip not in ips:
                ips.append(ip)
    return ips


def _candidates(ips: list[str], limit: int) -> list[str]:
    """候选排序：v4/v6 各取一半，先保证两族都有人测；不够再用剩下的补齐。

    只取"前 N 个"的话，AAAA 一多（或 A 一多）就会把另一族整个挤掉，而这次
    要的恰恰是"v6 到底比 v4 快多少"这个答案。
    """
    v4 = [ip for ip in ips if ":" not in ip]
    v6 = [ip for ip in ips if ":" in ip]
    half = max(1, limit // 2)
    picked = v4[:half] + v6[:half]
    rest = [ip for ip in v4[half:] + v6[half:] if ip not in picked]
    return (picked + rest)[:limit]


def _probe_ip(url: str, host: str, port: int, ip: str) -> float:
    """在这个 IP 上拉 2MiB 测吞吐（B/s）。失败返回 0。"""
    cmd = ["curl", "-sS", "-L", "--fail", "-o", os.devnull,
           "--max-time", f"{CDN_PROBE_TIMEOUT:g}",
           "-r", f"0-{CDN_PROBE_BYTES - 1}",
           "-w", "%{speed_download}",
           "--resolve", f"{host}:{port}:{_bracket(ip)}", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=CDN_PROBE_TIMEOUT + 10)
    except (OSError, subprocess.SubprocessError):
        return 0.0
    # 超时/被 --max-time 掐断时 curl 仍会写出 %{speed_download}，那是这次探测
    # 唯一有信息量的数字（慢链路上正是全部候选都跑不满 2MiB 的情况），照收。
    try:
        return float(r.stdout.decode(errors="replace").strip() or 0)
    except ValueError:
        return 0.0


def _read_cdn_cache(out_dir: Path, host: str) -> dict | None:
    path = out_dir / CDN_CACHE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("host") != host or not data.get("ip"):
        return None
    at = float(data.get("at") or 0)
    if time.time() - at > CDN_PROBE_TTL:
        return None
    return {"host": host, "port": int(data.get("port") or 443), "ip": data["ip"],
            "at": at, "speed": float(data.get("speed") or 0)}


def _write_cdn_cache(out_dir: Path, pick: dict) -> None:
    try:
        (out_dir / CDN_CACHE).write_text(
            json.dumps({k: pick[k] for k in ("host", "port", "ip", "at", "speed")},
                       ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _forget_cdn(out_dir: Path) -> None:
    (out_dir / CDN_CACHE).unlink(missing_ok=True)


def _remote_size(url: str, hdr_file: Path | None) -> int:
    """服务端没给 size 时问一次 Content-Length（拿不到就退回单流，不猜）。"""
    cmd = ["curl", "-sS", "-L", "-I", "-o", os.devnull, "--max-time", "20"]
    if hdr_file:
        cmd += ["-H", f"@{hdr_file}"]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 0
    if r.returncode != 0:
        return 0
    for line in reversed(r.stdout.decode(errors="replace").splitlines()):
        if line.lower().startswith("content-length:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return 0
    return 0


class DownloadError(Exception):
    pass


class RangeUnsupported(DownloadError):
    """服务端不认 Range：分段这条路走不通，必须退回单流。"""


class Prefetcher(threading.Thread):
    """趁当前这片刻在跑活的时候，把服务端预锁（prelock）的下一片先下好。

    治的是"交付完一片才去下下一片"的空窗：慢机器一片跑十几小时，这期间网络
    完全闲着；等 finish 拿到下一片再去下 20G，就是半小时起步的白等。
    服务端派 active 时本来就把下一片以 prelock 锁给了这台设备（见
    dispatch.assign），这里只是把"预留"兑现成"已经躺在本地"——不新增协议，
    也不问服务端要任何额外东西。

    手上没有 prelock（库存不足/配额已满/tier 不在白名单）时就是 no-op。

    独立的 RunState：download() 会往 state 里写进度，绝不能拿 active 的那个
    ——finish 申报会带上最终 progress，被预取的百分比顶掉就是假数据。
    """

    def __init__(self, lease: dict, out_dir: Path, cfg: dict) -> None:
        super().__init__(daemon=True, name="fox-prefetch")
        self.lease = lease
        self.out_dir = out_dir
        self.cfg = cfg
        self.state = RunState()
        self.error: Exception | None = None

    def run(self) -> None:
        pkg = self.lease["package"]
        log(f"预取下一片 {pkg}（趁当前这片跑活时下好，下一轮直接开工）")
        try:
            download(self.lease, self.out_dir, self.state, self.cfg)
        except DownloadError as e:
            if self.state.stop.is_set():
                log(f"预取已中断：{pkg}（.part 保留，轮到这片时断点续传）")
            else:
                log(f"[warn] 预取失败（不影响当前作业，轮到这片时再试）：{e}")
        except FileNotFoundError:
            log("[warn] 预取跳过：找不到 curl")
        except Exception as e:                      # 预取永不拖垮主流程
            log(f"[warn] 预取异常（不影响当前作业）：{e}")
        else:
            log(f"预取完成：{pkg}")

    def finish(self) -> None:
        """主流程收尾：等它下完；下不完就掐掉，把 .part 留给下一轮续传。

        必须等它真死透再返回——它手里正写着 <pkg>.part，而下一轮很可能就是
        给同一个包续传，两个 curl 往一个文件里写会把分片写坏。
        """
        self.join(timeout=PREFETCH_JOIN_WAIT)
        if not self.is_alive():
            return
        self.state.stop.set()
        self.join(timeout=PREFETCH_STOP_WAIT)
        if self.is_alive():
            log("[warn] 预取线程未能及时停止（下一轮续传可能与它撞车，"
                "若分片校验失败会自动重下）")


def start_prefetch(ahead: dict | None, out_dir: Path, cfg: dict, args) -> Prefetcher | None:
    """该不该预取。--no-download / --no-prefetch 时一律不预取。"""
    if not ahead:
        return None
    if getattr(args, "no_download", False) or getattr(args, "no_prefetch", False):
        return None
    pre = Prefetcher(ahead, out_dir, cfg)
    pre.start()
    return pre


# ------------------------------------------------------------------ 干活

class RunState:
    """本地作业状态。申报制下它只服务两处：任务内进度文件解析（tagger 写
    .fox-progress）和 finish 时的一次性最终进度上报——没有心跳线程读它了。"""

    def __init__(self) -> None:
        self.stop = threading.Event()          # 本地要停（Ctrl-C/SIGTERM）
        self.phase = "idle"
        self.done = 0
        self.total = 0
        self.lock = threading.Lock()

    def progress(self) -> dict:
        with self.lock:
            return {"phase": self.phase, "done": self.done, "total": self.total}


# /done 的 stats 会写进服务端的 packages.note（dispatch._finish_impl）。
# 服务端旧版把它截到 200 字，装不下 id 明细；新的上限见
# server/fox-label-deployed-20261112/dispatch.py 的 STATS_NOTE_LIMIT。
# 这里按 16 KiB 收口，既留足几百个 id，又远低于 64 KiB 的 POST body 上限。
SKIP_NOTE_LIMIT = 16 * 1024

# /api/v1/skips 每批条数。50 条 × 约 80 字符 ≈ 4 KB，远低于 64 KiB 的
# POST body 上限；条数小也意味着一次失败只丢这一小批，重发成本低。
SKIP_BATCH = 50
# 打标期间扫账本发现新条目的周期（秒）。120s 与旧版心跳同频：管理员在面板上
# 看到的延迟感与过去"心跳报进度"一致，不会更差。
SKIP_SCAN_INTERVAL = 120.0


def _skip_rows(out_dir: Path, package: str) -> list[dict]:
    """读出某分片的跳过/剔除明细，按 image_id 排序去重。

    ★必须按 `shard == package` 过滤：worker 的 out_dir 是**所有租约共用**的
      （默认 work/fox-work），skipped.jsonl / excused.jsonl 会累积多个分片。
      不加过滤就会把别的片的 id 混进当前片的申报——那正是"一个回传里塞全部"
      的毛病，而且管理员按包看时会看到不属于这个包的 id。
    """
    out: dict[str, dict] = {}
    for name, kind in (("skipped.jsonl", "skipped"),
                       ("excused.jsonl", "excused")):
        path = Path(out_dir) / name
        if not path.is_file():
            continue
        try:
            for line in path.open("r", encoding="utf-8", errors="replace"):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if not _same_shard(obj.get("shard"), package):
                    continue
                iid = str(obj.get("image_id") or "").strip()
                if not iid:
                    continue
                # 同一个 id 若两本都有，以 excused 为准（那是最终结论）
                out[iid] = {"image_id": iid, "kind": kind,
                            "reason": str(obj.get("error") or "").strip()[:120]}
        except OSError as e:
            # ★不能静默 pass：读不动就等于这一片一条明细都报不出去，日志里
            #   却一切正常。至少要说一声，否则"服务端收不到"永远查不出原因。
            log(f"[warn] 读不了账本 {path}（{e}），本片明细可能不完整")
    return sorted(out.values(), key=lambda r: r["image_id"])


def _same_shard(recorded: object, package: str) -> bool:
    """账本里的 shard 与当前包名是否指同一个包。

    写入侧（client.py）存的是 `Path(task.shard).name`，即 basename；但
    `TaskRow.shard` 在 HF 直读时是 `hf://org/repo/shard-00001.tar#member`、
    在本地目录输入时是绝对路径。两边直接全等比较会**永远不相等**，
    结果是每一片都报"0 条"，明细静默丢失且毫无报错。

    所以两侧都先归一成 basename 再比：去掉 `#成员`、去掉协议头、取路径末段。
    """
    text = str(recorded or "").strip().split("#", 1)[0]
    if not text or not package:
        return False
    text = text.replace("\\", "/").rstrip("/")
    name = text.rsplit("/", 1)[-1]
    want = str(package).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return name == want


def skip_note_for(out_dir: Path, package: str) -> tuple[str, int, int]:
    """给 finish 的 stats 追加"本片哪些 id 被跳过/剔除、因为什么"。

    ★必须按 `shard == package` 过滤：worker 的 out_dir 是**所有租约共用**的
      （默认 work/fox-work），skipped.jsonl / excused.jsonl 会累积多个分片。
      不加过滤就会把别的片的 id 混进当前片的申报——那正是"一个回传里塞全部"
      的毛病，而且管理员按包看时会看到不属于这个包的 id。

    ★为什么不改成"回传账本文件"：产物回传现在是关的，打好的标先留在成员
      电脑上；finish 是每片唯一一定会发生的写通道，按片各申报一次，天然分开。

    返回 (明细段, 已写入条数, 总条数)；没有命中就返回 ("", 0, 0)，
    不污染原本简短的 stats。

    ★装不下时**不会留到下一批**：/done 是租约唯一的关闭路径，一片只调一次，
      调完就关，没有第二次机会。所以这里必须把"另有 N 张没报"明写出来，
      日志也要用「已报/总数」而不是只报已写入数——否则服务端少收了一批，
      本地却显示"全部上报成功"，排查时完全看不出来。
      完整清单始终留在本机 skipped.jsonl / excused.jsonl，不会丢数据。
    """
    rows = _skip_rows(out_dir, package)
    if not rows:
        return "", 0, 0
    total = len(rows)
    parts: list[str] = []
    used = 0
    # 给尾部"另有 N 张"的提示留位置，别让 note 自己顶破上限
    tail_room = 96
    for d in rows:
        seg = f"{d['image_id']}[{d['kind']}] {d['reason']}"
        # 逐条累加，绝不切在半个 image_id 中间
        if used + len(seg) + 2 > SKIP_NOTE_LIMIT - tail_room:
            break
        parts.append(seg)
        used += len(seg) + 2
    note = "; ".join(parts)
    if len(parts) < total:
        note += (f" …另有 {total - len(parts)} 张未随本次申报"
                 f"（完整清单见本机 skipped.jsonl / excused.jsonl）")
    return note, len(parts), total


class SkipReporter:
    """打标期间把新增的跳过/剔除明细分批申报到调度中心（POST /api/v1/skips）。

    为什么需要它：`/done` 是租约唯一的关闭路径，一片只调一次，所以塞在
    stats 里的明细有 16 KiB 上限、且装不下的部分没有第二次机会。而这个通道
    在租约还开着时可以被调用任意多次，于是"发不完下一批继续"才成立。

    设计要点：
      * **游标按 image_id 记，不按行号**。账本是追加写，但 retry 轮会再追加
        同一个 id 的行；用行号会在文件被重写/轮转时错位，用 id 集合最稳。
      * **只有服务端明确 ack 才推进游标**。404（服务端没升级）直接禁用本次
        上报、退回 finish 的 stats 通道；网络失败保留本批待下次重发，不丢。
      * **按批 50 条**：一次失败只影响一小批，body 也只有几 KB。
      * **失败绝不打断打标**：这个类不抛异常，出问题只 log。
    """

    def __init__(self, cfg: dict, out_dir: Path, package: str,
                 lease_id: str = "", interval: float = SKIP_SCAN_INTERVAL) -> None:
        self.cfg = cfg
        self.out_dir = Path(out_dir)
        self.package = package
        self.lease_id = lease_id
        self.interval = max(1.0, interval)
        self.sent: set[str] = set()      # 已被服务端确认的 image_id
        self.pending: list[dict] = []    # 上次没发成功、要重发的批次
        self.batch_no = 0
        self.next_at = 0.0               # 0 = 第一拍立刻扫（早发现早报）
        self.enabled = True
        self.total_sent = 0

    def disable(self, why: str) -> None:
        if self.enabled:
            left = len(self.pending)
            extra = (f"；另有 {left} 条这次没发出去，会随 finish 的 stats 再带一份"
                     if left else "")
            log(f"[warn] 跳过明细改为随 finish 上报（{why}）{extra}；"
                f"本机账本不受影响")

    def tick(self) -> None:
        """由主循环周期性调用；内部自己按 interval 节流。"""
        if not self.enabled:
            return
        now = time.time()
        if now < self.next_at:
            return
        self.next_at = now + self.interval
        try:
            self._flush()
        except (AuthError, ApiError, NetError) as e:
            # 这一批没被服务端确认，批次号必须退回去：换了号重发，服务端的
            # 幂等去重就失效了，同一批明细会入两次。放在这里统一回退，
            # 而不是让 _flush 的每个异常分支各自记得做。
            self._rewind_batch()
            if getattr(e, "code", 0) == 429:
                log("[warn] 跳过明细上报被限流，下一周期重发同一批")
            else:
                log(f"[warn] 跳过明细上报失败（下次再试）：{e}")
        except Exception as e:                      # 兜底：绝不打断打标
            self._rewind_batch()
            log(f"[warn] 跳过明细上报异常（已忽略）：{e}")

    def _rewind_batch(self) -> None:
        """退回到"这一批还没发出去"的状态，下次用同一个 batch_no 重发。"""
        if self.batch_no > 0:
            self.batch_no -= 1

    def _flush(self) -> None:
        if not self.lease_id:
            # 没有租约 ID 服务端无法归属校验，发了也是 401/409。
            # 直接停手退回 finish 通道，而不是每个周期都白报一次。
            self.enabled = False
            self.disable("手里没有租约 ID")
            return
        if not self.pending:
            fresh = [r for r in _skip_rows(self.out_dir, self.package)
                     if r["image_id"] not in self.sent]
            self.pending = fresh
        while self.pending:
            batch = self.pending[:SKIP_BATCH]
            self.batch_no += 1
            try:
                api(self.cfg, "/api/v1/skips",
                    {"lease_id": self.lease_id, "package": self.package,
                     "batch_no": self.batch_no, "items": batch}, retries=0)
            except ApiError as e:
                if e.code == 404:
                    # 旧服务端没有这个路由：停手，退回 finish 的 stats 通道。
                    # 回退批次号是给"服务端中途升级/切流量"留的正确性——这个
                    # 实例之后不再发送，但下次新建的 reporter 会从 1 开始，
                    # 不能留下一个已消耗但没落库的号。
                    self._rewind_batch()
                    self.enabled = False
                    self.disable("服务端未提供 /api/v1/skips")
                    return
                if e.code == 429:
                    # 限流：抛出去让 tick 统一回退 batch_no 并记日志。
                    # 不能用 return——那样批次号停在已递增的位置，下次换个号
                    # 重发，服务端的幂等去重就失效了。
                    raise
                if e.code == 409:
                    # 租约已被关闭（管理员释放/重复申报）：明细仍会随 finish
                    # 的 stats 走，这里没必要继续硬报。
                    self._rewind_batch()
                    self.enabled = False
                    self.disable("租约已关闭")
                    return
                raise
            # 服务端确认后才推进游标，网络失败时这批原样留着下次重发
            self.sent.update(r["image_id"] for r in batch)
            self.total_sent += len(batch)
            self.pending = self.pending[SKIP_BATCH:]
            log(f"[ok] 跳过明细已上报 {len(batch)} 条"
                f"（本片累计 {self.total_sent}）")


def declare_finish(cfg: dict, lease_id: str, ok: bool, reason: str,
                   progress: dict, note: str | None = None,
                   fail_kind: str = "", metrics: dict | None = None,
                   parts: dict | None = None) -> dict:
    """finish 申报（SPEC-declare §7.2）：done/failed 都必须显式申报。

    申报制语义：finish 是租约唯一的客户端关闭路径，而 open 租约不会过期，
    所以网络不通时**本地退避重试直到成功**（1/2/4…上限 300s）是安全的——
    重试要么最终落账（好），要么一直失败一直重试（管理员会在面板看到
    这台设备的租约仍 open，可人工干预）。

    409（租约已关闭：管理员已释放/重复申报）不重试，直接抛 ApiError 给上层。
    兼容期兜底：finish 响应里的 rejected 分支按原契约保留解析，新服务端
    该字段恒为空、不会触发，仅防未来语义回摆。
    """
    # 测试钩子：FOXLABEL_FINISH_BACKOFF 覆盖初始退避秒数（默认 1s），生产不设。
    try:
        first_wait = max(0.05, float(os.environ.get("FOXLABEL_FINISH_BACKOFF") or 1.0))
    except ValueError:
        first_wait = 1.0
    body = {"lease_id": lease_id, "progress": progress,
            **({"stats": note} if note else {}), **({} if ok else {"reason": reason}),
            # v3.4：失败类型枚举（服务端据此统计），原文仍在 reason 里。
            **({} if ok or not fail_kind else {"fail_kind": fail_kind}),
            # 旁证指标：下载/跑活耗时、张数、重试次数——只有数字，不含网络位置信息。
            **({"metrics": metrics} if metrics else {}),
            # 分项申报（SPEC-parts §4，只增）：{"tag":"done"} 这样的对象。
            # 不带它 = 整包口径，老服务端照旧处理。
            **({"parts": parts} if parts else {})}
    wait = 0.0
    tries = 0
    while True:
        try:
            # retries=0：重试完全交给上面的退避循环（可观测、有日志、受
            # FINISH_RETRY_MAX 约束），不让 api() 的内部重试把短时断网吞掉。
            res = api(cfg, "/api/v1/done" if ok else "/api/v1/fail", body, retries=0)
            rejected = res.get("rejected") or []
            if rejected:               # 契约兜底：新服务端恒空，仅留解析
                log(f"[warn] finish 响应含 rejected={rejected}（兼容分支，未触发）")
            return res
        except ApiError:
            raise                     # 4xx（401/409/…）：重试没有意义
        except (NetError, urllib.error.URLError, socket.timeout, ConnectionError) as e:
            tries += 1
            wait = min(wait * 2 if wait else first_wait, FINISH_RETRY_MAX)
            log(f"[warn] finish 申报失败（第 {tries} 次）：{e}——"
                f"租约不会过期，{wait:.0f}s 后重试，直到成功")
            time.sleep(wait)

def run_task(lease: dict, shard: Path, args, state: RunState, out_dir: Path) -> int:
    """跑外部命令（tagger / bridge）。--cmd 里可用占位符：
    {shard} {package} {out} {lease}"""
    if not args.cmd:
        log("没有指定 --cmd，视为协调层演练（下载后直接上报完成）")
        return 0
    # 这一片本机实际会做哪几侧（= 服务端说缺的 ∩ 本机能产的）。
    # 交集为空说明派工与能力不符：本机做这一侧交不出任何东西，白下载 1.8G
    # 还占着租约不让人补。直接退出让服务端回收，比跑一趟更省。
    want = wanted_parts(lease)
    mine = parts_to_do(lease, args)
    if want and not mine:
        log(f"[warn] 这一片只缺 {'/'.join(want)}，而本机只做 "
            f"{'/'.join(sorted(local_caps(getattr(args, 'parts', None))))}"
            f"——派工与能力不符，直接退回不下载（让服务端派给合适的机器）")
        return 1
    parts_arg = ",".join(mine or want)
    argv = args.cmd.format(shard=str(shard), package=lease["package"],
                           out=str(out_dir), lease=lease["lease_id"],
                           parts=parts_arg)
    env = dict(os.environ)
    env.update({"FOXLABEL_PACKAGE": lease["package"], "FOXLABEL_LEASE_ID": lease["lease_id"],
                "FOXLABEL_REPO": lease["repo"], "FOXLABEL_OUT": str(out_dir),
                "FOXLABEL_PROGRESS": str(out_dir / ".fox-progress"),
                # 这一片要我补哪几项（SPEC-parts §5）。空串 = 维持原样全做。
                # 任务脚本据此关掉另一侧：只补 tag 的机器不该把 NL 模型也拉起来，
                # 那既花时间又会覆盖别人已经交好的产物。
                "FOXLABEL_PARTS": parts_arg})
    state.phase, state.done = "work", 0
    log(f"执行：{argv}")
    proc = subprocess.Popen(argv, shell=True, env=env, cwd=args.cwd or None)
    prog = out_dir / ".fox-progress"
    t0 = time.time()
    while proc.poll() is None:
        # 申报制（SPEC-declare）：本地停止只剩 Ctrl-C/SIGTERM——没有"租约被收回"
        # 这种服务端触发的停止（open 租约永久有效，无心跳失败计数）。
        if state.stop.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return -1
        if prog.exists():
            try:
                data = json.loads(prog.read_text(encoding="utf-8").strip() or "{}")
                state.done = int(data.get("done") or 0)
                state.total = int(data.get("total") or 0)
            except (ValueError, OSError):
                pass
        time.sleep(0.3)
    log(f"命令结束，退出码 {proc.returncode}，用时 {time.time() - t0:.1f}s")
    return proc.returncode


def do_one(cfg: dict, lease: dict, args, state: RunState,
           ahead: dict | None = None) -> tuple[bool, str, str, dict]:
    """处理一个租约：下载 → 跑活 → 返回是否成功。

    本机重试（`--retries`，默认 1，即最多跑两次）：
      * 下载失败重试：分片是断点续传的，重试只是接着下，成本很低；
      * 命令失败重试：显存被别的进程抢走、后端 503、临时文件冲突，大多第二次就好。
    两者都只在本地重试，**不消耗服务端的 attempt、也不惊动派工**；真跑不通才
    走 fail 申报，让服务端把片退回池子换台机器——那比在同一台机器上硬磨合理。

    `ahead` 是服务端同一批派下来的 prelock（下一片）。本片下完、开始跑活之前
    起一个后台线程把它下好：慢机器一片跑十几小时，网络全程闲着，等到下一轮
    再下就是纯白等（见 Prefetcher）。
    """
    out_dir = Path(args.out) if args.out else Path.cwd() / "fox-work"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 本片是否成功。用可变容器是为了让 finally 能读到——局部变量在下载失败那条
    # 早退路径上还没赋值，直接读会 UnboundLocalError。
    _succeeded = {"v": False}
    # 明细上报器在**这里**建而不是在 run_loop：它要用本片的 lease_id 做归属校验，
    # 而 lease_id 只有拿到租约之后才知道。传进来的 reporter 只是兜底（测试用），
    # 正常情况下以本片的为准——跨片复用会把 A 片的 id 写进 B 片的租约。
    reporter = SkipReporter(cfg, out_dir, lease["package"],
                            lease_id=str(lease.get("lease_id") or ""))
    shard = out_dir / lease["package"]
    retries = max(0, int(getattr(args, "retries", 0) or 0))
    pre: Prefetcher | None = None
    metrics: dict = {}
    dl_retries = 0
    task_retries = 0
    try:
        if not args.no_download:
            err: DownloadError | None = None
            for attempt in range(retries + 1):
                try:
                    shard = download(lease, out_dir, state, cfg)
                    err = None
                    break
                except DownloadError as e:
                    err = e
                    dl_retries = attempt + 1
                    if attempt < retries:
                        nap = 3 * (attempt + 1)
                        log(f"[retry] 下载失败（{e}），{nap}s 后重试"
                            f"（{attempt + 1}/{retries}）")
                        time.sleep(nap)
                except FileNotFoundError:
                    die(EXIT_USAGE, "找不到 curl，请先安装（apt install curl）")
            if err is not None:
                detail = f"下载失败：{err}"
                return (False, detail, classify_fail(detail),
                        {**_dl_stats(lease), "retries": dl_retries})
            # 本片已落地、命令还没起：正是开预取的时机（不与本片的下载抢带宽）。
            metrics.update(_dl_stats(lease))
            pre = start_prefetch(ahead, out_dir, cfg, args)
        t_task = time.time()
        rc = run_task(lease, shard, args, state, out_dir)
        attempt = 0
        while rc != 0 and attempt < retries:
            attempt += 1
            task_retries = attempt
            nap = 3 * attempt
            log(f"[retry] 任务命令失败（退出码 {rc}），{nap}s 后重试（{attempt}/{retries}）")
            time.sleep(nap)
            rc = run_task(lease, shard, args, state, out_dir)
        metrics["task_s"] = round(time.time() - t_task, 1)
        metrics["retries"] = dl_retries + task_retries
        if state.total:
            metrics["images"] = state.total
        if rc != 0:
            detail = f"任务命令失败（退出码 {rc}）"
            return False, detail, classify_fail(detail), metrics
        have = set(cfg.get("have") or [])
        have.add(f"{lease['repo']}/{lease['package']}")
        cfg["have"] = sorted(have)[-200:]
        save_cfg_soft(cfg)
        _succeeded["v"] = True
        return True, "完成", "", metrics
    finally:
        # 无论成功、失败还是 Ctrl-C，都必须把预取线程收干净：它正在写
        # <pkg>.part，留到下一轮会和下一轮的 curl 抢同一个文件。
        if pre is not None:
            pre.finish()
        # 收尾补发：把这一片剩下的明细一次性发完（不靠 120s 周期碰运气）。
        #
        # ★只在**这一片会走 /done** 时发：以 fail 申报的片，服务端会把包退回
        #   池子换台机器重做，此时写进这条租约，同一批 id 就会在服务端出现两次
        #   （一条来自失败的租约、一条来自重做的那条）。
        #   失败的片交给 finish 的 stats 就够——它本来就没什么"合法剔除"。
        #
        # ★用 _succeeded 而不是局部 ok：ok 只有走到后面才赋值，下载失败那条
        #   早退路径上还没有它，在 finally 里读会 UnboundLocalError。
        if _succeeded["v"] and reporter.enabled:
            reporter.next_at = 0.0
            reporter.tick()


_RELEASED: set[str] = set()


# ------------------------------------------------------------------ 分项产物
# 一片有两个可以各自完成的产物：tag（分类头）与 nl（描述）。机器能力不同——
# 2080Ti 这类没有稀疏计算的卡跑大张量 tag 模型慢，跑 Q6 的 Qwen3-VL 反而还行，
# 所以允许"只做 NL"。那一片的 tag 依然缺着，服务端按分项记在账上等人来补。

PARTS = ("tag", "nl")


def wanted_parts(lease: dict) -> list[str]:
    """这一片我们打算做哪几项。

    服务端给的 missing_parts 是**权威答案**（它还欠什么）；但它只在租约视图里
    且只在新服务端出现——老服务端没有这个字段，那就什么都不限，按整包做，
    与改造前逐字一致。返回空列表 = 不限制。
    """
    raw = lease.get("missing_parts")
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x) in PARTS]


def wanted_parts_arg(lease: dict) -> str:
    """逗号串，塞给任务命令；空串表示"不限制"。

    ⚠ **只表达"服务端说缺什么"，不等于"本机该做哪些"**。命令与环境变量都
    改用 parts_to_do() 的结果；这里是留给外部脚本/诊断用的原始口径，
    别拿它去申报 finish（会把本机做不出的那侧也标成 done）。
    """
    return ",".join(wanted_parts(lease))


def local_caps(parts_arg: str | None) -> set[str]:
    """本机**能产出**哪几侧，来自 worker 的 --parts。

    both / pref-tag / pref-nl / 未指定 = 两侧都能做：倾向只影响服务端派工顺序，
    不限制本机产出。只有 "tag" / "nl" 是硬约束（那种机器根本没装另一侧的模型）。
    """
    p = str(parts_arg or "").strip().lower()
    if p in PARTS:
        return {p}
    return set(PARTS)


def parts_to_do(lease: dict, args) -> list[str]:
    """这一片**本机实际会做**哪几侧 = 服务端说缺的 ∩ 本机能产的。

    ★为什么必须求交，不能直接用 missing_parts：
      一台只会做 tag 的机器（--parts tag）领到「缺 tag + 缺 nl」的片时，
      missing_parts 是 ["tag","nl"]。若照它申报，这一片会被记成两侧都完成，
      而 nl 根本没做——正是分项账本想解决的那个"静默漏做"，
      只不过这次是**谎报完成**，比不报更难发现（账面全齐，数据缺一半）。
    """
    return [k for k in wanted_parts(lease) if k in local_caps(getattr(args, "parts", None))]


def parts_declare(lease: dict, ok: bool, args=None) -> dict:
    """finish 时申报的分项结果（SPEC-parts §4）。

    * 成功：只申报**这一片本机真的做了的那几项**为 done。没做的一侧不出现，
      服务端保持它的原值——绝不能顺手把别人交好的另一侧也标成 done，
      更不能把"服务端说缺"当成"本机做了"（见 parts_to_do 的说明）。
    * 失败：什么都不申报（服务端本来也不会因为 fail 改动分项）。
    * 老服务端不认这个字段？它只解析已知键，多一个 parts 无害；
      真正老到会 400 的版本不会——契约是只增的。
    """
    if not ok:
        return {}
    # args=None 是留给旧调用点的兼容：那时拿不到本机能力，只能按服务端口径。
    # 新调用点必须传 args，否则又会回到"谎报完成"。
    mine = parts_to_do(lease, args) if args is not None else wanted_parts(lease)
    if not mine:
        return {}                  # 不限制 = 整包口径，交给服务端按旧规则记 done
    return {k: "done" for k in mine}


def release_unused(cfg: dict, leases: list[dict], keep: set[str] | None = None) -> None:
    """把手上用不到的租约还给服务端（申报制下走管理员语义的 release 端点）。

    只还"确定不会做"的片：收工时多派的 prelock、--once/--max-packages 到配额
    后服务端新派来的下一片、SIGTERM/Ctrl-C 优雅收尾时的全部租约（含 active，
    见 cmd_run 的 KeyboardInterrupt 分支——那是设备管理员的主动申报动作）。
    申报制下 open 租约永久有效，租着不还并不会过期消失，反而会占住包让别人
    领不到——所以该还的还是要主动还。
    崩溃不走这里：interrupt 的 active 租约保留在设备名下，重启后 handshake
    幂等续接（见 _run_loop / SPEC-declare §7）。
    """
    keep = keep or set()
    for lease in leases:
        lid = lease["lease_id"]
        if lid in keep or lid in _RELEASED:
            continue
        try:
            api(cfg, "/api/v1/release", {"lease_id": lid}, retries=0)
            _RELEASED.add(lid)
            log(f"已归还租约 {lease['kind']}:{lease['package']}"
                + ("（预锁已失效/丢失，不影响当前作业）" if lease["kind"] == "prelock" else ""))
        except ApiError as e:
            if e.code == 409:          # 已经归还/已被回收：幂等处理，不告警
                _RELEASED.add(lid)
                continue
            log(f"[warn] 归还租约失败（不影响已交付的数据）：{e}")
        except (NetError, AuthError) as e:
            log(f"[warn] 归还租约失败（不影响已交付的数据）：{e}")


def _claim(cfg: dict) -> list[dict]:
    """握一次手，把派到手上的租约取回来。空列表 = 池子空。

    幂等续接（SPEC-declare §7）：服务端 assign 对同设备同 kind 的 open 租约
    直接原样返回，所以崩溃/Ctrl-C 后重启时，同一片会派回来——
    本地已校验通过的分片在 download() 里直接复用，不重复下载。
    """
    # 顺带把这台机器已有的产出报上去，服务端会优先把同一片派回来；
    # hw（SPEC-link §1.1）是可选追加字段，收集失败降级为不报，不阻塞握手。
    body = {"client_ver": VERSION, "local_pkgs": cfg.get("have") or []}
    hw = sanitize_hw(collect_hw(cfg))
    if hw is not None:
        body["hw"] = hw
    # 这台机器愿意做哪几项（SPEC-parts §2）。服务端**不按硬件硬派**，只把它
    # 当派工排序的偏好——快不快只有本机知道，所以是自述而非探测。
    # 配置里没有这项就整个字段不发，老口径"都做"。
    parts_pref = cfg.get("parts")
    if isinstance(parts_pref, list) and parts_pref:
        body["parts"] = [str(x) for x in parts_pref if str(x) in PARTS]
    had_secret = bool(cfg.get("secret"))
    if not had_secret:
        # 老配置（服务端签发时代的）丢了 secret：本机补一份，服务端按"首次登记"
        # 或"绑定为空"接受它
        cfg["secret"] = new_local_secret()
        save_cfg_soft(cfg)
    try:
        hs = api(cfg, "/api/v1/handshake", body, retries=2)
    except AuthError as e:
        die(EXIT_AUTH, f"认证失败：{e}\n"
                       f"    secret 以本机为准；若这个 user_id 被别的机器占着，"
                       f"请管理员执行 server.py forget --user {cfg['user_id']} 后重试")
    except (ApiError, NetError) as e:
        die(EXIT_USAGE, f"握手失败：{e}")
    issued = hs.get("secret")
    if issued and not had_secret:
        cfg["secret"] = issued                 # 本机原本没有，收下服务端签发的
        save_cfg_after_handshake(cfg, True)
    elif issued and issued != cfg["secret"]:
        # 本机本来就有：不覆写。老服务端可能回另一把，留个痕迹方便排查
        log(f"[warn] 服务端回了另一把 secret，本机保留原有的不覆写（{CONFIG}）")
    log(f"身份 {cfg['user_id']} / 设备 {cfg['device_id']}　服务端 {cfg.get('server_id')}")
    return hs.get("leases") or []


def cmd_run(args) -> int:
    global DL_CONNS, DL_CDN_PROBE
    cfg = load_cfg()
    # --parts 只影响"这轮申报意愿"，不写回配置：同一台机器今天做 NL、
    # 明天有空做 tag，不该被一次命令永久钉死。
    pref = getattr(args, "parts", None)
    if pref:
        got = [x.strip().lower() for x in str(pref).split(",") if x.strip()]
        bad = [x for x in got if x not in PARTS]
        if bad:
            die(EXIT_USAGE, f"--parts 里有未知分项：{bad}（只认 {'/'.join(PARTS)}）")
        cfg["parts"] = got
    out_dir = Path(args.out) if args.out else Path.cwd() / "fox-work"
    # 下载口径在这里定一次，download()/Prefetcher 都读这两个模块级开关。
    DL_CONNS = max(1, min(16, int(getattr(args, "download_conns", DEFAULT_CONNS) or DEFAULT_CONNS)))
    DL_CDN_PROBE = not getattr(args, "no_cdn_probe", False)

    # 本次已交付的片数。--max-packages 按它停手；计数用可变容器是为了
    # 让 _run_loop 能就地加，不用改它的返回值（那里已经有 6 种退出路径）。
    done = {"n": 0}
    last_work = time.time()
    # 连续失败计数：只在"任务/下载失败(6)"上累加，成功一次就清零。到上限才停手。
    consec_fail = 0
    max_fails = max(1, int(getattr(args, "max_fails", MAX_CONSEC_FAILS) or MAX_CONSEC_FAILS))

    while True:
        leases = _claim(cfg)
        if not leases:
            log("服务端暂无派工（池子空了或配额已满）")
            if not args.wait:
                return EXIT_OK
            if args.idle_timeout and time.time() - last_work >= args.idle_timeout:
                log(f"已经 {args.idle_timeout:.0f}s 没有派工，收工")
                return EXIT_OK
            # 长驻等待：不退出，隔一会儿再问。这一轮手上没租约，所以发
            # 60s idle ping 供面板三态展示（失败退避；旧服务端 404 静默停发）。
            # 申报制下它只是"我在，空闲"的展示信号，不承载任何续约语义。
            pinger = IdlePinger(cfg)
            pinger.start()
            nap = WAIT_POLL
            if args.idle_timeout:
                # 别睡过头：--idle-timeout 比轮询间隔短的时候要能准时收工
                nap = max(1.0, min(WAIT_POLL,
                                   args.idle_timeout - (time.time() - last_work)))
            log(f"{nap:.0f}s 后再看（Ctrl-C 结束）")
            while nap > 0 and not pinger.stop.is_set():
                step = min(1.0, nap)
                time.sleep(step)
                nap -= step
            if pinger.stop.is_set():
                pinger.join(timeout=5)
                if pinger.auth_error is not None:
                    die(EXIT_AUTH, f"认证失效：{pinger.auth_error}")
                # 404（旧服务端）静默降级：之后不再发 ping，主循环照常继续
            continue

        log("派工：" + "、".join(f"{l['kind']}:{l['package']}" for l in leases))
        held: list[dict] = list(leases)
        interrupted = False
        try:
            rc = _run_loop(cfg, args, out_dir, leases, held, done)
        except KeyboardInterrupt:
            # SIGTERM/Ctrl-C 优雅收尾：这是设备管理员的**主动申报动作**——
            # 手上全部租约（含正在处理的 active）当场归还，包立刻回池，
            # 别人马上能领。与崩溃不同：崩溃时进程可能已死，租约留名下
            # 交管理员面板处理（SPEC-declare §7）。
            interrupted = True
            release_unused(cfg, held)
            print("\n收到中断，已停止（未提交的产物保留在本地）")
            return EXIT_OK
        except BaseException:
            # 崩溃/异常退出：**不还租约**（申报制，SPEC-declare §7）。open
            # 租约永久有效，留在设备名下正好——重启后 handshake 幂等续接
            # 把同一片派回来，本地已校验的分片直接复用，不重复下载。
            # 管理员随时可在面板人工释放。
            raise
        finally:
            if not interrupted:
                release_unused(cfg, held)
        last_work = time.time()

        if rc == EXIT_TASK:
            consec_fail += 1
        elif rc == EXIT_OK:
            consec_fail = 0

        if not args.wait:
            return rc
        if rc == EXIT_LEASE:
            # 管理员在面板上把租约释放了（或这条已经申报过）。这不是故障，
            # 长驻模式的重心是"继续领下一片"，退出只会让这台机器白站着。
            log("[..] 租约被关闭，重新握手领下一片")
            time.sleep(1.0)
            continue
        if rc == EXIT_TASK:
            if consec_fail >= max_fails:
                die(EXIT_STUCK,
                    f"连续 {consec_fail} 片失败，已停手（{out_dir} 里的产物保留）。\n"
                    f"    常见原因：显存不足、模型/命令路径变了、产出盘满了、后端服务没起。\n"
                    f"    修好后重跑同一条 run 命令即可；本地已校验的分片会复用。")
            nap = min(60, 5 * consec_fail)
            log(f"[..] 这一片没成，{nap}s 后重新领工（连续失败 {consec_fail}/{max_fails}）")
            time.sleep(nap)
            continue
        if rc != EXIT_OK:
            # 剩下的只有配置/认证这类"重试也一样"的错误，交给人处理
            return rc
        if args.max_packages and done["n"] >= args.max_packages:
            log(f"已达本次配额 {args.max_packages} 片，收工")
            return EXIT_OK


def _run_loop(cfg: dict, args, out_dir: Path,
              leases: list[dict], held: list[dict], done: dict) -> int:
    while leases:
        active = next((l for l in leases if l["kind"] == "active"), None)
        if active is None:
            log("手上没有 active，结束")
            break
        state = RunState()
        held[:] = [l for l in leases if l["lease_id"] != active["lease_id"]]
        # 服务端在同一批里给的下一片（prelock）。这一片跑活时顺手把它下好，
        # 交付完直接开工，不再有"下 20G 的空窗"。没有就是 None，行为不变。
        ahead = next((l for l in leases if l["kind"] == "prelock"), None)
        try:
            # 申报制（SPEC-declare）：打标期间零定时网络活动——Beater 已拆除，
            # 不发心跳、不轮询，服务端也不会自动收回租约。本地停止只剩 Ctrl-C。
            ok, detail, fail_kind, metrics = do_one(cfg, active, args, state, ahead)
        except KeyboardInterrupt:
            # SIGTERM/Ctrl-C 到达在 do_one 内部：把 active 也交给上层
            # cmd_run 的优雅收尾分支归还（挂到 held 里带上）。
            held.append(active)
            raise

        # finish 申报：done/failed 都必须显式申报（租约唯一的客户端关闭路径）。
        # done 带最终 progress（面板租约进度停在最后已知值，finish 一次性补齐）；
        # failed 带 reason 与最终 progress。
        final_progress = state.progress()
        # 本片跳过/剔除的 id 与原因随 finish 一起申报。只在 ok（走 /done）时
        # 拼：/fail 的 stats 不写 packages.note，塞进去等于白费。
        # 服务端若还是旧版（stats 截 200 字），被切掉的只是明细段——统计部分
        # 在前面，功能不受影响；升级服务端后明细自动全量到位。
        note = args.note or ""
        if ok:
            extra, n, total = skip_note_for(Path(args.out), active["package"])
            if n:
                note = (f"{note} / " if note else "") + f"跳过明细({n}/{total}): {extra}"
                log(f"[ok] 本片 {n}/{total} 条跳过/剔除明细随 finish 上报调度中心")
                if n < total:
                    log(f"[warn] 另有 {total - n} 条装不进本次申报，"
                        f"完整清单留在本机 skipped.jsonl / excused.jsonl")
        try:
            res = declare_finish(cfg, active["lease_id"], ok, detail,
                                 final_progress, note,
                                 fail_kind=fail_kind, metrics=metrics,
                                 parts=parts_declare(active, ok, args))
        except AuthError as e:
            die(EXIT_AUTH, f"认证失败：{e}")
        except ApiError as e:
            if e.code == 409:
                # 租约已关闭：多半是管理员在面板释放过，或这条租约已申报过。
                log(f"[X] {e.msg}（租约已关闭——可能被管理员释放或已申报过，"
                    f"停手退出；未提交的产物保留在本地，重新运行即可）")
                return EXIT_LEASE
            die(EXIT_USAGE, f"申报失败：{e}")
        # declare_finish 内部已把网络故障重试到成功；到这里申报一定已落账。

        if not ok:
            log(f"[X] 本次失败（已申报 fail）：{detail}")
            # 申报 fail 的响应里服务端**照样会补派下一片**（assign 无条件执行）。
            # 以前这里直接 return，那片就成了没人认领的 open 租约——申报制下不会
            # 过期，只能等管理员释放，正是"池子莫名少几片"的来源之一。既然这一轮
            # 已经失败了，先把它还回去，让别的设备马上能领。
            release_unused(cfg, res.get("leases") or [])
            return EXIT_TASK
        log(f"已交付 {active['package']}")
        done["n"] += 1
        if args.once:
            release_unused(cfg, res.get("leases") or [])
            return EXIT_OK
        if args.max_packages and done["n"] >= args.max_packages:
            # 配额到了就停手，别把服务端刚派来的下一片揣着——还回去让它给别人。
            log(f"已达本次配额 {args.max_packages} 片，收工")
            release_unused(cfg, res.get("leases") or [])
            return EXIT_OK
        leases = res.get("leases") or []
        if leases:
            log("新派工：" + "、".join(f"{l['kind']}:{l['package']}" for l in leases))
    log("服务端没有更多派工，收工")
    return EXIT_OK


# ------------------------------------------------------------------ CLI

def main() -> int:
    ap = argparse.ArgumentParser(prog="worker.py", description="fox-label v3 客户端")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("login", help="首次登录：登记身份并保存（本机自持 secret）")
    a.add_argument("--server", default=None)
    a.add_argument("--user-id", default=None)
    a.add_argument("--secret", default=None,
                   help="手动指定一把 secret（默认沿用本地已有的，不覆写）")
    a.add_argument("--new-secret", action="store_true", dest="new_secret",
                   help="强制现生成一把新的本机 secret（本地那把作废，需管理员放掉旧绑定）")
    a.set_defaults(func=cmd_login)

    a = sub.add_parser("show", help="看本地身份与当前派工")
    a.set_defaults(func=cmd_show)

    a = sub.add_parser("run", help="等派工并干活（申报制：finish 显式申报，无心跳）")
    a.add_argument("--out", default=None, help="工作目录（默认 ./fox-work）")
    a.add_argument("--interval", type=float, default=None,
                   help="已废弃（3.0 心跳间隔）。保留解析以兼容旧脚本，无任何效果")
    a.add_argument("--once", action="store_true", help="只处理一个包就退出")
    a.add_argument("--wait", action="store_true",
                   help=f"常驻等派工：池子空了不退出，每 {WAIT_POLL}s 再问一次（默认干完就退）")
    a.add_argument("--idle-timeout", type=float, default=0,
                   help="配合 --wait：连续这么多秒没有派工就退出（0 = 一直等）")
    a.add_argument("--max-packages", type=int, default=0,
                   help="本次最多交付多少片，够了就退出并归还多派的（0 = 不限）")
    a.add_argument("--retries", type=int, default=1,
                   help="同一片在本机最多重跑几次（默认 1；下载与命令各算各的预算）")
    a.add_argument("--max-fails", type=int, default=MAX_CONSEC_FAILS, dest="max_fails",
                   help=f"连续多少片失败就停手并退出码 7（默认 {MAX_CONSEC_FAILS}）")
    a.add_argument("--no-download", action="store_true", help="跳过下载（协调层演练）")
    a.add_argument("--no-prefetch", action="store_true", dest="no_prefetch",
                   help="不预取下一片（默认会趁当前片跑活时把预锁那片下好）")
    a.add_argument("--download-conns", type=int, default=DEFAULT_CONNS,
                   dest="download_conns",
                   help=f"每片并发连接数（默认 {DEFAULT_CONNS}，1 = 老的单流；上限 16）")
    a.add_argument("--no-cdn-probe", action="store_true", dest="no_cdn_probe",
                   help="关闭每片开工前的 CDN 边缘 IP 测速选优（默认开，按直连口径）")
    a.add_argument("--parts", default=None,
                   help="这台机器只做哪几项：tag / nl（逗号分隔，如 nl 或 tag,nl）。"
                        "只做一项时服务端会把缺这一项的片优先派给你，交付后另一项"
                        "仍记在账上等别人补。不写 = 都做（老口径）")
    a.add_argument("--cmd", default=None,
                   help="干活的命令模板，可用 {shard} {package} {out} {lease} {parts}")
    a.add_argument("--cwd", default=None, help="命令的工作目录")
    a.add_argument("--note", default=None, help="提交时的备注（随 finish 申报带给服务端）")
    a.set_defaults(func=cmd_run)

    args = ap.parse_args()
    # POSIX 上把 SIGTERM 也走与 Ctrl-C 同一条收尾路径：否则 --wait 常驻时被
    # systemd/容器停止会硬杀，finally 里的归还租约跑不到，那片只能等 TTL。
    def _graceful(signum, frame):
        raise KeyboardInterrupt

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)):
        if sig is not None:
            try:
                signal.signal(sig, _graceful)
            except (ValueError, OSError):
                pass
    if args.cmd == "login" and not (args.server and args.user_id):
        die(EXIT_USAGE, "login 需要 --server 和 --user-id")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n收到中断，已停止（未提交的产物保留在本地）")
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
