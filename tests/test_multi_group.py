"""多群 + 每群独立人设的测试。"""
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
                "fish": {"bot_name": "大肥鱼", "system_prompt": "你是大肥鱼，爱吃白饭。"},
                "assistant": {"bot_name": "助手", "system_prompt": "你是正经助手，说话专业。"},
            },
            "wechat": {
                "adapter": "wechatauto",
                "groups": [
                    {"name": "群A", "persona": "fish", "trigger": "all"},
                    {"name": "群B", "persona": "assistant", "trigger": "mention"},
                ],
            },
            "context": {"persist_dir": str(tmp_path)},
        }
    )


def test_config_groups_and_personas(tmp_path):
    cfg = make_cfg(tmp_path)
    groups = cfg.group_list()
    assert [g.name for g in groups] == ["群A", "群B"]
    assert cfg.persona_for(groups[0]).bot_name == "大肥鱼"
    assert cfg.persona_for(groups[1]).bot_name == "助手"
    assert cfg.trigger_for(groups[0]) == "all"
    assert cfg.trigger_for(groups[1]) == "mention"


def test_unknown_persona_rejected(tmp_path):
    with pytest.raises(ValueError, match="不存在的人设"):
        AppConfig.from_dict(
            {
                "deepseek": {"api_key": "sk-test"},
                "wechat": {"adapter": "wechatauto", "groups": [{"name": "群A", "persona": "nope"}]},
                "context": {"persist_dir": str(tmp_path)},
            }
        )


def test_engine_uses_persona_per_group(tmp_path):
    cfg = make_cfg(tmp_path)
    captured: list[str] = []

    class LLM:
        last_usage: dict = {}

        def chat(self, messages, **kwargs):
            captured.append(messages[0]["content"])
            return "嗯"

    engine = ReplyEngine(cfg, LLM(), ContextStore(str(tmp_path)))
    m1 = IncomingMessage(room_id="idA", sender_id="u", sender_name="小明", text="在吗", room_name="群A")
    m2 = IncomingMessage(room_id="idB", sender_id="u", sender_name="小明", text="在吗", room_name="群B")
    engine.generate([m1])
    engine.generate([m2])
    assert "大肥鱼" in captured[0]
    assert "正经助手" in captured[1]


def test_mention_trigger_per_group(tmp_path):
    cfg = make_cfg(tmp_path)
    engine = ReplyEngine(cfg, type("L", (), {"last_usage": {}, "chat": lambda *a, **k: "x"})(), ContextStore(str(tmp_path)))
    plain = IncomingMessage(room_id="idB", sender_id="u", sender_name="小明", text="在吗", room_name="群B")
    assert not engine.should_reply(plain)          # 群B 是 mention 触发，没 @ 不回
    mentioned = IncomingMessage(
        room_id="idB", sender_id="u", sender_name="小明", text="@助手 在吗", room_name="群B"
    )
    assert engine.should_reply(mentioned)          # 文本里 @ 了 bot_name 就回


def test_adapter_polls_multiple_groups():
    class DB:
        rows = {
            "g1": [{"local_id": 1, "type": "文本", "content": "群A消息", "sender_username": "u1"}],
            "g2": [{"local_id": 2, "type": "文本", "content": "群B消息", "sender_username": "u2"}],
        }

        def get_messages(self, gid, limit=50, offset=0):
            return self.rows.get(gid, [])

        def get_group_members(self, gid):
            return [{"username": "u1", "nick_name": "甲"}, {"username": "u2", "nick_name": "乙"}]

        def get_groups(self):
            return [{"username": "g1", "name": "群A"}, {"username": "g2", "name": "群B"}]

    adapter = WechatautoAdapter(group_names=["群A", "群B"], poll_seconds=0.05)
    adapter._db = DB()
    adapter._groups = {
        "群A": {"id": "g1", "last_id": 0, "members": {}, "members_at": 0.0},
        "群B": {"id": "g2", "last_id": 0, "members": {}, "members_at": 0.0},
    }
    adapter._group_id = "g1"
    adapter._self_wxid = "wxid_me"
    got: list[IncomingMessage] = []
    adapter.on_message(got.append)
    adapter._poll_once()
    assert {m.room_name for m in got} == {"群A", "群B"}
    assert {m.text for m in got} == {"群A消息", "群B消息"}


def test_send_routes_to_right_group():
    adapter = WechatautoAdapter(group_names=["群A", "群B"])
    adapter._groups = {"群A": {"id": "g1"}, "群B": {"id": "g2"}}
    assert adapter._name_for_room("g2") == "群B"
    assert adapter._name_for_room("群A") == "群A"
    assert adapter._id_for_room("群B") == "g2"
