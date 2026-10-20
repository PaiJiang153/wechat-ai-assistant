"""Concurrency and failure regressions. No real WeChat, API or user data."""
from __future__ import annotations

import json
import os
import threading
import time
import types
import zipfile
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.bot import Bot
from src.config import AppConfig, load_config, MemoryConfig
from src.context import ContextStore
from src.engine import ReplyEngine
from src.maintenance import backup, cleanup
from src.memory import MemoryExtractor, MemoryManager, ProfileStore
from src.models import IncomingMessage
from src.runtime import DeliveryResult, SendReceipt, UsageMeter
from src.proactive import ProactiveScheduler
from src.config import ProactiveConfig
from src.wechatauto_adapter import WechatautoAdapter


def config(tmp_path, **chat):
    cfg = AppConfig.from_dict({"wechat": {"adapter": "mock"},
                               "chat": {"debounce_seconds": 0.02, "min_interval_seconds": 0, **chat},
                               "logging": {"dir": str(tmp_path / "logs")}})
    cfg.context.persist_dir = str(tmp_path / "rooms")
    cfg.memory.dir = str(tmp_path / "memory")
    cfg.vision.media_dir = str(tmp_path / "media")
    cfg.files.dir = str(tmp_path / "files")
    cfg.imagegen.out_dir = str(tmp_path / "generated")
    cfg.stickers.dir = str(tmp_path / "stickers")
    cfg.wechat.state_dir = str(tmp_path / "state")
    return cfg


class LLM:
    last_usage = {}

    def chat(self, messages, **kwargs):
        return "收到啦"


def message(text="你好", room="r1", mid=""):
    return IncomingMessage(room_id=room, room_name=room, sender_id="u", text=text, msg_id=mid)


def engine_for(cfg, llm=None, **kwargs):
    return ReplyEngine(cfg, llm or LLM(), ContextStore(cfg.context.persist_dir), **kwargs)


def test_examples_validate_without_credentials():
    root = Path(__file__).resolve().parents[1]
    for name in ("config.example.yaml", "config.minimal.yaml"):
        assert load_config(root / name).wechat.adapter == "wechatauto"


def test_environment_key_takes_precedence(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cfg.deepseek.api_key = "file-key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-key")
    assert cfg.deepseek.resolve_api_key() == "environment-key"


@pytest.mark.parametrize("value", ["25:00", "10:99", "1:30", "10:30:00"])
def test_invalid_active_hours_rejected(value):
    with pytest.raises(ValueError):
        AppConfig.from_dict({"proactive": {"active_from": value}})


def test_simultaneous_rooms_keep_their_own_images(tmp_path):
    barrier = threading.Barrier(2)

    class ToolLLM(LLM):
        def complete(self, messages, tools=None):
            if any(m["role"] == "tool" for m in messages):
                barrier.wait(timeout=3)
                return types.SimpleNamespace(content="画好了", tool_calls=[])
            prompt = messages[-1]["content"]
            call = types.SimpleNamespace(id="image", function=types.SimpleNamespace(
                name="generate_image", arguments=json.dumps({"prompt": prompt})))
            return types.SimpleNamespace(content="", tool_calls=[call])

    gen = types.SimpleNamespace(generate=lambda prompt, size: prompt + ".png")
    engine = engine_for(config(tmp_path), ToolLLM(), imagegen=gen)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(engine.generate, [message("A", "A")], commit=False)
        second = pool.submit(engine.generate, [message("B", "B")], commit=False)
        a, b = first.result(timeout=5), second.result(timeout=5)
    assert a.room_id == "A" and a.images == ["u：A.png"]
    assert b.room_id == "B" and b.images == ["u：B.png"]
    assert all(t.role == "user" for t in engine.store.get("A").turns)


class ReceiptAdapter:
    name = "test"

    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.sent = []

    def send_text(self, room, text, at=None):
        self.sent.append(text)
        receipt = SendReceipt()
        receipt.set_result(DeliveryResult(next(self.statuses)))
        return receipt

    def stop(self):
        pass


