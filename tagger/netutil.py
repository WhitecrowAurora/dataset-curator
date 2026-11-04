"""网络小工具：判定回环地址，并据此决定要不要信任系统代理。

为什么需要这个：httpx 默认 `trust_env=True`，会从环境变量与 Windows 注册表
读系统代理。打标机常开着代理软件（Clash / VPN 客户端），它的规则未必把
127.0.0.1 排除在外——于是"连本机 vLLM"的请求被塞进代理。

后果不是报错，而是更难查的几种：

- 代理没开：所有本机请求直接失败，而 vLLM 明明好好地起着。
- 代理开着：请求绕一圈回来，几十 MB 的 base64 图片体被多转发一次，
  吞吐掉一半，而且代理的连接超时会伪装成"服务端超时"。
- `/health` 探测被代理回 502，"服务没起"和"地址填错"再也分不清。

所以凡是目标是回环地址的连接，一律 `trust_env=False`，直连。远程地址维持
默认行为——那边可能真的需要代理才能出去。
"""

from __future__ import annotations

from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})


def is_loopback_url(url: str) -> bool:
    """这个地址是不是指向本机。

    判定只看主机名，不看端口——127.0.0.1 上的任何端口都是本机。
    认不出来的地址按"不是本机"处理：宁可让远程走默认代理，也不要
    把一个真远程地址错误地直连。
    """
    text = (url or "").strip()
    if not text:
        return False
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return False
    return host.lower() in LOOPBACK_HOSTS


def trust_env_for(url: str) -> bool:
    """给 httpx 的 `trust_env` 用：本机地址返回 False（不吃系统代理）。"""
    return not is_loopback_url(url)
