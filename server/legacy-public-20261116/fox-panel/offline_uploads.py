"""流式代理离线回收包；面板只通过 HTTP 访问调度服务。"""
import http.client
import json
import socket
from urllib.parse import urlsplit

MAX_UPLOAD = 64 * 1024 * 1024


def upload(handler):
    lengths = handler.headers.get_all('Content-Length', [])
    try:
        if len(lengths) != 1 or handler.headers.get('Transfer-Encoding'):
            raise ValueError('上传必须提供唯一 Content-Length')
        length = int(lengths[0])
        if not 0 < length <= MAX_UPLOAD:raise ValueError('网页回收包不能超过 64 MiB')
        ws = handler.headers.get('X-Workspace-Id', '')
        if not ws or len(ws) > 128 or any(not (ch.isascii() and (ch.isalnum() or ch in '_-')) for ch in ws):
            raise ValueError('请选择有效的工作区')
    except ValueError as exc:
        handler.close_connection = True
        return handler._json(413, {'ok': False, 'error': str(exc)})
    up = urlsplit(handler.up.base)
    connection = (http.client.HTTPSConnection if up.scheme == 'https' else http.client.HTTPConnection)(
        up.hostname, up.port, timeout=180)
    previous_timeout = handler.connection.gettimeout()
    try:
        handler.connection.settimeout(180)
        headers = {'Authorization': 'Bearer ' + handler.up.key(), 'Content-Type': 'application/octet-stream',
                   'Content-Length': str(length), 'X-Workspace-Id': ws, **handler._write_headers()}
        connection.putrequest('POST', up.path.rstrip('/') + '/admin/v1/offline-import-upload')
        for key, value in headers.items():connection.putheader(key, value)
        connection.endheaders()
        remaining = length
        while remaining:
            block = handler.rfile.read(min(64 * 1024, remaining))
            if not block:raise ValueError('上传被中断，请重新选择回收包')
            connection.send(block);remaining -= len(block)
        response = connection.getresponse()
        raw = response.read()
        return handler._pass_through(response.status, raw, {})
    except (OSError, socket.timeout, http.client.HTTPException, ValueError):
        handler.close_connection = True
        return handler._json(503, {'ok': False, 'error': '回收包上传中断，请重试；尚未确认的结果不会登记完成'})
    finally:
        connection.close();handler.connection.settimeout(previous_timeout)
