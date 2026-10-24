"""群聊上下文记忆：按群维护对话历史，裁剪后拼成 DeepSeek 的 messages。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from .runtime import atomic_json

log = logging.getLogger("dsbot.context")


@dataclass
class Turn:
    role: str          # user / assistant
    name: str          # 发言人显示名
    content: str
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Turn":
        return cls(
            role=str(d.get("role", "user")),
            name=str(d.get("name", "")),
            content=str(d.get("content", "")),
            ts=float(d.get("ts", 0.0)),
        )


class RoomContext:
    """一个群的对话上下文。"""

    def __init__(self, room_id: str, room_name: str = "", turns: list[Turn] | None = None) -> None:
        self.room_id = room_id
        self.room_name = room_name
        self.turns: list[Turn] = turns or []

    # ---------- 写入 ----------
    def add_user(self, name: str, text: str, ts: float | None = None) -> None:
        self.turns.append(Turn("user", name, text, ts or time.time()))

    def add_assistant(self, text: str, ts: float | None = None) -> None:
        self.turns.append(Turn("assistant", "", text, ts or time.time()))

    # ---------- 读取 ----------
    def build_messages(
        self,
        system_prompt: str,
        window_messages: int,
        max_context_chars: int,
    ) -> list[dict[str, str]]:
        """返回可直接给 OpenAI 兼容接口的 messages。"""
        recent = self.turns[-window_messages:] if window_messages > 0 else []
        # One pass, with a strict budget even when the newest message is huge.
        selected = []
        remaining = max(0, max_context_chars)
        for turn in reversed(recent):
            if len(turn.content) > remaining:
                if not selected and remaining:
                    selected.append(Turn(turn.role, turn.name, turn.content[:remaining], turn.ts))
                break
            selected.append(turn)
            remaining -= len(turn.content)
        recent = list(reversed(selected))

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        for t in recent:
            # 历史里残留的"[表情包]"占位回复会让模型跟着只回这三个字，直接跳过
            if t.role == "assistant" and t.content.strip() == "[表情包]":
                continue
            if t.role == "assistant":
                messages.append({"role": "assistant", "content": t.content})
            else:
                speaker = t.name or "群友"
                messages.append({"role": "user", "content": f"{speaker}：{t.content}"})
        return messages

    def trim(self, keep: int) -> None:
        if keep > 0 and len(self.turns) > keep:
            self.turns = self.turns[-keep:]

    def reset(self) -> None:
        self.turns.clear()

    def to_dict(self, room_name: str = "") -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "room_name": self.room_name or room_name,
            "updated_at": time.time(),
            "turns": [t.to_dict() for t in self.turns],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RoomContext":
        return cls(
            room_id=str(d.get("room_id", "")),
            room_name=str(d.get("room_name", "")),
            turns=[Turn.from_dict(t) for t in d.get("turns", [])],
        )


class ContextStore:
    """按群持久化上下文（JSON 文件，原子写入）。"""

    def __init__(self, persist_dir: str | Path, keep_turns: int = 200) -> None:
        self.dir = Path(persist_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep_turns = keep_turns
        self._cache: dict[str, RoomContext] = {}
        self._guard = threading.RLock()
        self._locks: dict[str, threading.RLock] = {}

    def lock_for(self, room_id: str):
        with self._guard:
            return self._locks.setdefault(room_id, threading.RLock())

    def _path(self, room_id: str) -> Path:
        digest = hashlib.sha1(room_id.encode("utf-8")).hexdigest()[:16]
        return self.dir / f"{digest}.json"

    def get(self, room_id: str, room_name: str = "") -> RoomContext:
        with self.lock_for(room_id):
            return self._get(room_id, room_name)

    def _get(self, room_id: str, room_name: str = "") -> RoomContext:
        if room_id in self._cache:
            ctx = self._cache[room_id]
            if room_name and not ctx.room_name:
                ctx.room_name = room_name
            return ctx
        ctx = RoomContext(room_id, room_name)
        path = self._path(room_id)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                ctx = RoomContext.from_dict(data)
                ctx.room_id = room_id
            except Exception as exc:  # noqa: BLE001
                log.warning("读取上下文失败 %s：%s（已忽略旧文件）", path, exc)
        self._cache[room_id] = ctx
        return ctx

    def save(self, ctx: RoomContext) -> None:
        with self.lock_for(ctx.room_id):
            ctx.trim(self.keep_turns)
            atomic_json(self._path(ctx.room_id), ctx.to_dict())

    def reset(self, room_id: str) -> None:
        with self.lock_for(room_id):
            ctx = self.get(room_id)
            ctx.reset()
            self.save(ctx)
