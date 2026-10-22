"""表情包库：记录对方常发/群里常见的表情包，主动发时复用同款。

微信没有"上传表情"的开放接口，所以每次识别动画表情时，顺手把它下载下来的 GIF
登记到对应群/私聊的库里（按 md5 去重、按出现次数排序）。主动发表情包时优先挑
这个聊天对象最常用的那几张——风格自然就是对方的风格。

发送形式：微信只能以 GIF 图片发送（SendFiles），收件方看到的是动图而不是"收藏
表情"，这是第三方库的能力边界。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("dsbot.stickers")


def _key(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]


def _is_image(path: Path) -> bool:
    """按文件头判断能不能当图片发（旧版本可能把加密的微信缓存登记进来）。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    return head[:3] == b"\xff\xd8\xff" or head[:4] in (b"\x89PNG", b"GIF8", b"RIFF")


class StickerStore:
    def __init__(self, root: str | Path, max_items: int = 80) -> None:
        self.dir = Path(root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_items = max_items
        self._lock = threading.Lock()

    def _path(self, chat: str) -> Path:
        return self.dir / f"{_key(chat)}.json"

    def load(self, chat: str) -> dict[str, Any]:
        with self._lock:
            path = self._path(chat)
            data: dict[str, Any] = {"chat": chat, "items": [], "updated_at": 0.0}
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data.update(loaded)
                        data["chat"] = chat
                except Exception as exc:  # noqa: BLE001
                    log.warning("读取表情库失败 %s：%s", path, exc)
            return data

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            path = self._path(str(data.get("chat") or ""))
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)

    def record(self, chat: str, md5: str, path: str | Path, sender: str = "") -> None:
        chat = (chat or "").strip()
        if not chat or not md5:
            return
        path = str(path)
        if not Path(path).exists():
            return
        data = self.load(chat)
        items: list[dict] = data.setdefault("items", [])
        for item in items:
            if item.get("md5") == md5:
                item["count"] = int(item.get("count") or 0) + 1
                item["last_ts"] = time.time()
                item["path"] = path
                if sender:
                    item["sender"] = sender
                data["updated_at"] = time.time()
                self.save(data)
                return
        items.append(
            {"md5": md5, "path": path, "sender": sender, "count": 1, "last_ts": time.time()}
        )
        items.sort(key=lambda i: int(i.get("count") or 0), reverse=True)
        data["items"] = items[: self.max_items]
        data["updated_at"] = time.time()
        self.save(data)
        log.info("表情库 +1（%s，共 %d 张）：%s", chat, len(data["items"]), Path(path).name)

    def pick(self, chat: str, rng: random.Random | None = None) -> str | None:
        """按出现次数加权挑一张对方常用的表情；文件不存在或不是图片会自动清理。"""
        data = self.load(chat)
        items = [
            i
            for i in data.get("items") or []
            if i.get("path") and Path(str(i["path"])).exists() and _is_image(Path(str(i["path"])))
        ]
        if len(items) != len(data.get("items") or []):
            data["items"] = items
            self.save(data)
        if not items:
            return None
        rng = rng or random
        weights = [max(1, int(i.get("count") or 1)) for i in items]
        chosen = rng.choices(items, weights=weights, k=1)[0]
        return str(chosen["path"])

    def count(self, chat: str) -> int:
        return len(self.load(chat).get("items") or [])
