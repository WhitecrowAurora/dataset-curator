#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fox-label 客户端 CLI —— 多仓库打标包认领、下载、提交（纯标准库，单文件分发）。

子命令
------
  login     保存服务端地址与密钥（会先校验）
  repos     列出你有权访问的仓库
  list      查看包列表（--repo 过滤，--watch 实时跟随）
  claim     认领包（支持 repo/包名 或 --repo）
  release   放弃自己认领的包
  submit    把已认领的包标记为已完成
  my        查看我认领的包
  download  用你自己的 HF token 下载包（续传 + sha256 校验，自动顺带 manifest）

包引用写法
----------
  shard-000001.tar                      单仓库时可省略仓库名（多仓库同名会报错提示）
  MatchaCuratio/Anime-Fox768-400k/shard-000001.tar   显式指定仓库

运行环境：Python 3.9+，无需第三方库；下载需要系统有 curl（Win10+ 自带 curl.exe）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

CONFIG_PATH = Path(os.path.expanduser("~/.config/fox-label/config.json"))

STATUS_LABEL = {"available": "未认领", "claimed": "已认领", "completed": "已完成"}
STATUS_HINT = {"未认领": "available", "已认领": "claimed", "已完成": "completed"}
HF_BASE = os.environ.get("FOXLABEL_HF_ENDPOINT", "https://huggingface.co").rstrip("/")


# ---------------------------------------------------------------- 工具

def pad(text, width: int) -> str:
    text = str(text)
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(0, width - w)


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def fit(text, width: int) -> str:
    """按显示宽度截断并补齐（长仓库名不撑破表格）。"""
    text = str(text)
    if sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text) <= width:
        return pad(text, width)
    out, w = [], 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > width - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "~" + " " * (width - w - 1)


def split_ref(ref: str) -> tuple[str | None, str]:
    """'repo/ns/name/shard.tar' -> ('repo/ns/name', 'shard.tar')；无 '/' 时仓库为 None。"""
    if "/" not in ref:
        return None, ref
    repo, pkg = ref.rsplit("/", 1)
    return repo, pkg


def short_repo(repo: str) -> str:
    """表格里只显示仓库名最后两段，避免长名撑爆列宽。"""
    parts = repo.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else repo


# ---------------------------------------------------------------- 配置

class ConfigError(Exception):
    pass


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise ConfigError("尚未登录，请先运行：python client.py login --server <地址> --key <密钥>")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("server") or not cfg.get("key"):
        raise ConfigError("配置不完整，请重新 login")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- HTTP

