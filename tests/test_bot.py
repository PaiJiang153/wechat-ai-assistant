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


def test_dynamic_silence_chance_by_scenario():
    from src.bot import dynamic_silence_chance

    # 有提问 / 被叫名字：必回
    assert dynamic_silence_chance(0.05, "这条路怎么走？", False, "大肥鱼") == 0.0
    assert dynamic_silence_chance(0.05, "你在吗", False, "大肥鱼") == 0.0
    assert dynamic_silence_chance(0.05, "大肥鱼看看这个", False, "大肥鱼") == 0.0
    # 基准为 0 时永远不静默
    assert dynamic_silence_chance(0.0, "哈哈哈", False, "") == 0.0
    # 活跃对话：基准减半
    assert dynamic_silence_chance(0.05, "今天天气不错", True, "") == 0.025
    # 极短闲聊：放大 4 倍；基准过大时封顶 50%
    assert dynamic_silence_chance(0.05, "哈哈哈", False, "") == 0.2
    assert dynamic_silence_chance(0.4, "哈哈哈", False, "") == 0.5


def test_chunk_send_yields_to_new_pending_message(tmp_path):
    """分条发送中途对方插话：停发剩余旧内容，等新消息重新生成。"""
    long_reply = "第一句讲完，" * 30  # 足够拆成多条
    data = {
        "deepseek": {"api_key": "sk-test"},
        "chat": {"debounce_seconds": 0.1, "min_interval_seconds": 0, "max_replies_per_minute": 0,
                 "max_message_chars": 50, "max_reply_chunks": 5},
        "context": {"persist_dir": str(tmp_path / "rooms")},
        "wechat": {"adapter": "mock"},
    }
    cfg = AppConfig.from_dict(data)
    llm = FakeLLM(long_reply)
    store = ContextStore(cfg.context.persist_dir)
    engine = ReplyEngine(cfg, llm, store, self_wxid="wxid_bot")
    adapter = CaptureAdapter()
    bot = Bot(cfg, adapter, engine)

    first = msg("开始吧", mid="m1")
    interject = msg("等等，我插一句", mid="m2")
    real_send = bot._safe_send
    state = {"n": 0}

    def send_then_interject(room_id, at_id, text):
        state["n"] += 1
        result = real_send(room_id, at_id, text)
        if state["n"] == 1:  # 第一块发出后：新消息到达且新一轮已并行生成完毕
            bot._pending[first.room_id] = [interject]
            bot._next_prep_ready.add(first.room_id)
        return result

    bot._safe_send = send_then_interject
    bot._process(first.room_id, [first], engine.store.lock_for(first.room_id))

    sent_texts = [t for _, t, _ in adapter.sent]
    assert sent_texts == [long_reply[:50]]  # 只发了第一块，剩余让位


def test_pending_absorbed_before_first_send(tmp_path):
    """生成期间就有新消息：先并进来重新生成，再发送（回复内容基于全部消息）。"""
    data = {
        "deepseek": {"api_key": "sk-test"},
        "chat": {"debounce_seconds": 0.1, "min_interval_seconds": 0, "max_replies_per_minute": 0},
        "context": {"persist_dir": str(tmp_path / "rooms")},
        "wechat": {"adapter": "mock"},
    }
    cfg = AppConfig.from_dict(data)
    llm = FakeLLM("收到")
    store = ContextStore(cfg.context.persist_dir)
    engine = ReplyEngine(cfg, llm, store, self_wxid="wxid_bot")
    adapter = CaptureAdapter()
    bot = Bot(cfg, adapter, engine)

    first = msg("在吗", mid="m1")
    bot._pending[first.room_id] = [msg("补充一件事", mid="m2")]
    bot._process(first.room_id, [first], engine.store.lock_for(first.room_id))

    assert len(llm.calls) == 2  # 第一轮被丢弃，第二轮带上新消息重新生成
    assert "补充一件事" in str(llm.calls[-1])
    assert [t for _, t, _ in adapter.sent] == ["收到"]


