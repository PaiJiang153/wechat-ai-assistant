"""网络客户端工厂：默认绕过系统代理。

背景：Windows 的系统代理（注册表 ProxyEnable/ProxyServer，常见是 Clash 的
127.0.0.1:7890/10090）会被 httpx/requests 的 `trust_env=True` 自动读取。
代理软件没启动时，连接代理端口会被拒绝（WinError 10061），表现为
「模型调用失败：Connection error」——但 api.deepseek.com 直连其实是通的。

所以这里统一：
* 默认 `use_system_proxy=False` → 所有 API 请求直连，不读系统代理；
* 需要走代理（比如访问 Google Gemini）时，把 config.yaml 的
  `network.use_system_proxy` 改成 true，再重启。
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("dsbot.net")

_use_proxy = False


def configure(use_system_proxy: bool) -> None:
    global _use_proxy
    _use_proxy = bool(use_system_proxy)
    log.info(
        "网络模式：%s",
        "跟随系统代理（代理软件需保持运行）" if _use_proxy else "直连（忽略系统代理，避免代理没开时连不上）",
    )


def use_system_proxy() -> bool:
    return _use_proxy


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = _use_proxy  # False 时 requests 不读系统/环境代理
    return session


def request(method: str, url: str, **kwargs):
    with _session() as session:
        return session.request(method, url, **kwargs)


def get(url: str, **kwargs):
    return request("GET", url, **kwargs)


def post(url: str, **kwargs):
    return request("POST", url, **kwargs)


def openai_http_client(timeout: float = 300.0):
    """给 openai SDK 用的 httpx 客户端（trust_env 与网络模式一致）。"""
    try:
        import httpx2  # 当前环境 openai 用的是 httpx2 分支
    except ImportError:  # pragma: no cover
        try:
            import httpx as httpx2  # type: ignore[no-redef]
        except ImportError:
            return None
    return httpx2.Client(trust_env=_use_proxy, timeout=timeout)
