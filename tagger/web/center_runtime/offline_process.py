"""离线任务的下载和执行子进程，不连接调度中心。"""
import json
from collections import deque
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

from ...runtime.processes import spawn_owned


def _valid(path, task):
    from ...fox_worker import sha256_file
    return (path.is_file() and path.stat().st_size > 0
            and (not task.get("size") or path.stat().st_size == task["size"])
            and (not task.get("sha256") or sha256_file(path) == task["sha256"]))


def _shard(task, shards, state):
    from ... import fox_worker as worker
    cache = shards / task["repo_type"] / task["repo"]
    dest = cache / task["package"]
    if not dest.exists():
        copied = shards / task["package"]
        if task.get("sha256") and _valid(copied, task):
            return copied
    elif not _valid(dest, task):
        archived = shards / ".delete" / str(time.time_ns()) / dest.relative_to(shards)
        archived.parent.mkdir(parents=True, exist_ok=True)
        dest.replace(archived)
    downloaded = worker.download(task, cache, state, {})
    if not _valid(downloaded, task):
        raise worker.DownloadError("分片下载大小或哈希与离线清单不一致：" + task["package"])
    return downloaded


def _child(shards, out):
    from ... import fox_worker as worker
    from ...storage.atomic import write_bytes
    task = json.load(sys.stdin)
    worker.DL_CONNS = max(1,min(16,int(os.environ.get("TAGGER_DL_CONNS","4"))))
    worker.DL_CDN_PROBE = os.environ.get("FOXLABEL_CDN_PROBE","1") != "0"
    state = worker.RunState()
    stop = Path(os.environ["FOXLABEL_STOP_FILE"])
    def watch():
        while not state.stop.wait(.2):
            if stop.exists():
                state.stop.set()
    threading.Thread(target=watch, daemon=True).start()
    start = time.monotonic()
    shard = _shard(task, shards, state)
    metrics = {"source": str(shard.resolve()), "download_s": round(time.monotonic() - start, 2), **worker._dl_stats(task)}
    if state.stop.is_set():
        return 1
    env = {**os.environ, "FOXLABEL_PARTS": ",".join(task["missing_parts"]), "FOXLABEL_PACKAGE": task['package']}
    from ...center_runtime.artist import prepare as prepare_artist
    prepare_artist({}, task, out)
    from ...center_runtime.annotation import environment as annotation_environment
    env.update(annotation_environment(task))
    start = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-m", "tagger.cli", "client", "--shard", str(shard),
                             "--out-dir", str(out), "--artifact-format", "jsonl",
                             "--progress-file", str(out / ".fox-progress")], env=env)
    rc = proc.wait()
    if rc:
        return rc
    progress = out / ".fox-progress"
    final = json.loads(progress.read_text(encoding="utf-8")) if progress.is_file() else {}
    metrics.update(task_s=round(time.monotonic() - start, 2), images=final.get("total", 0))
    write_bytes(out / ".offline-metrics.json", json.dumps(metrics).encode())
    return 0


def execute(task, shards, out, env, cancel, log):
    stop = out / ("stop-request-" + secrets.token_hex(8))
    env = {**env, "FOXLABEL_STOP_FILE": str(stop), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = spawn_owned([sys.executable, "-B", "-m", __name__, str(shards), str(out)], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            **({"start_new_session": True} if sys.platform != "win32" else {}))
    tail = deque(maxlen=8)
    failure = []
    def pump():
        for line in proc.stdout:
            line = line.rstrip()
            if line:tail.append(line)
            if "Error:" in line or "[client] 已停止：" in line or "[client] HF 访问失败：" in line:
                failure[:] = [line]
            log(line)
    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    try:
        proc.stdin.write(json.dumps(task)); proc.stdin.close()
        while proc.poll() is None:
            if cancel.wait(.2):
                stop.touch()
                raise ValueError("已停止离线任务")
        if proc.returncode:
            thread.join(timeout=2)
            detail = ": " + (failure[-1] if failure else tail[-1]) if tail else ""
            raise ValueError(f"离线分包执行失败（退出码 {proc.returncode}）{detail}")
        return json.loads((out / ".offline-metrics.json").read_text(encoding="utf-8"))
    finally:
        if proc.poll() is None:
            from ..center import _kill_tree
            _kill_tree(proc.pid)
            proc.wait(timeout=15)
        thread.join(timeout=2)
        proc.stdout.close()


if __name__ == "__main__":
    sys.exit(_child(Path(sys.argv[1]), Path(sys.argv[2])))
