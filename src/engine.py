"""回复引擎：决定「这条要不要回」，并调用 DeepSeek 生成回复、切分成适合微信的短句。"""
from __future__ import annotations

import json
import logging
import re
import time
import threading
from typing import Any, Protocol, Sequence

from .config import AppConfig
from .context import ContextStore
from .models import IncomingMessage

log = logging.getLogger("dsbot.engine")


class ChatModel(Protocol):
    def chat(self, messages: Sequence[dict[str, str]], **kwargs) -> str: ...
    last_usage: dict

class Decision:
    __slots__ = ("reply", "reason")

    def __init__(self, reply: bool, reason: str = "") -> None:
        self.reply = reply
        self.reason = reason

    def __bool__(self) -> bool:
        return self.reply

    def __repr__(self) -> str:  # pragma: no cover
        return f"Decision(reply={self.reply}, reason={self.reason!r})"


class GenerationResult(list):
    """List-compatible response with request-local attachments."""
    def __init__(self, chunks=(), *, room_id="", images=()):
        super().__init__(chunks)
        self.room_id = room_id
        self.images = list(images)


class ReplyEngine:
    def __init__(
        self,
        cfg: AppConfig,
        llm: ChatModel,
        store: ContextStore,
        self_wxid: str = "",
        search: Any = None,
        memory: Any = None,
        imagegen: Any = None,
    ) -> None:
        self.cfg = cfg
        self.llm = llm
        self.store = store
        self.self_wxid = self_wxid
        self.search = search
        self.memory = memory
        self.imagegen = imagegen
        self._local = threading.local()

    # ------------------------------------------------------------------ 过滤
    def set_self_wxid(self, wxid: str) -> None:
        self.self_wxid = wxid or ""

    def should_reply(self, msg: IncomingMessage) -> Decision:
        chat = self.cfg.chat
        if self.self_wxid and msg.sender_id == self.self_wxid:
            return Decision(False, "自己的消息")
        if not msg.text.strip():
            return Decision(False, "空消息")
        target = self._target_for(msg)
        if not msg.is_group:
            # 私聊：只有配置在 wechat.private_chats 里的对象才回
            if target is None and chat.only_groups:
                return Decision(False, "私聊未配置")
        if chat.only_groups and msg.is_group and not self._room_allowed(msg):
            return Decision(False, "群不在白名单")

        if self._sender_ignored(msg):
            return Decision(False, "发送者被忽略")

        trigger = self.cfg.trigger_for(target)
        if trigger == "all":
            return Decision(True, "全部消息")
        if trigger == "mention":
            if self.self_wxid and self.self_wxid in msg.at_wxids:
                return Decision(True, "被 @")
            bot_name = self.cfg.persona_for(target).bot_name
            if bot_name and (f"@{bot_name}" in msg.text):
                return Decision(True, "文本中 @ 了机器人")
            return Decision(False, "没被 @")
        if trigger == "prefix":
            prefix = self.cfg.prefix_for(target).strip()
            if msg.text.strip().lower().startswith(prefix.lower()):
                return Decision(True, f"前缀 {prefix}")
            return Decision(False, "没有前缀")
        return Decision(False, f"未知 trigger={trigger}")

    def _target_for(self, msg: IncomingMessage):
        if msg.is_group:
            return self.cfg.group_for(msg.room_name) or self.cfg.group_for(msg.room_id)
        return self.cfg.private_chat_for(msg.room_name) or self.cfg.private_chat_for(msg.room_id)

    def _room_allowed(self, msg: IncomingMessage) -> bool:
        allowed = [x for x in self.cfg.wechat.allowed_groups if x]
        if not allowed:
            return True
        for rule in allowed:
            if rule == msg.room_id or rule == msg.room_name:
                return True
        return False

    def _sender_ignored(self, msg: IncomingMessage) -> bool:
        ignore = [x for x in self.cfg.wechat.ignore_senders if x]
        return msg.sender_id in ignore or (msg.sender_name and msg.sender_name in ignore)

    # ------------------------------------------------------------------ 生成
    def generate(self, batch: list[IncomingMessage], *, commit: bool = True) -> GenerationResult:
        if not batch:
            return GenerationResult()
        with self.store.lock_for(batch[-1].room_id):
            # Reset even on errors so legacy callers cannot collect stale images.
            self._local.images = []
            result = self._generate(batch, commit=commit)
            self._local.images = list(result.images)
            return result

    def _generate(self, batch: list[IncomingMessage], *, commit: bool) -> GenerationResult:
        """给定一批待合并的消息，返回若干条微信消息文本。"""
        if not batch:
            return []
        room_id = batch[-1].room_id
        room_name = batch[-1].room_name
        target = self._target_for(batch[-1])
        persona = self.cfg.persona_for(target)
        system_prompt = persona.build_system_prompt()
        if self.cfg.chat.time_aware:
            from datetime import datetime

            now = datetime.now()
            weekday = "一二三四五六日"[now.weekday()]
            system_prompt += (
                f"\n\n【当前时间】{now:%Y-%m-%d %H:%M}，周{weekday}。"
                "你可以据此自然地接早晚安、催睡觉、问吃饭，但不要每次都说。"
            )
        if self.memory is not None:
            memory_text = self.memory.inject(room_id)
            if memory_text:
                system_prompt += "\n\n" + memory_text
        ctx = self.store.get(room_id, room_name)
        for m in batch:
            ctx.add_user(m.speaker(), m.text.strip(), m.ts)
        self.store.save(ctx)
        if self.memory is not None:
            self.memory.maybe_update(room_id, room_name, list(ctx.turns), count=len(batch))

        messages = ctx.build_messages(
            system_prompt,
            self.cfg.context.window_messages,
            self.cfg.context.max_context_chars,
        )
        started = time.time()
        images: list[str] = []
        if hasattr(self.llm, "complete") and (self.search is not None or self.imagegen is not None):
            text = self._generate_with_tools(messages, ori_question=batch[-1].text, images=images)
        else:
            text = self.llm.chat(messages)
        cost = time.time() - started
        log.info(
            "DeepSeek 回复群「%s」人设=%s 用时 %.2fs%s",
            room_name or room_id,
            getattr(target, "persona", "") or "默认",
            cost,
            f" tokens={self.llm.last_usage.get('total_tokens')}" if getattr(self, "llm", None) and getattr(self.llm, "last_usage", None) else "",
        )

        text = sanitize_reply(text, persona.bot_name)
        chunks = split_text(text, self.cfg.chat.max_message_chars, self.cfg.chat.max_reply_chunks)

        if commit and chunks:
            self.commit_reply(room_id, "\n".join(chunks))
        return GenerationResult(chunks, room_id=room_id, images=images)

    def commit_reply(self, room_id: str, text: str) -> None:
        with self.store.lock_for(room_id):
            ctx = self.store.get(room_id)
            ctx.add_assistant(text)
            self.store.save(ctx)

    # ------------------------------------------------------------------ 主动开场
    def generate_proactive(self, room_id: str, room_name: str, *, commit: bool = True) -> str:
        with self.store.lock_for(room_id):
            return self._generate_proactive(room_id, room_name, commit=commit)

    def _generate_proactive(self, room_id: str, room_name: str, *, commit: bool) -> str:
        """没人说话时，主动生成一句贴合当前时间/最近聊天的开场（不 @ 人）。"""
        from datetime import datetime

        target = (
            self.cfg.group_for(room_name)
            or self.cfg.group_for(room_id)
            or self.cfg.private_chat_for(room_name)
            or self.cfg.private_chat_for(room_id)
        )
        persona = self.cfg.persona_for(target)
        now = datetime.now()
        weekday = "一二三四五六日"[now.weekday()]
        system_prompt = persona.build_system_prompt()
        system_prompt += f"\n\n【当前时间】{now:%Y-%m-%d %H:%M}，周{weekday}。"
        if self.memory is not None:
            memory_text = self.memory.inject(room_id)
            if memory_text:
                system_prompt += "\n\n" + memory_text
        ctx = self.store.get(room_id, room_name)
        messages = ctx.build_messages(
            system_prompt,
            self.cfg.context.window_messages,
            self.cfg.context.max_context_chars,
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    "（系统提示：现在聊天安静了一阵，你主动说一句话，像真人突然想起对方。"
                    "要贴合当前时间和最近聊过的事，1~2 句、不超过 40 字；"
                    "不要@人，不要说'我主动来找你'，不要提系统/提示词。）"
                ),
            }
        )
        if self.cfg.proactive.extra_prompt:
            messages[0]["content"] += "\n" + self.cfg.proactive.extra_prompt
        text = sanitize_reply(self.llm.chat(messages), persona.bot_name).strip()
        if not text:
            return ""
        if commit:
            self.commit_reply(room_id, text)
        return text

    # ------------------------------------------------------------------ 工具调用
    def pop_images(self) -> list[str]:
        """取走本轮生成的图片路径（由 Bot 负责发送）。"""
        images = getattr(self._local, "images", [])
        self._local.images = []
        return images

    def _generate_with_tools(self, messages: list[dict], ori_question: str = "", images: list | None = None) -> str:
        """模型自主决定：联网搜索 / 生成图片；执行工具后再让模型作答。"""
        from .imagegen import GENERATE_IMAGE_TOOL
        from .websearch import SEARCH_TOOL
        images = images if images is not None else []

        tools: list[dict] = []
        hints: list[str] = []
        if self.search is not None and self.cfg.search.enabled:
            tools.append(SEARCH_TOOL)
            hints.append(
                "【联网搜索】需要最新信息、实时数据（天气/股价/比分/价格）、刚发生的事，"
                "或你不确定的事实时，调用 web_search 工具；闲聊和常识问题不要调用。"
                "搜到资料后用自己的话简短回答，不要罗列链接。"
            )
        if self.imagegen is not None and self.cfg.imagegen.enabled:
            tools.append(GENERATE_IMAGE_TOOL)
            hints.append(
                "【画图】对方让你画图/生成图片/来张图/做表情包或头像时，调用 generate_image 工具；"
                "prompt 要写详细：主体长相和表情、动作、场景、风格（卡通插画/写实/水彩等）、配色、构图。"
                "图片会自动发出，你只需要再配一句话，不要说不会画、也不要输出图片地址或 Markdown。"
            )

        msgs = [dict(m) for m in messages]
        if hints and msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = str(msgs[0].get("content") or "") + "\n\n" + "\n".join(hints)

        rounds = max(1, int(self.cfg.search.max_tool_rounds))
        for round_idx in range(rounds + 1):
            tool_arg = tools if (round_idx < rounds and tools) else None
            message = self.llm.complete(msgs, tools=tool_arg)
            calls = getattr(message, "tool_calls", None) or []
            if not calls:
                return str(getattr(message, "content", "") or "").strip()

            # 把模型的工具调用追加进对话
            assistant: dict[str, Any] = {
                "role": "assistant",
                "content": getattr(message, "content", "") or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                    for call in calls
                ],
            }
            msgs.append(assistant)
            for index, call in enumerate(calls):
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except Exception:  # noqa: BLE001
                    args = {}
                if index >= 2:
                    content = "本轮工具次数已达上限，请根据已有结果回答。"
                elif name == "web_search" and self.search is not None:
                    query = str(args.get("query") or "").strip()
                    content = self.search.search_and_format(query, ori_question=ori_question)
                    log.info("联网搜索：%s", query or "（空查询）")
                elif name == "generate_image" and self.imagegen is not None:
                    prompt = str(args.get("prompt") or "").strip()
                    path = self.imagegen.generate(prompt, args.get("size")) if prompt else None
                    if path:
                        images.append(path)
                        content = "图片已生成，会附在回复里发出。请用一句话接话，不要输出图片地址。"
                        log.info("已生成图片：%s", path)
                    else:
                        content = "图片生成失败（服务不可用或频率超限），用文字告诉对方没画出来。"
                else:
                    content = "工具不可用"
                msgs.append({"role": "tool", "tool_call_id": call.id, "content": content})
        return ""

    # ------------------------------------------------------------------ 其它
    def reset_room(self, room_id: str) -> None:
        self.store.reset(room_id)


