"""核心引擎单测：全部用 FakeLLM，不联网、不碰真实 DeepSeek API。

覆盖：
  * should_reply：all / mention / prefix 三种触发规则、自己的消息、群白名单、ignore_senders
  * generate：上下文带入历史、batch 合并、长回复拆分、Markdown 清洗
  * ContextStore：落盘后重新加载，历史还在
"""
from __future__ import annotations

import pytest

from src.config import AppConfig
from src.context import ContextStore
from src.engine import ReplyEngine, sanitize_reply, split_text
from src.models import IncomingMessage

ROOM_ID = "test@chatroom"
ROOM_NAME = "测试群"


class FakeLLM:
    """固定返回文本的假大模型，记录每次收到的 messages。"""

    def __init__(self, replies=("好的呀",)):
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []
        self.last_usage: dict = {"total_tokens": 42}

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append([dict(m) for m in messages])
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0]


def make_cfg(**chat_kwargs) -> AppConfig:
    cfg = AppConfig()
    cfg.chat.only_groups = True
    for key, value in chat_kwargs.items():
        setattr(cfg.chat, key, value)
    return cfg


def make_msg(
    text: str = "你好",
    *,
    sender_id: str = "wxid-friend",
    sender_name: str = "朋友",
    room_id: str = ROOM_ID,
    room_name: str = ROOM_NAME,
    is_group: bool = True,
    at_wxids: list[str] | None = None,
    msg_id: str = "m1",
) -> IncomingMessage:
    return IncomingMessage(
        room_id=room_id,
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        room_name=room_name,
        msg_id=msg_id,
        is_group=is_group,
        at_wxids=list(at_wxids or []),
    )


def make_engine(cfg: AppConfig, store: ContextStore, llm: FakeLLM, self_wxid: str = "") -> ReplyEngine:
    return ReplyEngine(cfg, llm, store, self_wxid=self_wxid)


@pytest.fixture()
def store(tmp_path) -> ContextStore:
    return ContextStore(tmp_path / "rooms")


@pytest.fixture()
def llm() -> FakeLLM:
    return FakeLLM()


# ======================================================================
# should_reply：触发规则
# ======================================================================
def test_trigger_all_replies_every_message(store, llm):
    engine = make_engine(make_cfg(trigger="all"), store, llm)
    assert engine.should_reply(make_msg("随便聊聊")).reply is True
    assert engine.should_reply(make_msg("   ")).reply is False  # 空消息


def test_trigger_mention(store, llm):
    engine = make_engine(make_cfg(trigger="mention"), store, llm, self_wxid="wxid-bot")

    assert engine.should_reply(make_msg("大家好")).reply is False
    assert engine.should_reply(make_msg("在吗", at_wxids=["wxid-bot"])).reply is True
    assert engine.should_reply(make_msg("@DeepSeek 讲个笑话")).reply is True


def test_trigger_prefix(store, llm):
    engine = make_engine(make_cfg(trigger="prefix", prefix="ds"), store, llm)

    assert engine.should_reply(make_msg("ds 在吗")).reply is True
    assert engine.should_reply(make_msg("DS：帮我翻译")).reply is True  # 大小写不敏感
    assert engine.should_reply(make_msg("在吗")).reply is False


def test_unknown_trigger_is_dropped(store, llm):
    engine = make_engine(make_cfg(trigger="sometimes"), store, llm)
    decision = engine.should_reply(make_msg("hi"))
    assert decision.reply is False
    assert "未知" in decision.reason


# ======================================================================
# should_reply：过滤规则
# ======================================================================
def test_ignores_self_message(store, llm):
    engine = make_engine(make_cfg(trigger="all"), store, llm, self_wxid="wxid-bot")
    decision = engine.should_reply(
        make_msg("这是机器人自己发的", sender_id="wxid-bot", sender_name="DeepSeek")
    )
    assert decision.reply is False
    assert decision.reason == "自己的消息"


def test_group_whitelist(store, llm):
    cfg = make_cfg(trigger="all")
    cfg.wechat.allowed_groups = ["测试群", "other@chatroom"]
    engine = make_engine(cfg, store, llm)

    assert engine.should_reply(make_msg("hi", room_name="测试群")).reply is True
    assert engine.should_reply(make_msg("hi", room_name="测试群2")).reply is False  # 必须精确匹配
    assert engine.should_reply(make_msg("hi", room_id="other@chatroom", room_name="别的群")).reply is True

    decision = engine.should_reply(make_msg("hi", room_id="x@chatroom", room_name="陌生群"))
    assert decision.reply is False
    assert decision.reason == "群不在白名单"


