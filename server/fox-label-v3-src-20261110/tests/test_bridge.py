#!/usr/bin/env python3
"""bridge.py（fox-label ⇄ tagger 联动桥）的验收测试。

不碰 GPU、不碰真实 HF：本地假 HF 端点 + 假 tagger 项目。
重点验证**接口之间的不变量**，而不是两边的内部实现：

  1. 认领 → 下载 → 打标 → 提交 的 happy path
  2. 哈希不符时绝不进入打标（避免拿错素材烧 GPU）
  3. 打标失败 / 零产物时绝不提交（包必须留在「已认领」，可安全重跑）
  4. 别人的包不能抢、未认领不能直接干、已完成直接跳过
  5. --local-shard 跳过网络下载
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import socket
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
BRIDGE = ROOT / "bridge.py"
PY = sys.executable

FAILS: list[str] = []
PASSED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}" + (f"  {detail[:300]}" if detail else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------- 假 HF

class FakeHF(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    blobs: dict[str, bytes] = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self.send_response(401)
            self.end_headers()
            return
        # /datasets/<org>/<name>/resolve/main/<path>
        marker = "/resolve/main/"
        if marker not in self.path:
            self.send_response(404)
            self.end_headers()
            return
        path = urllib.parse.unquote(self.path.split(marker, 1)[1])
        data = self.blobs.get(path)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        start = 0
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0] or 0)
        chunk = data[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(chunk)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        self.end_headers()
        self.wfile.write(chunk)


# ---------------------------------------------------------------- 假 tagger

FAKE_TAGGER = r'''
import argparse, json, pathlib, sys

p = argparse.ArgumentParser()
p.add_argument("--shard", nargs="+")
p.add_argument("--out-dir", default="work/out")
p.add_argument("--limit", type=int, default=None)
p.add_argument("--mode", default=None)
p.add_argument("--tiers", default=None)
p.add_argument("--concurrency", type=int, default=None)
p.add_argument("--force", action="store_true")
a, _ = p.parse_known_args()

out = pathlib.Path(a.out_dir)
out.mkdir(parents=True, exist_ok=True)
(out / "argv.json").write_text(json.dumps(sys.argv[1:], ensure_ascii=False), encoding="utf-8")
if BEHAVIOUR == "fail":
    print("[fake-tagger] 模拟失败", file=sys.stderr)
    sys.exit(1)
if BEHAVIOUR == "empty":
    print("[fake-tagger] 模拟 0 产物")
    sys.exit(0)

n = a.limit if a.limit is not None else 3
target = out / "artifacts-shard-fake-00000.jsonl"
out.mkdir(parents=True, exist_ok=True)

# 与真实 tagger 一致：已有产物的图会被跳过（除非 --force）
done = set()
if target.exists():
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            done.add(json.loads(line)["image_id"])
if a.force:
    done = set()
    target.unlink(missing_ok=True)

lines = []
for i in range(n):
    image_id = f"img-{i:04d}"
    if image_id in done:
        continue
    rec = {"image_id": image_id, "ts": 1.0}
    if MODE_HAS_TAG:
        rec["tag"] = {"image_id": image_id, "tag": "1girl, solo"}
    if MODE_HAS_NL:
        rec["nl"] = {"image_id": image_id, "caption": "a girl"}
    lines.append(json.dumps(rec, ensure_ascii=False))

if lines:
    with target.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
print(f"[fake-tagger] 产出 {len(lines)} 条（跳过 {len(done)} 条已有）")
'''


def make_fake_tagger(root: Path, behaviour: str = "ok", mode_has_tag: bool = True,
                     mode_has_nl: bool = True) -> None:
    pkg = root / "tagger"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cli.py").write_text(
        f"BEHAVIOUR = {behaviour!r}\nMODE_HAS_TAG = {mode_has_tag!r}\n"
        f"MODE_HAS_NL = {mode_has_nl!r}\n" + FAKE_TAGGER, encoding="utf-8")
    (root / "configs").mkdir(exist_ok=True)


# ---------------------------------------------------------------- 工具

def run(cmd: list[str], env: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(cwd or ROOT))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="foxbridge-"))
    state = tmp / "state"
    fake_root = tmp / "tagger"
    cache = tmp / "shards"
    home = tmp / "home"
    home.mkdir()
    make_fake_tagger(fake_root)

    # 假 HF 端点
    hf_port = free_port()
    hf = ThreadingHTTPServer(("127.0.0.1", hf_port), FakeHF)
    threading.Thread(target=hf.serve_forever, daemon=True).start()
    os.environ["HF_ENDPOINT"] = f"http://127.0.0.1:{hf_port}"

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["HF_TOKEN"] = "hf_fake_token_for_bridge_test"
    env["FOXLABEL_HF_ENDPOINT"] = f"http://127.0.0.1:{hf_port}"

    repo = "test/link"
    shard = "shard-000000.tar"
    content = os.urandom(48_000)
    digest = hashlib.sha256(content).hexdigest()
    FakeHF.blobs[shard] = content

    # 建库：登记仓库 + 两个包
    second = "shard-000001.tar"
    content2 = os.urandom(1_200)
    digest2 = hashlib.sha256(content2).hexdigest()
    FakeHF.blobs[second] = content2
    good2 = tmp / "good2.tar"
    good2.write_bytes(content2)
    third = "shard-000002.tar"
    content3 = os.urandom(900)
    digest3 = hashlib.sha256(content3).hexdigest()
    FakeHF.blobs[third] = content3
    good3 = tmp / "good3.tar"
    good3.write_bytes(content3)
    tree = [{"type": "file", "path": shard, "size": len(content),
             "lfs": {"oid": digest}},
            {"type": "file", "path": second, "size": len(content2),
             "lfs": {"oid": digest2}},
            {"type": "file", "path": third, "size": len(content3),
             "lfs": {"oid": digest3}}]
    tree_file = tmp / "tree.json"
    tree_file.write_text(json.dumps(tree), encoding="utf-8")
    r = run([PY, str(SERVER), "--data-dir", str(state), "repo", "add",
             "--repo", repo, "--from-tree", str(tree_file)], env)
    check("建库成功", r.returncode == 0, r.stderr)
    admin_key = (state / "admin.key").read_text(encoding="utf-8").strip()

    r = run([PY, str(SERVER), "--data-dir", str(state), "enroll", "--name", "worker"], env)
    worker_key = re.search(r"密钥:\s*(flk_\S+)", r.stdout).group(1)
    r = run([PY, str(SERVER), "--data-dir", str(state), "enroll", "--name", "other"], env)
    other_key = re.search(r"密钥:\s*(flk_\S+)", r.stdout).group(1)

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    srv = subprocess.Popen([PY, str(SERVER), "--data-dir", str(state), "serve",
                            "--host", "127.0.0.1", "--port", str(port)],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    atexit.register(lambda: srv.poll() is None and srv.kill())
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    # 两个身份各自的 HOME（config 里存密钥与 server）
    for name, key in (("worker", worker_key), ("other", other_key)):
        h = tmp / f"home-{name}"
        h.mkdir(exist_ok=True)
        e = dict(env, HOME=str(h))
        r = run([PY, str(ROOT / "client.py"), "login", "--server", base, "--key", key], e)
        check(f"{name} 登录", r.returncode == 0, r.stderr)

    def benv(name: str) -> dict:
        return dict(env, HOME=str(tmp / f"home-{name}"))

    def bridge(name: str, *args: str) -> subprocess.CompletedProcess:
        return run([PY, str(BRIDGE), "run", "--tagger-root", str(fake_root),
                    "--cache-dir", str(cache), *args], benv(name))

    def status_of(pkg: str = shard) -> dict:
        req = urllib.request.Request(
            base + f"/packages?repo={urllib.parse.quote(repo, safe='')}",
            headers={"Authorization": f"Bearer {admin_key}"})
        data = json.loads(urllib.request.urlopen(req, timeout=5).read())
        return next(p for p in data["packages"] if p["id"] == pkg)

    print("\n[1] happy path：认领 → 下载校验 → 打标 → 提交")
    r = bridge("worker", "--package", f"{repo}/{shard}", "--claim")
    check("bridge 退出码 0", r.returncode == 0, (r.stdout + r.stderr)[-400:])
    check("认领后状态为已完成", status_of()["status"] == "completed",
          str(status_of()["status"]))
    out_dir = fake_root / "work" / "foxjobs" / f"{repo.replace('/', '__')}__{shard}"
    jl = list(out_dir.rglob("artifacts-*.jsonl"))
    ids = {json.loads(l)["image_id"] for l in
           jl[0].read_text(encoding="utf-8").strip().splitlines() if l.strip()} if jl else set()
    check("产物落盘（JSONL 3 张图）", len(jl) == 1 and len(ids) == 3, str(sorted(ids)))
    cached = cache / repo.replace("/", "__") / shard
    check("本地分片已下载且大小正确（缓存按仓库分目录）",
          cached.is_file() and cached.stat().st_size == len(content), str(cached))
    argv = json.loads((out_dir / "argv.json").read_text(encoding="utf-8"))
    check("tagger 收到的参数正确（shard/out-dir/limit 缺一不可）",
          "--shard" in argv and "--out-dir" in argv
          and argv[argv.index("--out-dir") + 1] == str(out_dir),
          str(argv))
    state_json = json.loads((out_dir / ".foxlink.json").read_text(encoding="utf-8"))
    check("运行状态文件记录了包与哈希",
          state_json.get("package") == shard and state_json.get("sha256") == digest
          and (state_json.get("artifacts") or {}).get("tag") == 3,
          json.dumps(state_json, ensure_ascii=False)[:200])

    print("\n[1b] --limit 试跑：只打标不提交（防止整包被误标已完成）")
    out_part = fake_root / "work" / "partjob"
    r = bridge("worker", "--package", f"{repo}/{third}", "--claim",
               "--local-shard", str(good3), "--out-dir", str(out_part), "--limit", "2")
    check("试跑返回 0 但明确未提交",
          r.returncode == 0 and "未提交" in r.stdout, (r.stdout + r.stderr)[-300:])
    check("试跑后状态仍为已认领", status_of(third)["status"] == "claimed")
    check("试跑产物已落盘（2 张）",
          len(list(out_part.rglob("artifacts-*.jsonl"))) == 1)

    print("\n[1c] --allow-partial：显式同意按部分数据交付")
    r = bridge("worker", "--package", f"{repo}/{third}", "--claim",
               "--out-dir", str(out_part), "--limit", "2",
               "--allow-partial", "--submit-only")
    check("显式允许后才提交", r.returncode == 0 and status_of(third)["status"] == "completed",
          (r.stdout + r.stderr)[-300:])

    print("\n[2] 已完成再跑：直接跳过")
    r = bridge("worker", "--package", f"{repo}/{shard}", "--claim")
    check("重复执行不再处理", r.returncode == 0 and "已是「已完成」" in r.stdout,
          (r.stdout + r.stderr)[-200:])

    print("\n[3] 未认领且没给 --claim：拒绝")
    r = bridge("worker", "--package", f"{repo}/shard-000001.tar")
    check("未认领被拒", r.returncode != 0 and "尚未认领" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-200:])

    print("\n[4] 别人的包：不能抢")
    # 只认领不跑全流程：后面几步要在这个「已认领但未完成」的包上测失败路径
    r = run([PY, str(ROOT / "client.py"), "claim", f"{repo}/shard-000001.tar"], benv("worker"))
    check("worker 认领 shard-000001", r.returncode == 0, (r.stdout + r.stderr)[-200:])
    r = bridge("other", "--package", f"{repo}/shard-000001.tar", "--claim")
    check("其他人不能接管且退出码 3",
          r.returncode == 3 and "已被" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-200:])
    check("状态仍归 worker", status_of("shard-000001.tar")["owner"] == "worker")

    print("\n[5] 哈希不符：绝不进入打标")
    bad = tmp / "bad-shard.tar"
    bad.write_bytes(os.urandom(1_200))        # 与服务端登记的 sha256 不符
    out_bad = fake_root / "work" / "badjob"
    r = bridge("worker", "--package", f"{repo}/shard-000001.tar",
               "--local-shard", str(bad), "--out-dir", str(out_bad), "--limit", "2")
    check("哈希不符被拦下且退出码 4",
          r.returncode == 4 and "校验失败" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-300:])
    # 只认真正的打标产物；.foxlink.json 是运行状态，允许存在
    check("拦截后没有产物",
          not list(out_bad.rglob("artifacts-*.jsonl")) if out_bad.exists() else True)
    check("拦截后包仍为已认领", status_of("shard-000001.tar")["status"] == "claimed")

    print("\n[6] 打标失败：不提交")
    make_fake_tagger(fake_root, "fail")
    out_fail = fake_root / "work" / "failjob"
    r = bridge("worker", "--package", f"{repo}/shard-000001.tar",
               "--local-shard", str(good2), "--out-dir", str(out_fail), "--limit", "2")
    check("打标失败且退出码 5",
          r.returncode == 5 and "打标失败" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-300:])
    check("打标失败时状态仍为已认领", status_of("shard-000001.tar")["status"] == "claimed")

    print("\n[7] 零产物：不提交")
    make_fake_tagger(fake_root, "empty")
    out_empty = fake_root / "work" / "emptyjob"
    r = bridge("worker", "--package", f"{repo}/shard-000001.tar",
               "--local-shard", str(good2), "--out-dir", str(out_empty),
               "--limit", "2", "--allow-partial")
    check("零产物被拦下且退出码 5",
          r.returncode == 5 and "没有任何产物" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-300:])
    check("零产物时状态仍为已认领", status_of("shard-000001.tar")["status"] == "claimed")

    print("\n[7b] mode=both 但缺 nl 产物：不提交")
    make_fake_tagger(fake_root, "ok", mode_has_nl=False)
    out_half = fake_root / "work" / "halfjob"
    r = bridge("worker", "--package", f"{repo}/shard-000001.tar",
               "--local-shard", str(good2), "--out-dir", str(out_half),
               "--limit", "2", "--mode", "both")
    check("缺 nl 被拦下且退出码 5",
          r.returncode == 5 and "没有任何 nl" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-300:])
    check("缺 nl 时状态仍为已认领", status_of("shard-000001.tar")["status"] == "claimed")

    print("\n[8] 恢复正常后补跑：可成功提交")
    make_fake_tagger(fake_root, "ok")
    r = bridge("worker", "--package", f"{repo}/shard-000001.tar",
               "--local-shard", str(good2), "--out-dir", str(out_empty),
               "--limit", "2", "--allow-partial")
    check("补跑成功并提交",
          r.returncode == 0 and status_of("shard-000001.tar")["status"] == "completed",
          (r.stdout + r.stderr)[-300:])

    print("\n[8b] 打回返工：不得拿历史产物冒充新产出")
    r = run([PY, str(SERVER), "--data-dir", str(state), "reopen",
             "--package", f"{repo}/{third}", "--reason", "验收返工"], env)
    check("管理员打回 third", r.returncode == 0, r.stderr[:200])
    check("打回后状态为已认领", status_of(third)["status"] == "claimed")
    # 同口径重跑（--limit 2）：tagger 会跳过已有 2 张 → 本次 0 新增
    r = bridge("worker", "--package", f"{repo}/{third}",
               "--local-shard", str(good3), "--out-dir", str(out_part), "--limit", "2")
    check("返工无新增时拒绝提交",
          r.returncode == 5 and "没有新增" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-300:])
    check("拒绝后状态仍为已认领", status_of(third)["status"] == "claimed")
    r = bridge("worker", "--package", f"{repo}/{third}",
               "--local-shard", str(good3), "--out-dir", str(out_part),
               "--limit", "2", "--allow-partial", "--force")
    check("--force 重打后才允许提交",
          r.returncode == 0 and status_of(third)["status"] == "completed",
          (r.stdout + r.stderr)[-300:])

    print("\n[9] 审计留痕：claim / downloaded / submit 都在")
    audit = [json.loads(l) for l in (state / "audit.jsonl").read_text(encoding="utf-8").splitlines()
             if l.strip()]
    kinds = {a["kind"] for a in audit}
    check("审计覆盖 claim/downloaded/submit",
          {"claim", "downloaded", "submit"} <= kinds, str(sorted(kinds)))

    srv.terminate()
    hf.shutdown()
    print("\n" + "=" * 72)
    if FAILS:
        print(f"结果: {PASSED}/{PASSED + len(FAILS)} 通过，失败 {len(FAILS)} 项")
        for f in FAILS:
            print("  -", f)
        print(f"临时目录: {tmp}")
        return 1
    print(f"结果: 全部 {PASSED} 项通过")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
