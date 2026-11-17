"""有限网络重试与不会携带凭据的错误说明。"""
import errno
import socket
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead, RemoteDisconnected
from urllib.error import HTTPError, URLError

ATTEMPTS = 3
TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}


def temporary(exc):
    if isinstance(exc, HTTPError):
        return exc.code in TRANSIENT_HTTP
    if isinstance(exc, URLError):
        return temporary(exc.reason)
    return isinstance(exc, (TimeoutError, ConnectionError, IncompleteRead, RemoteDisconnected)) or (
        isinstance(exc, socket.gaierror) and exc.errno == socket.EAI_AGAIN) or (
        isinstance(exc, OSError) and exc.errno in {errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED})


def describe(exc):
    if isinstance(exc, HTTPError):
        names = {401: "读取凭据无效", 403: "没有仓库读取权限", 404: "仓库、版本或文件不存在",
                 429: "仓库请求限流"}
        return names.get(exc.code, "仓库服务错误") + f"（HTTP {exc.code}）"
    if isinstance(exc, URLError):
        return describe(exc.reason)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "仓库连接超时"
    if temporary(exc):
        return "仓库连接中断"
    return str(exc) if isinstance(exc, ValueError) else "清单读取失败：" + type(exc).__name__


def check(canceled):
    if canceled is not None and canceled.is_set():
        raise InterruptedError("清单任务已停止")


def pause(exc, attempt, canceled=None, notify=None):
    """仅临时故障可重试；认证、格式和摘要错误立即失败。"""
    check(canceled)
    if not temporary(exc) or attempt >= ATTEMPTS:
        raise exc
    delay = 2 ** attempt
    raw = exc.headers.get("Retry-After") if isinstance(exc, HTTPError) else None
    if raw:
        try:
            delay = float(raw)
        except ValueError:
            try:
                stamp = parsedate_to_datetime(raw)
                delay = stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                pass
    if isinstance(exc, HTTPError):
        exc.close()
    delay = max(0, min(delay, 30))
    if notify:
        notify(f"{describe(exc)}，{delay:g} 秒后重试（{attempt + 1}/{ATTEMPTS}）")
    if canceled is None:
        time.sleep(delay)
    else:
        canceled.wait(delay)
    check(canceled)


def call(operation, canceled=None, notify=None):
    for attempt in range(1, ATTEMPTS + 1):
        check(canceled)
        try:
            return operation()
        except Exception as exc:
            pause(exc, attempt, canceled, notify)
