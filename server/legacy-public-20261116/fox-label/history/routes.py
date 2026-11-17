"""历史检查完成接入，沿用客户端身份和托管会话。"""
from .store import finish
from .client import begin, cancel, catalog, reports
from managed.contracts import ManagedError


def handle(handler, dispatch, method, writable):
    action = handler.path.split("?", 1)[0].removeprefix('/api/v1/history/')
    handlers = {'finish': finish, 'begin': begin, 'cancel': cancel, 'catalog': catalog, 'reports': reports}
    if not handler.path.split("?", 1)[0].startswith('/api/v1/history/') or action not in handlers:
        return False
    try:
        if method != "POST":
            handler._json(405, {"error": "需要 POST"})
        elif action not in ('catalog', 'reports') and not writable():
            handler._json(403, {"error": "服务端写通道已关闭"})
        else:
            handler._json(200, handlers[action](handler.store, dispatch, handler._read_json(limit=65536, strict=True)))
    except dispatch.AuthError as exc:
        handler._json(401, {"error": str(exc)})
    except (ManagedError, ValueError) as exc:
        handler._json(getattr(exc, "status", 400), {"error": str(exc)})
    return True
