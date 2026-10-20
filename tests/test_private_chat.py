"""私聊（非群聊）支持测试。"""
from __future__ import annotations

import pytest

from src.config import AppConfig
from src.context import ContextStore
from src.engine import ReplyEngine
from src.models import IncomingMessage
from src.wechatauto_adapter import WechatautoAdapter


def make_cfg(tmp_path) -> AppConfig:
    return AppConfig.from_dict(
        {
            "deepseek": {"api_key": "sk-test"},
            "personas": {
                "sister": {"bot_name": "妹妹", "system_prompt": "你是傲娇妹妹，嘴硬心软。"},
                "fish": {"bot_name": "大肥鱼", "system_prompt": "你是大肥鱼。"},
            },
            "wechat": {
                "adapter": "wechatauto",
                "groups": [{"name": "群A", "persona": "fish", "trigger": "all"}],
                "private_chats": [
                    {"name": "小红", "persona": "sister", "trigger": "all"},
                ],
            },
            "context": {"persist_dir": str(tmp_path)},
        }
    )


def private_msg(text: str) -> IncomingMessage:
    return IncomingMessage(
        room_id="wxid_contact",
        sender_id="wxid_contact",
        sender_name="小红",
        text=text,
        room_name="小红",
        is_group=False,
    )


def test_private_chat_config(tmp_path):
    cfg = make_cfg(tmp_path)
    chats = cfg.private_chat_list()
    assert len(chats) == 1
    assert chats[0].name == "小红"
    assert cfg.persona_for(chats[0]).bot_name == "妹妹"
    assert cfg.private_chat_for("小红") is not None


def test_engine_replies_to_configured_private_chat(tmp_path):
    cfg = make_cfg(tmp_path)
    engine = ReplyEngine(cfg, type("L", (), {"last_usage": {}, "chat": lambda *a, **k: "x"})(), ContextStore(str(tmp_path)))
    assert engine.should_reply(private_msg("在吗"))


def test_engine_ignores_unconfigured_private_chat(tmp_path):
    cfg = make_cfg(tmp_path)
    engine = ReplyEngine(cfg, type("L", (), {"last_usage": {}, "chat": lambda *a, **k: "x"})(), ContextStore(str(tmp_path)))
    stranger = IncomingMessage(
        room_id="wxid_x", sender_id="wxid_x", sender_name="陌生人", text="你好", room_name="陌生人", is_group=False
    )
    assert not engine.should_reply(stranger)


def test_private_chat_uses_own_persona(tmp_path):
    cfg = make_cfg(tmp_path)
    captured: list[str] = []

    class LLM:
        last_usage: dict = {}

        def chat(self, messages, **kwargs):
            captured.append(messages[0]["content"])
            return "哼"

    engine = ReplyEngine(cfg, LLM(), ContextStore(str(tmp_path)))
    engine.generate([private_msg("在吗")])
    assert "傲娇妹妹" in captured[0]


def test_adapter_polls_private_and_routes_send():
    class DB:
        def get_messages(self, user, limit=50, offset=0):
            if user == "wxid_contact":
                return [{"local_id": 5, "type": "文本", "content": "在吗", "sender_username": "wxid_contact"}]
            return []

        def search_contact(self, name):
            return [{"username": "wxid_contact", "nick_name": "小红", "remark": ""}]

        def username_by_nickname(self, name):
            return "wxid_contact"

        def get_groups(self):
            return []

    adapter = WechatautoAdapter(private_names=["小红"], poll_seconds=0.05)
    adapter._db = DB()
    adapter._privates = {"小红": {"id": "wxid_contact", "last_id": 0}}
    adapter._self_wxid = "wxid_bot"
    got: list[IncomingMessage] = []
    adapter.on_message(got.append)
    adapter._poll_once()
    assert len(got) == 1
    assert got[0].is_group is False
    assert got[0].room_name == "小红"
    assert got[0].text == "在吗"
    assert adapter._name_for_room("wxid_contact") == "小红"
    assert adapter._id_for_room("小红") == "wxid_contact"


def test_unknown_private_name_rejected(tmp_path):
    with pytest.raises(ValueError, match="不存在的人设"):
        AppConfig.from_dict(
            {
                "deepseek": {"api_key": "sk-test"},
                "wechat": {
                    "adapter": "wechatauto",
                    "private_chats": [{"name": "某人", "persona": "nope"}],
                },
                "context": {"persist_dir": str(tmp_path)},
            }
        )