def api(cfg: dict, path: str, method: str = "GET", payload: dict | None = None):
    url = cfg["server"].rstrip("/") + path
    data = None
    headers = {"Authorization": f"Bearer {cfg['key']}"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except ValueError:
            return e.code, {"error": body[:200]}
    except URLError as e:
        raise SystemExit(f"连不上服务端 {cfg['server']}：{e.reason}")


def qs(**kw) -> str:
    pairs = [f"{k}={quote(str(v), safe='')}" for k, v in kw.items() if v]
    return ("?" + "&".join(pairs)) if pairs else ""


def die(code: int, obj: dict) -> None:
    msg = obj.get("error") or obj
    if code == 401:
        print("认证失败：密钥无效或已被吊销，请联系管理员。", file=sys.stderr)
    elif code == 403:
        print(f"无权限：{msg}", file=sys.stderr)
    elif code == 409:
        print(f"冲突：{msg}", file=sys.stderr)
    else:
        print(f"错误（HTTP {code}）：{msg}", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------- 展示

def render(rows: list[dict], summary: dict | None = None, title: str | None = None,
           show_repo: bool = True) -> str:
    out = []
    if title:
        out.append(title)
    if summary:
        out.append(f"总计 {summary['total']} 个包 / {human_size(summary['bytes_total'])}   "
                   f"未认领 {summary['available']} · 已认领 {summary['claimed']} · "
                   f"已完成 {summary['completed']}")
    out.append("")
    head = (pad("仓库", 34) if show_repo else "") + pad("包", 20) + pad("大小", 10) \
        + pad("状态", 9) + pad("认领人", 12) + "更新时间"
    out.append(head)
    out.append("-" * (sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in head) - 2))
    for r in rows:
        out.append((fit(r["repo"], 34) if show_repo else "")
                   + fit(r["id"], 20) + pad(human_size(r.get("size")), 10)
                   + pad(r.get("status_label") or r.get("status"), 9)
                   + pad(r.get("owner") or "-", 12)
                   + (r.get("updated_at") or "-"))
    return "\n".join(out)


# ---------------------------------------------------------------- 子命令

def cmd_login(args) -> int:
    cfg = {"server": args.server, "key": args.key.strip()}
    code, obj = api(cfg, "/me")
    if code != 200:
        die(code, obj)
    cfg["name"] = obj.get("name")
    save_config(cfg)
    print(f"已保存配置到 {CONFIG_PATH}")
    print(f"服务端: {cfg['server']}")
    print(f"身份  : {cfg['name']}（{obj.get('role')}）")
    grants = obj.get("grants")
    print(f"授权  : {'、'.join(grants) if grants else '全部仓库'}")
    return 0


def cmd_repos(args) -> int:
    cfg = load_config()
    code, obj = api(cfg, "/repos")
    if code != 200:
        die(code, obj)
    rows = obj.get("repos") or []
    if not rows:
        print("没有可访问的仓库")
        return 0
    print(pad("仓库", 44) + pad("类型", 9) + pad("包数", 6) + pad("未认领", 8)
          + pad("已认领", 8) + pad("已完成", 8) + "公共清单")
    print("-" * 104)
    for r in rows:
        s = r.get("summary") or {}
        print(fit(r["repo"], 44) + pad(r.get("repo_type", "dataset"), 9)
              + pad(s.get("total", 0), 6) + pad(s.get("available", 0), 8)
              + pad(s.get("claimed", 0), 8) + pad(s.get("completed", 0), 8)
              + (r.get("manifest") or "-"))
    return 0


def cmd_list(args) -> int:
    cfg = load_config()
    code, obj = api(cfg, "/packages" + qs(repo=args.repo))
    if code != 200:
        die(code, obj)
    rows = obj["packages"]
    if args.status:
        want = STATUS_HINT.get(args.status, args.status)
        rows = [r for r in rows if r["status"] == want]
    multi = len({r["repo"] for r in rows}) > 1
    print(render(rows, obj.get("summary"), show_repo=True if multi or args.repo is None else False))

    if not args.watch:
        return 0

    print("\n[watch] 正在跟随状态变化，Ctrl-C 退出…\n")
    known = {(r["repo"], r["id"]): r["status"] for r in obj["packages"]}
    base_url = cfg["server"].rstrip("/") + "/events"
    last_seq = 0
    attempts = 0

    def handle_event(ev: dict) -> None:
        nonlocal last_seq
        seq = ev.get("seq")
        if isinstance(seq, int):
            last_seq = max(last_seq, seq)
        kind = ev.get("kind")
        if kind == "cursor":
            return                      # 服务端下发的游标，仅用于断点续传
        if kind == "error":
            print(f"[watch] 服务端提示：{ev.get('detail')}", file=sys.stderr)
            return
        code, payload = api(cfg, "/packages" + qs(repo=args.repo))
        if code != 200:
            print(f"[watch] 拉取状态失败 HTTP {code}，稍后重试", file=sys.stderr)
            return
        changed = []
        for r in payload["packages"]:
            k = (r["repo"], r["id"])
            if known.get(k) != r["status"]:
                known[k] = r["status"]
                changed.append(r)
        if not changed:
            return
        stamp = ev.get("ts", "")
        for r in changed:
            print(f"[{stamp}] {short_repo(r['repo'])}/{r['id']:<18} -> {r.get('status_label')}"
                  f"{'（' + r['owner'] + '）' if r.get('owner') else ''}", flush=True)
        sm = payload["summary"]
        print(f"   汇总: 未认领 {sm['available']} · 已认领 {sm['claimed']} · "
              f"已完成 {sm['completed']}\n", flush=True)

    while True:
        url = base_url + (f"?since={last_seq}" if last_seq else "")
        req = Request(url, headers={"Authorization": f"Bearer {cfg['key']}"})
        try:
            with urlopen(req, timeout=None) as resp:
                print("[watch] 已连接，等待变更…", flush=True)
                attempts = 0
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    handle_event(ev)
        except KeyboardInterrupt:
            return 0
        except (BrokenPipeError, ConnectionResetError):
            pass
        except URLError as e:
            print(f"[watch] 连接失败：{e.reason}", file=sys.stderr)
        attempts += 1
        if attempts > 5:
            print("[watch] 连续重连失败，退出", file=sys.stderr)
            return 1
        time.sleep(min(2 ** attempts, 10))


def _act(cfg, ref: str, repo_opt: str | None, action: str, payload: dict | None = None) -> tuple[int, dict]:
    repo, pkg = split_ref(ref)
    repo = repo_opt or repo
    return api(cfg, f"/packages/{quote(pkg, safe='')}/{action}" + qs(repo=repo), "POST", payload or {})


def cmd_claim(args) -> int:
    cfg = load_config()
    rc = 0
    for ref in args.packages:
        code, obj = _act(cfg, ref, args.repo, "claim")
        if code == 200:
            print(f"[OK] 已认领 {obj.get('repo')}/{obj.get('id')}")
        else:
            tag = {401: "认证失败（密钥无效/被吊销）", 403: "无权限",
                   404: "不存在", 409: "冲突"}.get(code, f"HTTP {code}")
            print(f"[X] {ref}: {tag} — {obj.get('error') or obj}", file=sys.stderr)
            rc = 1
    return rc


def cmd_release(args) -> int:
    cfg = load_config()
    code, obj = _act(cfg, args.package, args.repo, "release",
                     {"reason": args.reason} if args.reason else {})
    if code != 200:
        die(code, obj)
    print(f"[OK] 已释放 {obj.get('repo')}/{obj.get('id')}（回到未认领）")
    return 0


def cmd_submit(args) -> int:
    cfg = load_config()
    code, obj = _act(cfg, args.package, args.repo, "submit",
                     {"note": args.note} if args.note else {})
    if code != 200:
        die(code, obj)
    print(f"[OK] 已提交 {obj.get('repo')}/{obj.get('id')}，状态 -> 已完成（{obj.get('completed_at')}）")
    return 0


def cmd_my(args) -> int:
    cfg = load_config()
    code, obj = api(cfg, "/me")
    if code != 200:
        die(code, obj)
    rows = obj.get("packages") or []
    print(f"我是 {obj.get('name')}（{obj.get('role')}），共认领 {len(rows)} 个包")
    print()
    print(render(rows, show_repo=True))
    return 0


# ---------------------------------------------------------------- 下载

def hf_token() -> str | None:
    """返回 token；没有则返回 None（公开仓库可匿名下载）。"""
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if tok:
        return tok.strip()
    f = Path(os.path.expanduser("~/.cache/huggingface/token"))
    if f.is_file():
        return f.read_text(encoding="utf-8").strip() or None
    return None


def sha256_file(path: Path, chunk: int = 4 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


_curl_ver: tuple[int, int] | None = None


def _curl_supports_header_file() -> bool:
    """curl >= 7.55 支持 `-H @文件`，可避免 token 出现在进程命令行（ps 可见）。"""
    global _curl_ver
    if _curl_ver is None:
        try:
            out = subprocess.run(["curl", "--version"], capture_output=True, text=True,
                                 timeout=5).stdout
            m = re.search(r"curl (\d+)\.(\d+)", out)
            _curl_ver = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        except Exception:
            _curl_ver = (0, 0)
    return _curl_ver >= (7, 55)


def _auth_args(tok: str) -> tuple[list[str], str | None]:
    """返回 (curl 参数, 临时头部文件路径)。优先用头部文件，避免 token 进 ps。"""
    if _curl_supports_header_file():
        fd, path = tempfile.mkstemp(prefix="foxlabel-hdr-")
        try:
            os.write(fd, f"Authorization: Bearer {tok}\n".encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return ["-H", f"@{path}"], path
    return ["-H", f"Authorization: Bearer {tok}"], None


def curl_download(url: str, dest: Path, sha256: str | None, size: int | None) -> bool:
    if dest.exists():
        if sha256 and sha256_file(dest) == sha256:
            print(f"[OK] {dest.name} 已存在且校验通过，跳过")
            return True
        if not sha256 and size and dest.stat().st_size == size:
            print(f"[OK] {dest.name} 已存在且大小正确，跳过")
            return True

    if shutil.which("curl") is None:
        raise SystemExit("需要 curl 用于断点续传下载（Win10+ 自带 curl.exe）")

    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[DL] {dest.name} {human_size(size)} -> {dest}")
    cmd = [
        "curl", "-L", "-C", "-", "--fail",
        "--retry", "5", "--retry-delay", "3", "--retry-all-errors",
        "--speed-limit", "50000", "--speed-time", "60",
        "--connect-timeout", "20",
        "--progress-bar" if sys.stderr.isatty() else "--no-progress-meter",
    ]
    tok = hf_token()
    hdr_file = None
    if tok:
        extra, hdr_file = _auth_args(tok)
        cmd += extra
    cmd += ["-o", str(dest), url]

    rc = 1
    try:
        for attempt in range(1, 4):
            rc = subprocess.call(cmd)
            if rc == 0:
                break
            print(f"  [retry] 第 {attempt} 次中断（退出码 {rc}），2 秒后续传…", file=sys.stderr)
            time.sleep(2)
    finally:
        if hdr_file:
            try:
                os.unlink(hdr_file)
            except OSError:
                pass
    if rc != 0:
        print("[X] 下载失败；重新执行本命令会从断点继续", file=sys.stderr)
        return False

    if sha256:
        got = sha256_file(dest)
        if got != sha256:
            print(f"[X] 校验失败！\n  期望 {sha256}\n  实际 {got}\n  已删除损坏文件，请重下。",
                  file=sys.stderr)
            dest.unlink(missing_ok=True)
            return False
        print(f"[OK] {dest.name} 下载完成，sha256 校验通过")
    else:
        print(f"[OK] {dest.name} 下载完成（无校验值，跳过校验）")
    return True


def safe_name(name: str) -> str:
    """服务端/仓库返回的名字不可信：只保留 basename，拒绝 .. 与空名。"""
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    if base in ("", ".", ".."):
        raise SystemExit(f"拒绝可疑文件名：{name!r}")
    return base


def hf_url(repo: str, repo_type: str, path: str) -> str:
    """HF_ENDPOINT 可指向镜像站（如 https://hf-mirror.com）或测试用端点。"""
    base = os.environ.get("HF_ENDPOINT", HF_BASE).rstrip("/")
    prefix = "/datasets" if repo_type == "dataset" else ""
    return f"{base}{prefix}/{repo}/resolve/main/{path}"


def cmd_download(args) -> int:
    cfg = load_config()
    code, obj = api(cfg, "/packages" + qs(repo=args.repo))
    if code != 200:
        die(code, obj)
    repos_meta = {r["repo"]: r for r in (obj.get("repos") or [])}
    by_key = {(p["repo"], p["id"]): p for p in obj["packages"]}

    targets: list[tuple[str, str]] = []
    for ref in args.packages:
        repo, pkg = split_ref(ref)
        repo = repo or args.repo
        if not repo:
            cands = [k for k in by_key if k[1] == pkg]
            if len(cands) != 1:
                print(f"[X] {ref}: 无法确定仓库（候选 {len(cands)} 个），请用 repo/包名 或 --repo",
                      file=sys.stderr)
                return 1
            repo = cands[0][0]
        targets.append((repo, pkg))
    if args.mine:
        me = api(cfg, "/me")[1]
        targets += [(r["repo"], r["id"]) for r in (me.get("packages") or [])]

    outdir = Path(args.out).expanduser().resolve()

    # 多仓库时按仓库分子目录，避免同名文件（如两边的 manifest.jsonl）互相覆盖
    used_repos = sorted({r for r, _ in targets}) or ([args.repo] if args.repo else list(repos_meta))
    multi = len(used_repos) > 1

    def dest_for(repo: str, name: str) -> Path:
        if multi:
            # 仓库名同样不可信：去掉 .. 段并摊平
            safe_repo = repo.replace("\\", "/").replace("/", "__").replace("..", "_")
            base = outdir / safe_repo
        else:
            base = outdir
        return base / safe_name(name)

    # 1) 涉及到的仓库，各自先取公共清单
    if not args.no_manifest:
        for repo in used_repos:
            meta = repos_meta.get(repo)
            if not meta or not meta.get("manifest"):
                continue
            ok = curl_download(hf_url(repo, meta.get("repo_type", "dataset"), meta["manifest"]),
                               dest_for(repo, meta["manifest"]),
                               meta.get("manifest_sha256") or None,
                               meta.get("manifest_size") or None)
            if not ok:
                return 1

    if not targets:
        if args.no_manifest:
            raise SystemExit("请指定包名，或用 --mine 下载我认领的全部包")
        return 0

    # 2) 逐个下载
    ok = True
    for repo, pkg_id in targets:
        pkg = by_key.get((repo, pkg_id))
        if pkg is None:
            print(f"[X] {repo}/{pkg_id}: 服务端没有这个包", file=sys.stderr)
            ok = False
            continue
        if pkg["status"] == "completed" and pkg.get("owner") == cfg.get("name"):
            print(f"[skip] {repo}/{pkg_id} 已完成，无需下载")
            continue
        if pkg["status"] != "claimed" or pkg.get("owner") != cfg.get("name"):
            if not args.force:
                print(f"[X] {repo}/{pkg_id}: 状态 {pkg.get('status_label')}"
                      f"{'，认领人 ' + pkg['owner'] if pkg.get('owner') else ''}"
                      f" —— 不是你认领的包（加 --force 强制下载）", file=sys.stderr)
                ok = False
                continue
        meta = repos_meta.get(repo) or {}
        got = curl_download(hf_url(repo, meta.get("repo_type", "dataset"), pkg_id),
                            dest_for(repo, pkg_id), pkg.get("sha256"), pkg.get("size"))
        ok = ok and got
        if got:
            c2, _ = api(cfg, f"/packages/{quote(pkg_id, safe='')}/downloaded" + qs(repo=repo), "POST")
            if c2 != 200:
                print(f"  [warn] 已下载标记上报失败（HTTP {c2}）", file=sys.stderr)
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="client.py", description="fox-label 客户端（多仓库）")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("login", help="保存服务端地址与密钥")
    a.add_argument("--server", required=True, help="如 http://192.168.5.185:31831")
    a.add_argument("--key", required=True)
    a.set_defaults(func=cmd_login)

    a = sub.add_parser("repos", help="列出可访问的仓库")
    a.set_defaults(func=cmd_repos)

    a = sub.add_parser("list", help="列出包")
    a.add_argument("--repo", default=None, help="只看某个仓库")
    a.add_argument("--status", default=None, help="筛选：未认领/已认领/已完成")
    a.add_argument("--watch", action="store_true")
    a.set_defaults(func=cmd_list)

    a = sub.add_parser("claim", help="认领包")
    a.add_argument("packages", nargs="+", help="包名，或 repo/包名")
    a.add_argument("--repo", default=None)
    a.set_defaults(func=cmd_claim)

    a = sub.add_parser("release", help="放弃认领")
    a.add_argument("package")
    a.add_argument("--repo", default=None)
    a.add_argument("--reason", default=None)
    a.set_defaults(func=cmd_release)

    a = sub.add_parser("submit", help="标记完成")
    a.add_argument("package")
    a.add_argument("--repo", default=None)
    a.add_argument("--note", default=None)
    a.set_defaults(func=cmd_submit)

    a = sub.add_parser("my", help="我的包")
    a.set_defaults(func=cmd_my)

    a = sub.add_parser("download", help="下载包（续传+校验）")
    a.add_argument("packages", nargs="*", help="包名，或 repo/包名")
    a.add_argument("--repo", default=None)
    a.add_argument("--mine", action="store_true", help="下载我认领的全部包")
    a.add_argument("--out", default="./data")
    a.add_argument("--force", action="store_true")
    a.add_argument("--no-manifest", action="store_true")
    a.set_defaults(func=cmd_download)

    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
