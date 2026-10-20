"""微信适配器抽象层。

核心逻辑（DeepSeek 对话引擎、上下文、过滤、限流）完全不依赖微信，
只依赖这里的抽象接口。这样：
  * 微信侧可以换实现（WeChatFerry / 付费 4.x 方案 / 未来官方 API）
  * 可以用 MockAdapter / 网页测试台在没有微信的情况下跑通全部逻辑
"""
from __future__ import annotations

from typing import Callable, Optional

from .models import IncomingMessage
from .runtime import SendReceipt, DeliveryResult

MessageHandler = Callable[[IncomingMessage], None]


class WeChatAdapter:
    """所有微信接入实现的基类。"""

    name = "base"

    def __init__(self) -> None:
        self._on_message: Optional[MessageHandler] = None

    # ---------- 生命周期 ----------
    def on_message(self, handler: MessageHandler) -> None:
        """注册消息回调。适配器收到消息后调用 handler(IncomingMessage)。"""
        self._on_message = handler

    def start(self) -> None:
        """阻塞式启动：登录微信、开始收消息。"""
        raise NotImplementedError

    def stop(self) -> None:
        """优雅退出。"""

    # ---------- 发送 ----------
    def send_text(self, room_id: str, text: str, at_wxid: Optional[str] = None) -> SendReceipt | DeliveryResult | None:
        """返回异步送达回执或同步结果；历史同步适配器可返回 None。

        confirmed 表示接口已核验送达；unknown 禁止自动重发。
        """
        raise NotImplementedError

    # ---------- 信息 ----------
    def get_self_wxid(self) -> str:
        """机器人自己（小号）的 wxid。"""
        return ""

    def describe(self) -> dict:
        return {"adapter": self.name, "self_wxid": self.get_self_wxid()}
