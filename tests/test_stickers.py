"""表情库测试。"""
from __future__ import annotations

import random
from pathlib import Path

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


def test_keyword_pool_match_and_priority(tmp_path):
    from src.stickers import KeywordPool

    (tmp_path / "笑死,哈哈.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 8)
    (tmp_path / "晚安.gif").write_bytes(b"GIF89a" + b"0" * 8)
    (tmp_path / "readme.txt").write_bytes(b"not an image")
    pool = KeywordPool(tmp_path)
    assert pool.count() == 2  # 非图片文件不计入
    assert Path(pool.match("这也太好笑了，哈哈哈")).name == "笑死,哈哈.jpg"
    assert Path(pool.match("我睡了，大家晚安")).name == "晚安.gif"
    assert pool.match("没有任何触发词") is None
    # 多词同时命中时按文件名排序取优先级高的
    assert Path(pool.match("晚安，笑死")).name == "晚安.gif"
    # 英文触发词不区分大小写
    (tmp_path / "OK.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 8)
    assert Path(KeywordPool(tmp_path).match("oK 啊")).name == "OK.jpg"


def test_keyword_pool_empty_dir(tmp_path):
    from src.stickers import KeywordPool

    pool = KeywordPool(tmp_path / "not-exist")
    assert pool.count() == 0
    assert pool.match("笑死") is None


def test_keyword_pool_match_skips_excluded(tmp_path):
    from src.stickers import KeywordPool

    (tmp_path / "晚安.gif").write_bytes(b"GIF89a" + b"0" * 8)
    (tmp_path / "睡觉,困.gif").write_bytes(b"GIF89a" + b"0" * 8)
    pool = KeywordPool(tmp_path)
    first = pool.match("困了，晚安")
    assert Path(first).name == "晚安.gif"  # 文件名排序优先
    second = pool.match("困了，晚安", exclude={first})
    assert Path(second).name == "睡觉,困.gif"
    assert pool.match("困了，晚安", exclude={first, second}) is None
