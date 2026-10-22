"""网页测试台接口测试：Flask test_client + FakeLLM，不联网、不调真实 DeepSeek。"""
from __future__ import annotations

import pytest

from src.config import AppConfig
from src.context import ContextStore
from src.engine import ReplyEngine
from web.app import ROOM_ID, create_app

ROOM_NAME = "我、朋友和 DeepSeek"


class FakeLLM:
    def __init__(self, replies=("哈哈，那就火锅吧",)):
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []
        self.last_usage: dict = {"total_tokens": 12}

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append([dict(m) for m in messages])
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0]


class BoomLLM:
    """模拟 DeepSeek 不可用。"""

    last_usage: dict = {}

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("模拟网络断开")


def build_app(tmp_path, *, llm=None, api_key: str = "", **chat_overrides):
    cfg = AppConfig()
    cfg.context.persist_dir = str(tmp_path / "rooms")
    cfg.deepseek.api_key = api_key
    for key, value in chat_overrides.items():
        setattr(cfg.chat, key, value)

    model = llm if llm is not None else FakeLLM()
    store = ContextStore(cfg.context.persist_dir)
    engine = ReplyEngine(cfg, model, store)
    app = create_app(cfg, engine)
    app.config.update(TESTING=True)
    return app, engine, model


@pytest.fixture()
def bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return build_app(tmp_path)


# ======================================================================
# 页面 / 摘要
# ======================================================================
def test_index_page_renders(bundle):
    app, _, _ = bundle
    client = app.test_client()
    resp = client.get("/")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert ROOM_NAME in html
    assert "/static/css/style.css" in html
    assert "/static/js/app.js" in html
    # 离线性：页面里不引用任何外部 CDN / 外链资源
    assert "http://" not in html
    assert "https://" not in html


def test_status_summary(bundle):
    app, _, _ = bundle
    data = app.test_client().get("/api/status").get_json()

    assert data["bot"]["model"] == "deepseek-chat"
    assert data["trigger"]["mode"] == "all"
    assert data["trigger"]["label"] == "每条消息都回"
    assert data["api_key_configured"] is False
    assert data["api_key_hint"] == ""
    assert data["room"]["name"] == ROOM_NAME
    assert set(data["room"]["members"]) == {"我", "朋友", "DeepSeek"}
    assert data["demo"]["offline"] is True
    assert data["stats"]["sent"] == 0


def test_status_never_leaks_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    secret = "sk-test-secret-1234567890abcdef"
    app, _, _ = build_app(tmp_path, api_key=secret)

    resp = app.test_client().get("/api/status")
    data = resp.get_json()
    body = resp.get_data(as_text=True)

    assert data["api_key_configured"] is True
    assert data["api_key_hint"] != secret
    assert "***" in data["api_key_hint"]
    assert secret not in body


# ======================================================================
# 发送 / 消息列表
# ======================================================================
def test_send_returns_replies_and_messages(bundle):
    app, _, llm = bundle
    client = app.test_client()

    resp = client.post("/api/send", json={"speaker": "朋友", "text": "今天吃什么？"})
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["replied"] is True
    assert data["replies"] == ["哈哈，那就火锅吧"]
    assert len(data["messages"]) == 2

    user_msg, bot_msg = data["messages"]
    assert (user_msg["role"], user_msg["speaker"], user_msg["text"]) == ("user", "朋友", "今天吃什么？")
    assert (bot_msg["role"], bot_msg["speaker"], bot_msg["text"]) == ("bot", "DeepSeek", "哈哈，那就火锅吧")
    assert bot_msg["at"] == "@朋友"  # 默认 reply_with_at=true
    assert user_msg["failed"] is False
    assert user_msg["time"] and bot_msg["time"]
    assert user_msg["ts"] <= bot_msg["ts"]

    # 引擎确实拿到了 system prompt 和带发言人前缀的消息
    assert llm.calls[0][0]["role"] == "system"
    assert llm.calls[0][-1]["content"] == "朋友：今天吃什么？"


def test_send_as_me_then_fetch_messages(bundle):
    app, _, _ = bundle
    client = app.test_client()

    client.post("/api/send", json={"speaker": "我", "text": "我想吃烤肉"})
    resp = client.get("/api/messages")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    data = resp.get_json()

    assert data["count"] == 2
    assert [m["speaker"] for m in data["messages"]] == ["我", "DeepSeek"]
    # 中文直接输出，不是 \uXXXX 转义
    assert "我想吃烤肉" in resp.get_data(as_text=True)


