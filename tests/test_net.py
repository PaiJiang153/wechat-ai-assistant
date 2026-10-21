"""网络出口测试：默认直连，可切换跟随系统代理。"""
from __future__ import annotations

import pytest

from src import net


def test_default_bypass_system_proxy():
    net.configure(False)
    assert net.use_system_proxy() is False
    with net._session() as session:
        assert session.trust_env is False


def test_switch_to_system_proxy():
    net.configure(True)
    try:
        assert net.use_system_proxy() is True
        with net._session() as session:
            assert session.trust_env is True
    finally:
        net.configure(False)


def test_openai_http_client_respects_mode():
    pytest.importorskip("httpx2")
    net.configure(False)
    client = net.openai_http_client(timeout=5)
    try:
        assert client is not None
        assert getattr(client, "_trust_env", None) in (False, None)
    finally:
        client.close()
