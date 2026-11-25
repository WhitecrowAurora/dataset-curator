"""curl 分段传输、续传与有限重试。每次重试重新读取本地偏移。"""
from concurrent.futures import ThreadPoolExecutor
import re
import shutil
import subprocess
import threading
import time

from .download_network import curl_env
from .download_segments import segment_plan

CONNECT_TIMEOUT = 15
STALL_TIMEOUT = 30
RETRY_DELAY = 2
RETRIES = 3


class DownloadError(Exception):
    pass


class RangeUnsupported(DownloadError):
    """源忽略了 Range，需要退回单流。"""


def _command(url, hdr_file, resolve):
    cmd = ["curl", "-sS", "-L", "--fail", "--connect-timeout", str(CONNECT_TIMEOUT),
           "--speed-limit", "1", "--speed-time", str(STALL_TIMEOUT), *resolve]
    if hdr_file:
        cmd += ["-H", f"@{hdr_file}"]
    return cmd + [url]


def _retryable(rc, error):
    if rc == 22:
        return bool(re.search(r"\b(?:408|429|500|502|503|504)\b", error))
    return rc in (5, 6, 7, 16, 18, 28, 35, 52, 55, 56, 92)


def _retry(attempt, rc, error, state, stopped, log):
    if state.stop.is_set() or stopped.is_set():
        raise DownloadError("下载已中断")
    if attempt >= RETRIES or not _retryable(rc, error):
        raise DownloadError(f"curl 退出码 {rc} {error[-200:]}")
    log(f"[warn] 下载连接中断（curl {rc}），{RETRY_DELAY:g} 秒后续传（重试 {attempt + 1}/{RETRIES}）")
    until = time.monotonic() + RETRY_DELAY
    while time.monotonic() < until:
        if state.stop.wait(min(0.1, max(0, until - time.monotonic()))) or stopped.is_set():
            raise DownloadError("下载已中断")


def stream(url, part, hdr_file, resolve, total, state, log):
    stopped = threading.Event()
    for attempt in range(RETRIES + 1):
        if state.stop.is_set():
            raise DownloadError("下载已中断")
        if total and part.is_file() and part.stat().st_size == total:
            return  # 数据齐全后交给上层校验，避免向 EOF 再发 Range。
        cmd = _command(url, hdr_file, resolve) + ["-C", "-", "-o", str(part)]
        with subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=curl_env(url)) as proc:
            while proc.poll() is None:
                if state.stop.wait(0.1):
                    proc.terminate()
                    break
                if part.exists() and total:
                    state.done = min(99, int(part.stat().st_size / total * 100))
            rc = proc.wait()
            error = proc.stderr.read().decode(errors="replace")
        if state.stop.is_set():
            raise DownloadError("下载已中断")
        if rc == 0 and part.exists():
            if total and part.stat().st_size != total:
                raise DownloadError(f"下载大小不符：期望 {total}，实得 {part.stat().st_size}")
            return
        _retry(attempt, rc, error, state, stopped, log)


def segments(url, dest, hdr_file, resolve, total, conns, state, source_id, log):
    bounds, paths = segment_plan(dest, total, conns, source_id or url)
    procs = [None] * len(bounds)
    errors = [None] * len(bounds)
    stopped = threading.Event()
    pending = sum(not path.exists() or path.stat().st_size != end - start + 1
                  for (start, end), path in zip(bounds, paths))
    log(f"分段续传：{len(bounds)} 段，待下载 {pending} 段，最多 {min(conns, pending)} 连接")

    def worker(i, start, end, path):
        try:
            want = end - start + 1
            for attempt in range(RETRIES + 1):
                if state.stop.is_set() or stopped.is_set():
                    return
                have = path.stat().st_size if path.exists() else 0
                if have > want:
                    path.write_bytes(b"")
                    have = 0
                if have == want:
                    return
                cmd = _command(url, hdr_file, resolve) + ["-r", f"{start + have}-{end}"]
                with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=curl_env(url)) as proc:
                    procs[i] = proc
                    try:
                        with path.open("ab") as fh:
                            written = have
                            while chunk := proc.stdout.read1(1 << 20):
                                written += len(chunk)
                                if written > want:
                                    raise RangeUnsupported("服务端忽略 Range（返回了整文件），退回单流下载")
                                fh.write(chunk)
                                if state.stop.is_set() or stopped.is_set():
                                    break
                    except BaseException:
                        if proc.poll() is None:
                            proc.terminate()
                        raise
                    if proc.poll() is None and (state.stop.is_set() or stopped.is_set()):
                        proc.terminate()
                    rc = proc.wait()
                    error = proc.stderr.read().decode(errors="replace")
                if rc == 0 and path.stat().st_size == want:
                    return
                _retry(attempt, rc or 18, error, state, stopped, log)
        except Exception as exc:
            errors[i] = exc
            stopped.set()

    with ThreadPoolExecutor(max_workers=conns, thread_name_prefix="segment") as pool:
        tasks = [pool.submit(worker, i, start, end, paths[i]) for i, (start, end) in enumerate(bounds)]
        while any(not task.done() for task in tasks):
            if state.stop.is_set() or stopped.is_set():
                for proc in procs:
                    if proc is not None and proc.poll() is None:
                        proc.terminate()
            got = sum(path.stat().st_size for path in paths if path.exists())
            state.done = min(99, int(got / total * 100))
            time.sleep(0.1)
        for task in tasks:
            task.result()
    if state.stop.is_set():
        raise DownloadError("下载已中断")
    failures = [e for e in errors if e is not None]
    if failures:
        original = next((e for e in failures if str(e) != "下载已中断"), failures[0])
        raise next((e for e in failures if isinstance(e, RangeUnsupported)), original)
    part = dest.with_suffix(dest.suffix + ".part")
    with part.open("wb") as out:
        for path in paths:
            with path.open("rb") as fh:
                shutil.copyfileobj(fh, out, 1 << 20)
    if part.stat().st_size != total:
        raise DownloadError(f"拼接后大小不符：期望 {total}，实得 {part.stat().st_size}")
    for path in paths:
        path.unlink(missing_ok=True)
