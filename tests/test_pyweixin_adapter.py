"""pyweixin（微信 4.x UI 自动化）适配器的单元测试。

用假模块替换 pyweixin / pyautogui，不依赖真实微信、不弹窗。
"""
from __future__ import annotations

import sys
import types

import pytest

from src.pyweixin_adapter import PyweixinAdapter


# ---------------------------------------------------------------------- 假 UI
class FakeEdit:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def set_text(self, text: str) -> None:
        self.texts.append(text)


class FakeDialog:
    def __init__(self) -> None:
        self.closed = False
        self.edit = FakeEdit()
        self.restored = 0

    def exists(self, timeout: float = 0.5) -> bool:
        return not self.closed

    def restore(self) -> None:
        self.restored += 1

    def close(self) -> None:
        self.closed = True

    def child_window(self, **kwargs):
        return self.edit


class FakeMonitor:
    """按预设批次返回消息；批次用完后再空转一轮，然后让适配器停止。"""

    batches: list[dict] = []
    adapter: PyweixinAdapter | None = None
    calls = 0
    empties = 0

    @classmethod
    def listen_on_chat(cls, dialog, duration, groupMembers=None, close_dialog_window=True):
        cls.calls += 1
        if cls.batches:
            return cls.batches.pop(0)
        cls.empties += 1
        if cls.empties >= 2 and cls.adapter is not None:
            cls.adapter.stop()
        return {"文本内容": [], "消息发送者": []}


class FakeNavigator:
    dialog = FakeDialog()

    @staticmethod
    def open_weixin():
        return object()

    @classmethod
    def open_seperate_dialog_window(cls, **kwargs):
        return cls.dialog


class FakeTools:
    @staticmethod
    def is_weixin_running() -> bool:
        return True

    @staticmethod
    def get_weixin_version() -> str:
        return "4.1.13.65"


class FakeEdits:
    InputEdit = {"title": "输入", "control_type": "Edit"}


def install_fakes(monkeypatch, batches: list[dict]) -> FakeDialog:
    pkg = types.ModuleType("pyweixin")
    pkg.Navigator = FakeNavigator
    pkg.Monitor = FakeMonitor
    pkg.Tools = FakeTools
    ui = types.ModuleType("pyweixin.Uielements")
    ui.Edits = FakeEdits
    pkg.Uielements = ui
    monkeypatch.setitem(sys.modules, "pyweixin", pkg)
    monkeypatch.setitem(sys.modules, "pyweixin.Uielements", ui)

    pyautogui = types.ModuleType("pyautogui")
    calls: list[tuple] = []
    pyautogui.hotkey = lambda *a, **k: calls.append(a)
    pyautogui.calls = calls
    monkeypatch.setitem(sys.modules, "pyautogui", pyautogui)

    FakeMonitor.batches = list(batches)
    FakeMonitor.calls = 0
    FakeMonitor.empties = 0
    FakeNavigator.dialog = FakeDialog()
    # 发送间隔 0.8s 会让测试变慢，直接打掉
    import src.pyweixin_adapter as mod

    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    return FakeNavigator.dialog


def make_adapter(**kwargs) -> PyweixinAdapter:
    return PyweixinAdapter(
        group_name="我+朋友+DeepSeek",
        group_members=["我", "朋友", "DeepSeek"],
        poll_seconds=0.5,
        self_name="DeepSeek",
        **kwargs,
    )


# ---------------------------------------------------------------------- 用例
def test_receives_and_dispatches(monkeypatch):
    install_fakes(
        monkeypatch,
        [{"文本内容": ["我 晚上吃什么"], "消息发送者": ["我"]}],
    )
    adapter = make_adapter()
    FakeMonitor.adapter = adapter
    got = []
    adapter.on_message(got.append)
    adapter.start()

    assert len(got) == 1
    assert got[0].text == "晚上吃什么"
    assert got[0].sender_name == "我"
    assert got[0].room_name == "我+朋友+DeepSeek"


def test_ignores_own_messages(monkeypatch):
    install_fakes(
        monkeypatch,
        [{"文本内容": ["DeepSeek 收到"], "消息发送者": ["DeepSeek"]}],
    )
    adapter = make_adapter()
    FakeMonitor.adapter = adapter
    got = []
    adapter.on_message(got.append)
    adapter.start()
    assert got == []


def test_send_via_ui(monkeypatch):
    dialog = install_fakes(monkeypatch, [])
    adapter = make_adapter()
    FakeMonitor.adapter = adapter
    adapter.on_message(lambda m: None)
    adapter.send_text("我+朋友+DeepSeek", "你好呀", at_wxid="我")
    adapter.start()  # 第一轮就 drain 发送队列

    assert dialog.edit.texts, "应该通过 UI 输入框发送"
    assert dialog.edit.texts[0] == "@我 你好呀"
    import pyautogui  # 已被替换成假模块

    assert ("alt", "s") in pyautogui.calls


def test_echo_of_own_reply_is_ignored(monkeypatch):
    install_fakes(monkeypatch, [])
    adapter = make_adapter()
    got = []
    adapter.on_message(got.append)
    adapter._sent_recent.append(("刚发的一句话", 9e18 + 1))  # 未来时间，确保在窗口内
    adapter._dispatch_text("刚发的一句话", "")
    assert got == []


def test_requires_group_name():
    with pytest.raises(ValueError):
        PyweixinAdapter(group_name="")
