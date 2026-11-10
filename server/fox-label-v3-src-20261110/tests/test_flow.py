#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fox-label v2 双端模拟验证（多仓库）。

不依赖真实 HuggingFace：本地 HTTP 服务模拟 resolve 端点（支持 Range）；
服务端与客户端都以真实子进程运行，全部走真实 HTTP。

覆盖：多仓库登记/同步/停用、按仓库授权、同名包歧义、跨仓库认领与下载隔离、
状态机全路径、SSE、吊销即时生效、审计留痕。

运行：python tests/test_flow.py
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
CLIENT = ROOT / "client.py"
PY = sys.executable

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {extra}")
        FAILS.append(name)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run(args, env, timeout=120):
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)


def wait_ready(url: str, timeout=20) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def api_get(url: str, key: str):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def api_get_raw(url: str, key: str):
    """返回 (状态码, 负载)，4xx 也照常解析。"""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


# ---------------------------------------------------------------- 假 HF 端点

class FakeHF(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    blobs: dict[tuple[str, str], bytes] = {}

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        try:
            repo = path.split("/datasets/")[1].split("/resolve/main/")[0]
            name = path.rsplit("/resolve/main/", 1)[1]
        except IndexError:
            self.send_response(404)
            self.end_headers()
            return
        blob = self.blobs.get((repo, name))
        if blob is None:
            self.send_response(404)
            self.end_headers()
            return
        total = len(blob)
        start = 0
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            part = rng.split("=", 1)[1].split("-")[0]
            if part.isdigit():
                start = int(part)
        chunk = blob[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(chunk)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{total-1}/{total}")
        self.end_headers()
        self.wfile.write(chunk)


def tree_for(repo: str, names: list[str]) -> list[dict]:
    return [{"type": "file", "path": n,
             "size": len(FakeHF.blobs[(repo, n)]),
             "lfs": {"oid": hashlib.sha256(FakeHF.blobs[(repo, n)]).hexdigest(),
                     "size": len(FakeHF.blobs[(repo, n)]), "pointerSize": 130}}
            for n in names]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="foxlabel-v2-"))
    home_a, home_b = tmp / "home_a", tmp / "home_b"
    home_a.mkdir(); home_b.mkdir()
    state = tmp / "state"; state.mkdir()
    data = tmp / "data"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    repo_a, repo_b, repo_c = "test/repo-a", "test/repo-b", "test/repo-c"
    FakeHF.blobs = {
        (repo_a, "shard-000000.tar"): os.urandom(120_000),
        (repo_a, "shard-000001.tar"): os.urandom(90_000),
        (repo_a, "shard-000002.tar"): os.urandom(80_000),
        (repo_a, "manifest.jsonl"): b'{"image_id":"a1"}\n{"image_id":"a2"}\n',
        (repo_b, "shard-000000.tar"): os.urandom(60_000),
        (repo_b, "manifest.jsonl"): b'{"image_id":"b1"}\n',
        (repo_c, "shard-000100.tar"): os.urandom(30_000),   # 该仓库没有 manifest
    }
    tree_a = tmp / "tree_a.json"
    tree_c = tmp / "tree_c.json"
    tree_c.write_text(json.dumps(tree_for(repo_c, ["shard-000100.tar"])), encoding="utf-8")
    tree_b = tmp / "tree_b.json"
    tree_a.write_text(json.dumps(tree_for(repo_a, ["shard-000000.tar", "shard-000001.tar",
                                                   "manifest.jsonl"])), encoding="utf-8")
    tree_b.write_text(json.dumps(tree_for(repo_b, ["shard-000000.tar", "manifest.jsonl"])),
                      encoding="utf-8")

    fake = ThreadingHTTPServer(("127.0.0.1", free_port()), FakeHF)
    threading.Thread(target=fake.serve_forever, daemon=True).start()

    env = os.environ.copy()
    env.update({"HOME": str(home_a), "FOXLABEL_HF_ENDPOINT": f"http://127.0.0.1:{fake.server_address[1]}",
                "PYTHONIOENCODING": "utf-8", "HF_TOKEN": "hf_fake_for_test"})
    ENV_A = env
    ENV_B = dict(env, HOME=str(home_b))

    print("=" * 72)
    print("fox-label v2 多仓库双端验证")
    print(f"临时目录: {tmp}")
    print("=" * 72)

    # ---------------------------------------------------- 1. 登记两个仓库
    print("\n[1] 登记仓库")
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
             "--repo", repo_a, "--from-tree", str(tree_a)], env)
    check("repo add A 成功", r.returncode == 0 and "新增包数 : 2" in r.stdout, r.stderr[:300])
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
             "--repo", repo_b, "--from-tree", str(tree_b)], env)
    check("repo add B 成功", r.returncode == 0 and "新增包数 : 1" in r.stdout, r.stderr[:300])
    check("第二次 repo add 不重复打印管理员密钥", "管理员密钥" not in r.stdout, r.stdout[-200:])

    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
             "--repo", repo_c, "--from-tree", str(tree_c)], env)
    check("repo add C（无 manifest）成功", r.returncode == 0 and "未找到" in r.stdout, r.stdout)

    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
             "--repo", repo_a, "--from-tree", str(tree_a)], env)
    check("重复登记被拒绝", r.returncode != 0)

    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "list"], env)
    rows = [l for l in r.stdout.splitlines() if l.startswith("test/repo-")]
    check("repo list 显示三个仓库", len(rows) == 3, r.stdout)

    admin_key = (state / "admin.key").read_text().strip()

    # ---------------------------------------------------- 2. 按仓库授权
    print("\n[2] 按仓库授权签发密钥")
    keys = {}
    r = run([PY, str(SERVER), "--data-dir", str(state), "enroll", "--name", "alice"], env)
    check("alice 授权全部仓库", r.returncode == 0 and "全部仓库" in r.stdout, r.stdout)
    keys["alice"] = [l.split(":", 1)[1].strip() for l in r.stdout.splitlines()
                     if l.startswith("密钥")][0]
    r = run([PY, str(SERVER), "--data-dir", str(state), "enroll", "--name", "bob",
             "--repo", repo_a], env)
    check("bob 仅授权 repo-a", r.returncode == 0 and repo_a in r.stdout, r.stdout)
    keys["bob"] = [l.split(":", 1)[1].strip() for l in r.stdout.splitlines()
                   if l.startswith("密钥")][0]
    r = run([PY, str(SERVER), "--data-dir", str(state), "enroll", "--name", "eve",
             "--repo", "nonexist/repo"], env)
    check("授权未登记仓库被拒绝", r.returncode != 0)

    # ---------------------------------------------------- 3. 服务与可见性
    print("\n[3] 启动服务与仓库可见性")
    srv = subprocess.Popen([PY, str(SERVER), "--data-dir", str(state), "serve",
                            "--host", "127.0.0.1", "--port", str(port)],
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # 断言中途崩溃也要回收服务进程，否则每跑一次就泄漏一个实例
    atexit.register(lambda: srv.poll() is None and srv.kill())
    check("服务就绪", wait_ready(base), "服务 20s 内未就绪")
    if FAILS:
        srv.terminate()
        return 1

    for name, e in (("alice", ENV_A), ("bob", ENV_B)):
        r = run([PY, str(CLIENT), "login", "--server", base, "--key", keys[name]], e)
        check(f"{name} 登录成功", r.returncode == 0 and name in r.stdout, r.stderr[:200])

    r = run([PY, str(CLIENT), "repos"], ENV_A)
    check("alice 看到两个仓库", repo_a in r.stdout and repo_b in r.stdout, r.stdout)
    repos_payload = api_get(base + "/repos", admin_key)
    by_repo = {x["repo"]: x for x in repos_payload["repos"]}
    check("无 manifest 的仓库不被回填",
          by_repo[repo_c].get("manifest") is None, str(by_repo[repo_c]))
    check("有 manifest 的仓库仍正确",
          by_repo[repo_a].get("manifest") == "manifest.jsonl", str(by_repo[repo_a]))

    r = run([PY, str(CLIENT), "repos"], ENV_B)
    check("bob 只看到 repo-a", repo_a in r.stdout and repo_b not in r.stdout, r.stdout)

    r = run([PY, str(CLIENT), "list"], ENV_A)
    check("alice 列表含 3 个包", r.stdout.count("shard-0000") == 3, r.stdout)
    r = run([PY, str(CLIENT), "list"], ENV_B)
    check("bob 列表只有 repo-a 的 2 个包", r.stdout.count("shard-0000") == 2, r.stdout)

    # ---------------------------------------------------- 4. 越权与歧义
    print("\n[4] 越权与同名歧义")
    r = run([PY, str(CLIENT), "claim", f"{repo_b}/shard-000000.tar"], ENV_B)
    check("bob 认领未授权仓库被拒", r.returncode != 0 and "无权限" in r.stderr, r.stderr[:200])

    r = run([PY, str(CLIENT), "claim", "shard-000000.tar"], ENV_A)
    check("同名包裸引用被拒绝（歧义）", r.returncode != 0 and "冲突" in r.stderr, r.stderr[:300])

    r = run([PY, str(CLIENT), "claim", f"{repo_a}/shard-000000.tar"], ENV_A)
    check("显式仓库引用认领成功", r.returncode == 0, r.stderr[:200])
    r = run([PY, str(CLIENT), "claim", "shard-000000.tar", "--repo", repo_b], ENV_A)
    check("--repo 方式认领成功", r.returncode == 0, r.stderr[:200])

    payload = api_get(base + "/packages", admin_key)
    got = {(p["repo"], p["id"]): p for p in payload["packages"]}
    check("两个仓库的同名包独立记录 owner",
          got[(repo_a, "shard-000000.tar")]["owner"] == "alice"
          and got[(repo_b, "shard-000000.tar")]["owner"] == "alice",
          str([(k, v["owner"]) for k, v in got.items()]))

    # ---------------------------------------------------- 5. 并发认领
    print("\n[5] 跨用户并发认领同一包")
    results: list[int] = []
    t1 = threading.Thread(target=lambda: results.append(
        run([PY, str(CLIENT), "claim", f"{repo_a}/shard-000001.tar"], ENV_A).returncode))
    t2 = threading.Thread(target=lambda: results.append(
        run([PY, str(CLIENT), "claim", f"{repo_a}/shard-000001.tar"], ENV_B).returncode))
    t1.start(); t2.start(); t1.join(); t2.join()
    check("并发认领：恰好一个成功", sorted(results) == [0, 1], str(results))

    payload = api_get(base + "/packages", admin_key)
    owner = [p for p in payload["packages"]
             if p["repo"] == repo_a and p["id"] == "shard-000001.tar"][0]["owner"]
    check("赢家已记录", owner in ("alice", "bob"), str(owner))

    # ---------------------------------------------------- 6. 多仓库下载隔离
    print("\n[6] 多仓库下载（按仓库分子目录）")
    r = run([PY, str(CLIENT), "download", "--mine", "--out", str(data)], ENV_A)
    check("alice 下载 --mine 成功", r.returncode == 0, r.stderr[-400:])
    dir_a = data / repo_a.replace("/", "__")
    dir_b = data / repo_b.replace("/", "__")
    check("repo-a 分片落盘且校验通过",
          (dir_a / "shard-000000.tar").exists() and "校验通过" in r.stdout, r.stdout[-300:])
    check("repo-b 分片落在独立子目录", (dir_b / "shard-000000.tar").exists())
    check("两份 manifest 互不覆盖",
          (dir_a / "manifest.jsonl").read_bytes().startswith(b'{"image_id":"a1"}')
          and (dir_b / "manifest.jsonl").read_bytes().startswith(b'{"image_id":"b1"}'))

    payload = api_get(base + "/packages", admin_key)
    dl = [p for p in payload["packages"] if p["owner"] == "alice" and p.get("downloaded_at")]
    check("已下载标记按仓库分别记录", len(dl) >= 2, str(len(dl)))

    # ---------------------------------------------------- 7. repo sync
    print("\n[7] 增量同步")
    FakeHF.blobs[(repo_a, "shard-000009.tar")] = os.urandom(40_000)
    tree_a2 = tmp / "tree_a2.json"
    tree_a2.write_text(json.dumps(tree_for(repo_a, ["shard-000000.tar", "shard-000001.tar",
                                                    "shard-000009.tar", "manifest.jsonl"])),
                       encoding="utf-8")
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "sync",
             "--repo", repo_a, "--from-tree", str(tree_a2)], env)
    check("repo sync 补录新增文件", r.returncode == 0 and "新增 1" in r.stdout, r.stdout)
    r = run([PY, str(CLIENT), "list", "--repo", repo_a], ENV_A)
    check("客户端可见新包", "shard-000009.tar" in r.stdout, r.stdout)

    # ---------------------------------------------------- 8. 停用仓库
    print("\n[8] 停用仓库")
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "disable", "--repo", repo_b], env)
    check("停用成功", r.returncode == 0)
    r = run([PY, str(CLIENT), "list"], ENV_A)
    check("停用后列表不含该仓库", "shard-000000" in r.stdout and repo_b.split("/")[-1] not in r.stdout
          .replace(repo_a.split("/")[-1], ""), r.stdout)
    r = run([PY, str(CLIENT), "claim", f"{repo_b}/shard-000000.tar"], ENV_A)
    check("停用后认领被拒", r.returncode != 0, r.stderr[:200])
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "enable", "--repo", repo_b], env)
    check("重新启用成功", r.returncode == 0)

    # ---------------------------------------------------- 9. 状态机
    print("\n[9] 状态机全路径")
    winner_env = ENV_A if owner == "alice" else ENV_B
    winner_name = owner
    r = run([PY, str(CLIENT), "submit", f"{repo_a}/shard-000001.tar", "--note", "完成"], winner_env)
    check("认领人提交成功", r.returncode == 0 and "已完成" in r.stdout, r.stderr[:200])
    other_env = ENV_B if winner_env is ENV_A else ENV_A
    r = run([PY, str(CLIENT), "submit", f"{repo_a}/shard-000001.tar"], other_env)
    check("非认领人提交被拒", r.returncode != 0, r.stdout + r.stderr)

    r = run([PY, str(SERVER), "--data-dir", str(state), "reopen",
             "--package", f"{repo_a}/shard-000001.tar", "--reason", "返工"], env)
    check("管理员打回（保留认领人）", r.returncode == 0 and "已认领" in r.stdout, r.stderr[:200])
    r = run([PY, str(SERVER), "--data-dir", str(state), "release",
             "--package", f"{repo_a}/shard-000001.tar", "--reason", "回收"], env)
    check("管理员释放", r.returncode == 0, r.stderr[:200])

    r = run([PY, str(CLIENT), "claim", f"{repo_a}/shard-000009.tar"], ENV_B)
    check("bob 认领增量包", r.returncode == 0, r.stderr[:200])
    r = run([PY, str(CLIENT), "release", f"{repo_a}/shard-000009.tar", "--reason", "放弃"], ENV_B)
    check("认领人自愿释放", r.returncode == 0, r.stderr[:200])

    # ---------------------------------------------------- 10. SSE
    print("\n[10] SSE 实时推送（含 repo 字段）")
    events: list[dict] = []

    def listen():
        req = urllib.request.Request(base + "/events",
                                     headers={"Authorization": f"Bearer {keys['alice']}"})
        deadline = time.time() + 12
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                for raw in resp:
                    line = raw.decode().strip()
                    if line.startswith("data:"):
                        ev = json.loads(line[5:].strip())
                        events.append(ev)
                        if ev.get("kind") == "claim":
                            return
                    if time.time() > deadline:
                        return
        except Exception:
            pass

    th = threading.Thread(target=listen, daemon=True)
    th.start()
    time.sleep(0.8)
    run([PY, str(CLIENT), "claim", f"{repo_a}/shard-000001.tar"], ENV_A)
    th.join(timeout=8)
    check("SSE 事件带 repo 字段", any(e.get("repo") == repo_a for e in events), str(events[:2]))

    # ---------------------------------------------------- 11. 吊销
    print("\n[11] 吊销即时生效")
    r = run([PY, str(SERVER), "--data-dir", str(state), "revoke", "--name", "alice"], env)
    check("吊销成功", r.returncode == 0)
    r = run([PY, str(CLIENT), "list"], ENV_A)
    check("吊销后 401", r.returncode != 0 and "认证失败" in r.stderr, r.stderr[:200])

    # ---------------------------------------------------- 12. 看板与审计
    print("\n[12] 看板与审计")
    r = run([PY, str(SERVER), "--data-dir", str(state), "status"], env)
    check("status 含仓库列与按用户统计", repo_a in r.stdout and "用户" in r.stdout, r.stdout[-400:])
    audit = [json.loads(l) for l in (state / "audit.jsonl").read_text(encoding="utf-8").splitlines()
             if l.strip()]
    kinds = {a["kind"] for a in audit}
    check("审计覆盖仓库与状态流转",
          {"repo_add", "repo_sync", "repo_disable", "repo_enable", "claim", "submit",
           "reopen", "release", "admin_release"} <= kinds, str(kinds))
    check("审计记录带仓库标识",
          any(a.get("repo") == repo_a for a in audit if a["kind"] == "claim"))
    check("审计精简（无请求日志）", len(audit) < 80, str(len(audit)))

    # ---------------------------------------------------- 13. 审计回归
    print("\n[13] 审计回归（服务端加固 / 路径安全 / 已完成跳过）")

    # 13.1 请求体超限
    body = b"a" * 70000
    req = urllib.request.Request(
        base + "/packages/shard-000000.tar/claim?repo=" + urllib.parse.quote(repo_a, safe=""),
        data=body, method="POST", headers={"Authorization": f"Bearer {keys['bob']}"})
    try:
        urllib.request.urlopen(req, timeout=10)
        check("超大请求体被拒绝", False)
    except urllib.error.HTTPError as e:
        check("超大请求体返回 413", e.code == 413, str(e.code))

    # 13.2 运行期新增仓库（非 ASCII 名称）并可查询 —— 同时验证热加载
    repo_d = "测试/中文仓库"
    FakeHF.blobs[(repo_d, "shard-000200.tar")] = os.urandom(20_000)
    tree_d = tmp / "tree_d.json"
    tree_d.write_text(json.dumps(tree_for(repo_d, ["shard-000200.tar"])), encoding="utf-8")
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
             "--repo", repo_d, "--from-tree", str(tree_d)], env)
    check("运行期新增仓库成功", r.returncode == 0, r.stderr[:200])
    payload = api_get(base + "/packages?repo=" + urllib.parse.quote(repo_d, safe=""), admin_key)
    check("非 ASCII 仓库名可查询（查询参数解码）",
          len(payload.get("packages") or []) == 1, str(payload.get("summary")))

    # 13.3 路径穿越防护（客户端单元级）
    sys.path.insert(0, str(ROOT))
    import client as cm
    check("文件名只保留 basename", cm.safe_name("sub/x.tar") == "x.tar")
    check("路径穿越被摊平", cm.safe_name("../../etc/passwd") == "passwd")

    # 13.4 已完成的包在 --mine 时跳过而非报错
    run([PY, str(CLIENT), "claim", f"{repo_a}/shard-000009.tar"], ENV_B)
    run([PY, str(CLIENT), "submit", f"{repo_a}/shard-000009.tar", "--note", "x"], ENV_B)
    r = run([PY, str(CLIENT), "download", "--mine", "--out", str(data / "skip")], ENV_B)
    check("已完成包下载被跳过且不报错",
          r.returncode == 0 and "[skip]" in r.stdout, (r.stdout + r.stderr)[-300:])

    # 13.5 仓库侧内容变化必须留痕（已完成包被替换）
    FakeHF.blobs[(repo_a, "shard-000009.tar")] = os.urandom(41_000)
    tree_a3 = tmp / "tree_a3.json"
    tree_a3.write_text(json.dumps(tree_for(
        repo_a, ["shard-000000.tar", "shard-000001.tar", "shard-000009.tar", "manifest.jsonl"])),
        encoding="utf-8")
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "sync",
             "--repo", repo_a, "--from-tree", str(tree_a3)], env)
    check("repo sync 执行成功", r.returncode == 0, r.stderr[:200])
    audit2 = [json.loads(l) for l in (state / "audit.jsonl").read_text(encoding="utf-8").splitlines()
              if l.strip()]
    check("已完成包内容变化被留痕",
          any(a["kind"] == "pkg_changed" and a["pkg"] == "shard-000009.tar" for a in audit2),
          str([a["kind"] for a in audit2][-5:]))

    # ---------------------------------------------------- 14. 审计修复回归
    print("\n[14] 审计修复回归（授权 fail-open / prune / 密钥生命周期 / health）")

    # 14.1 受限密钥在仓库被删除后不得升级为全仓库（B2 fail-open）
    r = run([PY, str(SERVER), "--data-dir", str(state), "enroll",
             "--name", "dave", "--repo", repo_c], env)
    dave = re.search(r"密钥:\s*(flk_\S+)", r.stdout)
    check("enroll 受限用户 dave", r.returncode == 0 and dave is not None, r.stdout[-200:])
    dave_key = dave.group(1) if dave else ""
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "remove",
             "--repo", repo_c, "--force"], env)
    check("删除 dave 唯一授权仓库", r.returncode == 0, r.stderr[:200])
    code, payload = api_get_raw(base + "/packages", dave_key)
    check("删库后受限密钥不会升级为全仓库",
          code == 200 and not payload.get("packages"), f"{code} {payload.get('summary')}")
    code, _ = api_get_raw(base + "/packages?repo=" + urllib.parse.quote(repo_a, safe=""), dave_key)
    check("删库后无法访问其他仓库", code == 403, str(code))

    # 14.2 吊销后可重新签发同名密钥（H4）
    r = run([PY, str(SERVER), "--data-dir", str(state), "revoke", "--name", "dave"], env)
    check("吊销 dave", r.returncode == 0, r.stderr[:200])
    r = run([PY, str(SERVER), "--data-dir", str(state), "enroll",
             "--name", "dave", "--repo", repo_a], env)
    check("吊销后可重新 enroll 同名用户", r.returncode == 0, (r.stdout + r.stderr)[-200:])

    # 14.3 不能吊销最后一个管理员密钥（H1）
    r = run([PY, str(SERVER), "--data-dir", str(state), "revoke", "--name", "admin"], env)
    check("拒绝吊销最后一个管理员密钥", r.returncode != 0 and "最后一个" in r.stderr, r.stderr[:200])

    # 14.4 非法 include 正则不得留下坏仓库（M2）
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
             "--repo", "test/bad-regex", "--include", "^shard-(\\d+\\.tar$",
             "--from-tree", str(tree_a)], env)
    check("非法正则被拒", r.returncode != 0, (r.stdout + r.stderr)[-160:])
    rows = [l for l in run([PY, str(SERVER), "--data-dir", str(state), "repo", "list"],
                           env).stdout.splitlines() if l.startswith("test/bad-regex")]
    check("非法正则未留下仓库行", not rows, str(rows))

    # 14.5 prune 只清理未认领包，绝不删已认领的（B3）
    repo_f = "test/repo-prune"
    for n in ("shard-000300.tar", "shard-000301.tar", "shard-000302.tar"):
        FakeHF.blobs[(repo_f, n)] = os.urandom(9_000)
    tree_f = tmp / "tree_f.json"
    tree_f.write_text(json.dumps(tree_for(repo_f, ["shard-000300.tar", "shard-000301.tar",
                                                   "shard-000302.tar"])), encoding="utf-8")
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
             "--repo", repo_f, "--from-tree", str(tree_f)], env)
    check("新建 prune 测试仓库", r.returncode == 0, r.stderr[:200])
    r = run([PY, str(SERVER), "--data-dir", str(state), "enroll", "--name", "pruner",
             "--repo", repo_f], env)
    pruner = re.search(r"密钥:\s*(flk_\S+)", r.stdout)
    check("enroll pruner", r.returncode == 0 and pruner is not None, r.stdout[-160:])
    run([PY, str(CLIENT), "login", "--server", base, "--key",
         pruner.group(1) if pruner else ""], ENV_B)
    r = run([PY, str(CLIENT), "claim", f"{repo_f}/shard-000301.tar"], ENV_B)
    check("pruner 认领中间包", r.returncode == 0, r.stderr[:200])
    tree_f2 = tmp / "tree_f2.json"
    tree_f2.write_text(json.dumps(tree_for(repo_f, ["shard-000300.tar"])), encoding="utf-8")
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "sync",
             "--repo", repo_f, "--prune", "--from-tree", str(tree_f2)], env)
    check("prune 执行成功", r.returncode == 0, r.stderr[:200])
    c = sqlite3.connect(state / "db.sqlite3")
    rows = dict(c.execute("SELECT id, status FROM packages WHERE repo=?", (repo_f,)).fetchall())
    c.close()
    check("已认领包未被 prune 删除", rows.get("shard-000301.tar") == "claimed", str(rows))
    check("未认领的消失包被清理", "shard-000302.tar" not in rows and "shard-000300.tar" in rows,
          str(rows))

    # 14.6 /health 不需要密钥且只回廉价计数（H3）
    with urllib.request.urlopen(base + "/health", timeout=5) as resp:
        health = json.loads(resp.read())
    check("/health 无需鉴权且不含全量统计",
          health.get("ok") is True and set(health) == {"ok", "repos", "packages"}, str(health))

    srv.terminate()
    try:
        srv.wait(timeout=10)
    except subprocess.TimeoutExpired:
        srv.kill()
    fake.shutdown()

    print("\n" + "=" * 72)
    if FAILS:
        print(f"结果: {CHECKS - len(FAILS)}/{CHECKS} 通过，失败 {len(FAILS)} 项")
        for f in FAILS:
            print(f"  - {f}")
        print(f"临时目录保留: {tmp}")
        return 1
    print(f"结果: 全部 {CHECKS} 项通过")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
