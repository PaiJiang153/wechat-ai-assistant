"""wechatauto 适配器单元测试（用假模块，不连真实微信）。"""
from __future__ import annotations

import sys
import threading
import time
import types

from src.wechatauto_adapter import WechatautoAdapter

GROUP_ID = "123@chatroom"
GROUP_NAME = "测试群"


class FakeDB:
    groups = [{"username": GROUP_ID, "name": GROUP_NAME}]
    members = [
        {"username": "wxid_me", "nick_name": "小号"},
        {"username": "wxid_friend", "nick_name": "朋友"},
    ]
    messages: list[dict] = []

    def get_self_info(self):
        return {"username": "wxid_me", "nick_name": "小号"}

    def get_groups(self):
        return self.groups

    def get_group_members(self, gid):
        return self.members

    def get_messages(self, gid, limit=20, offset=0):
        return list(self.messages)[-limit:]


class FakeWx:
    def __init__(self):
        self.sent = []

    def SendMsg(self, msg, who=None, at=None, **kwargs):
        self.sent.append({"msg": msg, "who": who, "at": at})
        return {"status": "成功"}


class FakeVision:
    def __init__(self, text: str = "一只橘猫趴在键盘上睡觉") -> None:
        self.text = text
        self.calls: list[tuple[str, str]] = []

    def describe(self, path, kind: str = "image") -> str:
        self.calls.append((str(path), kind))
        return self.text


class FakeDownloader:
    def __init__(self, path) -> None:
        self.path = path

    def download_image(self, gid, local_id, save_dir=None):
        return str(self.path)


class FakeASR:
    def __init__(self, text: str = "今天好累") -> None:
        self.text = text
        self.calls: list[str] = []

    def transcribe_silk(self, path) -> str:
        self.calls.append(str(path))
        return self.text


class FakeMediaDownloader(FakeDownloader):
    def __init__(self, voice=None, media=None) -> None:
        super().__init__(media)
        self.voice = voice
        self.media = media

    def download_voice(self, gid, local_id, save_dir=None):
        return str(self.voice) if self.voice else None

    def download_media(self, gid, local_id, save_dir=None):
        return str(self.media) if self.media else None


def install_fake(monkeypatch, messages):
    FakeDB.messages = list(messages)
    mod = types.ModuleType("wechatauto")
    mod.WeChatDB = FakeDB
    mod.WeChat = FakeWx
    monkeypatch.setitem(sys.modules, "wechatauto", mod)


def make_adapter(**kwargs):
    return WechatautoAdapter(group_name=GROUP_NAME, poll_seconds=0.05, **kwargs)


def test_resolves_group_and_skips_history(monkeypatch):
    install_fake(monkeypatch, [{"local_id": 10, "type": "文本", "content": "旧消息", "sender_username": "wxid_friend"}])
    adapter = make_adapter(skip_history=True)
    adapter._db = None
    got = []
    adapter.on_message(got.append)
    t = threading.Thread(target=adapter.start, daemon=True)
    t.start()
    time.sleep(0.3)
    FakeDB.messages.append({"local_id": 11, "type": "文本", "content": "wxid_friend:\n新消息", "sender_username": "wxid_friend", "create_time": 1.0})
    time.sleep(0.3)
    adapter.stop()
    t.join(timeout=2)
    assert [m.text for m in got] == ["新消息"]
    assert got[0].sender_name == "朋友"


def test_ignores_self_message(monkeypatch):
    install_fake(monkeypatch, [{"local_id": 1, "type": "文本", "content": "x", "sender_username": "wxid_me"}])
    adapter = make_adapter(skip_history=False)
    got = []
    adapter.on_message(got.append)
    t = threading.Thread(target=adapter.start, daemon=True)
    t.start()
    time.sleep(0.2)
    adapter.stop()
    t.join(timeout=2)
    assert got == []


def test_non_text_messages_become_placeholders(monkeypatch):
    install_fake(monkeypatch, [{"local_id": 1, "type": "图片", "content": "whatever", "sender_username": "wxid_friend"}])
    adapter = make_adapter(skip_history=False)
    got = []
    adapter.on_message(got.append)
    t = threading.Thread(target=adapter.start, daemon=True)
    t.start()
    time.sleep(0.2)
    adapter.stop()
    t.join(timeout=2)
    assert [m.text for m in got] == ["[图片]"]


def test_send_text_queues_then_sends_in_worker(monkeypatch):
    """send_text 只入队，_drain_outbox 在同线程里真正发送并校验。"""
    install_fake(monkeypatch, [])
    adapter = make_adapter()
    adapter._wx = FakeWx()
    adapter._db = FakeDB()
    adapter._group_id = GROUP_ID
    adapter._self_wxid = "wxid_me"
    FakeDB.messages = [{"local_id": 1, "type": "文本", "content": "你好呀", "sender_username": "wxid_me"}]

    adapter.send_text(GROUP_ID, "你好呀", at_wxid="朋友")
    assert adapter._wx.sent == []          # 还没发，只是入队
    adapter._drain_outbox()
    assert adapter._wx.sent == [{"msg": "你好呀", "who": GROUP_NAME, "at": None}]


def test_refuses_wrong_account(monkeypatch):
    """配置要求小号，但当前登录的是主号时必须拒绝启动。"""
    install_fake(monkeypatch, [])
    adapter = WechatautoAdapter(
        group_name=GROUP_NAME, poll_seconds=0.05, expected_self_name="机器人小号"
    )
    try:
        adapter.start()
    except RuntimeError as exc:
        assert "机器人小号" in str(exc)
        assert "拒绝启动" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("账号不匹配时必须拒绝启动")


