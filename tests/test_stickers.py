"""表情库测试。"""
from __future__ import annotations

import random

from src.stickers import StickerStore


def _gif(tmp_path, name="a.gif"):
    p = tmp_path / name
    p.write_bytes(b"GIF89a" + b"0" * 20)
    return p


def test_record_dedup_and_count(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    gif = _gif(tmp_path)
    store.record("群A", "md5a", gif, sender="小明")
    store.record("群A", "md5a", gif, sender="小明")
    data = store.load("群A")
    assert len(data["items"]) == 1
    assert data["items"][0]["count"] == 2
    assert store.count("群A") == 1


def test_pick_returns_existing_file(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    gif = _gif(tmp_path)
    store.record("群A", "md5a", gif)
    picked = store.pick("群A", rng=random.Random(1))
    assert picked == str(gif)


def test_pick_skips_missing_files(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    gif = _gif(tmp_path)
    store.record("群A", "md5a", gif)
    gif.unlink()
    assert store.pick("群A") is None
    assert store.load("群A")["items"] == []


def test_isolated_per_chat(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.record("群A", "md5a", _gif(tmp_path, "a.gif"))
    store.record("私聊B", "md5b", _gif(tmp_path, "b.gif"))
    assert store.count("群A") == 1
    assert store.count("私聊B") == 1
    assert store.count("群C") == 0
