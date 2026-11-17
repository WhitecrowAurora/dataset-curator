"""复用调度身份的分块接收接口，不创建用户或领取租约。"""
import json
from contextlib import contextmanager
from urllib.parse import parse_qs, unquote, urlparse

from .blobs import BlobError, MAX_CHUNK


def _identity(handler, dispatch, c):
    user = unquote(handler.headers.get("X-Fox-User", ""))
    device_id = handler.headers.get("X-Fox-Device", "")
    if handler.headers.get("X-Fox-History"):
        from history.store import identity
        check, device = identity(c, dispatch, user, device_id,
                                 handler.headers.get("X-Fox-Secret", ""),
                                 handler.headers["X-Fox-History"], handler.headers.get("X-Fox-Session", ""))
        if check["status"] in ("registered", "incomplete", "verifying"):
            raise ValueError("历史检查已登记，不能覆盖验收产物")
        return {"repo": check["repo"], "pkg": check["package"],
                "lease_id": check["check_id"], "kind": "history"}, device
    device = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
    if not device or device["user_id"] != user:
        raise dispatch.AuthError("设备与用户不匹配")
    # 先检查归属，避免旧调度鉴权的自动改绑行为影响派工记录。
    device = dispatch._require_device(c, user, device_id,
                                      handler.headers.get("X-Fox-Secret", ""))
    status = c.execute("SELECT status FROM users WHERE user_id=?", (user,)).fetchone()
    lease = c.execute("SELECT * FROM leases WHERE lease_id=?",
                      (handler.headers.get("X-Fox-Lease", ""),)).fetchone()
    if not status or status["status"] != "active" or device["status"] in ("archived", "blocked"):
        raise dispatch.AuthError("身份不可接收产物")
    if (not lease or lease["user_id"] != user or lease["device_id"] != device_id
            or lease["kind"] not in ("active", "offline") or lease["status"] not in ("open", "done")):
        raise ValueError("当前身份没有这个可回传的租约")
    if lease["kind"] == "offline":
        from assignments.offline import authenticate
        authenticate(c,dispatch,lease,handler.headers.get("X-Fox-Secret",""))
    newer = c.execute("SELECT 1 FROM lease_parts old JOIN lease_parts n ON n.repo=old.repo AND n.pkg=old.pkg AND n.part=old.part"
                       " JOIN leases later ON later.lease_id=n.lease_id JOIN leases prior ON prior.lease_id=old.lease_id"
                       " WHERE old.lease_id=? AND (later.issued_at>prior.issued_at OR"
                       " (later.issued_at=prior.issued_at AND later.rowid>prior.rowid)) LIMIT 1",
                       (lease["lease_id"],)).fetchone()
    if newer:
        raise ValueError("不能回传旧轮次租约")
    repo = c.execute("SELECT enabled FROM repos WHERE repo=?", (lease["repo"],)).fetchone()
    if not repo or not repo["enabled"]:
        raise ValueError("仓库已停用")
    return dict(lease), device


def handle(handler, dispatch, method, writable):
    path = urlparse(handler.path).path.rstrip("/")
    if not path.startswith("/api/blob/"):
        return False
    svc = handler.store.delivery
    if svc is None:
        handler._json(503, {"error": "未启用调度产物接收"})
        return True
    try:
        if method == "POST" and not writable():
            handler._json(403, {"error": "服务端写通道已关闭"})
            return True
        c = handler.store.conn()
        try:
            lease, device = _identity(handler, dispatch, c)
        finally:
            c.close()
        q = parse_qs(urlparse(handler.path).query)
        if path in ('/api/blob/resume', '/api/blob/reconcile') and lease['kind'] != 'history':
            from .reconciliation import snapshot, accept
            c = handler.store.conn()
            try:
                if method == 'GET' and path == '/api/blob/resume':
                    pkg = dispatch._row(c, 'SELECT sha256 FROM packages WHERE repo=? AND id=?', (lease['repo'], lease['pkg']))
                    out = snapshot(c, lease['repo'], lease['pkg'], pkg['sha256'])
                elif method == 'POST' and path == '/api/blob/reconcile':
                    handler._read_json()
                    with handler.store._write_lock, dispatch._Tx(c):
                        lease, device = _identity(handler, dispatch, c)
                        out = accept(handler.store, dispatch, c, lease, device)
                else:
                    raise ValueError('请求方法不正确')
            finally:
                c.close()
        elif method == "GET" and path == "/api/blob/auth":
            out = {"required": True, "identity": "fox-worker"}
        elif method == "GET" and path == "/api/blob/have":
            have, bad = svc.blobs.have((q.get("sha") or [""])[0].split(","))
            out = {"have": have, "bad": bad}
        elif method == "POST" and path == "/api/blob/chunk":
            lengths = handler.headers.get_all("Content-Length") or []
            if len(lengths) != 1 or handler.headers.get("Transfer-Encoding"):
                raise ValueError("需要唯一的 Content-Length")
            length = int(lengths[0])
            if not 0 < length <= MAX_CHUNK:
                raise ValueError("块大小超出上限")
            data = handler.rfile.read(length)
            if len(data) != length:
                raise ValueError("块正文不完整")
            out = {"new": svc.blobs.put_chunk((q.get("sha") or [""])[0], data), "bytes": length}
        elif method == "POST" and path == "/api/blob/commit":
            body = handler._read_json(limit=1 << 20, strict=True)
            if not isinstance(body, dict):
                raise ValueError("清单必须是 JSON 对象")
            @contextmanager
            def publication():
                with handler.store._write_lock:
                    if not writable():
                        raise ValueError("服务端写通道已关闭")
                    c = handler.store.conn()
                    try:
                        with dispatch._Tx(c):
                            _identity(handler, dispatch, c)
                            yield
                    finally:
                        c.close()
            if lease["kind"] == "history":
                from history.uploads import publish
                out = publish(svc, lease, device, body, publication)
            else:
                out = svc.publish(lease, device, body, publication)
        else:
            handler._json(404, {"error": "未知回传接口"})
            return True
        handler._json(200, out)
    except dispatch.AuthError as e:
        handler._json(401, {"error": str(e)})
    except (ValueError, BlobError) as e:
        handler._json(400, {"error": str(e)})
    except Exception as e:
        handler._note("delivery", 503, type(e).__name__)
        handler._json(503, {"error": "产物发布失败，请检查目标目录或 HF 权限；本机产物保留"})
    return True