def test_ignore_senders_matches_wxid_or_name(store, llm):
    cfg = make_cfg(trigger="all")
    cfg.wechat.ignore_senders = ["wxid-spam", "讨厌的人"]
    engine = make_engine(cfg, store, llm)

    by_id = engine.should_reply(make_msg("hi", sender_id="wxid-spam", sender_name="路人"))
    assert by_id.reply is False
    assert by_id.reason == "发送者被忽略"

    by_name = engine.should_reply(make_msg("hi", sender_id="wxid-x", sender_name="讨厌的人"))
    assert by_name.reply is False
    assert by_name.reason == "发送者被忽略"

    assert engine.should_reply(make_msg("hi", sender_id="wxid-ok", sender_name="朋友")).reply is True


def test_only_groups_can_be_disabled(store, llm):
    cfg = make_cfg(trigger="all")
    cfg.chat.only_groups = False
    engine = make_engine(cfg, store, llm)
    assert engine.should_reply(make_msg("私聊消息", is_group=False)).reply is True


# ======================================================================
# generate：上下文 / batch / 拆分 / 清洗
# ======================================================================
def test_generate_passes_history_to_llm(store, llm):
    engine = make_engine(make_cfg(), store, llm)
    llm.replies = ["先吃火锅吧", "火锅挺好的"]

    first = engine.generate([make_msg("今天吃什么？", sender_name="朋友")])
    assert first == ["先吃火锅吧"]
    call1 = llm.calls[0]
    assert call1[0]["role"] == "system"
    assert "DeepSeek" in call1[0]["content"]
    assert call1[-1] == {"role": "user", "content": "朋友：今天吃什么？"}

    second = engine.generate(
        [make_msg("那就火锅", sender_id="wxid-me", sender_name="我", msg_id="m2")]
    )
    assert second == ["火锅挺好的"]
    call2 = llm.calls[1]
    contents = [m["content"] for m in call2]
    assert "朋友：今天吃什么？" in contents
    assert "先吃火锅吧" in contents  # 上一轮 DeepSeek 的回复也带上了
    assert call2[-1] == {"role": "user", "content": "我：那就火锅"}


def test_generate_merges_batch_into_one_call(store, llm):
    engine = make_engine(make_cfg(), store, llm)
    llm.replies = ["都在呢"]

    chunks = engine.generate(
        [
            make_msg("在吗", sender_name="朋友", msg_id="m1"),
            make_msg("周末开黑吗", sender_id="wxid-me", sender_name="我", msg_id="m2"),
        ]
    )
    assert chunks == ["都在呢"]
    assert len(llm.calls) == 1
    users = [m["content"] for m in llm.calls[0] if m["role"] == "user"]
    assert users == ["朋友：在吗", "我：周末开黑吗"]


def test_generate_splits_long_reply(store, llm):
    cfg = make_cfg(max_message_chars=50, max_reply_chunks=3)
    engine = make_engine(cfg, store, llm)
    long_text = "这是一句测试用的话。" * 6  # 60 字，超过 50 字上限
    llm.replies = [long_text]

    chunks = engine.generate([make_msg("说长一点")])
    assert len(chunks) == 2
    assert all(len(c) <= 50 for c in chunks)
    # 全局规则：句尾句号会被去掉，中间的句号换成逗号
    from src.engine import sanitize_reply

    assert "".join(chunks) == sanitize_reply(long_text)


def test_generate_sanitizes_llm_output(store, llm):
    engine = make_engine(make_cfg(), store, llm)
    llm.replies = ["DeepSeek：**好嘞，收到**"]

    chunks = engine.generate([make_msg("在吗")])
    assert chunks == ["好嘞，收到"]
    # 存进上下文的也是清洗后的版本
    assistant = [t for t in engine.store.get(ROOM_ID).turns if t.role == "assistant"]
    assert assistant and assistant[-1].content == "好嘞，收到"


def test_generate_empty_batch_does_not_call_llm(store, llm):
    engine = make_engine(make_cfg(), store, llm)
    assert engine.generate([]) == []
    assert llm.calls == []


# ======================================================================
# 文本工具：sanitize_reply / split_text
# ======================================================================
def test_sanitize_reply_cleans_markdown():
    raw = "**你好**\n# 标题\n> 引用\n```python\nprint(1)\n```"
    assert sanitize_reply(raw) == "你好\n标题\n引用\nprint(1)"


def test_sanitize_reply_removes_bot_prefix():
    assert sanitize_reply("DeepSeek：哈哈", "DeepSeek") == "哈哈"
    assert sanitize_reply("deepseek: 在的", "DeepSeek") == "在的"
    assert sanitize_reply("正常聊天", "DeepSeek") == "正常聊天"