@pytest.mark.parametrize("status", ["failed", "unknown", "expired"])
def test_unconfirmed_sends_never_become_assistant_history(tmp_path, status):
    cfg = config(tmp_path)
    engine = engine_for(cfg)
    bot = Bot(cfg, ReceiptAdapter([status]), engine)
    try:
        bot._process("r1", [message()], engine.store.lock_for("r1"))
        assert [t.role for t in engine.store.get("r1").turns] == ["user"]
        assert bot.stats.replied == 0
        assert bot.stats.delivery_failed + bot.stats.delivery_unknown == 1
    finally:
        bot.shutdown()


def test_only_delivered_chunks_enter_history(tmp_path):
    cfg = config(tmp_path, max_message_chars=50)
    llm = LLM()
    llm.chat = lambda *a, **kw: "长句子。" * 50
    engine = engine_for(cfg, llm)
    adapter = ReceiptAdapter(["confirmed", "unknown", "confirmed"])
    bot = Bot(cfg, adapter, engine)
    try:
        bot._process("r1", [message()], engine.store.lock_for("r1"))
        assert len(adapter.sent) == 2
        assert [t.content for t in engine.store.get("r1").turns if t.role == "assistant"] == [adapter.sent[0]]
    finally:
        bot.shutdown()


def connected_adapter(**kwargs):
    adapter = WechatautoAdapter(group_name="群A", **kwargs)
    adapter._group_id = "g1"
    adapter._groups = {"群A": {"id": "g1", "last_id": 0, "members": {"u": "朋友"}, "members_at": time.time()}}
    adapter._self_wxid, adapter._self_name = "bot", "小号"
    return adapter


def test_real_mentions_use_nickname_and_verification():
    adapter = connected_adapter()
    calls = []
    gui = types.SimpleNamespace(at_member=lambda *a, **kw: calls.append((a, kw)) or {"status": "成功"})
    adapter._wx = types.SimpleNamespace(_gui=gui)
    result = adapter._send_now("g1", "你好", "u")
    assert result.confirmed
    assert calls == [(("朋友", "你好"), {"who": "群A", "verify": True})]


def test_unknown_delivery_is_not_resent():
    adapter = connected_adapter()
    calls = []
    adapter._wx = types.SimpleNamespace(_gui=types.SimpleNamespace(
        send_msg=lambda *a, **kw: calls.append(a) or {"status": "失败"}))
    assert adapter._send_now("g1", "你好", None).status == "unknown"
    assert len(calls) == 1


def test_mentions_from_metadata_and_actual_account_name():
    adapter = connected_adapter()
    assert adapter._extract_mentions({"msg_source": "<atuserlist><![CDATA[bot,u]]></atuserlist>"}, "hi") == ["bot", "u"]
    assert adapter._extract_mentions({}, "@小号\u2005你好") == ["bot"]
    assert adapter._extract_mentions({}, "@小号外卖") == []


def test_system_events_are_not_chat_messages():
    adapter = connected_adapter()
    assert adapter._normalise_content({"type": "系统", "content": "你拍了拍自己"}) == ""


def test_bad_room_does_not_block_other_receivers():
    adapter = connected_adapter()
    adapter._groups["群B"] = {"id": "g2", "last_id": 0, "members_at": time.time()}
    class DB:
        def get_messages(self, room, **kwargs):
            if room == "g1":
                raise RuntimeError("broken shard")
            return [{"local_id": 1, "type": "文本", "content": "ok", "sender_username": "u"}]
    adapter._db = DB()
    received = []
    adapter.on_message(received.append)
    adapter._poll_once()
    assert len(received) == 1 and received[0].room_id == "g2"


def test_ambiguous_or_unconfigured_destinations_are_rejected():
    adapter = connected_adapter()
    adapter._db = types.SimpleNamespace(get_groups=lambda: [
        {"name": "群A", "username": "one"}, {"name": "群A", "username": "two"}])
    with pytest.raises(RuntimeError, match="同名"):
        adapter._resolve_group_id("群A")
    with pytest.raises(ValueError):
        adapter._name_for_room("unconfigured")


