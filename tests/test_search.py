"""联网搜索单元测试：SearchClient 格式化/缓存 + 引擎的工具调用循环。"""
from __future__ import annotations

import json
import types

import pytest

from src.config import AppConfig, DeepSeekConfig, SearchConfig
from src.context import ContextStore
from src.engine import ReplyEngine
from src.models import IncomingMessage
from src.websearch import SearchClient, parse_tool_query


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_search_client_formats_results(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return FakeResponse(
            {
                "results": [
                    {
                        "title": "AI 新闻",
                        "url": "https://example.com/a",
                        "summary": "今天发布了新模型。",
                        "date": "2026-10-19T00:00:00+00:00",
                    },
                    {"title": "第二条", "url": "https://example.com/b", "snippet": "摘要二"},
                ]
            }
        )

    monkeypatch.setattr("src.websearch.net.post", fake_post)
    cfg = SearchConfig(count=5, max_results_for_model=2)
    client = SearchClient(DeepSeekConfig(api_key="sk-test"), cfg)
    text = client.search_and_format("AI 新闻")
    assert "AI 新闻" in text
    assert "https://example.com/a" in text
    assert "联网搜索到的资料" in text
    # 第二次同样查询走缓存，不再发请求
    client.search_and_format("AI 新闻")
    assert len(calls) == 1


def test_parse_tool_query():
    assert parse_tool_query('{"query": "今天天气"}') == "今天天气"
    assert parse_tool_query("bad json") == ""


class FakeSearch:
    def __init__(self):
        self.queries = []

    def search_and_format(self, query, ori_question=""):
        self.queries.append(query)
        return "以下是联网搜索到的资料：\n- 今天的新闻：AI 圈发布了新模型。"


class FakeToolLLM:
    """第一次返回工具调用，第二次返回最终回答。"""

    last_usage = {"total_tokens": 100}

    def __init__(self):
        self.complete_calls = 0

    def chat(self, messages, **kwargs):
        return "不该走到这里"

    def complete(self, messages, tools=None, **kwargs):
        self.complete_calls += 1
        if self.complete_calls == 1:
            call = types.SimpleNamespace(
                id="call_1",
                function=types.SimpleNamespace(
                    name="web_search",
                    arguments=json.dumps({"query": "今天 AI 新闻"}, ensure_ascii=False),
                ),
            )
            return types.SimpleNamespace(content="", tool_calls=[call])
        # 第二次：检查工具结果确实进了对话
        assert any(m.get("role") == "tool" for m in messages)
        return types.SimpleNamespace(content="刚搜了，今天 AI 圈有新模型发布。", tool_calls=[])


def test_engine_uses_search_tool_once(tmp_path):
    cfg = AppConfig.from_dict(
        {
            "deepseek": {"api_key": "sk-test"},
            "chat": {"debounce_seconds": 0.1, "min_interval_seconds": 0, "max_replies_per_minute": 0},
            "context": {"persist_dir": str(tmp_path)},
            "wechat": {"adapter": "mock"},
            "search": {"enabled": True, "max_tool_rounds": 2},
        }
    )
    llm = FakeToolLLM()
    search = FakeSearch()
    engine = ReplyEngine(cfg, llm, ContextStore(str(tmp_path)), self_wxid="bot", search=search)
    msg = IncomingMessage(
        room_id="r@chatroom", sender_id="wxid_a", sender_name="小明", text="帮我查下今天AI新闻"
    )
    chunks = engine.generate([msg])
    assert chunks
    assert "新模型" in "".join(chunks)
    assert search.queries == ["今天 AI 新闻"]


def test_engine_without_search_uses_plain_chat(tmp_path):
    cfg = AppConfig.from_dict(
        {
            "deepseek": {"api_key": "sk-test"},
            "context": {"persist_dir": str(tmp_path)},
            "wechat": {"adapter": "mock"},
        }
    )

    class PlainLLM:
        last_usage = {}

        def chat(self, messages, **kwargs):
            return "普通回答"

    engine = ReplyEngine(cfg, PlainLLM(), ContextStore(str(tmp_path)), search=None)
    msg = IncomingMessage(room_id="r@chatroom", sender_id="a", sender_name="我", text="在吗")
    assert engine.generate([msg]) == ["普通回答"]