def test_generation_overlaps_with_slow_sending(tmp_path):
    """发送慢时，下一轮的生成不等它：读新上下文先生成，发完保序接上。"""
    import threading

    data = {
        "deepseek": {"api_key": "sk-test"},
        "chat": {"debounce_seconds": 0.1, "min_interval_seconds": 0, "max_replies_per_minute": 0},
        "context": {"persist_dir": str(tmp_path / "rooms")},
        "wechat": {"adapter": "mock"},
    }
    cfg = AppConfig.from_dict(data)
    llm = FakeLLM("收到")
    store = ContextStore(cfg.context.persist_dir)
    engine = ReplyEngine(cfg, llm, store, self_wxid="wxid_bot")
    adapter = CaptureAdapter()
    bot = Bot(cfg, adapter, engine)

    release = threading.Event()
    first_sent = threading.Event()
    real_send = bot._safe_send

    def slow_send(room_id, at_id, text):
        result = real_send(room_id, at_id, text)
        first_sent.set()
        release.wait(5)  # 模拟 UI 发送很慢
        return result

    bot._safe_send = slow_send
    room = "room@chatroom"
    bot._slots.acquire()  # 生产路径里每个批次在 _on_debounce 各占一个配额
    bot._slots.acquire()
    with bot._guard:
        bot._ready[room] = __import__("collections").deque([[msg("第一句", mid="m1")], [msg("第二句", mid="m2")]])
        bot._running.add(room)
    worker = threading.Thread(target=bot._run_room, args=(room,), daemon=True)
    worker.start()

    assert first_sent.wait(5)          # 第一轮已进入发送（被人为卡住）
    assert len(llm.calls) >= 2         # 卡住期间第二轮已并行完成生成
    release.set()
    worker.join(10)
    assert len(adapter.sent) == 2      # 两轮都按序发出


def test_deliver_reply_with_humanize_and_no_interval_wait(tmp_path):
    """回归：humanize 开启且最小间隔无需等待时，发送阶段不得漏赋 at_id 等变量。"""
    data = {
        "deepseek": {"api_key": "sk-test"},
        "chat": {"debounce_seconds": 0.1, "min_interval_seconds": 0, "max_replies_per_minute": 0,
                 "reply_with_at": False},
        "humanize": {
            "enabled": True,
            "typing_min": 0, "typing_max": 0,
            "split_chance": 0, "silence_chance": 0,
            "double_text_chance": 0, "at_chance": 1.0,
        },
        "context": {"persist_dir": str(tmp_path / "rooms")},
        "wechat": {"adapter": "mock"},
    }
    cfg = AppConfig.from_dict(data)
    llm = FakeLLM("好的没问题")
    store = ContextStore(cfg.context.persist_dir)
    engine = ReplyEngine(cfg, llm, store, self_wxid="wxid_bot")
    adapter = CaptureAdapter()
    bot = Bot(cfg, adapter, engine)

    m = msg("说句话", mid="m1")
    bot._process(m.room_id, [m], engine.store.lock_for(m.room_id))
    assert [t for _, t, _ in adapter.sent] == ["好的没问题"]


def test_cross_batch_sends_never_interleave(tmp_path):
    """跨批次的两个发送线程：第二个批次必须等第一个批次全部发完，不交错。"""
    import threading

    data = {
        "deepseek": {"api_key": "sk-test"},
        "chat": {"debounce_seconds": 0.1, "min_interval_seconds": 0, "max_replies_per_minute": 0,
                 "max_message_chars": 50, "max_reply_chunks": 5},
        "context": {"persist_dir": str(tmp_path / "rooms")},
        "wechat": {"adapter": "mock"},
    }
    cfg = AppConfig.from_dict(data)
    long_reply = "第一轮的句子，" * 20   # 拆成 3 条
    llm = FakeLLM(long_reply)
    store = ContextStore(cfg.context.persist_dir)
    engine = ReplyEngine(cfg, llm, store, self_wxid="wxid_bot")
    adapter = CaptureAdapter()
    bot = Bot(cfg, adapter, engine)

    release = threading.Event()
    first_sent = threading.Event()
    real_send = bot._safe_send
    state = {"n": 0}

    def slow_send(room_id, at_id, text):
        result = real_send(room_id, at_id, text)
        state["n"] += 1
        if state["n"] == 1:
            first_sent.set()
            release.wait(5)  # 卡住第一轮发送
        return result

    bot._safe_send = slow_send
    room = "room@chatroom"
    bot._slots.acquire()
    bot._slots.acquire()
    with bot._guard:
        bot._ready[room] = __import__("collections").deque([
            [msg("第一条", mid="a1")],
            [msg("第二条", mid="b1")],
        ])
        bot._running.add(room)
    worker = threading.Thread(target=bot._run_room, args=(room,), daemon=True)
    worker.start()

    assert first_sent.wait(5)
    # 第一轮卡在第 1 条时，第二轮早已生成完并尝试发送，但必须被会话锁挡住
    import time as _t
    _t.sleep(0.5)
    sent_now = [t for _, t, _ in adapter.sent]
    assert sent_now == [long_reply[:50]]          # 只有第一轮第 1 条，无交错
    release.set()
    worker.join(10)
    texts = [t for _, t, _ in adapter.sent]
    # 第一轮第 1 条发出后标记已置位：剩余旧稿让位，第二轮完整 3 条接管
    assert len(texts) == 4
    assert texts[0] == long_reply[:50]
    assert texts[1:] == texts[1:4] and len(set(texts[1:])) == 3  # 第二轮 3 条各不相同