def test_split_text_short_and_empty():
    assert split_text("", 100, 3) == []
    assert split_text("   ", 100, 3) == []
    assert split_text("你好", 100, 3) == ["你好"]


def test_split_text_hard_cuts_long_sentence():
    chunks = split_text("a" * 30, max_chars=10, max_chunks=10)
    assert chunks == ["a" * 10, "a" * 10, "a" * 10]


def test_split_text_limits_chunk_count():
    text = "甲。" * 40  # 80 字
    chunks = split_text(text, max_chars=10, max_chunks=3)
    assert len(chunks) == 3
    assert chunks[-1].endswith("…")
    assert all(len(c) <= 11 for c in chunks)  # 末尾允许 1 个省略号


def test_split_text_comma_mode_one_fragment_per_chunk():
    text = "谁是大肥鱼，你才是大肥鱼。我先去吃饭啦，这个你自己测一下~"
    chunks = split_text(text, max_chars=120, max_chunks=12, split_commas=True)
    assert chunks == ["谁是大肥鱼", "你才是大肥鱼。", "我先去吃饭啦", "这个你自己测一下~"]
    # 短回复在逗号模式下也要拆，不再走"整句直接返回"的快速路径；条尾逗号去掉
    assert split_text("好，", 100, 3, split_commas=True) == ["好"]
    assert split_text("嗯，好，行吧。", 100, 3, split_commas=True) == ["嗯", "好", "行吧。"]


# ======================================================================
# ContextStore：落盘 / 重载 / 重置
# ======================================================================
def test_context_store_persists_and_reloads(tmp_path):
    persist_dir = tmp_path / "rooms"
    cfg = make_cfg()
    llm = FakeLLM(["记住啦，你喜欢猫"])
    engine = ReplyEngine(cfg, llm, ContextStore(persist_dir))

    engine.generate([make_msg("我喜欢猫", sender_name="朋友")])
    assert len(list(persist_dir.glob("*.json"))) == 1

    # 模拟重启：新建 ContextStore，从磁盘读回历史
    reloaded_store = ContextStore(persist_dir)
    ctx = reloaded_store.get(ROOM_ID)
    assert [t.role for t in ctx.turns] == ["user", "assistant"]
    assert ctx.turns[0].name == "朋友"
    assert "我喜欢猫" in ctx.turns[0].content
    assert "记住啦" in ctx.turns[1].content

    # 新引擎接着聊，历史会被带进 prompt
    llm2 = FakeLLM(["你喜欢猫"])
    engine2 = ReplyEngine(cfg, llm2, reloaded_store)
    engine2.generate([make_msg("我刚才说我喜欢什么？", msg_id="m2")])
    prompt = "".join(m["content"] for m in llm2.calls[0])
    assert "我喜欢猫" in prompt


def test_context_store_keeps_room_name(tmp_path):
    persist_dir = tmp_path / "rooms"
    store = ContextStore(persist_dir)
    ctx = store.get("room-1", "老群名")
    assert ctx.room_name == "老群名"
    store.save(ctx)

    reloaded = ContextStore(persist_dir).get("room-1")
    assert reloaded.room_name == "老群名"
    assert reloaded.turns == []


def test_reset_room_clears_context(tmp_path):
    persist_dir = tmp_path / "rooms"
    store = ContextStore(persist_dir)
    engine = ReplyEngine(make_cfg(), FakeLLM(["收到"]), store)

    engine.generate([make_msg("记住这句")])
    assert engine.store.get(ROOM_ID).turns

    engine.reset_room(ROOM_ID)
    assert store.get(ROOM_ID).turns == []
    assert ContextStore(persist_dir).get(ROOM_ID).turns == []


def test_sanitize_reply_drops_placeholder_only():
    assert sanitize_reply("[表情包]", "大肥鱼") == ""
    assert sanitize_reply("「[表情包]」", "大肥鱼") == ""
    assert sanitize_reply("[表情包] 你好呀", "大肥鱼") == "[表情包] 你好呀"


def test_build_messages_skips_placeholder_assistant_turns(tmp_path):
    from src.context import ContextStore

    store = ContextStore(tmp_path / "rooms")
    ctx = store.get("r1", "测试群")
    ctx.add_user("小明", "发个表情", 1.0)
    ctx.add_assistant("[表情包]")
    ctx.add_user("小明", "再来一个", 2.0)
    ctx.add_assistant("好嘞")
    msgs = ctx.build_messages("系统", 10, 10000)
    contents = [m["content"] for m in msgs if m["role"] == "assistant"]
    assert contents == ["好嘞"]
