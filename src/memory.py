"""长期记忆：给每个群/私聊维护一份「人物档案」。

思路
====
* 平时照常聊天，每收到 `every_n_messages` 条消息，后台异步让模型把最近对话里
  **稳定的事实**（喜好、生日、最近在忙什么、称呼习惯……）摘出来，合并进档案；
* 下次生成回复时，把档案注入 system prompt，模型就能"记得"几天前的事；
* 档案按群/私聊隔离，存在 `data/memory/<hash>.json`，重启不丢。

只存事实，不存原文；档案最多 `max_facts` 条、注入不超过 `max_inject_chars` 字。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .config import MemoryConfig
from .runtime import atomic_json, file_lock

log = logging.getLogger("dsbot.memory")

EXTRACT_SYSTEM = (
    "你是记忆整理助手。你会收到一份「已有档案」和一段最近的群聊/私聊记录，"
    "请把值得长期记住的**稳定事实**更新进档案。只输出 JSON 数组，不要解释。"
)

EXTRACT_USER = """已有档案（可能为空）：
{existing}

最近对话：
{dialogue}

要求：
1. 只记录稳定、以后还用得上的事实：对方的称呼/称呼偏好、生日、喜好（吃/喝/游戏/动漫）、
   讨厌的东西、正在忙的事（考试/项目/工作）、重要日期、口头习惯、人际关系。
