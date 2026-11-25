"""调度连接的本机网络策略，供页面和独立 worker 共用。"""
from functools import lru_cache
import json
import os
from urllib.parse import urlsplit
import urllib.request

from .. import netutil

DEFAULTS = {"proxy_mode": "auto", "proxy_url": ""}
ENV_KEY = "LULYNX_CENTER_NETWORK"


def config(values):
    mode = values.get("proxy_mode", "auto")
    address = str(values.get("proxy_url") or "").strip()
    if mode not in ("auto", "direct", "custom"):
        raise ValueError("连接方式请选择自动、直连或指定代理")
    if mode == "custom" and address:
        try:
            parsed = urlsplit(address)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and parsed.port and not parsed.username and not parsed.password
                     and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
                     and not any(ch.isspace() for ch in address))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("代理地址请填写 HTTP / HTTPS 地址和端口，例如 http://127.0.0.1:47826")
        address = address.rstrip("/")
    if mode == "custom" and not address:
        raise ValueError("指定代理时请填写代理地址")
    return {"proxy_mode": mode, "proxy_url": address}


def environment(server, values):
    return {ENV_KEY: json.dumps({"server": server, **config(values)})}


def _policy(url, values):
    if netutil.is_loopback_url(url):
        return "direct", ""
    if values is None or "proxy_mode" not in values:
        try:
            saved = json.loads(os.environ.get(ENV_KEY, "{}"))
            target, center = urlsplit(url), urlsplit(saved.get("server", ""))
            values = saved if (target.scheme, target.hostname, target.port) == (center.scheme, center.hostname, center.port) else {}
        except (ValueError, TypeError, AttributeError):
            values = {}
    policy = config(values)
    return policy["proxy_mode"], policy["proxy_url"]


def httpx_options(url, values=None):
    mode, proxy = _policy(url, values)
    if mode == "direct":
        return {"trust_env": False}
    if mode == "custom":
        return {"trust_env": False, "proxy": proxy}
    return netutil.httpx_options(url)


class _ExplicitProxy(urllib.request.ProxyHandler):
    """用户指定代理时，不让系统 NO_PROXY 把请求偷偷改成直连。"""
    def proxy_open(self, req, proxy, type):
        original = req.type
        parsed = urlsplit(proxy)
        req.set_proxy(parsed.netloc, parsed.scheme)
        if original == parsed.scheme or original == "https":
            return None
        return self.parent.open(req, timeout=req.timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        return None


@lru_cache(maxsize=16)
def _opener(mode, proxy, redirects):
    handlers = []
    if mode == "direct":
        handlers.append(urllib.request.ProxyHandler({}))
    elif mode == "custom":
        handlers.append(_ExplicitProxy({"http": proxy, "https": proxy}))
    if not redirects:
        handlers.append(_NoRedirect())
    return urllib.request.build_opener(*handlers)


def opener(url, values=None, *, redirects=False):
    return _opener(*_policy(url, values), redirects)


def curl_env(url, values=None):
    mode, proxy = _policy(url, values)
    env = netutil.curl_env(url)
    if mode != "auto":
        keys = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
        for key in keys:
            env.pop(key, None)
            env.pop(key.upper(), None)
        if mode == "direct":
            env.update(no_proxy="*", NO_PROXY="*")
        else:
            env.update(http_proxy=proxy, https_proxy=proxy, no_proxy="", NO_PROXY="")
    return env
