"""长期记忆测试。"""
from __future__ import annotations

from src.config import AppConfig, MemoryConfig
from src.memory import MemoryExtractor, MemoryManager, ProfileStore


class FakeLLM:
    last_usage: dict = {}

    def __init__(self, reply: str = '["小明喜欢白米饭", "小红最近在准备考试"]'):
        self.reply = reply
        self.calls: list = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return self.reply


def test_profile_store_roundtrip(tmp_path):
    store = ProfileStore(tmp_path)
    store.save({"room_id": "r1", "room_name": "群A", "facts": ["喜欢白米饭"], "updated_at": 1.0})
    record = store.load("r1")
    assert record["room_name"] == "群A"
    assert record["facts"] == ["喜欢白米饭"]


def test_extractor_parses_json(tmp_path):
    cfg = MemoryConfig(dir=str(tmp_path), max_facts=10)
    extractor = MemoryExtractor(FakeLLM('说明文字 [ "事实一", "事实二" ] 结束'), cfg)
    facts = extractor.extract([], "小明：我喜欢白米饭")
    assert facts == ["事实一", "事实二"]


def test_extractor_falls_back_to_lines(tmp_path):
    cfg = MemoryConfig(dir=str(tmp_path), max_facts=10)
    extractor = MemoryExtractor(FakeLLM("- 事实一\n- 事实二"), cfg)
    facts = extractor.extract([], "对话")
    assert "事实一" in facts and "事实二" in facts


def test_extractor_replaces_profile_with_authoritative_json(tmp_path):
    cfg = MemoryConfig(dir=str(tmp_path), max_facts=3)
    extractor = MemoryExtractor(FakeLLM('["新事实", "旧事实"]'), cfg)
    facts = extractor.extract(["旧事实", "更旧事实"], "对话")
    assert facts == ["新事实", "旧事实"]


def test_manager_inject_and_threshold(tmp_path, monkeypatch):
    cfg = MemoryConfig(dir=str(tmp_path), every_n_messages=2, max_facts=5)
    manager = MemoryManager(cfg, FakeLLM())

    # 让异步线程同步执行，便于断言
    class SyncThread:
        def __init__(self, target=None, args=(), daemon=None, **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr("src.memory.threading.Thread", SyncThread)

    turn = type("T", (), {"role": "user", "name": "小明", "content": "我喜欢白米饭"})()
    manager.maybe_update("r1", "群A", [turn])   # 第 1 条，不更新
    assert manager.inject("r1") == ""
    manager.maybe_update("r1", "群A", [turn])   # 第 2 条，触发
    text = manager.inject("r1")
    assert "长期记忆" in text
    assert "白米饭" in text


def test_engine_injects_memory(tmp_path):
    from src.config import AppConfig
    from src.context import ContextStore
    from src.engine import ReplyEngine
    from src.models import IncomingMessage

    cfg = AppConfig.from_dict(
        {
            "deepseek": {"api_key": "sk-test"},
            "context": {"persist_dir": str(tmp_path / "rooms")},
            "wechat": {"adapter": "mock"},
            "memory": {"enabled": True, "every_n_messages": 100},
        }
    )
    memory = MemoryManager(cfg.memory, FakeLLM())
    memory.store.save(
        {"room_id": "r1", "room_name": "群A", "facts": ["小明喜欢白米饭"], "updated_at": 1.0}
    )
    captured: list[str] = []

    class LLM:
        last_usage: dict = {}

        def chat(self, messages, **kwargs):
            captured.append(messages[0]["content"])
            return "嗯"

    engine = ReplyEngine(cfg, LLM(), ContextStore(str(tmp_path / "rooms")), memory=memory)
    engine.generate([IncomingMessage(room_id="r1", sender_id="u", sender_name="小明", text="在吗")])
    assert "长期记忆" in captured[0]
    assert "白米饭" in captured[0]


def test_engine_generate_proactive(tmp_path):
    from src.config import AppConfig
    from src.context import ContextStore
    from src.engine import ReplyEngine

    cfg = AppConfig.from_dict(
        {
            "deepseek": {"api_key": "sk-test"},
            "context": {"persist_dir": str(tmp_path / "rooms")},
            "wechat": {"adapter": "mock"},
        }
    )
    llm = FakeLLM("在忙吗，突然想起你")
    engine = ReplyEngine(cfg, llm, ContextStore(str(tmp_path / "rooms")))
    text = engine.generate_proactive("r1", "群A")
    assert text == "在忙吗，突然想起你"
    ctx = engine.store.get("r1")
    assert ctx.turns and ctx.turns[-1].role == "assistant"