2. 忽略一次性的闲聊、情绪、天气、临时安排。
3. 用第三人称短句，每条不超过 40 字，例如「小明不介意被叫东家，喜欢白米饭」。
4. 与已有事实重复或冲突时，用新的替换旧的。
5. 最多保留 {max_facts} 条，按重要程度排序。
只输出 JSON 字符串数组，例如 ["事实1", "事实2"]。"""


def _room_key(room_id: str) -> str:
    return hashlib.sha1(room_id.encode("utf-8")).hexdigest()[:16]


class ProfileStore:
    """人物档案的持久化（每个群/私聊一个 JSON）。"""

    def __init__(self, root: str | Path) -> None:
        self.dir = Path(root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, room_id: str) -> Path:
        return self.dir / f"{_room_key(room_id)}.json"

    def load(self, room_id: str, room_name: str = "") -> dict[str, Any]:
        with self._lock:
            path = self._path(room_id)
            record: dict[str, Any] = {
                "room_id": room_id,
                "room_name": room_name,
                "facts": [],
                "updated_at": 0.0,
            }
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        record.update(data)
                        record["room_id"] = room_id
                        if room_name:
                            record["room_name"] = room_name
                except Exception as exc:  # noqa: BLE001
                    log.warning("读取档案失败 %s：%s（忽略旧文件）", path, exc)
            return record

    def save(self, record: dict[str, Any]) -> None:
        with self._lock:
            path = self._path(str(record.get("room_id") or ""))
            atomic_json(path, record)

    def edit(self, room_id: str, facts: list[str], room_name: str = "", *, expected_revision: int | None = None) -> dict:
        with self._lock, file_lock(self._path(room_id).with_suffix(".lock")):
            record = self.load(room_id, room_name)
            if expected_revision is not None and int(record.get("revision", 0)) != expected_revision:
                raise ValueError("这份记忆已被后台或其他窗口更新，请重新读取后再保存。")
            record.update(facts=list(dict.fromkeys(f.strip()[:80] for f in facts if f.strip())),
                          updated_at=time.time(), revision=int(record.get("revision", 0)) + 1,
                          evidence={f: {"source": "manual", "updated_at": time.time()} for f in facts})
            self.save(record)
            return record

    def list_profiles(self) -> list[dict]:
        records = []
        for path in self.dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(record, dict):
                    records.append(record)
            except (ValueError, OSError):
                continue
        return records


class MemoryExtractor:
    def __init__(self, llm: Any, cfg: MemoryConfig) -> None:
        self.llm = llm
        self.cfg = cfg

    def extract(self, existing: list[str], dialogue: str) -> list[str]:
        prompt = EXTRACT_USER.format(
            existing="\n".join(f"- {f}" for f in existing) or "（空）",
            dialogue=dialogue[-6000:],
            max_facts=self.cfg.max_facts,
        )
        messages = [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        try:
            if self.cfg.model:
                raw = self.llm.chat(messages, model=self.cfg.model)
            else:
                raw = self.llm.chat(messages)
        except Exception as exc:  # noqa: BLE001
            log.warning("档案提取失败：%s", exc)
            return list(existing)
        return self._parse(raw, existing)

    def _parse(self, raw: str, existing: list[str]) -> list[str]:
        text = (raw or "").strip()
        facts: list[str] = []
        authoritative = False
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                if isinstance(data, list):
                    if not all(isinstance(x, str) for x in data):
                        return list(existing)
                    authoritative = True
                    facts = [x.strip() for x in data if x.strip()]
            except Exception:  # noqa: BLE001
                facts = []
        if not facts and not authoritative:  # 兜底：按行/分号拆
            for line in re.split(r"[\n;；]", text):
                if not re.match(r"^\s*(?:[-*]|\d+[.、])", line):
                    continue
                line = re.sub(r"^\s*[-*\d.、]+\s*", "", line).strip().strip('"')
                if 2 <= len(line) <= 80:
                    facts.append(line)
        cleaned: list[str] = []
        seen: set[str] = set()
        # Valid JSON is the complete replacement profile. Re-appending old facts
        # would resurrect facts deliberately removed or corrected by the model.
        for fact in facts if authoritative else facts + list(existing):
            fact = " ".join(str(fact).split())[:80]
            if not fact or fact in seen:
                continue
            seen.add(fact)
            cleaned.append(fact)
        return cleaned[: self.cfg.max_facts]


class MemoryManager:
    """计数 + 异步更新 + 注入文本。"""

    def __init__(self, cfg: MemoryConfig, llm: Any) -> None:
        self.cfg = cfg
        self.store = ProfileStore(cfg.dir)
        self.extractor = MemoryExtractor(llm, cfg)
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._updating: set[str] = set()

    def inject(self, room_id: str) -> str:
        if not self.cfg.enabled:
            return ""
        record = self.store.load(room_id)
        facts = [str(x) for x in (record.get("facts") or []) if str(x).strip()]
        if not facts:
            return ""
        body = "\n".join(f"- {f}" for f in facts[: self.cfg.max_facts])
        block = (
            "【长期记忆（之前聊到过，可以自然引用，不要逐条背出来）】\n"
            f"{body}"
        )
        return block[: self.cfg.max_inject_chars]

    def maybe_update(self, room_id: str, room_name: str, turns: list[Any], count: int = 1) -> None:
        """每 N 条消息触发一次异步提取（不阻塞回复）。"""
        if not self.cfg.enabled:
            return
        with self._lock:
            total = self._counts.get(room_id, 0) + count
            self._counts[room_id] = total
            if total < self.cfg.every_n_messages or room_id in self._updating:
                return
            self._counts[room_id] = total % self.cfg.every_n_messages
            self._updating.add(room_id)
        snapshot = list(turns)[-80:]
        threading.Thread(
            target=self._update, args=(room_id, room_name, snapshot), daemon=True
        ).start()

    def _update(self, room_id: str, room_name: str, turns: list[Any]) -> None:
        try:
            dialogue = "\n".join(
                f"{getattr(t, 'name', '') or '我'}：{getattr(t, 'content', '')}"
                for t in turns
                if getattr(t, "role", "") == "user"
            )
            if not dialogue.strip():
                return
            record = self.store.load(room_id, room_name)
            revision = int(record.get("revision", 0))
            existing = [str(x) for x in (record.get("facts") or [])]
            facts = self.extractor.extract(existing, dialogue)
            record["facts"] = facts[: self.cfg.max_facts]
            record["room_name"] = room_name
            record["updated_at"] = time.time()
            record["revision"] = revision + 1
            evidence = record.get("evidence") or {}
            record["evidence"] = {fact: evidence.get(fact) or {
                "source": "conversation", "updated_at": record["updated_at"],
                "message_timestamps": [getattr(t, "ts", 0) for t in turns if getattr(t, "role", "") == "user"][-10:],
            } for fact in facts}
            with self.store._lock, file_lock(self.store._path(room_id).with_suffix(".lock")):
                current = self.store.load(room_id, room_name)
                if int(current.get("revision", 0)) != revision:
                    log.info("档案已被人工修订，跳过旧提取结果")
                    return
                self.store.save(record)
            log.info("长期记忆已更新（%s）：%d 条事实", room_name or room_id, len(record["facts"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("更新长期记忆失败：%s", exc)
        finally:
            with self._lock:
                self._updating.discard(room_id)