def test_bounded_outbox_expiry_and_stop(monkeypatch):
    adapter = connected_adapter(queue_size=1, task_ttl_seconds=1)
    receipt = adapter.send_text("g1", "first")
    assert adapter.send_text("g1", "overflow").result().status == "failed"
    receipt.created_at -= 2
    monkeypatch.setattr(adapter._stop, "wait", lambda seconds: False)
    adapter._drain_outbox()
    assert receipt.result().status == "expired"
    pending = adapter.send_text("g1", "pending")
    adapter.stop()
    assert pending.result().status == "expired"


def test_pagination_shards_ties_and_cursor_restart(tmp_path):
    rows = [{"sort_seq": i // 2 + 1, "local_id": i // 2 + 1,
             "shard": str(i % 2), "type": "文本", "content": str(i), "sender_username": "u"}
            for i in range(160)]
    db = types.SimpleNamespace(get_messages=lambda room, limit=20, offset=0:
                               sorted(rows, key=lambda r: r["sort_seq"], reverse=True)[offset:offset+limit])
    adapter = connected_adapter(queue_size=23)
    adapter._db = db
    adapter._state_path = tmp_path / "cursor.json"
    received = []
    adapter.on_message(received.append)
    for _ in range(9):
        adapter._poll_once()
    assert len(received) == 160
    assert len({m.msg_id for m in received}) == 160
    restarted = connected_adapter()
    restarted._db = db
    restarted._saved_cursors = json.loads(adapter._state_path.read_text(encoding="utf-8"))
    restarted._initialize_cursor(restarted._groups["群A"], "g1")
    restarted.on_message(lambda m: pytest.fail("old message replayed"))
    restarted._poll_once()


def test_continuous_messages_do_not_debounce_forever(tmp_path):
    cfg = config(tmp_path, debounce_seconds=0.08, max_debounce_seconds=0.12)
    adapter = ReceiptAdapter(["confirmed"] * 20)
    bot = Bot(cfg, adapter, engine_for(cfg))
    try:
        for i in range(16):
            bot.on_message(message(mid=str(i)))
            time.sleep(0.02)
        assert adapter.sent
    finally:
        bot.shutdown()


def test_empty_authoritative_memory_removes_old_facts(tmp_path):
    llm = LLM()
    llm.chat = lambda *a, **kw: "[]"
    extractor = MemoryExtractor(llm, MemoryConfig(dir=str(tmp_path)))
    assert extractor.extract(["obsolete"], "请忘记") == []


def test_manual_edit_wins_over_inflight_extraction(tmp_path):
    started, finish = threading.Event(), threading.Event()
    llm = LLM()
    def respond(*a, **kw):
        started.set()
        assert finish.wait(3)
        return '["stale"]'
    llm.chat = respond
    manager = MemoryManager(MemoryConfig(dir=str(tmp_path)), llm)
    turn = types.SimpleNamespace(role="user", name="我", content="new", ts=1)
    with ThreadPoolExecutor(1) as pool:
        job = pool.submit(manager._update, "r", "room", [turn])
        assert started.wait(2)
        ProfileStore(tmp_path).edit("r", ["manual"])
        finish.set()
        job.result(timeout=3)
    assert manager.store.load("r")["facts"] == ["manual"]


def test_cleanup_preview_and_backup_exclude_keys(tmp_path):
    cfg = config(tmp_path)
    root = Path(cfg.vision.media_dir)
    root.mkdir()
    old, new = root / "old.gif", root / "new.gif"
    old.write_bytes(b"gif")
    new.write_bytes(b"gif")
    os.utime(old, (1, 1))
    assert cleanup(cfg)["files"] == [str(old.resolve())]
    assert old.exists()
    ProfileStore(cfg.memory.dir).edit("r", ["fact"])
    archive = tmp_path / "backup.zip"
    backup(cfg, str(archive))
    with zipfile.ZipFile(archive) as z:
        assert all(name.startswith(("rooms/", "memory/", "stickers/", "state/")) for name in z.namelist())
        assert "config.yaml" not in z.namelist()
    cleanup(cfg, apply=True)
    assert not old.exists() and new.exists()


