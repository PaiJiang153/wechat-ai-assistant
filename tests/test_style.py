"""全局文字风格测试：任何聊天都不在句尾出现句号。"""
from __future__ import annotations

from src.bot import split_reply_in_two
from src.engine import sanitize_reply


def test_trailing_period_removed():
    assert sanitize_reply("你好。") == "你好"
    assert sanitize_reply("我是大肥鱼。") == "我是大肥鱼"


def test_inner_period_becomes_comma():
    assert sanitize_reply("写完作业。我去吃饭。") == "写完作业，我去吃饭"


def test_multiple_periods_collapsed():
    assert sanitize_reply("好呀。。") == "好呀"
    assert sanitize_reply("好呀！。") == "好呀！"


def test_multiline_periods_removed():
    assert sanitize_reply("第一行。\n第二行。") == "第一行\n第二行"


def test_other_punctuation_kept():
    assert sanitize_reply("真的吗？！") == "真的吗？！"
    assert sanitize_reply("好耶！") == "好耶！"


def test_split_keeps_no_period():
    text = sanitize_reply("今天好累呀。早点睡吧。明天再聊。")
    for part in split_reply_in_two(text):
        assert not part.endswith("。")