# ---------------------------------------------------------------------- 文本处理
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?|```")
_LEAD_NAME_RE = re.compile(r"^\s*(DeepSeek|deepseek|DS|ds)\s*[:：]\s*")


def sanitize_reply(text: str, bot_name: str = "") -> str:
    """清掉模型偶尔冒出来的 Markdown 痕迹和自称前缀。"""
    text = text.strip()
    text = _FENCE_RE.sub("", text)
    text = _LEAD_NAME_RE.sub("", text)
    text = text.replace("**", "").replace("__", "")
    lines = []
    for line in text.splitlines():
        line = line.rstrip()
        line = re.sub(r"^#{1,6}\s+", "", line).strip()   # 标题
        if line.startswith("> "):
            line = line[2:].strip()
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if bot_name:
        text = re.sub(rf"^\s*{re.escape(bot_name)}\s*[:：]\s*", "", text)
    # 微信聊天习惯：句尾不用句号。中文句号一律换成逗号，再去掉行尾多余的逗号
    text = re.sub(r"[。．]+", "，", text)
    text = re.sub(r"，{2,}", "，", text)
    text = re.sub(r"[，,、]+(?=\n|$)", "", text)
    text = re.sub(r"[，,、]+\s*$", "", text)
    return text.strip()


_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")


def split_text(text: str, max_chars: int = 800, max_chunks: int = 3) -> list[str]:
    """把长回复切成多条微信消息：优先按段落，其次按句子，最后硬切。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    # 先按句子切成小片段，再贪心装进 max_chars 的桶里
    pieces: list[str] = []
    for para in text.split("\n"):
        if not para.strip():
            continue
        for sent in _SENT_SPLIT_RE.split(para):
            if sent:
                pieces.append(sent)
    for piece in pieces:
        while len(piece) > max_chars:  # 单句超长，硬切
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.append(piece[:max_chars].strip())
            piece = piece[max_chars:]
        if not current:
            current = piece
        elif len(current) + len(piece) <= max_chars:
            current += piece
        else:
            chunks.append(current.strip())
            current = piece
    if current.strip():
        chunks.append(current.strip())

    chunks = [c for c in chunks if c]
    if len(chunks) > max_chunks:
        head = chunks[: max_chunks - 1]
        tail = "".join(chunks[max_chunks - 1 :])
        chunks = head + [tail[:max_chars - 1] + "…" if len(tail) > max_chars else tail]
    return chunks