def test_maintenance_commands_from_cli(tmp_path, capsys):
    import yaml
    from src.main import main
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"wechat": {"adapter": "mock"}}), encoding="utf-8")
    def call(*args):
        assert main(["-c", str(config_path), *args]) == 0
        return json.loads(capsys.readouterr().out)
    assert call("memory", "set", "--room", "r", "--fact", "corrected")["facts"] == ["corrected"]
    assert call("memory", "show", "--room", "r")["facts"] == ["corrected"]
    assert call("memory", "list")[0]["room_id"] == "r"
    assert call("memory", "clear", "--room", "r")["facts"] == []
    assert call("cleanup")["applied"] is False
    assert call("backup", "--output", str(tmp_path / "state.zip"))["files"] == 1
    assert "usage_today" in call("status")


def test_usage_meter_survives_restart_and_concurrent_updates(tmp_path):
    path = tmp_path / "usage.json"
    meter = UsageMeter(path)
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: meter.record(total_tokens=10), range(40)))
    assert UsageMeter(path).snapshot()["total_tokens"] == 400


def test_late_confirmation_commits_history_once(tmp_path):
    cfg = config(tmp_path, delivery_timeout=0.02)
    receipt = SendReceipt()
    receipt.set_running_or_notify_cancel()
    adapter = ReceiptAdapter([])
    adapter.send_text = lambda *a, **kw: receipt
    engine = engine_for(cfg)
    bot = Bot(cfg, adapter, engine)
    try:
        assert not bot._safe_send("r1", None, "迟到的回复")
        assert not engine.store.get("r1").turns
        receipt.set_result(DeliveryResult("confirmed"))
        deadline = time.monotonic() + 2
        while bot.stats.delivery_unknown and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [t.content for t in engine.store.get("r1").turns] == ["迟到的回复"]
        assert bot.stats.replied == 1 and bot.stats.delivery_unknown == 0
        assert bot.metrics.snapshot()["send_attempts"] == 1
        assert bot.metrics.snapshot()["confirmed"] == 1
    finally:
        bot.shutdown()


def test_stop_cancels_unstarted_send(tmp_path):
    cfg = config(tmp_path)
    engine = engine_for(cfg)
    bot = Bot(cfg, ReceiptAdapter([]), engine)
    receipt = SendReceipt()
    bot.shutdown()
    assert bot._await_delivery(receipt).status == "expired"
    assert receipt.cancelled()


def test_late_receipt_never_blocks_gui_on_room_lock(tmp_path):
    cfg = config(tmp_path, delivery_timeout=0.01)
    receipt = SendReceipt()
    receipt.set_running_or_notify_cancel()
    adapter = ReceiptAdapter([])
    adapter.send_text = lambda *a, **kw: receipt
    engine = engine_for(cfg)
    bot = Bot(cfg, adapter, engine)
    try:
        assert not bot._safe_send("r1", None, "late")
        with engine.store.lock_for("r1"):
            gui = threading.Thread(target=receipt.set_result, args=(DeliveryResult("confirmed"),))
            gui.start()
            gui.join(timeout=0.5)
            assert not gui.is_alive()
        deadline = time.monotonic() + 2
        while bot.stats.delivery_unknown and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bot.stats.replied == 1
    finally:
        bot.shutdown()


def test_split_reply_includes_ellipsis_in_character_limit():
    from src.engine import split_text
    assert all(len(chunk) <= 50 for chunk in split_text("很长的句子。" * 100, 50, 3))


def test_unstructured_model_error_does_not_become_a_memory(tmp_path):
    llm = LLM()
    llm.chat = lambda *a, **kw: "抱歉，服务暂时不可用"
    extractor = MemoryExtractor(llm, MemoryConfig(dir=str(tmp_path)))
    assert extractor.extract(["existing"], "dialogue") == ["existing"]


