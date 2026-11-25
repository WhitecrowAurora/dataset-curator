"""分片下载的独立网络策略；不影响调度通信和本机推理。"""
import json
import os
from urllib.parse import urlsplit
from urllib.request import proxy_bypass_environment

from . import network

DEFAULTS = {"download_proxy_mode": "auto", "download_proxy_url": ""}
ENV_KEY = "LULYNX_DOWNLOAD_NETWORK"


def config(values):
    mode = values.get("download_proxy_mode", "auto")
    address = str(values.get("download_proxy_url") or "").strip()
    if mode == "center":
        return {"download_proxy_mode": mode, "download_proxy_url": address}
    policy = network.config({"proxy_mode": mode, "proxy_url": address})
    return {"download_proxy_mode": mode, "download_proxy_url": policy["proxy_url"]}


def environment(values):
    saved = config(values)
    policy = network.config(values) if saved["download_proxy_mode"] == "center" else {
        "proxy_mode": saved["download_proxy_mode"], "proxy_url": saved["download_proxy_url"]}
    return {ENV_KEY: json.dumps(policy)}


def curl_env(url):
    try:
        policy = json.loads(os.environ.get(ENV_KEY, "{}"))
        policy = network.config(policy)
    except (ValueError, TypeError, AttributeError):
        policy = network.DEFAULTS
    return network.curl_env(url, policy)


def uses_proxy(url):
    env = curl_env(url)
    target = urlsplit(url)
    no_proxy = env.get("no_proxy", env.get("NO_PROXY", ""))
    if proxy_bypass_environment(target.netloc, {"no": no_proxy}):
        return False
    key = target.scheme + "_proxy"
    return bool(env.get(key) or env.get(key.upper()) or env.get("all_proxy") or env.get("ALL_PROXY"))
