"""主动开场测试。"""
from __future__ import annotations

import threading
from datetime import datetime

from src.config import ProactiveConfig
from src.proactive import ProactiveScheduler


def make(cfg, rng=None):
    events: list[tuple] = []
    scheduler = ProactiveScheduler(
        cfg=cfg,
        targets_fn=lambda: [("g1", "群A")],
        text_fn=lambda room_id, room_name: "在干嘛呢",
        send_text=lambda room_id, text: events.append(("text", room_id, text)),
        send_sticker=lambda room_id, path: events.append(("sticker", room_id, path)),
        poke=lambda room_id: events.append(("poke", room_id)),
        pick_sticker=lambda room_name: "sticker.gif",
        stop_event=threading.Event(),
        rng=rng,
    )
    return scheduler, events


class FixedRng:
    def __init__(self, value: float):
        self.value = value

    def random(self) -> float:
        return self.value


def test_active_hours():
    cfg = ProactiveConfig(active_from="08:30", active_to="23:30")
    scheduler, _ = make(cfg)
    assert scheduler.in_active_hours(datetime(2026, 10, 20, 9, 0))
    assert not scheduler.in_active_hours(datetime(2026, 10, 20, 3, 0))


def test_active_hours_cross_midnight():
    cfg = ProactiveConfig(active_from="22:00", active_to="02:00")
    scheduler, _ = make(cfg)
    assert scheduler.in_active_hours(datetime(2026, 10, 20, 23, 0))
    assert scheduler.in_active_hours(datetime(2026, 10, 20, 1, 0))
    assert not scheduler.in_active_hours(datetime(2026, 10, 20, 12, 0))


def test_pick_action_thresholds():
    cfg = ProactiveConfig(poke_chance=0.05, sticker_chance=0.15, text_chance=0.80)
    assert make(cfg, FixedRng(0.01))[0].pick_action() == "poke"
    assert make(cfg, FixedRng(0.10))[0].pick_action() == "sticker"
    assert make(cfg, FixedRng(0.50))[0].pick_action() == "text"
    sparse = ProactiveConfig(poke_chance=0.05, sticker_chance=0.15, text_chance=0.50)
    assert make(sparse, FixedRng(0.95))[0].pick_action() == "none"


def test_act_text():
    cfg = ProactiveConfig(poke_chance=0.0, sticker_chance=0.0, text_chance=1.0)
    scheduler, events = make(cfg, FixedRng(0.5))
    scheduler.act("g1", "群A")
    assert events == [("text", "g1", "在干嘛呢")]


def test_act_poke():
    cfg = ProactiveConfig(poke_chance=1.0, sticker_chance=0.0, text_chance=0.0)
    scheduler, events = make(cfg, FixedRng(0.01))
    scheduler.act("g1", "群A")
    assert events == [("poke", "g1")]


def test_act_sticker():
    cfg = ProactiveConfig(poke_chance=0.0, sticker_chance=1.0, text_chance=0.0)
    scheduler, events = make(cfg, FixedRng(0.1))
    scheduler.act("g1", "群A")
    assert events == [("sticker", "g1", "sticker.gif")]


def test_act_sticker_falls_back_to_text():
    cfg = ProactiveConfig(poke_chance=0.0, sticker_chance=1.0, text_chance=0.0)
    scheduler, events = make(cfg, FixedRng(0.1))
    scheduler.pick_sticker = lambda room_name: None
    scheduler.act("g1", "群A")
    assert events == [("text", "g1", "在干嘛呢")]
