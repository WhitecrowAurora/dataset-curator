#!/usr/bin/env python3
"""fox-label ⇄ tagger 联动桥。

把「认领分片 → 下载校验 → 本地打标 → 产物校验 → 提交完成」串成一条命令，
让打标成员不需要在 fox-label 与 tagger 两个工具之间手工搬状态。

设计边界
--------
* fox-label 负责**协调**：谁可以拿哪个分片、是否已下载、是否已完成、审计留痕。
* tagger 负责**干活**：怎么解码、怎么调 GPU、产出什么文件（本项目不复制它的逻辑）。
* 本桥只做三件事：调用两边的既有接口、校验接口之间的不变量、失败时不误提交。

关键不变量
----------
1. 只有**认领人本人**能提交；本桥在动手前先确认 owner，不做「先干完再发现没权限」。
2. 下载完成后分片的 sha256 必须等于服务端登记的 sha256（HF 的 lfs.oid 就是内容哈希），
   否则不进入打标阶段——一个分片要烧十几 GPU 小时，哈希不对必须当场拦。
3. 打标失败、或产物为 0 条，**绝不提交**；包保持「已认领」，成员可安全重跑。
4. 重复执行是安全的：分片按哈希跳过、tagger 自带的逐图续跑会跳过已完成产物。

用法
----
    python bridge.py run  --package org/repo/shard-000003.tar --claim --limit 20
    python bridge.py run  --package shard-000003.tar --repo org/repo --local-shard ./shard.tar
    python bridge.py next                # 我能干/在干的包
    python bridge.py run --package ... --dry-run     # 只做认领+下载+校验，不打标
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import client as fox  # noqa: E402  (复用既有客户端：api / 下载 / 哈希 / 配置)

STATE_FILE = ".foxlink.json"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_OWNER = 3
EXIT_CHECKSUM = 4
EXIT_TAGGER = 5


# ------------------------------------------------------------------ 小工具

def human(n) -> str:
    return fox.human_size(n or 0)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def say(msg: str) -> None:
    print(msg, flush=True)


def die(code: int, msg: str):
    """按 README 承诺的退出码退出（0/2/3/4/5），别一律 SystemExit(1)。"""
    say(msg)
    raise SystemExit(code)


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------ 服务端

def find_package(cfg: dict, pkg_id: str | None, repo: str | None) -> dict:
    """在服务端定位一个包（走单包端点，不依赖分页）。"""
    if pkg_id is None:
        raise SystemExit("需要 --package 指定包名")
    path = f"/packages/{fox.quote(pkg_id, safe='')}"
    if repo:
        path += fox.qs(repo=repo)
    code, obj = fox.api(cfg, path)
    if code == 200:
        return obj
    if code == 409:                      # 同名包存在于多个仓库
        raise SystemExit(f"{obj.get('error')}\n      请用 --repo 指定仓库")
    if code == 404:
        raise SystemExit(f"找不到包 {pkg_id}" + (f"（仓库 {repo}）" if repo else ""))
    raise SystemExit(f"查询包失败：HTTP {code} {obj.get('error')}")


def ensure_owner(cfg: dict, pkg: dict, do_claim: bool) -> dict:
    """确保这个包由我持有。返回最新的 package dict。"""
    me = cfg.get("name")
    status, owner = pkg.get("status"), pkg.get("owner")
    ref = f"{pkg['repo']}/{pkg['id']}"

    if status == "completed":
        if owner == me:
            say(f"[skip] {ref} 已是「已完成」，无需重复处理")
            raise SystemExit(EXIT_OK)
        die(EXIT_NOT_OWNER, f"[X] {ref} 已由 {owner} 完成")

    if status == "claimed":
        if owner != me:
            die(EXIT_NOT_OWNER, f"[X] {ref} 已被 {owner} 认领，不能抢占（如需协调请联系管理员）")
        say(f"[ok] {ref} 已由我认领")
        return pkg

    # available
    if not do_claim:
        die(EXIT_NOT_OWNER, f"[X] {ref} 尚未认领；加 --claim 会自动认领，或先执行 client.py claim")
    code, obj = fox.api(cfg, f"/packages/{fox.quote(pkg['id'], safe='')}/claim"
                             + fox.qs(repo=pkg["repo"]), "POST", {})
    if code != 200:
        die(EXIT_USAGE, f"[X] 认领失败：HTTP {code} {obj.get('error')}")
    say(f"[ok] 已认领 {ref}")
    return find_package(cfg, pkg["id"], pkg["repo"])


# ------------------------------------------------------------------ 下载

def free_gb(path: Path) -> float:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 2**30


def download_shard(cfg: dict, pkg: dict, cache_dir: Path, local: Path | None,
                   force: bool = False) -> Path:
    """把分片放到本地并对齐服务端登记的 sha256。返回本地路径。"""
    # 按仓库分目录：不同仓库可能有同名 shard-000000.tar
    dest = cache_dir / pkg["repo"].replace("/", "__") / fox.safe_name(pkg["id"])
    want = pkg.get("sha256")
    size = pkg.get("size") or 0

    if local:
        if not local.is_file():
            raise SystemExit(f"[X] --local-shard 指向的文件不存在：{local}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if local.resolve() == dest.resolve():
            pass
        elif dest.is_file() and want and fox.sha256_file(dest) == want \
                and dest.stat().st_size == local.stat().st_size:
            say(f"[ok] 缓存里已有同一分片，跳过拷贝（{dest.name}）")
        else:
            say(f"[cp] 使用本地分片 {local} → {dest}")
            shutil.copy2(local, dest)
    else:
        need = size / 2**30 * 1.15 + 1.0
        if free_gb(cache_dir) < need:
            raise SystemExit(f"[X] 磁盘空间不足：需要约 {need:.1f} GiB，"
                             f"当前可用 {free_gb(cache_dir):.1f} GiB")
        url = fox.hf_url(pkg["repo"], pkg.get("repo_type", "dataset"), pkg["id"])
        if not fox.curl_download(url, dest, want, size):
            raise SystemExit("[X] 下载失败；重新执行本命令会断点续传")

    if want:
        got = fox.sha256_file(dest)
        if got != want:
            if force:
                if local is not None:
                    # 本地源本身就是坏的：删掉缓存只会拷回同一个坏文件，无限失败
                    die(EXIT_CHECKSUM, f"[X] 本地分片本身哈希不符，删缓存也救不了：{local}\n"
                                       f"    服务端 {want[:16]}…  本地 {got[:16]}…\n"
                                       f"    请重新获取该分片（或去掉 --local-shard/--no-download 走下载）")
                say(f"[warn] 哈希不符且指定了 --force-download，删除后重下：{dest.name}")
                dest.unlink(missing_ok=True)
                return download_shard(cfg, pkg, cache_dir, None, force=False)
            # 内容不对：不删（保留现场证据），但拒绝继续，避免拿错素材烧 GPU 时间
            die(EXIT_CHECKSUM,
                f"[X] 校验失败：{dest.name}\n"
                f"    服务端 sha256 {want[:16]}…\n"
                f"    本地   sha256 {got[:16]}…\n"
                f"    加 --force-download 可删除本地文件重新拉取")
    if want:
        say(f"[ok] 分片就绪 {dest.name}（{human(size)}，sha256 已对齐）")
    else:
        say(f"[warn] 分片就绪 {dest.name}（{human(size)}），但服务端没登记 sha256，"
            f"本次无法校验内容")
    return dest


def mark_downloaded(cfg: dict, pkg: dict) -> None:
    code, obj = fox.api(cfg, f"/packages/{fox.quote(pkg['id'], safe='')}/downloaded"
                             + fox.qs(repo=pkg["repo"]), "POST")
    if code == 200:
        say("[ok] 已向服务端登记「已下载」")
    else:
        say(f"[warn] 登记已下载失败：HTTP {code} {obj.get('error')}")


# ------------------------------------------------------------------ 打标

def resolve_tagger_python(root: Path, given: str | None) -> list[str]:
    """决定用哪个解释器跑 tagger（优先项目内 venv）。"""
    if given:
        return [given]
    for cand in (root / "env" / "linux" / "bin" / "python",
                 root / ".venv" / "bin" / "python"):
        if cand.is_file():
            return [str(cand)]
    return [sys.executable]


def scan_failure_books(out_dir: Path) -> dict:
    """读 tagger 的两本账，回答"这一片到底还欠几件事"。

    只数 id，不去重犯"同一 id 多行"的错：failures.jsonl 是追加写的，一张图
    失败三次就有三行，直接把行数当失败数会虚报三倍，而这个数会进服务端 note
    和状态机——虚报的数字比没有数字更糟。

    ★两本账不是"互斥的两拨图"，而是**同一批图的先后状态**：
      - failures.jsonl 是追加账：第一次失败时写进去，之后重试成功、或被剔除，
        那行都**不会删**（client.commit 只在失败分支写它，成功分支只写产物）。
        所以"在 failures 里"不等于"还欠着"，把全集当待办会虚报一堆已解决的图。
      - excused.jsonl 是终态账：重试仍失败、已从分母剔除的 id。人工删掉某行
        就等于让它回到"可重试"。
      因此真正还欠着的 = failures - excused。

      ★反过来 excused 绝不能减掉 failures：被剔除的 id 几乎必然同时躺在
        failures 里（它正是上一轮失败才被捞出来重试的），减完永远是 0，
        "重试后仍有 X 张失败，已从分母剔除"这句就再也写不进服务端 note，
        管理员看到"已完成但张数少了"却查不到原因——那正是设这个上报的原因。
    """
    def states(path: Path) -> tuple[set[str], set[str]]:
        """(当前仍失败/仍剔除的 id, 被冲销掉的 id)，按同一 id 最后一条为准。"""
        live: set[str] = set()
        resolved: set[str] = set()
        if not path.is_file():
            return live, resolved
        try:
            for line in path.open("r", encoding="utf-8", errors="replace"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rec = rec or {}
                iid = str(rec.get("image_id") or "")
                if not iid:
                    continue
                # 冲销行：这张重试成功/被跳过了。failures.jsonl 是追加账，旧
                # 失败行不删，所以按"同一 id 最后一条为准"——最后一条是冲销
                # 就把它移出待办。不认这个字段会把已解决的图全算成未解决。
                # 两个集合互斥：后来又失败一次就回到 live，不能留在 resolved 里。
                if rec.get("resolved"):
                    live.discard(iid)
                    resolved.add(iid)
                else:
                    live.add(iid)
                    resolved.discard(iid)
        except OSError:
            pass
        return live, resolved

    f_path = out_dir / "failures.jsonl"
    e_path = out_dir / "excused.jsonl"
    all_failed, resolved_ids = states(f_path)
    excused_ids = states(e_path)[0]
    # 还欠着的 = 失败过、但没有被判死。已剔除的 id 从待办里拿掉，但仍要如实
    # 算进 excused（见函数说明），两个数各管一件事，不互相减。
    pending = all_failed - excused_ids
    return {"failed": len(pending), "excused": len(excused_ids),
            "resolved": len(resolved_ids),
            "failed_file": str(f_path), "excused_file": str(e_path)}


# /submit 的 note 服务端不截断（server.py:1011-1013，直接写 packages.note 与
# events.detail），整个 POST body 上限 64 KiB。留一半给 SSE 广播和其余字段，
# 16 KiB 足够装下几百个 "id[kind] reason"。
EXCLUDED_NOTE_LIMIT = 16 * 1024


def read_excluded_details(out_dir: Path) -> list[dict]:
    """读两本账的**明细**，回答"哪些 id 被跳过、因为什么"。

    只报数量等于没报：管理员看板上看到"已完成但少 3 张"，若不写出是哪 3 张、
    各自什么原因，将来中央 merge 逐 id 对账时仍会把它们当成漏做，只能靠人工
    翻志愿者电脑上的文件。这里把 id 与原因一起带进服务端 note。

    两本账各管一类：
      skipped.jsonl —— 入口就被字节门槛判无效（没重试过，不是打标失败）
      excused.jsonl —— 重试后仍失败、已从分母剔除
    同一个 id 若两本都有，以 excused 为准（那是它的最终结论）。
    """
    out: dict[str, dict] = {}
    for name, kind in (("skipped.jsonl", "skipped"),
                       ("excused.jsonl", "excused")):
        path = out_dir / name
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
                iid = str(obj.get("image_id") or "").strip()
                if not iid:
                    continue
                reason = str(obj.get("error") or obj.get("reason") or "").strip()
                out[iid] = {"image_id": iid, "kind": kind,
                            "reason": reason[:120]}
        except OSError:
            pass
    return sorted(out.values(), key=lambda r: r["image_id"])


def build_submit_note(*, mode: str, n_img: int, grew: int, n_tag: int,
                      n_nl: int, limit, excused: int,
                      details: list[dict]) -> tuple[str, int]:
    """拼 /submit 的 note，返回 (note, 实际写进去的明细条数)。

    单独一个纯函数是为了能测：以前 note 直接拼在 _finish 里、末尾一句
    `note[:200]`，谁也没注意到"明细其实一条都没进服务端"。

    逐条累加而不是先拼好再截断：截断切在半个 image_id 中间，管理员看到的是
    一个不存在的 id，比少报几条更糟。装不下就明确写"另有 N 张见本机账本"。
    """
    note = (f"tagger {mode} / {n_img} 张（新增 {grew}）/ tag {n_tag} / nl {n_nl}"
            f" / limit {limit or '全量'}")
    if excused:
        # 「有条件完成」的说明必须进服务端 note：管理员事后看板看到"已完成"
        # 但张数少了，唯一能回答"为什么少"的就是这一句。只写在本地日志里，
        # 换一台机器/换了操作人就查不到了。
        note += f" / 重试后仍有 {excused} 张失败，已从分母剔除"
    if not details:
        return note, 0

    parts: list[str] = []
    used = len(note)
    for d in details:
        seg = f"{d['image_id']}[{d['kind']}] {d['reason']}"
        if used + len(seg) + 2 > EXCLUDED_NOTE_LIMIT:
            break
        parts.append(seg)
        used += len(seg) + 2
    note += " / 跳过明细: " + "; ".join(parts)
    if len(parts) < len(details):
        note += (f" …另有 {len(details) - len(parts)} 张"
                 f"（完整清单见本机 skipped.jsonl / excused.jsonl）")
    return note, len(parts)


def count_artifacts(out_dir: Path) -> dict:
    """统计产物。

    tagger 目前的产物是**按分片落的 JSONL**：每行一张图，
    形如 {"image_id":…, "tag":{…}, "nl":{…}, "ts":…}。
    老版本才是逐图的 `<id>-tag.json` / `<id>-nl.json`，这里两种都认。
    """
    seen: dict[str, set[str]] = {}
    files = 0
    for jl in sorted(out_dir.rglob("artifacts-*.jsonl")):
        files += 1
        for line in jl.open("r", encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            key = str(rec.get("image_id") or "")
            if not key:
                continue
            fields = seen.setdefault(key, set())
            if rec.get("tag"):
                fields.add("tag")
            if rec.get("nl"):
                fields.add("nl")
    images = len(seen)
    tag_hit = sum(1 for f in seen.values() if "tag" in f)
    nl_hit = sum(1 for f in seen.values() if "nl" in f)

    if images == 0:                       # 老布局兜底
        tag_files = list(out_dir.rglob("*-tag.json"))
        nl_files = list(out_dir.rglob("*-nl.json"))
        if tag_files or nl_files:
            return {"format": "per-image", "files": len(tag_files) + len(nl_files),
                    "images": max(len(tag_files), len(nl_files)),
                    "tag": len(tag_files), "nl": len(nl_files)}

    return {"format": "jsonl" if images else "none", "files": files,
            "images": images, "tag": tag_hit, "nl": nl_hit}


SECRET_FLAGS = {"--api-key", "--hf-token", "--token", "--password"}


def mask_secrets(cmd: list[str]) -> str:
    """命令行进日志/ps 前先给密钥打码。"""
    out, hide = [], False
    for item in cmd:
        if hide:
            out.append("***")
            hide = False
            continue
        if item in SECRET_FLAGS:
            out.append(item)
            hide = True
            continue
        for flag in SECRET_FLAGS:                 # --api-key=sk-xxx 形式
            if item.startswith(flag + "="):
                item = flag + "=***"
        out.append(item)
    return " ".join(out)


def run_tagger(py: list[str], root: Path, shard: Path, out_dir: Path,
               args: argparse.Namespace) -> int:
    cmd = py + ["-m", "tagger.cli", "client",
                "--shard", str(shard),
                "--out-dir", str(out_dir)]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.mode:
        cmd += ["--mode", args.mode]
    tiers = args.tiers
    if tiers is None:
        # 本机校准过的分档表（GPU 与默认设备不同）优先，避免又跑回 CPU
        cand = root / "configs" / "tiers-v100-16g.yaml"
        if cand.is_file():
            tiers = str(cand)
            say(f"[cfg] 使用本机分档表 {cand.name}")
    if tiers:
        cmd += ["--tiers", str(tiers)]
    if args.concurrency:
        cmd += ["--concurrency", str(args.concurrency)]
    if args.force:
        cmd += ["--force"]
    if args.tag_backend:
        cmd += ["--tag-backend", args.tag_backend]
    if args.url:
        cmd += ["--url", args.url]
    reserved = {"--out-dir", "--shard", "--limit", "--mode", "--tiers"}
    for extra in args.tagger_arg or []:
        flag = extra.split("=", 1)[0]
        if flag in reserved:
            die(EXIT_USAGE, f"[X] --tagger-arg 不允许覆盖 {flag}（会和桥自己的参数打架）；"
                            f"请用桥对应的选项")
        cmd.append(extra)

    env = dict(os.environ)
    if args.cuda_visible is not None:
        env["CUDA_VISIBLE_DEVICES"] = args.cuda_visible

    say(f"[run] {mask_secrets(cmd)}")
    say(f"[run] 工作目录 {root}，产物目录 {out_dir}")
    started = time.time()
    proc = subprocess.run(cmd, cwd=str(root), env=env)
    say(f"[run] tagger 退出码 {proc.returncode}，用时 {time.time() - started:.1f}s")
    return proc.returncode


# ------------------------------------------------------------------ 提交

def submit(cfg: dict, pkg: dict, note: str) -> None:
    code, obj = fox.api(cfg, f"/packages/{fox.quote(pkg['id'], safe='')}/submit"
                             + fox.qs(repo=pkg["repo"]), "POST", {"note": note})
    if code == 200:
        say(f"[ok] 已提交完成：{pkg['repo']}/{pkg['id']}")
        return
    raise SystemExit(f"[X] 提交失败：HTTP {code} {obj.get('error')}")


# ------------------------------------------------------------------ 命令

def cmd_next(args) -> int:
    cfg = fox.load_config()
    code, obj = fox.api(cfg, "/me")
    if code != 200:
        raise SystemExit(f"读取身份失败：HTTP {code} {obj.get('error')}")
    mine = obj.get("packages") or []
    avail_code, avail = fox.api(cfg, "/packages?limit=5000")
    free = [p for p in (avail.get("packages") or []) if p["status"] == "available"] \
        if avail_code == 200 else []
    say(f"身份 {obj['name']}（{obj['role']}）")
    if mine:
        say("\n我正在处理的包：")
        for p in mine:
            say(f"  {p['status_label']:<5} {p['repo']}/{p['id']}  {human(p.get('size'))}")
    else:
        say("\n我在处理的包：无")
    if free:
        say(f"\n可认领 {len(free)} 个，例如：")
        for p in free[:5]:
            say(f"  {p['repo']}/{p['id']}  {human(p.get('size'))}")
    return 0


def cmd_run(args) -> int:
    cfg = fox.load_config()
    # 先握手：服务端不可达是最常见的失败，别让用户先看到别的错
    code, me = fox.api(cfg, "/me")
    if code != 200:
        raise SystemExit(f"[X] 连不上服务端或密钥无效（HTTP {code}）：{me.get('error')}\n"
                         f"    检查 {fox.CONFIG_PATH} 或先跑 client.py login")

    tagger_root = Path(args.tagger_root).expanduser().resolve()
    if not tagger_root.is_dir():
        raise SystemExit(f"[X] tagger 项目目录不存在：{tagger_root}\n"
                         f"    用 --tagger-root 指定，或设 FOXLABEL_TAGGER_ROOT")

    # 1) 定位 + 权限
    pkg_id, repo = args.package, args.repo
    if pkg_id and not repo:
        maybe_repo, tail = fox.split_ref(pkg_id)
        if maybe_repo:
            code, obj = fox.api(cfg, "/repos")
            known = {r["repo"] for r in (obj.get("repos") or [])} if code == 200 else set()
            if maybe_repo in known:          # 形如 org/repo/shard.tar
                repo, pkg_id = maybe_repo, tail
    pkg = ensure_owner(cfg, find_package(cfg, pkg_id, repo), args.claim)

    out_dir = Path(args.out_dir).expanduser() if args.out_dir else \
        tagger_root / "work" / "foxjobs" / f"{pkg['repo'].replace('/', '__')}__{pkg['id']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / STATE_FILE
    state = load_state(state_path)
    state = {                                    # 每次重建，避免旧键残留误导追溯
        "repo": pkg["repo"], "package": pkg["id"],
        "sha256": pkg.get("sha256"),
        "started_at": state.get("started_at") or now(),
        "last_run_at": now(), "operator": cfg.get("name"),
        "tagger_root": str(tagger_root), "out_dir": str(out_dir),
        "status": "running",
    }
    save_state(state_path, state)

    say(f"\n=== {pkg['repo']}/{pkg['id']}  {human(pkg.get('size'))} ===")

    if args.submit_only:
        # 产物已经在了（比如上次 --limit 试跑、或手工跑过 tagger），只想补交
        arts = count_artifacts(out_dir)
        say(f"[ok] --submit-only：跳过下载与打标，按现有产物处理"
            f"（{arts['images']} 张 / tag {arts['tag']} / nl {arts['nl']}）")
        return _finish(cfg, pkg, out_dir, state_path, state, arts, grew=arts["images"],
                       args=args)

    # 2) 下载 + 校验
    cache_dir = Path(args.cache_dir).expanduser()
    local = Path(args.local_shard).expanduser() if args.local_shard else None
    if args.no_download:
        if local is None:
            local = cache_dir / fox.safe_name(pkg["id"])
        if not local.is_file():
            raise SystemExit(f"[X] --no-download 但本地没有分片：{local}")
    shard = download_shard(cfg, pkg, cache_dir, local, force=bool(args.force_download))
    if not args.dry_run:
        if pkg.get("downloaded_at"):
            say("[ok] 服务端已记录过「已下载」，不重复登记")
        else:
            mark_downloaded(cfg, pkg)

    if args.dry_run:
        say("[dry-run] 已完成认领与下载校验，未进入打标")
        return EXIT_OK

    # 3) 打标
    py = resolve_tagger_python(tagger_root, args.tagger_python)
    before = count_artifacts(out_dir)          # 打标前快照：用来识别「返工但一张没重打」
    rc = run_tagger(py, tagger_root, shard, out_dir, args)
    if rc != 0:
        state.update({"status": "tagger_failed", "rc": rc, "finished_at": now()})
        save_state(state_path, state)
        hint = ""
        if rc == 2:
            hint = ("\n    提示：tagger 的参数互斥——`--mode tag` 必须配 "
                    "`--tag-backend none`；生产口径用 `--mode both`（需先起 tagger serve）")
        die(EXIT_TAGGER, f"[X] 打标失败（退出码 {rc}），未提交；包仍归你认领，可直接重跑{hint}")

    arts = count_artifacts(out_dir)
    grew = arts["images"] - before["images"]
    return _finish(cfg, pkg, out_dir, state_path, state, arts, grew, args)


def _finish(cfg: dict, pkg: dict, out_dir: Path, state_path: Path, state: dict,
            arts: dict, grew: int, args) -> int:
    """统一的提交闸门：任何一条不过都不提交。"""
    n_tag, n_nl, n_img = arts["tag"], arts["nl"], arts["images"]
    mode = args.mode or "both"
    need_tag, need_nl = mode in ("both", "tag"), mode in ("both", "nl")

    state.update({"artifacts": arts, "new_images": grew, "mode": mode,
                  "finished_at": now(), "status": "artifacts_checked"})
    save_state(state_path, state)
    say(f"[ok] 产物：{n_img} 张图 / tag {n_tag} / nl {n_nl}"
        f"（本次新增 {grew} 张，{arts['format']}，{arts['files']} 个文件）→ {out_dir}")

    # ---- 提交闸门：任何一条不过都不提交（包留在「已认领」，可安全重跑）----
    if n_img == 0:
        die(EXIT_TAGGER, "[X] 没有任何产物，未提交（检查 --limit / 分片内容 / tagger 配置）")
    if grew <= 0 and not args.force:
        # 返工场景最容易在这里被误判成功：tagger 把历史产物判为「已完成」而全部跳过，
        # 若照旧提交，就等于把上一轮被打回的数据原样再交一次。
        die(EXIT_TAGGER,
            f"[X] 本次没有新增任何图片（产物目录里已有 {n_img - grew} 张）。\n"
            f"    返工/重打请加 --force（会让 tagger 忽略已有产物重跑）。")
    if need_tag and n_tag == 0:
        die(EXIT_TAGGER, "[X] 有产物但没有任何 tag，未提交（检查 --tag-backend / --mode）")
    if need_nl and n_nl == 0:
        die(EXIT_TAGGER, "[X] 没有任何 nl 产物，未提交（NL 服务是否已就绪？--url 是否指对）")
    if need_tag and need_nl and n_nl < n_tag:
        say(f"[warn] {n_tag - n_nl} 张图只有 tag 没有 nl（tagger 视 tag-or-nl 为已完成，"
            f"这些图的 nl 不会再补；需要时用 --force 重打）")

    # ---- 失败样本的下场：读 tagger 的两本账
    #
    # failures.jsonl = 还没处理完的失败（下次续跑还会做）
    # excused.jsonl  = 重试后仍失败、已从分母剔除的坏样本（不再重试）
    #
    # 为什么这在提交闸门里、而不是只写进 note：一个分片跑完还挂着 4 条失败，
    # 到底是"漏做了"还是"那 4 张试过两轮救不回来"，决定了这个包能不能交。
    # 只看产物张数分不清这两件事——而那正是「坏样本阻塞全流程」的根源。
    stats = scan_failure_books(out_dir)
    if stats["failed"]:
        # ★只提示、不 die。坏样本不能阻塞全流程——这正是「重试失败」那条路
        #   存在的理由。旧文案写"这批会阻塞提交"是假的，代码从来没在这里 return，
        #   留着只会让排查的人以为提交被拦了、去别处白找原因。
        say(f"[warn] 还有 {stats['failed']} 条未解决失败（在 failures.jsonl、"
            f"但不在 excused.jsonl）；本次仍按现有产物提交，建议之后点「重试失败」")
    if stats["resolved"]:
        say(f"[ok] 失败账里另有 {stats['resolved']} 张重试后已成功或被判无效，"
            f"不再计入待办")
    if stats["excused"]:
        say(f"[ok] {stats['excused']} 张重试后仍失败，已剔除出分母"
            f"（{stats['excused_file']}）")

    if args.limit and not args.allow_partial:
        # 试跑口径绝不能把包标成「已完成」：一个分片 6000+ 张，标 completed 后
        # 再跑会被「已完成跳过」挡住，只能找管理员 reopen。
        state.update({"status": "partial_not_submitted", "failure_books": stats})
        save_state(state_path, state)
        say(f"[skip] --limit {args.limit} 属于试跑：本次只打了 {grew or n_img} 张，"
            f"**未提交**（避免把整包标成已完成）。整片跑请去掉 --limit；"
            f"确认要按部分数据交付再加 --allow-partial。")
        return EXIT_OK

    if args.no_submit:
        state.update({"status": "not_submitted"})
        save_state(state_path, state)
        say("[skip] --no-submit：未提交，包保持已认领")
        return EXIT_OK

    # ---- 提交前复核归属：十小时打标期间可能被管理员改派/打回 ----
    cur = find_package(cfg, pkg["id"], pkg["repo"])
    if cur.get("status") != "claimed" or cur.get("owner") != cfg.get("name"):
        state.update({"status": "ownership_changed"})
        save_state(state_path, state)
        die(EXIT_NOT_OWNER,
            f"[X] 提交前复核失败：当前状态 {cur.get('status_label')}、认领人 {cur.get('owner')}；"
            f"产物已保留在 {out_dir}，请联系管理员后再决定")

    # ---- 把剔除/跳过的 id 与原因逐条带进服务端
    #
    # ★为什么不用"回传一个账本文件"：产物回传目前是关的，打好的标先留在
    #   志愿者电脑上；/submit 是唯一一定会发生、且**按分片各调一次**的写通道
    #   （out_dir 也是按包独立的，见 cmd_run），所以明细天然不会混成一大包。
    # ★为什么去掉原来的 note[:200]：那是**客户端自己**加的截断，服务端
    #   submit() 根本不截（直接写 packages.note 与 events.detail）。留着它，
    #   连一张 id 都塞不进去，只能报个数量。
    details = read_excluded_details(out_dir)
    note, reported = build_submit_note(
        mode=mode, n_img=n_img, grew=grew, n_tag=n_tag, n_nl=n_nl,
        limit=args.limit, excused=stats["excused"], details=details)
    if details:
        say(f"[ok] 已把 {reported}/{len(details)} 条跳过明细写进提交说明"
            f"（共 {len(details)} 张，按分片独立提交）")
    submit(cfg, pkg, note)
    after = find_package(cfg, pkg["id"], pkg["repo"])
    state.update({"status": "submitted", "note": note,
                  "excluded": len(details), "excluded_reported": reported})
    save_state(state_path, state)
    if after.get("status") != "completed":
        die(EXIT_USAGE, f"[X] 提交后回读状态仍为 {after.get('status')}，请检查服务端日志")
    say(f"[ok] 服务端确认：{after['status_label']}（{after.get('owner')}）")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bridge.py", description="fox-label ⇄ tagger 联动桥")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("next", help="查看我在处理/可认领的包")
    a.set_defaults(func=cmd_next)

    a = sub.add_parser("run", help="认领 → 下载校验 → 打标 → 校验 → 提交")
    a.add_argument("--package", required=True, help="包名，或 repo/包名")
    a.add_argument("--repo", default=None, help="仓库（同名包存在于多个仓库时必填）")
    a.add_argument("--claim", action="store_true", help="未认领时先认领")
    a.add_argument("--tagger-root",
                   default=os.environ.get("FOXLABEL_TAGGER_ROOT")
                   or str(Path.home() / "项目" / "tagger-deploy"))
    a.add_argument("--tagger-python", default=None, help="跑 tagger 的解释器（默认项目内 venv）")
    a.add_argument("--out-dir", default=None, help="产物目录（默认 tagger/work/foxjobs/…）")
    a.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "fox-label" / "shards"))
    a.add_argument("--local-shard", default=None, help="已有分片路径（跳过下载）")
    a.add_argument("--no-download", action="store_true", help="不下载，直接用缓存里的分片")
    a.add_argument("--force-download", action="store_true", help="哈希不符时删除重下")
    a.add_argument("--no-submit", action="store_true", help="做完不提交（演练）")
    a.add_argument("--dry-run", action="store_true", help="只认领+下载校验")
    # 透传给 tagger
    a.add_argument("--limit", type=int, default=None,
                   help="试跑张数：只打不提交（确认要按部分数据交付再加 --allow-partial）")
    a.add_argument("--allow-partial", action="store_true",
                   help="允许在 --limit 试跑后提交（默认不允许，避免整包被误标已完成）")
    a.add_argument("--submit-only", action="store_true",
                   help="跳过下载与打标，只按产物目录里已有的产物提交（补交/试跑后确认交付）")
    a.add_argument("--mode", default=None, choices=["nl", "tag", "both"],
                   help="both=出 tag+nl（需先起 tagger serve）；tag=仅 tag（须配 --tag-backend none）")
    a.add_argument("--tag-backend", default=None, dest="tag_backend",
                   help="透传给 tagger：pixai / wd-eva02 / none")
    a.add_argument("--url", default=None, help="透传给 tagger：NL 服务地址（默认它自己的 127.0.0.1:8000）")
    a.add_argument("--tiers", default=None, help="tagger 分档表（本机用 configs/tiers-v100-16g.yaml）")
    a.add_argument("--concurrency", type=int, default=None)
    a.add_argument("--force", action="store_true", help="透传给 tagger：忽略已有产物")
    a.add_argument("--cuda-visible", default=None, help="透传 CUDA_VISIBLE_DEVICES")
    a.add_argument("--tagger-arg", action="append", default=None, help="额外透传参数，可重复")
    a.set_defaults(func=cmd_run)
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
    except KeyboardInterrupt:
        say("\n已中断")
        return 130
    except SystemExit:
        raise
    except Exception as e:  # 兜底：别把栈甩给打标同学
        say(f"[X] {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
