"""没有微信时的适配器：控制台模拟群聊。

用法：
    python -m src.main mock
然后输入：
    我: 今天吃什么
    朋友: 火锅吧
    DeepSeek 会对每条消息按配置的触发规则回复（返回内容直接打印）。
命令：
    /reset   清空当前群上下文
    /quit    退出
"""
from __future__ import annotations

import logging
import threading
import time

from .adapter import WeChatAdapter
from .models import IncomingMessage

log = logging.getLogger("dsbot.mock")


class ConsoleAdapter(WeChatAdapter):
    name = "console-mock"

    def __init__(self, room_id: str = "mock-room@chatroom", room_name: str = "我和朋友们的群") -> None:
        super().__init__()
        self.room_id = room_id
        self.room_name = room_name
        self.self_wxid = "wxid_bot_self"
        self._seq = 0
        self._stop = threading.Event()

    def get_self_wxid(self) -> str:
        return self.self_wxid

    def start(self) -> None:
        print("=" * 60)
        print(" 模拟群聊模式（不需要微信）。输入格式：发送者: 内容")
        print(" 例如 我: 今天吃什么     朋友: 火锅吧")
        print(" 命令：/reset 清空上下文   /quit 退出")
        print("=" * 60)
        while not self._stop.is_set():
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in ("/quit", "/exit", "quit", "exit"):
                break
            if line == "/reset":
                if self._on_message:
                    _ResetFlag.emit(self)
                print("（上下文已清空）")
                continue
            speaker, text = self._parse(line)
            self._seq += 1
            msg = IncomingMessage(
                room_id=self.room_id,
                sender_id=f"wxid_{abs(hash(speaker)) % 10**8}",
                sender_name=speaker,
                text=text,
                room_name=self.room_name,
                msg_id=f"mock-{self._seq}",
                is_group=True,
            )
            if self._on_message:
                self._on_message(msg)

    def stop(self) -> None:
        self._stop.set()

    def send_text(self, room_id: str, text: str, at_wxid: str | None = None) -> None:
        prefix = ""
        if at_wxid:
            prefix = "@群友 "
        print(f"\n🤖 DeepSeek{prefix}: {text}\n")

    @staticmethod
    def _parse(line: str) -> tuple[str, str]:
        for sep in (":", "："):
            if sep in line:
                head, _, tail = line.partition(sep)
                head = head.strip()
                if head and len(head) <= 12 and " " not in head:
                    return head, tail.strip()
        return "我", line


class _ResetFlag:
    """给控制台用的轻量钩子：通知 Bot 清空上下文。"""

    @staticmethod
    def emit(adapter: ConsoleAdapter) -> None:
        handler = getattr(adapter, "on_reset", None)
        if handler:
            handler(adapter.room_id)
