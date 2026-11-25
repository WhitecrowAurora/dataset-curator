"""网页登录管理密钥，MCP 请求仅使用独立 Bearer 密钥。"""
import hmac
import json
from functools import lru_cache

from .store import AccessStore
from .protocol import VERSIONS, handle


@lru_cache(maxsize=4)
def access_store(path):
    return AccessStore(path)


def store_for(handler):
    from panel import CONFIG
    return access_store(str(handler.cfg.get('ai_access_file') or CONFIG.parent/'ai-access.sqlite3'))


def management(handler, method):
    from panel import write_enabled
    actor=handler._cookie_user()
    if not actor:return handler._json(401,dict(error='未登录'))
    store=store_for(handler)
    if method=='GET':
        value=store.view();value['write_enabled']=write_enabled(handler.cfg)
        return handler._json(200,value)
    if not write_enabled(handler.cfg):return handler._json(403,dict(error='面板处于只读模式'))
    from panel import WRITE_LIMIT
    if WRITE_LIMIT.take('ai-key:'+actor+'|'+handler._ip())>0:
        return handler._json(429,dict(error='操作过于频繁，请稍后再试'))
    cookie=handler._csrf_token() or '';header=handler.headers.get('X-CSRF-Token') or ''
    if not cookie or not hmac.compare_digest(cookie,header) or not handler._origin_ok() or not handler._fetch_site_ok():
        return handler._json(403,dict(error='来源或 CSRF 校验失败'))
    if (handler.headers.get('Content-Type') or '').split(';')[0].strip().lower()!='application/json':
        return handler._json(415,dict(error='Content-Type 必须为 application/json'))
    try:
        data=json.loads(handler._body(8192))
        if not isinstance(data,dict):raise ValueError('请求格式不正确')
        if data.get('action')=='create':
            result=store.create(data.get('name'),data.get('role','operate'),data.get('days',30),actor)
        elif data.get('action')=='revoke':result=store.revoke(data.get('id'))
        else:raise ValueError('未知密钥操作')
        return handler._json(200,result)
    except (ValueError,TypeError) as exc:return handler._json(400,dict(error=str(exc)))


def mcp(handler, method):
    from panel import PREVIEW_LIMIT
    # Origin is optional for non-browser MCP clients; when present it must be the
    # same public origin already configured for the administrator page.
    origin=handler.headers.get('Origin')
    if origin and (origin.lower().rstrip('/')!=handler._expected_origin()):
        return handler._json(403,dict(error='MCP 来源不被信任'))
    auth=handler.headers.get('Authorization') or ''
    key=auth[7:].strip() if auth.startswith('Bearer ') else ''
    store=store_for(handler);identity=store.authenticate(key)
    if not identity:
        handler.close_connection=True
        return handler._json(401,dict(error='需要有效的 AI 接口密钥'),{'WWW-Authenticate':'Bearer realm="lulynx-center"'})
    wait=PREVIEW_LIMIT.take('mcp:'+identity['id']+'|'+handler._ip())
    if wait>0:return handler._json(429,dict(error='请求过于频繁'),{'Retry-After':str(max(1,int(wait+1)))})
    if method!='POST':return handler._json(405,dict(error='请使用 MCP HTTP POST'),{'Allow':'POST'})
    version=handler.headers.get('MCP-Protocol-Version')
    if version and version not in VERSIONS:return handler._json(400,dict(error='MCP 协议版本不支持'))
    accept=handler.headers.get('Accept') or ''
    if 'application/json' not in accept and '*/*' not in accept:return handler._json(406,dict(error='Accept 必须包含 application/json'))
    if (handler.headers.get('Content-Type') or '').split(';')[0].strip().lower()!='application/json':
        return handler._json(415,dict(error='Content-Type 必须为 application/json'))
    try:
        message=json.loads(handler._body(128*1024))
    except (ValueError,UnicodeDecodeError):return handler._json(400,dict(error='MCP 请求体不是合法 JSON 或超过128KiB'))
    result=handle(handler,store,identity,message)
    if result is None:return handler._send(202,b'','application/json',{'Cache-Control':'no-store'})
    return handler._json(200,result)
