"""主动开场：随机间隔主动找人聊天（文本 / 表情包 / 戳一戳）。

设计要求（按用户要求）：
* **不固定时间**：两次主动消息的间隔在 [min_gap, max_gap] 之间随机，首次延迟也随机；
* **只在活跃时段**发（默认 08:30~23:30），避免半夜吵人；
* 内容要贴合当前时间 + 最近聊过的事：由模型基于最近上下文 + 当前时间生成；
* 三种动作：发一句话 / 发一张对方常用的表情包 / 戳一戳；
* 每天有上限，防止刷屏。
"""
from __future__ import annotations

import logging
import random
import threading
import time
import json
from pathlib import Path
from contextlib import nullcontext
from datetime import datetime
from typing import Callable

from .config import ProactiveConfig
from .runtime import atomic_json

log = logging.getLogger("dsbot.proactive")

# (room_id, room_name)
Target = tuple[str, str]


def _parse_hhmm(raw: str, default: int) -> int:
    try:
        hour, minute = str(raw).split(":")
        return int(hour) * 60 + int(minute)
    except Exception:  # noqa: BLE001
        return default


class ProactiveScheduler:
    def __init__(
        self,
        cfg: ProactiveConfig,
        targets_fn: Callable[[], list[Target]],
        text_fn: Callable[[str, str], str],
        send_text: Callable[[str, str], None],
        send_sticker: Callable[[str, str], None],
        poke: Callable[[str], None],
        pick_sticker: Callable[[str], str | None],
        stop_event: threading.Event,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.time,
        sleep_fn: Callable[[float], None] | None = None,
        room_lock_fn=None,
        eligible_fn=None,
        state_path=None,
        sticker_limit: int = 0,
    ) -> None:
        self.cfg = cfg
        self.targets_fn = targets_fn
        self.text_fn = text_fn
        self.send_text = send_text
        self.send_sticker = send_sticker
        self.poke = poke
        self.pick_sticker = pick_sticker
        self.stop_event = stop_event
        self.rng = rng or random.Random()
        self.clock = clock
        self._sleep = sleep_fn or self._default_sleep
        self._thread: threading.Thread | None = None
        self._sent_today = 0
        self._day = datetime.fromtimestamp(clock()).date()
        self.room_lock_fn = room_lock_fn
        self.eligible_fn = eligible_fn
        self._last_sent: dict[str, float] = {}
        self.state_path = Path(state_path) if state_path else None
        self.sticker_limit = sticker_limit
        self._stickers_today = 0
        if self.state_path and self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
                if saved.get("day") == self._day.isoformat():
                    self._sent_today = int(saved.get("sent", 0))
                    self._stickers_today = int(saved.get("stickers", 0))
                    self._last_sent = saved.get("last_sent", {})
            except (ValueError, OSError):
                log.warning("读取主动聊天计数失败")

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="proactive", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _default_sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.stop_event.wait(seconds)

    # ------------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        first = self.rng.uniform(3 * 60, min(20 * 60, self.cfg.max_gap_minutes * 60))
        log.info(
            "主动开场已启用：首次约 %.0f 分钟后检查，之后检查间隔 %.0f~%.0f 分钟随机",
            first / 60,
            self.cfg.min_gap_minutes,
            self.cfg.max_gap_minutes,
        )
        self._sleep(first)
        while not self.stop_event.is_set():
            try:
                targets = self.targets_fn()
                now = datetime.fromtimestamp(self.clock())
                self._roll_day(now)
                if targets and self.in_active_hours(now) and self._sent_today < self.cfg.max_per_day:
                    room_id, room_name = self.rng.choice(targets)
                    self.act(room_id, room_name)
                else:
                    log.debug(
                        "主动开场跳过：目标 %d 个，活跃时段=%s，今日已发 %d/%d",
                        len(targets),
                        self.in_active_hours(now),
                        self._sent_today,
                        self.cfg.max_per_day,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("主动开场执行失败：%s", exc)
            gap = self.rng.uniform(self.cfg.min_gap_minutes, self.cfg.max_gap_minutes) * 60
            self._sleep(gap)

    def _roll_day(self, now: datetime) -> None:
        if now.date() != self._day:
            self._day = now.date()
            self._sent_today = 0
            self._stickers_today = 0

    def in_active_hours(self, now: datetime | None = None) -> bool:
        now = now or datetime.fromtimestamp(self.clock())
        start = _parse_hhmm(self.cfg.active_from, 8 * 60 + 30)
        end = _parse_hhmm(self.cfg.active_to, 23 * 60 + 30)
        current = now.hour * 60 + now.minute
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end  # 跨零点

    def pick_action(self) -> str:
        """按配置概率决定这次做什么：text / sticker / poke / none。"""
        roll = self.rng.random()
        poke_end = self.cfg.poke_chance
        sticker_end = poke_end + self.cfg.sticker_chance
        text_end = min(1.0, sticker_end + self.cfg.text_chance)
        if roll < poke_end:
            return "poke"
        if roll < sticker_end:
            return "sticker"
        if roll < text_end:
            return "text"
        return "none"

    def act(self, room_id: str, room_name: str) -> str:
        lock = self.room_lock_fn(room_id) if self.room_lock_fn else nullcontext()
        with lock:
            self._roll_day(datetime.fromtimestamp(self.clock()))
            if self._sent_today >= self.cfg.max_per_day:
                return "none"
            if self.stop_event.is_set() or (self.eligible_fn and not self.eligible_fn(room_id)):
                return "none"
            if self.clock() - self._last_sent.get(room_id, float("-inf")) < self.cfg.min_gap_minutes * 60:
                return "none"
            action = self._act(room_id, room_name)
            if action != "none":
                self._last_sent[room_id] = self.clock()
                self._sent_today += 1
                if action == "sticker":
                    self._stickers_today += 1
                if self.state_path:
                    atomic_json(self.state_path, {"day": self._day.isoformat(), "sent": self._sent_today,
                                                 "stickers": self._stickers_today, "last_sent": self._last_sent})
            return action

    def _act(self, room_id: str, room_name: str) -> str:
        action = self.pick_action()
        log.info("主动开场 → %s（动作：%s）", room_name, action)
        if action == "poke":
            if self.poke(room_id) is False:
                return "none"
            return action
        if action == "sticker":
            if self.sticker_limit > 0 and self._stickers_today >= self.sticker_limit:
                action = "text"
        if action == "sticker":
            path = self.pick_sticker(room_name)
            if path:
                if self.send_sticker(room_id, path) is False:
                    return "none"
                return action
            action = "text"  # 没表情可用就退回说句话
        if action == "text":
            text = self.text_fn(room_id, room_name)
            if text:
                if self.eligible_fn and not self.eligible_fn(room_id):
                    return "none"
                if self.send_text(room_id, text) is False:
                    return "none"
            else:
                return "none"
        return action