def test_chunks_and_proactive_share_actual_send_limit(tmp_path):
    cfg = config(tmp_path, max_replies_per_minute=1)
    bot = Bot(cfg, ReceiptAdapter(["confirmed"] * 3), engine_for(cfg))
    try:
        assert bot._safe_send("r1", None, "第一条")
        assert not bot._safe_send("r1", None, "第二条")
        assert not bot._proactive_allowed("r1")
    finally:
        bot.shutdown()


def test_active_or_queued_conversations_are_not_proactive_targets(tmp_path):
    cfg = config(tmp_path)
    bot = Bot(cfg, ReceiptAdapter([]), engine_for(cfg))
    try:
        bot._last_activity["r1"] = time.monotonic()
        assert not bot._proactive_allowed("r1")
        bot._last_activity["r1"] -= cfg.proactive.min_gap_minutes * 60 + 1
        assert bot._proactive_allowed("r1")
        bot._running.add("r1")
        assert not bot._proactive_allowed("r1")
    finally:
        bot.shutdown()


def test_proactive_daily_count_survives_restart(tmp_path):
    kwargs = dict(cfg=ProactiveConfig(max_per_day=1, text_chance=1, sticker_chance=0, poke_chance=0),
                  targets_fn=lambda: [], text_fn=lambda *a: "你好", send_text=lambda *a: True,
                  send_sticker=lambda *a: None, poke=lambda *a: None, pick_sticker=lambda *a: None,
                  stop_event=threading.Event(), state_path=tmp_path / "proactive.json")
    first = ProactiveScheduler(**kwargs)
    assert first.act("r1", "room") == "text"
    second = ProactiveScheduler(**kwargs)
    assert second.act("r2", "other") == "none"


def test_huge_latest_message_respects_history_budget(tmp_path):
    store = ContextStore(tmp_path)
    context = store.get("r")
    context.add_user("我", "x" * 10000)
    messages = context.build_messages("system", 24, 100)
    assert messages[-1]["content"] == "我：" + "x" * 100
    assert len(context.turns[0].content) == 10000


def test_slow_media_does_not_block_other_room_or_gui(tmp_path, monkeypatch):
    blocked, release, other, sent = (threading.Event() for _ in range(4))
    rows = {"g1": [{"local_id": 1, "content": "slow", "sender_username": "u", "type": "文本"}],
            "g2": [{"local_id": 1, "content": "fast", "sender_username": "u", "type": "文本"}]}

    class DB:
        def get_self_info(self):
            return {"username": "bot", "nick_name": "小号"}
        def get_groups(self):
            return [{"name": "群A", "username": "g1"}, {"name": "群B", "username": "g2"}]
        def get_group_members(self, room):
            return []
        def get_messages(self, room, limit=20, offset=0):
            return rows[room][offset:offset+limit]

    monkeypatch.setitem(sys.modules, "wechatauto", types.SimpleNamespace(WeChatDB=DB, WeChat=lambda: object()))
    adapter = WechatautoAdapter(group_names=["群A", "群B"], skip_history=False, poll_seconds=0.5)
    def dispatch(row, name):
        if name == "群A":
            blocked.set()
            assert release.wait(3)
        else:
            other.set()
    monkeypatch.setattr(adapter, "_dispatch", dispatch)
    monkeypatch.setattr(adapter, "_send_now", lambda *a: sent.set() or DeliveryResult("confirmed"))
    identities = []
    adapter.on_self_wxid = identities.append
    thread = threading.Thread(target=adapter.start)
    thread.start()
    try:
        assert blocked.wait(2)
        assert other.wait(2)
        receipt = adapter.send_text("g2", "reply")
        assert sent.wait(2)
        assert receipt.result(timeout=2).confirmed
        assert identities == ["bot"]
    finally:
        release.set()
        adapter.stop()
        thread.join(timeout=3)
    assert not thread.is_alive()
