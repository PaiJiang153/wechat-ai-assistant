"""DeepSeek 客户端（OpenAI 兼容协议）。

DeepSeek 官方接口：base_url = https://api.deepseek.com
模型：deepseek-chat（通用）/ deepseek-reasoner（推理）
"""
from __future__ import annotations

import logging
import random
import time
import threading
from typing import Any, Sequence

from .config import DeepSeekConfig
from .runtime import UsageMeter

log = logging.getLogger("dsbot.llm")


class LLMError(RuntimeError):
    """调用大模型失败（已重试仍失败）。"""


class DeepSeekClient:
    def __init__(self, cfg: DeepSeekConfig) -> None:
        self.cfg = cfg
        self._local = threading.local()
        self.meter = UsageMeter()
        api_key = cfg.resolve_api_key()
        if not api_key:
            raise LLMError(
                "没有 DeepSeek API Key：请在 config.yaml 的 deepseek.api_key 填写，"
                "或设置环境变量 DEEPSEEK_API_KEY"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError("缺少 openai 依赖：pip install -r requirements.txt") from exc

        self._client = OpenAI(
            api_key=api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout,
            max_retries=0,  # 重试逻辑自己控制，便于日志和测试
        )
        self.last_usage: dict[str, Any] = {}

    @property
    def last_usage(self):
        return getattr(self._local, "usage", {})

    @last_usage.setter
    def last_usage(self, value):
        self._local.usage = value
        if value:
            self.meter.record(requests=1, **{k: int(v or 0) for k, v in value.items()})

    # ------------------------------------------------------------------
    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> str:
        """一次非流式对话，返回纯文本。失败抛 LLMError。"""
        import openai

        params: dict[str, Any] = {
            "model": model or self.cfg.model,
            "messages": list(messages),
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        # deepseek-reasoner 不支持 temperature，官方建议不传
        if str(params["model"]).endswith("reasoner"):
            params.pop("temperature", None)

        attempts = max(1, self.cfg.max_retries + 1)
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                resp = self._client.chat.completions.create(**params)
                text = (resp.choices[0].message.content or "").strip()
                if getattr(resp, "usage", None):
                    self.last_usage = {
                        "prompt_tokens": resp.usage.prompt_tokens,
                        "completion_tokens": resp.usage.completion_tokens,
                        "total_tokens": resp.usage.total_tokens,
                    }
                if not text:
                    # 推理型模型（如 deepseek-flash）可能把预算全花在推理上，
                    # 导致正文为空：把 max_tokens 翻倍再试一次。
                    cur = int(params.get("max_tokens", 0) or 0)
                    if cur and cur < 2400 and i < attempts - 1:
                        params["max_tokens"] = min(2400, cur * 2)
                        log.warning(
                            "模型返回空正文（推理可能占满了 max_tokens=%s），提高到 %s 重试",
                            cur, params["max_tokens"],
                        )
                        continue
                    raise LLMError("模型返回了空内容")
                return text
            except openai.AuthenticationError as exc:  # 不可重试
                raise LLMError(f"API Key 无效或已过期：{exc}") from exc
            except openai.BadRequestError as exc:  # 不可重试
                raise LLMError(f"请求参数错误：{exc}") from exc
            except Exception as exc:  # noqa: BLE001  网络/限流/5xx 等可重试
                last_exc = exc
                if i == attempts - 1:
                    break
                delay = min(20.0, (2 ** i) + random.random())
                log.warning("DeepSeek 调用失败（第 %d/%d 次），%.1fs 后重试：%s", i + 1, attempts, delay, exc)
                time.sleep(delay)
        raise LLMError(f"DeepSeek 调用失败：{last_exc}") from last_exc

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """返回原始 message 对象（可能带 tool_calls），供工具调用流程使用。"""
        import openai

        params: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": list(messages),
            "temperature": self.cfg.temperature if temperature is None else temperature,
            # 推理模型会花预算在思考上，工具调用轮次给足 token
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
        }
        if self.cfg.model.endswith("reasoner"):
            params.pop("temperature", None)
        if tools:
            params["tools"] = list(tools)

        attempts = max(1, self.cfg.max_retries + 1)
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                resp = self._client.chat.completions.create(**params)
                message = resp.choices[0].message
                if getattr(resp, "usage", None):
                    self.last_usage = {
                        "prompt_tokens": resp.usage.prompt_tokens,
                        "completion_tokens": resp.usage.completion_tokens,
                        "total_tokens": resp.usage.total_tokens,
                    }
                return message
            except openai.AuthenticationError as exc:
                raise LLMError(f"API Key 无效或已过期：{exc}") from exc
            except openai.BadRequestError as exc:
                raise LLMError(f"请求参数错误：{exc}") from exc
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if i == attempts - 1:
                    break
                delay = min(20.0, (2 ** i) + random.random())
                log.warning("模型调用失败（第 %d/%d 次），%.1fs 后重试：%s", i + 1, attempts, delay, exc)
                time.sleep(delay)
        raise LLMError(f"模型调用失败：{last_exc}") from last_exc

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


class UnconfiguredLLM:
    """没填 API Key 时的占位客户端。

    让网页测试台/演示界面在这种状态下也能启动（只是发消息时会报错提示），
    避免用户还没申请 Key 就完全看不到界面。
    """

    last_usage: dict[str, Any] = {}

    def __init__(self, reason: str = "") -> None:
        self.reason = reason or "未配置 DeepSeek API Key，请编辑 config.yaml 的 deepseek.api_key"

    def chat(self, messages, **kwargs) -> str:  # noqa: ANN001, ARG002
        raise LLMError(self.reason)

    def close(self) -> None:
        pass
