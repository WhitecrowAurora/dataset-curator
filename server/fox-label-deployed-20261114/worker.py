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
       ├── 下载分片（断点续传 + sha256 校验）
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

VERSION = "3.3.0"      # 3.3：本机重试 + 长驻会话不退（失败续跑）；3.2：本机自持 secret
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

# 分项意愿四值（SPEC-parts §2，2026-11-12 起）。与"能力"分开表达：
#   both       = 两侧都做（等同于不表态）
#   tag / nl   = **仅**这一侧，不含它的片不接
#   pref-tag / pref-nl = 优先这一侧，另一侧也能做（兜底接）
# 前两者必须与服务端 PARTS_CHOICES 完全一致，否则握手会被 400 拒掉。
PARTS_CHOICES = ("both", "tag", "nl", "pref-tag", "pref-nl")

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

    gpu 取 tagger/hardware.query_gpus() 主卡（第一块卡，GPUInfo.name/vram_gb），
    vram 以 MB 计（服务端 0~1e6 整数）。mode 取当前 --tiers/--tier 配置名
    （FOXLABEL_TIERS / FOXLABEL_TIER 环境变量或 cfg["mode"]，最长 32 字符）。
    tagger 不在本机装包环境里时（独立 worker 部署）退化为 null GPU 字段，
    但 mode 照报——mode 来自配置，不依赖硬件。
    """
    gpu_name = gpu_vram_mb = None
    try:
        from tagger.hardware import query_gpus   # noqa: 延迟导入，缺包不致命
        gpus = query_gpus()
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
    # 工作区：没活干时这一行是唯一能解释原因的地方（无归属 / 空工作区 / 已删除）。
    if "workspace" in res:
        print("工作区  : " + _workspace_line(res.get("workspace")))
    return EXIT_OK


def _workspace_line(ws: dict | None) -> str:
    if ws is None:
        return "无归属（所有启用的仓库都能拿）"
    ws_id = ws.get("ws_id") or "?"
    repos = ws.get("repos") or []
    if ws.get("missing"):
        return f"{ws_id} —— 服务端已不存在，派不到任何包（需重新指派或解绑）"
    if not repos:
        return f"{ws_id} —— 没有配置仓库，派不到任何包（需管理员补 --repo）"
    return f"{ws_id}（{len(repos)} 个仓库）：" + " / ".join(repos)


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


def download(lease: dict, out_dir: Path, state: "RunState", cfg: dict) -> Path:
    """下载分片：断点续传 + 校验。已存在且哈希正确则直接复用。"""
    dest = out_dir / lease["package"]
    want = lease.get("sha256")
    if dest.exists() and dest.stat().st_size > 0:
        if not want or sha256_file(dest) == want:
            log(f"本地已有校验通过的分片，跳过下载：{dest}")
            return dest
        log(f"本地分片校验失败，重新下载：{dest}")
        dest.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    url = hf_url(lease)
    tok = hf_token()
    cmd = ["curl", "-sS", "-L", "--fail", "--retry", "3", "--retry-delay", "2",
           "-C", "-", "-o", str(part)]
    hdr_file = None
    if tok:
        hdr_file = out_dir / ".fox-hf-headers"
        fd = os.open(hdr_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, f"Authorization: Bearer {tok}\n".encode())
        os.close(fd)
        cmd += ["-H", f"@{hdr_file}"]
    cmd.append(url)
    log(f"下载 {lease['package']}（{lease.get('size', 0) / 2**20:.0f} MiB）…")
    state.phase = "download"
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    t0 = time.time()
    while proc.poll() is None:
        if state.stop.is_set():
            proc.terminate()
            break
        if part.exists() and lease.get("size"):
            state.done = int(part.stat().st_size / lease["size"] * 100)
        time.sleep(0.3)
    rc = proc.wait()
    if hdr_file:
        hdr_file.unlink(missing_ok=True)
    if rc != 0 or not part.exists():
        err = (proc.stderr.read().decode(errors="replace") if proc.stderr else "")[-200:]
        raise DownloadError(f"curl 退出码 {rc} {err}")
    part.replace(dest)
    log(f"下载完成 {dest.name}，用时 {time.time() - t0:.1f}s")
    if want:
        got = sha256_file(dest)
        if got != want:
            dest.unlink(missing_ok=True)
            raise DownloadError(f"sha256 不符：\n  服务端 {want}\n  下载后 {got}")
        log("sha256 校验通过")
    return dest


class DownloadError(Exception):
    pass


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


def declare_finish(cfg: dict, lease_id: str, ok: bool, reason: str,
                   progress: dict, note: str | None = None) -> dict:
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
            **({"stats": note} if note else {}), **({} if ok else {"reason": reason})}
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
    argv = args.cmd.format(shard=str(shard), package=lease["package"],
                           out=str(out_dir), lease=lease["lease_id"])
    env = dict(os.environ)
    env.update({"FOXLABEL_PACKAGE": lease["package"], "FOXLABEL_LEASE_ID": lease["lease_id"],
                "FOXLABEL_REPO": lease["repo"], "FOXLABEL_OUT": str(out_dir),
                "FOXLABEL_PROGRESS": str(out_dir / ".fox-progress")})
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


def do_one(cfg: dict, lease: dict, args, state: RunState) -> tuple[bool, str]:
    """处理一个租约：下载 → 跑活 → 返回是否成功。

    本机重试（`--retries`，默认 1，即最多跑两次）：
      * 下载失败重试：分片是断点续传的，重试只是接着下，成本很低；
      * 命令失败重试：显存被别的进程抢走、后端 503、临时文件冲突，大多第二次就好。
    两者都只在本地重试，**不消耗服务端的 attempt、也不惊动派工**；真跑不通才
    走 fail 申报，让服务端把片退回池子换台机器——那比在同一台机器上硬磨合理。
    """
    out_dir = Path(args.out) if args.out else Path.cwd() / "fox-work"
    out_dir.mkdir(parents=True, exist_ok=True)
    shard = out_dir / lease["package"]
    retries = max(0, int(getattr(args, "retries", 0) or 0))
    if not args.no_download:
        err: DownloadError | None = None
        for attempt in range(retries + 1):
            try:
                shard = download(lease, out_dir, state, cfg)
                err = None
                break
            except DownloadError as e:
                err = e
                if attempt < retries:
                    nap = 3 * (attempt + 1)
                    log(f"[retry] 下载失败（{e}），{nap}s 后重试"
                        f"（{attempt + 1}/{retries}）")
                    time.sleep(nap)
            except FileNotFoundError:
                die(EXIT_USAGE, "找不到 curl，请先安装（apt install curl）")
        if err is not None:
            return False, f"下载失败：{err}"
    rc = run_task(lease, shard, args, state, out_dir)
    attempt = 0
    while rc != 0 and attempt < retries:
        attempt += 1
        nap = 3 * attempt
        log(f"[retry] 任务命令失败（退出码 {rc}），{nap}s 后重试（{attempt}/{retries}）")
        time.sleep(nap)
        rc = run_task(lease, shard, args, state, out_dir)
    if rc != 0:
        return False, f"任务命令失败（退出码 {rc}）"
    have = set(cfg.get("have") or [])
    have.add(f"{lease['repo']}/{lease['package']}")
    cfg["have"] = sorted(have)[-200:]
    save_cfg_soft(cfg)
    return True, "完成"


_RELEASED: set[str] = set()


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


def _claim(cfg: dict, parts: str | None = None) -> list[dict]:
    """握一次手，把派到手上的租约取回来。空列表 = 池子空。

    幂等续接（SPEC-declare §7）：服务端 assign 对同设备同 kind 的 open 租约
    直接原样返回，所以崩溃/Ctrl-C 后重启时，同一片会派回来——
    本地已校验通过的分片在 download() 里直接复用，不重复下载。

    parts 是分项意愿（'' = 不表态，服务端按"都做"算）。只在明确给出时上报：
    老服务端不认这个字段，但多一个键它也不该炸（dispatch 侧只管取值）。
    """
    # 顺带把这台机器已有的产出报上去，服务端会优先把同一片派回来；
    # hw（SPEC-link §1.1）是可选追加字段，收集失败降级为不报，不阻塞握手。
    body = {"client_ver": VERSION, "local_pkgs": cfg.get("have") or []}
    if parts:
        body["parts"] = parts
    hw = sanitize_hw(collect_hw(cfg))
    if hw is not None:
        body["hw"] = hw
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
    _announce_workspace(hs)
    return hs.get("leases") or []


# 上一次报过的工作区（JSON 串）。只在**变化**时打印：handshake 每轮都发，
# 每次都打一行"工作区：xxx"会把 wait 模式的日志刷成噪点。
_SHOWN_WS: str | None = "__init__"


def _announce_workspace(hs: dict) -> None:
    """把服务端给的工作区归属打出来——这是"为什么没派活"的第一个自查点。

    三种"派不到活"的原因必须分得清，否则用户只能干等：
      没归属（工作区 null）      → 正常，什么仓库都能拿
      工作区里没有仓库           → 管理员建了却忘了补 --repo
      工作区在服务端已被删除     → 归属还挂着，要重新指派或解绑
    最后两种的表现都是"一直空转"，日志里没有这句话就完全看不出区别。
    """
    global _SHOWN_WS
    if "workspace" not in hs:
        # 老服务端：没有工作区这个概念。不打印，免得显示成"无归属"误导人。
        return
    ws = hs.get("workspace")
    key = json.dumps(ws, ensure_ascii=False, sort_keys=True) if ws else None
    if key == _SHOWN_WS:
        return
    _SHOWN_WS = key
    if ws is None:
        log("[worker] 工作区：无归属（所有启用的仓库都能拿）")
        return
    ws_id = ws.get("ws_id") or "?"
    repos = ws.get("repos") or []
    if ws.get("missing"):
        log(f"[worker] 工作区 {ws_id} 在服务端已不存在——本机派不到任何包。"
            f"联系管理员重新指派，或用 server.py workspace unassign 解绑")
    elif not repos:
        log(f"[worker] 工作区 {ws_id} 里没有配置仓库——本机派不到任何包。"
            f"联系管理员用 workspace add 补 --repo")
    else:
        shown = " / ".join(repos[:3]) + (" …" if len(repos) > 3 else "")
        log(f"[worker] 工作区：{ws_id}（{len(repos)} 个仓库）{shown}")


def cmd_run(args) -> int:
    cfg = load_cfg()
    out_dir = Path(args.out) if args.out else Path.cwd() / "fox-work"

    # 本次已交付的片数。--max-packages 按它停手；计数用可变容器是为了
    # 让 _run_loop 能就地加，不用改它的返回值（那里已经有 6 种退出路径）。
    done = {"n": 0}
    last_work = time.time()
    # 连续失败计数：只在"任务/下载失败(6)"上累加，成功一次就清零。到上限才停手。
    consec_fail = 0
    max_fails = max(1, int(getattr(args, "max_fails", MAX_CONSEC_FAILS) or MAX_CONSEC_FAILS))

    parts_pref = str(getattr(args, "parts", None) or "").strip().lower()
    if parts_pref and parts_pref not in PARTS_CHOICES:
        die(EXIT_USAGE, f"--parts 只能是 {' / '.join(PARTS_CHOICES)}，收到 {parts_pref!r}")
    if parts_pref:
        log(f"[worker] 分项意愿：{parts_pref}")
    while True:
        leases = _claim(cfg, parts_pref)
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
        try:
            # 申报制（SPEC-declare）：打标期间零定时网络活动——Beater 已拆除，
            # 不发心跳、不轮询，服务端也不会自动收回租约。本地停止只剩 Ctrl-C。
            ok, detail = do_one(cfg, active, args, state)
        except KeyboardInterrupt:
            # SIGTERM/Ctrl-C 到达在 do_one 内部：把 active 也交给上层
            # cmd_run 的优雅收尾分支归还（挂到 held 里带上）。
            held.append(active)
            raise

        # finish 申报：done/failed 都必须显式申报（租约唯一的客户端关闭路径）。
        # done 带最终 progress（面板租约进度停在最后已知值，finish 一次性补齐）；
        # failed 带 reason 与最终 progress。
        final_progress = state.progress()
        try:
            res = declare_finish(cfg, active["lease_id"], ok, detail,
                                 final_progress, args.note)
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
    a.add_argument("--cmd", default=None,
                   help="干活的命令模板，可用 {shard} {package} {out} {lease}")
    a.add_argument("--cwd", default=None, help="命令的工作目录")
    a.add_argument("--note", default=None, help="提交时的备注（随 finish 申报带给服务端）")
    # 分项意愿（SPEC-parts §2）。四值枚举，跟"能力"是两件事：
    #   both / tag / nl        = 我**只**会做这几侧，包不含它就不接；
    #   pref-tag / pref-nl     = 我优先要那一侧，但另一侧也能做（兜底接）。
    # ★这个参数以前只写在调用方（tagger/web/center.py 会传），worker 自己
    #   却没定义——页面上选"只做 NL"启动时 worker 直接 argparse 报错退出，
    #   一个包都没派出去。参数被接受却到不了执行处是本项目复发率最高的一类 bug，
    #   这次是反过来：调用方给了，接收方根本没有这个口。
    a.add_argument("--parts", default=None, dest="parts", metavar="TIER",
                   help="分项意愿：both|tag|nl|pref-tag|pref-nl"
                        "（仅 X = 只接含 X 的片；优先 X = 优先接缺 X 的片，另一侧也做）")
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
