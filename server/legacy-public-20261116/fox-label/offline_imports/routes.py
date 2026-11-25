"""已通过管理员鉴权和写开关的接入适配。"""
import zipfile
from . import service


def upload(handler, ctx):
    import dispatch
    lengths = handler.headers.get_all('Content-Length', [])
    if len(lengths) != 1 or handler.headers.get('Transfer-Encoding'):
        handler.close_connection = True
        return handler._json(400, {'ok': False, 'error': '上传必须提供唯一 Content-Length'})
    timeout = handler.connection.gettimeout()
    try:
        length = int(lengths[0])
        if not 0 < length <= service.MAX_UPLOAD:
            handler.close_connection = True
            return handler._json(413, {'ok': False, 'error': '网页回收包不能超过 64 MiB'})
        if not handler.store.delivery:raise ValueError('服务器未启用产物接收')
        handler.connection.settimeout(180)
        return handler._json(200, service.receive(handler.store, handler.rfile, length,
                                                 handler.headers.get('X-Workspace-Id', ''), ctx['actor']))
    except (ValueError, zipfile.BadZipFile, UnicodeError) as exc:
        return handler._json(400, {'ok': False, 'error': str(exc)})
    except dispatch.AuthError as exc:
        return handler._json(401, {'ok': False, 'error': str(exc)})
    except OSError:
        handler.close_connection = True
        return handler._json(503, {'ok': False, 'error': '服务器暂时无法保存回收包，请稍后重试'})
    finally:handler.connection.settimeout(timeout)


def action(handler, dispatch, body, ctx):
    try:
        return handler._json(200, service.action(handler.store, dispatch, body, ctx['actor']))
    except (ValueError, zipfile.BadZipFile, UnicodeError) as exc:
        return handler._json(400, {'ok': False, 'error': str(exc)})
    except OSError:
        return handler._json(503, {'ok': False, 'error': '回收包暂时无法读取或发布，请稍后重试；本地产物保留'})
