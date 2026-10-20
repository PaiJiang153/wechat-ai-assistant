"""编排器（Bot）行为测试：去重、合并、@、限流、忽略自己。

用 FakeLLM + CaptureAdapter，完全离线。
"""
from __future__ import annotations

import time

import pytest

from src.adapter import WeChatAdapter
from src.bot import Bot
from src.config import AppConfig
from src.context import ContextStore
from src.engine import ReplyEngine
from src.models import IncomingMessage


class FakeLLM:
    last_usage: dict = {}

    def __init__(self, reply: str = "收到啦") -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []

    def chat(self, messages, **kwargs) -> str:
        self.calls.append(list(messages))
        return self.reply


class CaptureAdapter(WeChatAdapter):
    name = "capture"

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, str, str | None]] = []
        self.self_id = "wxid_bot"

    def get_self_wxid(self) -> str:
        return self.self_id

    def start(self) -> None:  # pragma: no cover
        pass

    def send_text(self, room_id: str, text: str, at_wxid: str | None = None) -> None:
        self.sent.append((room_id, text, at_wxid))


def build(tmp_path, **chat_overrides):
    data = {
        "deepseek": {"api_key": "sk-test"},
        "chat": {
            "debounce_seconds": 0.1,
            "min_interval_seconds": 0,
            "max_replies_per_minute": 0,
            **chat_overrides,
        },
        "context": {"persist_dir": str(tmp_path / "rooms")},
        "wechat": {"adapter": "mock"},
    }
    cfg = AppConfig.from_dict(data)
    llm = FakeLLM()
    store = ContextStore(cfg.context.persist_dir)
    engine = ReplyEngine(cfg, llm, store, self_wxid="wxid_bot")
    adapter = CaptureAdapter()
    bot = Bot(cfg, adapter, engine)
    return cfg, llm, adapter, bot


def msg(text: str, sender: str = "wxid_me", name: str = "我", mid: str = "") -> IncomingMessage:
    return IncomingMessage(
        room_id="room@chatroom",
        sender_id=sender,
        sender_name=name,
        text=text,
        room_name="三人群",
        msg_id=mid,
        is_group=True,
    )


def wait_for(predicate, timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_ignores_own_message(tmp_path):
    _, llm, adapter, bot = build(tmp_path)
    bot.on_message(msg("我是机器人自己", sender="wxid_bot", name="DeepSeek"))
    time.sleep(0.4)
    assert adapter.sent == []
    assert bot.stats.received == 1
    bot.shutdown()


def test_consecutive_messages_are_merged_into_one_reply(tmp_path):
    _, llm, adapter, bot = build(tmp_path)
    bot.on_message(msg("在吗", mid="1"))
    time.sleep(0.03)
    bot.on_message(msg("晚上吃什么", name="朋友", sender="wxid_friend", mid="2"))
    assert wait_for(lambda: bool(adapter.sent))
    time.sleep(0.2)
    assert len(llm.calls) == 1, "两条连续消息应该合并成一次模型调用"
    assert len(adapter.sent) == 1
    # 合并后的上下文里两条消息都在
    contents = [m["content"] for m in llm.calls[0] if m["role"] == "user"]
    assert any("在吗" in c for c in contents)
    assert any("晚上吃什么" in c for c in contents)
    bot.shutdown()


def test_reply_is_at_last_speaker(tmp_path):
    _, _, adapter, bot = build(tmp_path)
    bot.on_message(msg("你好", mid="1"))
    bot.on_message(msg("接个话", name="朋友", sender="wxid_friend", mid="2"))
    assert wait_for(lambda: bool(adapter.sent))
    time.sleep(0.2)
    room_id, text, at_id = adapter.sent[0]
    assert room_id == "room@chatroom"
    assert text
    assert at_id == "wxid_friend", "应该 @ 最后一个发消息的人"
    bot.shutdown()


def test_duplicate_msg_id_ignored(tmp_path):
    _, llm, adapter, bot = build(tmp_path)
    bot.on_message(msg("重复消息", mid="same-id"))
    assert wait_for(lambda: bool(adapter.sent))
    bot.on_message(msg("重复消息", mid="same-id"))
    time.sleep(0.4)
    assert len(llm.calls) == 1
    bot.shutdown()


def test_rate_limit(tmp_path):
    _, llm, adapter, bot = build(tmp_path, max_replies_per_minute=1)
    bot.on_message(msg("第一条", mid="1"))
    assert wait_for(lambda: bool(adapter.sent))
    bot.on_message(msg("第二条", mid="2"))
    time.sleep(0.5)
    assert len(adapter.sent) == 1, "超过每分钟上限应丢弃"
    bot.shutdown()


def test_long_reply_split_into_chunks(tmp_path):
    cfg, llm, adapter, bot = build(tmp_path, max_message_chars=60, max_reply_chunks=3)
    llm.reply = "这是很长的一段回复。" * 30
    bot.on_message(msg("说点长的", mid="1"))
    assert wait_for(lambda: len(adapter.sent) >= 2)
    time.sleep(0.2)
    assert 2 <= len(adapter.sent) <= 3
    assert all(len(t) <= 60 for _, t, _ in adapter.sent)
    # 只有第一条带 @
    assert adapter.sent[0][2] == "wxid_me"
    assert adapter.sent[1][2] is None
    bot.shutdown()


def test_context_persisted_between_bots(tmp_path):
    cfg, llm, adapter, bot = build(tmp_path)
    bot.on_message(msg("记住我叫小明", mid="1"))
    assert wait_for(lambda: bool(adapter.sent))
    bot.shutdown()

    # 新进程/新 Bot，同样目录：应该能读到历史
    cfg2 = AppConfig.from_dict(
        {
            "deepseek": {"api_key": "sk-test"},
            "chat": {"debounce_seconds": 0.1, "min_interval_seconds": 0, "max_replies_per_minute": 0},
            "context": {"persist_dir": str(tmp_path / "rooms")},
            "wechat": {"adapter": "mock"},
        }
    )
    llm2 = FakeLLM()
    engine2 = ReplyEngine(cfg2, llm2, ContextStore(cfg2.context.persist_dir), self_wxid="wxid_bot")
    adapter2 = CaptureAdapter()
    bot2 = Bot(cfg2, adapter2, engine2)
    bot2.on_message(msg("我叫什么来着", mid="2"))
    assert wait_for(lambda: bool(adapter2.sent))
    joined = "\n".join(m["content"] for m in llm2.calls[0])
    assert "小明" in joined
    bot2.shutdown()
