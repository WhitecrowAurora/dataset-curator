"""复用调度写开关、设备身份和数据库事务。"""
import sqlite3
from .store import SkipError, receive


def handle(handler, dispatch, payload_error, writable):
    try:
        if not writable():
            return handler._json(403, {"error": "服务端写通道已关闭"})
        body = handler._read_json(strict=True)
        with handler.store._write_lock:
            c = handler.store.conn()
            try:
                result = receive(c, dispatch, body)
            finally:
                c.close()
    except payload_error:
        return handler._json(413, {"error": "跳过明细上报过大"})
    except dispatch.AuthError as exc:
        return handler._json(401, {"error": str(exc)})
    except dispatch.LeaseError as exc:
        return handler._json(409, {"error": str(exc)})
    except (SkipError, ValueError, TypeError, UnicodeDecodeError) as exc:
        return handler._json(400, {"error": str(exc)})
    except (sqlite3.Error, OSError):
        return handler._json(503, {"error": "明细存储暂时不可用，请重试"})
    return handler._json(200, result)
