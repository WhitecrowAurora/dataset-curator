"""沿用管理员会话下载，外部链接仅能读取指定结果快照。"""
import re
import urllib.request
from urllib.parse import parse_qs,urlsplit,urlencode


def stream(handler,raw,*,public=False):
    parsed=urlsplit(raw);eid=parsed.path.rsplit('/',1)[-1]
    if not re.fullmatch('[0-9a-f]{32}',eid):
        return handler._json(400,{'error':'结果导出 ID 不正确'})
    query=''
    if public:
        token=(parse_qs(parsed.query).get('token') or [''])[0]
        if not re.fullmatch('[A-Za-z0-9_-]{40,60}',token):
            return handler._json(403,{'error':'分享链接不正确或已失效'})
        query='?'+urlencode({'token':token})
    req=urllib.request.Request(handler.up.base+'/admin/v1/result-exports/'+eid+query,
                               headers={'Authorization':'Bearer '+handler.up.key()})
    try:
        response=urllib.request.urlopen(req,timeout=180)
    except urllib.error.HTTPError as exc:
        return handler._json(exc.code,{'error':'分享链接已失效、结果未生成或已撤销，请联系管理员' if public else '结果快照尚未准备完成，请刷新导出记录'})
    except (OSError,urllib.error.URLError):
        return handler._json(502,{'error':'结果下载暂时不可用，请稍后重试'})
    with response:
        length=response.headers.get('Content-Length','');digest=response.headers.get('X-Artifact-SHA256','')
        if not length.isdigit() or not re.fullmatch('[0-9a-f]{64}',digest):
            return handler._json(502,{'error':'结果文件响应不完整'})
        handler._send(200,b'','application/zip',{'Content-Length':length,'X-Artifact-SHA256':digest,
            'Content-Disposition':'attachment; filename="lulynx-results-'+eid+'.zip"','Cache-Control':'no-store'})
        remaining=int(length)
        try:
            while remaining:
                chunk=response.read(min(1024*1024,remaining))
                if not chunk:handler.close_connection=True;return
                handler.wfile.write(chunk);remaining-=len(chunk)
        except (OSError,ConnectionError):handler.close_connection=True