def test_image_message_is_described_by_vision(tmp_path, monkeypatch):
    install_fake(monkeypatch, [])
    img = tmp_path / "a.png"
    img.write_bytes(b"fake-image")
    vision = FakeVision()
    adapter = make_adapter(vision=vision, media_dir=str(tmp_path))
    adapter._downloader = FakeDownloader(img)
    adapter._group_id = GROUP_ID
    text = adapter._normalise_content({"local_id": 7, "type": "图片", "content": "<img/>"})
    assert text == "[图片] 一只橘猫趴在键盘上睡觉"
    assert vision.calls and vision.calls[0][1] == "image"


def test_sticker_uses_local_cache(tmp_path, monkeypatch):
    install_fake(monkeypatch, [])
    md5 = "ab" * 16
    account_dir = tmp_path / "acc"
    sticker = account_dir / "cache" / "2026-10" / "Emoticon" / md5[:2] / md5
    sticker.parent.mkdir(parents=True)
    sticker.write_bytes(b"gif-bytes")
    vision = FakeVision()
    adapter = make_adapter(vision=vision)
    adapter._db = type("DB", (), {"account_dir": str(account_dir)})()
    adapter._group_id = GROUP_ID
    raw = f'<msg><emoji md5="{md5}" len="100" /></msg>'
    text = adapter._normalise_content({"type": "动画表情", "content": raw})
    assert text == "[表情包] 一只橘猫趴在键盘上睡觉"
    assert vision.calls[0][1] == "sticker"


def test_sticker_downloads_from_cdn(tmp_path, monkeypatch):
    install_fake(monkeypatch, [])
    import src.wechatauto_adapter as mod

    def fake_download(url, dest, timeout=30.0):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"GIF89a-fake")
        return True

    monkeypatch.setattr(mod, "_download_file", fake_download)
    vision = FakeVision()
    adapter = make_adapter(vision=vision, media_dir=str(tmp_path))
    adapter._db = type("DB", (), {"account_dir": str(tmp_path / "acc")})()
    md5 = "cd" * 16
    raw = f'<msg><emoji md5="{md5}" cdnurl="http://example.com/x?m={md5}&amp;filekey=1" /></msg>'
    text = adapter._normalise_content({"type": "动画表情", "content": raw})
    assert text == "[表情包] 一只橘猫趴在键盘上睡觉"
    assert (tmp_path / f"emoji_{md5}.gif").exists()
    assert vision.calls[0][1] == "sticker"


def test_image_without_vision_keeps_placeholder():
    adapter = make_adapter(vision=None)
    assert adapter._normalise_content({"local_id": 1, "type": "图片"}) == "[图片]"


def test_voice_message_transcribed(tmp_path, monkeypatch):
    install_fake(monkeypatch, [])
    silk = tmp_path / "v.silk"
    silk.write_bytes(b"\x02#!SILK_V3")
    asr = FakeASR("今天好累")
    adapter = make_adapter(asr=asr)
    adapter._downloader = FakeMediaDownloader(voice=silk)
    adapter._group_id = GROUP_ID
    text = adapter._normalise_content({"type": "语音", "local_id": 5})
    assert text == "[语音] 今天好累"
    assert asr.calls


def test_file_message_summarized(tmp_path, monkeypatch):
    install_fake(monkeypatch, [])
    from src.config import FilesConfig
    from src.files import FileSummarizer

    f = tmp_path / "a.txt"
    f.write_text("会议纪要：下周三交方案", encoding="utf-8")
    adapter = make_adapter(
        file_summarizer=FileSummarizer(FilesConfig()), files_dir=str(tmp_path / "files")
    )
    adapter._downloader = FakeMediaDownloader(media=f)
    adapter._group_id = GROUP_ID
    raw = "<msg><appmsg><title>a.txt</title><type>6</type></appmsg></msg>"
    text = adapter._normalise_content({"type": "文件/链接/卡片", "local_id": 9, "content": raw})
    assert text.startswith("[文件] a.txt")
    assert "下周三交方案" in text


def test_link_and_pat_cards():
    adapter = make_adapter()
    adapter._group_id = GROUP_ID
    link = adapter._normalise_content(
        {
            "type": "文件/链接/卡片",
            "local_id": 1,
            "content": "<msg><appmsg><title>某个链接</title><type>5</type></appmsg></msg>",
        }
    )
    assert link == "[链接] 某个链接"
    pat = adapter._normalise_content(
        {
            "type": "文件/链接/卡片",
            "local_id": 2,
            "content": '<msg><appmsg><title>"A" 拍了拍 "B"</title><type>62</type></appmsg></msg>',
        }
    )
    assert pat.startswith("[拍一拍]")


def test_send_file_and_poke_queued():
    adapter = make_adapter()
    adapter.send_file("gid", r"C:\tmp\x.gif")
    adapter.poke("gid")
    room, kind, payload, at, receipt = adapter._outbox.get_nowait()
    assert (room, kind, payload) == ("gid", "file", r"C:\tmp\x.gif")
    room, kind, payload, at, receipt = adapter._outbox.get_nowait()
    assert (room, kind) == ("gid", "poke")


def test_unknown_group_raises(monkeypatch):
    install_fake(monkeypatch, [])
    adapter = WechatautoAdapter(group_name="不存在的群", poll_seconds=0.05)
    try:
        adapter.start()
    except RuntimeError as exc:
        assert "找不到" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应该抛出找不到群的异常")
