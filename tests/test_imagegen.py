"""生图功能测试。"""
from __future__ import annotations

import base64
import types

from src.config import ImageGenConfig
from src.imagegen import ImageGenerator

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 600
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 600


class FakeResponse:
    def __init__(self, content: bytes, ctype: str = "image/jpeg"):
        self.content = content
        self.headers = {"content-type": ctype}
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}


def test_pollinations_generates_and_caches(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return FakeResponse(JPEG)

    monkeypatch.setattr("src.imagegen.requests.get", fake_get)
    cfg = ImageGenConfig(out_dir=str(tmp_path), provider="pollinations")
    gen = ImageGenerator(cfg)
    path = gen.generate("一只吃白饭的蓝色鲸鱼")
    assert path and str(path).endswith(".jpg")
    assert (tmp_path / __import__("pathlib").Path(path).name).exists()
    # 第二次同样 prompt 走缓存，不再请求
    assert gen.generate("一只吃白饭的蓝色鲸鱼") == path
    assert len(calls) == 1


def test_openai_provider_b64(tmp_path, monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return FakeResponse(PNG)

    monkeypatch.setattr("src.imagegen.requests.post", fake_post)
    cfg = ImageGenConfig(
        out_dir=str(tmp_path), provider="openai", api_key="sk-test", base_url="https://example.com/v1"
    )
    gen = ImageGenerator(cfg)
    path = gen.generate("test prompt")
    assert path and str(path).endswith(".png")


def test_rate_limit_daily(tmp_path, monkeypatch):
    monkeypatch.setattr("src.imagegen.requests.get", lambda *a, **k: FakeResponse(JPEG))
    cfg = ImageGenConfig(out_dir=str(tmp_path), provider="pollinations", max_per_day=1, per_minute=100)
    gen = ImageGenerator(cfg)
    assert gen.generate("一张图") is not None
    assert gen.generate("另一张图") is None


def test_disabled_returns_none(tmp_path):
    cfg = ImageGenConfig(out_dir=str(tmp_path), enabled=False)
    gen = ImageGenerator(cfg)
    assert gen.generate("任何图") is None


def test_gemini_provider_inline_data(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, headers, json))
        return FakeResponse(
            b"",
            ctype="application/json",
        )

    # FakeResponse.json 返回 b64_json，gemini 用 inlineData，单独构造
    class GeminiResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}}
                            ]
                        }
                    }
                ]
            }

    monkeypatch.setattr("src.imagegen.requests.post", lambda *a, **k: GeminiResponse())
    cfg = ImageGenConfig(
        out_dir=str(tmp_path),
        provider="gemini",
        api_key="AIza-test",
        model="gemini-2.5-flash-image",
    )
    gen = ImageGenerator(cfg)
    path = gen.generate("一只吃白饭的蓝色鲸鱼")
    assert path and str(path).endswith(".png")


def test_zhipu_provider_url_response(tmp_path, monkeypatch):
    class PostResp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"url": "https://example.com/x.png"}]}

    class GetResp:
        content = PNG
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    monkeypatch.setattr("src.imagegen.requests.post", lambda *a, **k: PostResp())
    monkeypatch.setattr("src.imagegen.requests.get", lambda *a, **k: GetResp())
    cfg = ImageGenConfig(
        out_dir=str(tmp_path), provider="zhipu", api_key="zhipu-test", model="cogview-3-flash"
    )
    gen = ImageGenerator(cfg)
    path = gen.generate("一只吃白饭的蓝色鲸鱼")
    assert path and str(path).endswith(".png")


def test_engine_calls_image_tool(tmp_path):
    from src.config import AppConfig
    from src.context import ContextStore
    from src.engine import ReplyEngine
    from src.models import IncomingMessage

    class FakeImageGen:
        def __init__(self):
            self.prompts = []
            self.path = str(tmp_path / "generated.png")
            __import__("pathlib").Path(self.path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)

        def generate(self, prompt, size=None):
            self.prompts.append(prompt)
            return self.path

    class ToolLLM:
        last_usage: dict = {}

        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kwargs):
            return "不该走这里"

        def complete(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                call = types.SimpleNamespace(
                    id="call_1",
                    function=types.SimpleNamespace(
                        name="generate_image",
                        arguments='{"prompt": "一只吃白饭的蓝色鲸鱼"}',
                    ),
                )
                return types.SimpleNamespace(content="", tool_calls=[call])
            assert any(m.get("role") == "tool" for m in messages)
            return types.SimpleNamespace(content="画好了，本鱼还挺圆。", tool_calls=[])

    cfg = AppConfig.from_dict(
        {
            "deepseek": {"api_key": "sk-test"},
            "context": {"persist_dir": str(tmp_path / "rooms")},
            "wechat": {"adapter": "mock"},
            "imagegen": {"enabled": True},
        }
    )
    imagegen = FakeImageGen()
    engine = ReplyEngine(
        cfg, ToolLLM(), ContextStore(str(tmp_path / "rooms")), imagegen=imagegen
    )
    chunks = engine.generate(
        [IncomingMessage(room_id="r1", sender_id="u", sender_name="小明", text="画一只吃白饭的鲸鱼")]
    )
    assert chunks and "画好了" in chunks[0]
    assert imagegen.prompts == ["一只吃白饭的蓝色鲸鱼"]
    assert engine.pop_images() == [imagegen.path]
    assert engine.pop_images() == []
