"""设备轮询接入；管理接口复用现有管理员鉴权。"""
import sqlite3

from .contracts import ManagedError
from .store import poll


def handle_poll(handler, dispatch, payload_error):
    try:
        body = handler._read_json(limit=256 * 1024, strict=True)
        result = poll(handler.store, dispatch, body)
    except payload_error:
        return handler._json(413, {"error": "托管上报过大"})
    except dispatch.AuthError as exc:
        return handler._json(401, {"error": str(exc)})
    except ManagedError as exc:
        return handler._json(exc.status, exc.body())
    except (ValueError, UnicodeDecodeError, TypeError):
        return handler._json(400, {"error": "托管上报格式不正确"})
    except (sqlite3.Error, OSError):
        return handler._json(503, {"error": "托管存储暂时不可用"})
    return handler._json(200, result)
