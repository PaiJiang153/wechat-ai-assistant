"""统一的消息模型：微信适配器把各种来源的消息归一化成 IncomingMessage。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class IncomingMessage:
    """一条进入机器人的消息（已归一化）。"""

    room_id: str                      # 群聊 id（如 xxx@chatroom）或单聊 wxid
    sender_id: str                    # 发送者 wxid
    text: str                         # 文本内容（非文本消息已转成占位描述）
    sender_name: str = ""             # 发送者显示名
    room_name: str = ""               # 群名
    msg_id: str = ""                  # 微信消息 id，用于去重
    ts: float = 0.0                   # 时间戳（秒）
    is_group: bool = True
    msg_type: int = 1                 # 微信原始消息类型
    at_wxids: list[str] = field(default_factory=list)  # 消息里 @ 到的 wxid

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = time.time()

    @property
    def at_me(self) -> bool:
        return bool(self.at_wxids)

    def speaker(self) -> str:
        return self.sender_name or self.sender_id


@dataclass
class OutgoingReply:
    """要发回微信的一条回复。"""

    room_id: str
    text: str
    at_sender_id: Optional[str] = None   # 非空则在群里 @ 这个人
