#!/usr/bin/env python3
"""fox-label v3 客户端：等派工、跑活、心跳续约。

和 v2 的 client.py 的区别
-------------------------
v2：自己去抢包（claim），抢不到 409。
v3：向服务端握手，**服务端派什么就干什么**；干的过程中心跳续约，
    租约一旦被服务端收回（超时/被踢），本地必须停手，产物保留。

身份
----
user_id 手填（打标同学自己填），device_id 首次运行自动生成并持久化，
secret 由服务端首次握手签发、本地保存；之后每次请求都要带。

一次完整循环
------------
    handshake ──► active(+prelock)
       │
       ├── 下载分片（断点续传 + sha256 校验）
       ├── 跑外部命令（tagger / bridge，由 --cmd 指定）
       │      └── 期间每 --interval 秒心跳：续约 + 报进度
       ├── 成功 → done（服务端把 prelock 升为 active 并补一个新的）
       └── 失败 → fail（包退回池子）

退出码
------
0 正常（含"池子空了"与 --once 完成）   2 用法/配置错误
3 认证失败（secret 不匹配）            4 下载或校验失败
5 租约已失效（被回收/被踢，需重新握手） 6 任务命令失败（fail 已上报）
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
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

VERSION = "3.0.0"
DEFAULT_SERVER = "http://127.0.0.1:31831"
CONFIG = Path(os.environ.get("FOXLABEL_WORKER_CONFIG")
              or Path.home() / ".config" / "fox-label" / "worker.json")

EXIT_OK, EXIT_USAGE, EXIT_AUTH, EXIT_DOWNLOAD, EXIT_LEASE, EXIT_TASK = 0, 2, 3, 4, 5, 6


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
            raise ApiError(e.code, _err(body_txt) or f"HTTP {e.code}")
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


class AuthError(Exception):
    pass


# ------------------------------------------------------------------ 登录

def cmd_login(args) -> int:
    cfg = {"server": args.server.rstrip("/"), "user_id": args.user_id}
    if CONFIG.exists():
        old = json.loads(CONFIG.read_text(encoding="utf-8"))
        if old.get("server", "").rstrip("/") == cfg["server"] and \
                old.get("user_id") == cfg["user_id"]:
            cfg.update({k: old[k] for k in ("secret", "device_id")
                        if old.get(k) and not cfg.get(k)})
    if getattr(args, "secret", None):
        cfg["secret"] = args.secret.strip()
    cfg.setdefault("device_id", uuid.uuid4().hex[:12])
    try:
        res = api(cfg, "/api/v1/handshake", {"client_ver": VERSION}, retries=0)
    except AuthError as e:
        die(EXIT_AUTH, f"认证失败：{e}\n"
                       f"    若是换机/重装：请管理员执行 "
                       f"`server.py reset-secret --user-id {cfg['user_id']}`，"
                       f"把新 secret 给你，然后：\n"
                       f"      worker.py login --server {cfg['server']} "
                       f"--user-id {cfg['user_id']} --secret <新secret>\n"
                       f"    若是填错了 user_id：换一个名字，或确认拼写。")
    except (ApiError, NetError) as e:
        die(EXIT_USAGE, f"连不上服务端 {cfg['server']}：{e}")
    if res.get("secret"):
        cfg["secret"] = res["secret"]
    cfg["device_id"] = res.get("device_id") or cfg["device_id"]
    cfg["server_id"] = res.get("server_id")
    cfg["beat_interval"] = res.get("beat_interval", 120)
    save_cfg(cfg)
    print(f"已保存配置：{CONFIG}")
    print(f"服务端 : {cfg['server']}（server_id={cfg.get('server_id')}）")
    print(f"身份   : {cfg['user_id']} / 设备 {cfg['device_id']}")
    if res.get("secret"):
        print("密钥   : 已签发并保存（600）")
    leases = res.get("leases") or []
    print(f"派工   : {len(leases)} 个" + (f"（{leases[0]['kind']} {leases[0]['package']}）"
                                         if leases else "（当前池子空）"))
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


# ------------------------------------------------------------------ 心跳

class RunState:
    def __init__(self) -> None:
        self.stop = threading.Event()          # 本地要停（租约丢了/收到停止）
        self.phase = "idle"
        self.done = 0
        self.total = 0
        self.reason = ""
        self.lock = threading.Lock()

    def progress(self) -> dict:
        with self.lock:
            return {"phase": self.phase, "done": self.done, "total": self.total}


class Beater(threading.Thread):
    """后台心跳：续约 + 报进度。失败只告警，绝不因此停工。"""

    def __init__(self, cfg: dict, interval: float, state: RunState,
                 lease_ids: list[str], active_id: str | None = None):
        super().__init__(daemon=True)
        self.cfg, self.interval, self.state = cfg, max(1.0, interval), state
        self.lease_ids = list(lease_ids)
        # 只有"正在处理的那一片"被收回才必须停手；预锁被收回只影响下一片
        self.active_id = active_id
        self.fails = 0
        self.extra = []

    def run(self) -> None:
        while True:
            if not self.lease_ids:
                return
            body = {"leases": [{"lease_id": lid, "progress": self.state.progress()}
                               for lid in self.lease_ids]}
            try:
                res = api(self.cfg, "/api/v1/heartbeat", body, retries=0)
                self.fails = 0
                rejected = res.get("rejected") or []
                for lid in rejected:
                    if lid == self.active_id:
                        log(f"租约 {lid} 已被服务端收回（超时/被踢），本地停手")
                        self.state.reason = "lease_lost"
                        self.state.stop.set()
                    elif lid in self.lease_ids:
                        log(f"[warn] 预锁 {lid} 已被收回，做完这片再要新的")
                        self.extra = []
                self.extra = res.get("leases") or self.extra
            except AuthError as e:
                # secret 失效（管理员重置/换机）：立即停，继续干也没有意义
                log(f"[X] 认证失效：{e}")
                self.state.reason = "auth_lost"
                self.state.stop.set()
            except (ApiError, NetError) as e:
                self.fails += 1
                # 网络抖动不影响本地继续干；连续失败才提示
                if self.fails in (1, 5, 15):
                    log(f"[warn] 心跳失败第 {self.fails} 次：{e}（本地继续，不影响作业）")
                if self.fails >= 5 and self.active_id:
                    # 连不上时**不要**擅自停：只做一次只读核对，
                    # 若服务端明确说"这类租约已经不存在"，才停。
                    try:
                        st = api(self.cfg, "/api/v1/status", {}, retries=0)
                        alive = {l["lease_id"] for l in st.get("leases") or []}
                        if self.active_id not in alive:
                            log("[X] 服务端已不认这个租约（可能已被回收），本地停手")
                            self.state.reason = "lease_lost"
                            self.state.stop.set()
                    except (ApiError, NetError):
                        pass            # 还是连不上，就继续干，靠 TTL 兜底
                    except AuthError:
                        self.state.reason = "auth_lost"
                        self.state.stop.set()
            if self.state.stop.wait(self.interval):
                return


# ------------------------------------------------------------------ 干活

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
    """处理一个租约：下载 → 跑活 → 返回是否成功。"""
    out_dir = Path(args.out) if args.out else Path.cwd() / "fox-work"
    out_dir.mkdir(parents=True, exist_ok=True)
    shard = out_dir / lease["package"]
    if not args.no_download:
        try:
            shard = download(lease, out_dir, state, cfg)
        except DownloadError as e:
            return False, f"下载失败：{e}"
        except FileNotFoundError:
            die(EXIT_USAGE, "找不到 curl，请先安装（apt install curl）")
    rc = run_task(lease, shard, args, state, out_dir)
    if state.stop.is_set() and state.reason == "lease_lost":
        return False, "租约已被收回"
    if rc != 0:
        return False, f"任务命令失败（退出码 {rc}）"
    have = set(cfg.get("have") or [])
    have.add(f"{lease['repo']}/{lease['package']}")
    cfg["have"] = sorted(have)[-200:]
    save_cfg(cfg)
    return True, "完成"


_RELEASED: set[str] = set()


def release_unused(cfg: dict, leases: list[dict], keep: set[str] | None = None) -> None:
    """把手上用不到的租约还给服务端（收工/中断时调用）。

    不归还的话，服务端在 done 之后自动派的那一片会闲置到 TTL 到期，
    别的设备这段时间领不到它。
    """
    keep = keep or set()
    for lease in leases:
        lid = lease["lease_id"]
        if lid in keep or lid in _RELEASED:
            continue
        try:
            api(cfg, "/api/v1/release", {"lease_id": lid}, retries=0)
            _RELEASED.add(lid)
            log(f"已归还租约 {lease['kind']}:{lease['package']}")
        except ApiError as e:
            if e.code == 409:          # 已经归还/已被回收：幂等处理，不告警
                _RELEASED.add(lid)
                continue
            log(f"[warn] 归还租约失败（不影响已交付的数据）：{e}")
        except (NetError, AuthError) as e:
            log(f"[warn] 归还租约失败（不影响已交付的数据）：{e}")


def cmd_run(args) -> int:
    cfg = load_cfg()
    interval = args.interval or float(cfg.get("beat_interval") or 120)
    out_dir = Path(args.out) if args.out else Path.cwd() / "fox-work"

    # 握手：顺带把这台机器已有的产出报上去，服务端会优先把同一片派回来
    try:
        hs = api(cfg, "/api/v1/handshake",
                 {"client_ver": VERSION, "local_pkgs": cfg.get("have") or []}, retries=2)
    except AuthError as e:
        die(EXIT_AUTH, f"认证失败：{e}")
    except (ApiError, NetError) as e:
        die(EXIT_USAGE, f"握手失败：{e}")
    if hs.get("secret") and hs["secret"] != cfg.get("secret"):
        cfg["secret"] = hs["secret"]
        save_cfg(cfg)
    log(f"身份 {cfg['user_id']} / 设备 {cfg['device_id']}　"
        f"服务端 {cfg.get('server_id')}　心跳 {interval:.0f}s")

    leases = hs.get("leases") or []
    if not leases:
        log("服务端暂无派工（池子空了或配额已满）")
        return EXIT_OK
    log("派工：" + "、".join(f"{l['kind']}:{l['package']}" for l in leases))

    held: list[dict] = list(leases)
    try:
        return _run_loop(cfg, args, interval, out_dir, leases, held)
    finally:
        release_unused(cfg, held)


def _run_loop(cfg: dict, args, interval: float, out_dir: Path,
              leases: list[dict], held: list[dict]) -> int:
    while leases:
        active = next((l for l in leases if l["kind"] == "active"), None)
        if active is None:
            log("手上没有 active，结束")
            break
        state = RunState()
        # 心跳覆盖"手上全部租约"：prelock 也要续，否则它会在干活期间超时失效
        lease_ids = [l["lease_id"] for l in leases]
        held[:] = [l for l in leases if l["lease_id"] != active["lease_id"]]
        beater = Beater(cfg, interval, state, lease_ids, active_id=active["lease_id"])
        beater.start()
        ok, detail = do_one(cfg, active, args, state)
        state.stop.set()
        beater.join(timeout=3)

        if state.reason == "auth_lost":
            die(EXIT_AUTH, f"认证失效：{detail}（产物保留在 {out_dir}）")
        if state.reason == "lease_lost":
            release_unused(cfg, leases, keep={active["lease_id"]})
            log(f"[X] {detail}：产物保留在 {out_dir}，重新运行会自动续跑")
            return EXIT_LEASE
        try:
            res = api(cfg, "/api/v1/done" if ok else "/api/v1/fail",
                      {"lease_id": active["lease_id"],
                       **({"stats": args.note} if ok and args.note else {}),
                       **({} if ok else {"reason": detail})})
        except AuthError as e:
            die(EXIT_AUTH, f"认证失败：{e}")
        except ApiError as e:
            if e.code == 409:
                log(f"[X] {e.msg}（多半是租约已被回收，重新运行即可）")
                return EXIT_LEASE
            die(EXIT_USAGE, f"上报失败：{e}")
        except NetError as e:
            log(f"[X] 上报失败：{e}（服务端不可达，产物保留在 {out_dir}）")
            return EXIT_USAGE

        if not ok:
            log(f"[X] 本次失败：{detail}")
            return EXIT_TASK
        log(f"已交付 {active['package']}")
        if args.once:
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

    a = sub.add_parser("login", help="首次登录：换取 secret 并保存")
    a.add_argument("--server", default=None)
    a.add_argument("--user-id", default=None)
    a.add_argument("--secret", default=None,
                   help="管理员重签后的 secret（换机/重装时用）")
    a.set_defaults(func=cmd_login)

    a = sub.add_parser("show", help="看本地身份与当前派工")
    a.set_defaults(func=cmd_show)

    a = sub.add_parser("run", help="等派工并干活（心跳续约）")
    a.add_argument("--out", default=None, help="工作目录（默认 ./fox-work）")
    a.add_argument("--interval", type=float, default=None, help="心跳间隔秒（默认取服务端建议值）")
    a.add_argument("--once", action="store_true", help="只处理一个包就退出")
    a.add_argument("--no-download", action="store_true", help="跳过下载（协调层演练）")
    a.add_argument("--cmd", default=None,
                   help="干活的命令模板，可用 {shard} {package} {out} {lease}")
    a.add_argument("--cwd", default=None, help="命令的工作目录")
    a.add_argument("--note", default=None, help="提交时的备注")
    a.set_defaults(func=cmd_run)

    args = ap.parse_args()
    if args.cmd == "login" and not (args.server and args.user_id):
        die(EXIT_USAGE, "login 需要 --server 和 --user-id")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n收到中断，已停止（未提交的产物保留在本地）")
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
