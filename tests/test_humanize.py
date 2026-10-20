"""拟人化行为测试：拆分、打字延迟、时间感知。"""
from __future__ import annotations

import pytest

from src.bot import Bot, split_reply_in_two
from src.config import AppConfig, HumanizeConfig
from src.engine import ReplyEngine
from src.models import IncomingMessage


def test_split_reply_in_two_at_punctuation():
    parts = split_reply_in_two("写作业呢，烦死了。你找我干嘛，不会又想让我帮你跑腿吧。")
    assert len(parts) == 2
    assert parts[0] + parts[1] == "写作业呢，烦死了。你找我干嘛，不会又想让我帮你跑腿吧。"


def test_split_reply_short_unchanged():
    assert split_reply_in_two("在") == ["在"]
    assert split_reply_in_two("好的") == ["好的"]


def test_split_reply_no_boundary_unchanged():
    assert split_reply_in_two("a" * 60) == ["a" * 60]


def test_typing_delay_proportional_and_capped():
    human = HumanizeConfig(typing_min=0.0, typing_max=0.0, typing_chars_per_second=10.0, max_typing_seconds=2.0)
    assert Bot._typing_delay("x" * 10, human) == pytest.approx(1.0)
    assert Bot._typing_delay("x" * 1000, human) == pytest.approx(2.0)


def test_humanize_chunks_split_thresholds():
    human = HumanizeConfig(split_chance=1.0, split_min_chars=18, split_max_chars=140)
    bot = object.__new__(Bot)
    long_text = "今天上课好累啊。老师拖堂了二十分钟，我快饿死了。"
    assert len(bot._humanize_chunks([long_text], human)) == 2
    assert bot._humanize_chunks(["太短了"], human) == ["太短了"]
    human0 = HumanizeConfig(split_chance=0.0)
    assert bot._humanize_chunks([long_text], human0) == [long_text]


def test_humanize_config_validation():
    with pytest.raises(ValueError, match="0~1"):
        AppConfig.from_dict(
            {
                "deepseek": {"api_key": "sk-test"},
                "wechat": {"adapter": "mock"},
                "humanize": {"split_chance": 1.5},
            }
        )


def test_engine_injects_current_time(tmp_path):
    cfg = AppConfig.from_dict(
        {
            "deepseek": {"api_key": "sk-test"},
            "chat": {"time_aware": True},
            "context": {"persist_dir": str(tmp_path)},
            "wechat": {"adapter": "mock"},
        }
    )
    captured = []

    class LLM:
        last_usage: dict = {}

        def chat(self, messages, **kwargs):
            captured.append(messages[0]["content"])
            return "嗯"

    engine = ReplyEngine(cfg, LLM(), __import__("src.context", fromlist=["ContextStore"]).ContextStore(str(tmp_path)))
    msg = IncomingMessage(room_id="r", sender_id="u", sender_name="我", text="在吗")
    engine.generate([msg])
    assert "【当前时间】" in captured[0]


def test_engine_time_awareness_off_by_default(tmp_path):
    cfg = AppConfig.from_dict(
        {
            "deepseek": {"api_key": "sk-test"},
            "context": {"persist_dir": str(tmp_path)},
            "wechat": {"adapter": "mock"},
        }
    )
    captured = []

    class LLM:
        last_usage: dict = {}

        def chat(self, messages, **kwargs):
            captured.append(messages[0]["content"])
            return "嗯"

    engine = ReplyEngine(cfg, LLM(), __import__("src.context", fromlist=["ContextStore"]).ContextStore(str(tmp_path)))
    engine.generate([IncomingMessage(room_id="r", sender_id="u", sender_name="我", text="在吗")])
    assert "【当前时间】" not in captured[0]
