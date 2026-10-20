"""本地网页测试台：不需要微信、不用扫码，在浏览器里演示「我 + 朋友 + DeepSeek」三人群聊。

由 `python -m src.main web --port 8000` 调用 `create_app(cfg, engine)` 启动。

设计说明：
  * 网页里的消息只存在进程内存里（刷新页面不丢，重启服务重置）；
  * 聊天上下文仍由核心的 `ContextStore` 落盘，和真实微信模式共用同一套记忆；
  * 为了像微信一样"发一条、回一条"，这里直接同步调用引擎，不经过 debounce。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from functools import wraps
from typing import Any

from flask import Flask, jsonify, render_template, request

from src.config import AppConfig
from src.engine import ReplyEngine
from src.models import IncomingMessage

log = logging.getLogger("dsbot.web")

ROOM_ID = "web-demo@chatroom"
ROOM_NAME = "我、朋友和 DeepSeek"

SPEAKER_ME = "我"
SPEAKER_FRIEND = "朋友"
SPEAKERS = (SPEAKER_ME, SPEAKER_FRIEND)

#: 网页里给每个发言人固定一个假 wxid（真实模式下这里是微信 wxid）
_SENDER_IDS = {SPEAKER_ME: "web-me", SPEAKER_FRIEND: "web-friend"}
#: 网页里代表 DeepSeek 的假 wxid，用于 mention 触发规则
BOT_SENDER_ID = "web-deepseek"

MAX_INPUT_CHARS = 2000


def _clock(ts: float) -> str:
    """把时间戳格式化成 HH:MM（本地时区）。"""
    return time.strftime("%H:%M", time.localtime(ts))


def _mask_key(key: str) -> str:
    """脱敏展示 API Key：只露头尾，永不返回完整 Key。"""
    if not key:
        return ""
    if len(key) < 12:
        return "已配置"
    return f"{key[:4]}***{key[-4:]}"


class DemoState:
    """网页模拟群的内存状态。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.messages: list[dict[str, Any]] = []
        self.seq = 0
        self.started_at = time.time()
        self.stats = {"sent": 0, "replied": 0, "dropped": 0, "errors": 0}