def test_context_continues_across_messages(bundle):
    app, _, llm = bundle
    client = app.test_client()

    client.post("/api/send", json={"speaker": "朋友", "text": "第一句话"})
    client.post("/api/send", json={"speaker": "我", "text": "第二句话"})

    assert len(llm.calls) == 2
    prompt = "".join(m["content"] for m in llm.calls[1])
    assert "朋友：第一句话" in prompt
    assert "哈哈，那就火锅吧" in prompt  # 上一轮回复作为 assistant 历史带上


def test_multi_chunk_reply_lists_all_bubbles(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    long_reply = "这是第一段。" * 10  # 60 字，超过 50 字上限
    app, _, _ = build_app(tmp_path, llm=FakeLLM([long_reply]), max_message_chars=50)

    data = app.test_client().post("/api/send", json={"speaker": "朋友", "text": "展开说说"}).get_json()

    assert len(data["replies"]) == 2
    assert len(data["messages"]) == 3  # 1 条用户 + 2 条 DeepSeek
    bots = [m for m in data["messages"] if m["role"] == "bot"]
    assert len(bots) == 2
    assert bots[0]["at"] == "@朋友"
    assert bots[1]["at"] is None  # 只有第一条 @ 人
    from src.engine import sanitize_reply

    assert "".join(m["text"] for m in bots) == sanitize_reply(long_reply)


# ======================================================================
# 参数校验
# ======================================================================
def test_send_validation(bundle):
    app, _, _ = bundle
    client = app.test_client()

    assert client.post("/api/send", json={"speaker": "朋友", "text": "   "}).status_code == 400
    assert client.post("/api/send", json={"speaker": "路人", "text": "hi"}).status_code == 400
    assert client.post("/api/send", data="not json", content_type="text/plain").status_code == 400
    assert (
        client.post("/api/send", json={"speaker": "朋友", "text": "x" * 2001}).status_code == 400
    )
    assert client.get("/api/messages").get_json()["count"] == 0


def test_method_not_allowed(bundle):
    app, _, _ = bundle
    assert app.test_client().get("/api/send").status_code == 405


# ======================================================================
# 重置
# ======================================================================
def test_reset_clears_messages_and_context(bundle):
    app, engine, llm = bundle
    client = app.test_client()

    client.post("/api/send", json={"speaker": "朋友", "text": "记住：我喜欢猫"})
    assert client.get("/api/messages").get_json()["count"] == 2
    assert engine.store.get(ROOM_ID).turns

    resp = client.post("/api/reset")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert client.get("/api/messages").get_json()["count"] == 0
    assert engine.store.get(ROOM_ID).turns == []

    # 重置后新一轮对话不再携带旧历史
    client.post("/api/send", json={"speaker": "我", "text": "还记得吗"})
    prompt = "".join(m["content"] for m in llm.calls[-1])
    assert "我喜欢猫" not in prompt


# ======================================================================
# 错误处理 / 触发规则 / 多实例隔离
# ======================================================================
def test_llm_failure_returns_502_and_marks_failed(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app, _, _ = build_app(tmp_path, llm=BoomLLM())

    resp = app.test_client().post("/api/send", json={"speaker": "朋友", "text": "在吗"})
    assert resp.status_code == 502
    data = resp.get_json()
    assert "DeepSeek 调用失败" in data["error"]
    assert data["replied"] is False
    assert data["replies"] == []
    assert data["messages"][0]["failed"] is True


def test_mention_trigger_requires_at(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app, _, llm = build_app(tmp_path, trigger="mention")
    client = app.test_client()

    resp = client.post("/api/send", json={"speaker": "朋友", "text": "大家好"})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["replied"] is False
    assert data["replies"] == []
    assert "没被 @" in data["reason"]
    assert "只在被 @ 时回" in data["trigger_label"]
    assert llm.calls == []

    data2 = client.post("/api/send", json={"speaker": "朋友", "text": "@DeepSeek 你好"}).get_json()
    assert data2["replied"] is True
    assert data2["replies"]


def test_two_apps_are_isolated(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app1, _, _ = build_app(tmp_path / "a")
    app2, _, _ = build_app(tmp_path / "b")

    app1.test_client().post("/api/send", json={"speaker": "我", "text": "hi"})
    assert app1.test_client().get("/api/messages").get_json()["count"] == 2
    assert app2.test_client().get("/api/messages").get_json()["count"] == 0
