"""联网搜索：调用 DeepSeek 官方 Search API（/v1/tools/search）。

设计
====
* 由模型通过 Tool Calls 自己决定什么时候搜（闲聊不搜，问新闻/实时信息才搜）；
* 结果压缩成一段带来源的文本喂回模型；
* 同一 query 5 分钟内缓存，避免重复计费（Search 按请求次数计费，5 元/1000 次）；
* 自带每分钟限流与失败重试（429/5xx 退避）。
"""
from __future__ import annotations

import logging
import time
import json
import threading
from collections import deque
from typing import Any

import requests

from .config import DeepSeekConfig, SearchConfig

log = logging.getLogger("dsbot.search")

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "联网搜索最新的网页信息。当用户问新闻、实时数据（天气/股价/比分/价格）、"
            "刚发生的事、或者你不确定的事实时调用。闲聊、常识、不需要时效性的问题不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，简短具体"},
            },
            "required": ["query"],
        },
    },
}


class SearchClient:
    def __init__(self, ds: DeepSeekConfig, cfg: SearchConfig) -> None:
        self.ds = ds
        self.cfg = cfg
        self.endpoint = ds.base_url.rstrip("/") + "/v1/tools/search"
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._calls: deque[float] = deque()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def search(self, query: str, ori_question: str = "", count: int | None = None) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        now = time.time()
        cached = self._cache.get(query)
        if cached and now - cached[0] < self.cfg.cache_ttl:
            return cached[1]
        if not self._rate_ok():
            log.warning("搜索频率超限（%d 次/分钟），本次跳过：%s", self.cfg.max_per_minute, query)
            return []

        payload: dict[str, Any] = {
            "query": query,
            "count": int(count or self.cfg.count),
            "need_content": bool(self.cfg.need_content),
        }
        if ori_question:
            payload["ori_question"] = ori_question[:128]
        headers = {
            "Authorization": f"Bearer {self.ds.resolve_api_key()}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = requests.post(self.endpoint, headers=headers, json=payload, timeout=self.cfg.timeout)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                results = (resp.json() or {}).get("results") or []
                self._cache[query] = (time.time(), results)
                log.info("联网搜索「%s」→ %d 条结果", query, len(results))
                return results
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        log.warning("搜索失败「%s」：%s", query, last_error)
        return []

    def search_and_format(self, query: str, ori_question: str = "") -> str:
        """搜索并整理成给模型读的参考材料。"""
        results = self.search(query, ori_question=ori_question)
        if not results:
            return "（没有搜到相关结果）"
        lines: list[str] = []
        used = 0
        for item in results[: self.cfg.max_results_for_model]:
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            summary = (item.get("summary") or item.get("snippet") or item.get("content") or "").strip()
            summary = " ".join(summary.split())
            if not (title or summary):
                continue
            date = (item.get("date") or "")[:10]
            text = f"- {title}"
            if date:
                text += f"（{date}）"
            if url:
                text += f"\n  链接：{url}"
            if summary:
                text += f"\n  摘要：{summary[:300]}"
            lines.append(text)
            used += 1
            if sum(len(x) for x in lines) > self.cfg.max_chars:
                break
        body = "\n".join(lines) if lines else "（没有搜到相关结果）"
        return f"以下是联网搜索到的资料（请据此回答，不要照抄，也不要罗列链接）：\n{body}"

    # ------------------------------------------------------------------
    def _rate_ok(self) -> bool:
        with self._lock:
            return self._rate_ok_locked()

    def _rate_ok_locked(self) -> bool:
        limit = self.cfg.max_per_minute
        if limit <= 0:
            return True
        now = time.time()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= limit:
            return False
        self._calls.append(now)
        return True


def parse_tool_query(arguments: str) -> str:
    """从模型返回的 tool_calls arguments 里取 query。"""
    try:
        data = json.loads(arguments or "{}")
        return str(data.get("query") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