def create_app(cfg: AppConfig, engine: ReplyEngine) -> Flask:
    """构建 Flask 应用。`cfg` 用于展示配置摘要，`engine` 负责真正的回复逻辑。"""
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # 开发时改了 css/js 刷新即生效
    app.json.ensure_ascii = False  # 让中文原样返回，方便调试
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    state = DemoState()
    engine_lock = threading.RLock()

    def serialized(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with engine_lock:
                return fn(*args, **kwargs)
        return wrapped
    bot_name = (cfg.persona.bot_name or "DeepSeek").strip() or "DeepSeek"
    app.extensions["demo_state"] = state
    app.extensions["reply_engine"] = engine
    app.extensions["demo_cfg"] = cfg

    mention_re = re.compile(r"@" + re.escape(bot_name), re.IGNORECASE)

    # ---------------------------------------------------------------- 内部工具
    def room_payload() -> dict[str, Any]:
        return {
            "id": ROOM_ID,
            "name": ROOM_NAME,
            "members": [SPEAKER_ME, SPEAKER_FRIEND, bot_name],
        }

    def snapshot() -> list[dict[str, Any]]:
        with state.lock:
            return [dict(m) for m in state.messages]

    def status_payload() -> dict[str, Any]:
        key = cfg.deepseek.resolve_api_key()
        with state.lock:
            stats = dict(state.stats)
            stats["messages"] = len(state.messages)
        return {
            "room": room_payload(),
            "bot": {
                "name": bot_name,
                "model": cfg.deepseek.model,
                "base_url": cfg.deepseek.base_url,
            },
            "trigger": {
                "mode": cfg.chat.trigger,
                "label": cfg.trigger_label,
                "prefix": cfg.chat.prefix,
            },
            "api_key_configured": bool(key),
            "api_key_hint": _mask_key(key),
            "rules": {
                "only_groups": cfg.chat.only_groups,
                "reply_with_at": cfg.chat.reply_with_at,
                "allowed_groups": list(cfg.wechat.allowed_groups),
                "ignore_senders": list(cfg.wechat.ignore_senders),
            },
            "context": {
                "window_messages": cfg.context.window_messages,
                "max_context_chars": cfg.context.max_context_chars,
                "persist_dir": cfg.context.persist_dir,
            },
            "limits": {
                "max_message_chars": cfg.chat.max_message_chars,
                "max_reply_chunks": cfg.chat.max_reply_chunks,
                "max_input_chars": MAX_INPUT_CHARS,
            },
            "demo": {"offline": True, "debounce": False},
            "stats": stats,
            "uptime_seconds": round(time.time() - state.started_at, 1),
            "usage_today": engine.llm.meter.snapshot() if hasattr(engine.llm, "meter") else {},
        }

    # ---------------------------------------------------------------- 页面与接口
    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/api/status")
    def api_status():
        return jsonify(status_payload())

    @app.get("/api/messages")
    def api_messages():
        messages = snapshot()
        return jsonify({"messages": messages, "count": len(messages), "room": room_payload()})

    @app.post("/api/reset")
    @serialized
    def api_reset():
        try:
            engine.reset_room(ROOM_ID)
        except Exception as exc:  # noqa: BLE001
            log.warning("清空网页群上下文失败：%s", exc)
        with state.lock:
            state.messages.clear()
            state.seq = 0
            state.stats = {"sent": 0, "replied": 0, "dropped": 0, "errors": 0}
        log.info("网页测试台已重置群 %s", ROOM_ID)
        return jsonify({"ok": True, "messages": [], "room": room_payload()})

    @app.post("/api/send")
    @serialized
    def api_send():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": '请求体必须是 JSON，例如 {"speaker": "我", "text": "你好"}'}), 400

        text = str(data.get("text") or "").strip()
        speaker = str(data.get("speaker") or SPEAKER_ME).strip()
        if not text:
            return jsonify({"error": "消息不能为空"}), 400
        if len(text) > MAX_INPUT_CHARS:
            return jsonify({"error": f"消息太长了，最多 {MAX_INPUT_CHARS} 个字符"}), 400
        if speaker not in SPEAKERS:
            return jsonify({"error": "speaker 只能是「我」或「朋友」"}), 400

        ts = time.time()
        with state.lock:
            state.seq += 1
            msg_id = state.seq
            state.messages.append(
                {
                    "id": msg_id,
                    "role": "user",
                    "speaker": speaker,
                    "text": text,
                    "time": _clock(ts),
                    "ts": round(ts, 3),
                    "failed": False,
                }
            )
            state.stats["sent"] += 1

        # 像微信适配器一样，把网页输入包装成一条标准的 IncomingMessage
        msg = IncomingMessage(
            room_id=ROOM_ID,
            sender_id=_SENDER_IDS[speaker],
            sender_name=speaker,
            text=text,
            room_name=ROOM_NAME,
            msg_id=f"web-{msg_id}",
            ts=ts,
            is_group=True,
            at_wxids=[BOT_SENDER_ID] if mention_re.search(text) else [],
        )

        try:
            with engine_lock:
                decision = engine.should_reply(msg)
                if not decision:
                    with state.lock:
                        state.stats["dropped"] += 1
                    log.info("网页消息不回复：%s | %s", decision.reason, text[:60])
                    return (
                        jsonify(
                            {
                                "replied": False,
                                "replies": [],
                                "reason": decision.reason,
                                "trigger_label": cfg.trigger_label,
                                "messages": snapshot(),
                            }
                        )
                    )

                replies = [c.strip() for c in (engine.generate([msg]) or []) if c and c.strip()]
                if replies:
                    now = time.time()
                    with state.lock:
                        for i, chunk in enumerate(replies):
                            state.seq += 1
                            state.messages.append(
                                {
                                    "id": state.seq,
                                    "role": "bot",
                                    "speaker": bot_name,
                                    "text": chunk,
                                    "time": _clock(now),
                                    "ts": round(now, 3),
                                    "at": f"@{speaker}" if cfg.chat.reply_with_at and i == 0 else None,
                                }
                            )
                        state.stats["replied"] += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("网页测试台生成回复失败：%s", exc)
            with state.lock:
                state.stats["errors"] += 1
                for m in state.messages:
                    if m["id"] == msg_id:
                        m["failed"] = True
            return (
                jsonify(
                    {
                        "error": "DeepSeek 调用失败，请查看本地日志并检查 Key、网络及模型配置。",
                        "replied": False,
                        "replies": [],
                        "messages": snapshot(),
                    }
                ),
                502,
            )

        if not replies:
            return jsonify(
                {
                    "replied": False,
                    "replies": [],
                    "reason": "模型没有返回内容",
                    "trigger_label": cfg.trigger_label,
                    "messages": snapshot(),
                }
            )

        return jsonify(
            {
                "replied": True,
                "replies": replies,
                "reason": decision.reason,
                "trigger_label": cfg.trigger_label,
                "messages": snapshot(),
            }
        )

    @app.after_request
    def _no_store_for_api(response):
        with state.lock:
            if len(state.messages) > 1000:
                del state.messages[:-1000]
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    from .manage import register_management
    register_management(app, cfg)
    return app
