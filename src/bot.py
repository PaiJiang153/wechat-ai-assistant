"""编排层：把微信适配器收到的消息，经过过滤 → 合并 → DeepSeek → 发回群里。

关键行为（都可通过 config.yaml 调整）：
  * 连续消息合并（避免两个人同时说话时机器人回两遍）
  * 每群串行处理，保证上下文顺序
  * 频率限制，防止刷屏和烧钱
  * 自己发的消息、系统消息、非白名单群一律忽略
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, Future, TimeoutError
from typing import Iterable

from .adapter import WeChatAdapter
from .config import AppConfig, HumanizeConfig
from .engine import ReplyEngine
from .models import IncomingMessage
from .proactive import ProactiveScheduler
from .runtime import DeliveryResult, UsageMeter, atomic_json
from pathlib import Path

log = logging.getLogger("dsbot.bot")

_BREAK_RE = re.compile(r"[。！？!?…；;，,、\s]")


def split_reply_in_two(text: str) -> list[str]:
    """在标点/空格边界把一条回复拆成两条，尽量对半；拆不了就原样返回。"""
    text = (text or "").strip()
    if len(text) < 8:
        return [text]
    mid = len(text) // 2
    candidates = [m.end() for m in _BREAK_RE.finditer(text)]
    candidates = [i for i in candidates if 4 <= i <= len(text) - 4]
    if not candidates:
        return [text]
    idx = min(candidates, key=lambda i: abs(i - mid))
    first, second = text[:idx].strip(), text[idx:].strip()
    if not first or not second:
        return [text]
    return [first, second]


class BotStats:
    def __init__(self) -> None:
        self.received = 0
        self.replied = 0
        self.dropped = 0
        self.errors = 0
        self.generated = 0
        self.delivery_failed = 0
        self.delivery_unknown = 0
        self.expired = 0
        self._lock = threading.Lock()
        self.started_at = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            result = {k: v for k, v in vars(self).items() if not k.startswith("_")}
            result["uptime_seconds"] = round(time.time() - self.started_at, 1)
            return result

    def add(self, name: str, count: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + count)


class Bot:
    def __init__(self, cfg: AppConfig, adapter: WeChatAdapter, engine: ReplyEngine) -> None:
        self.cfg = cfg
        self.adapter = adapter
        self.engine = engine
        self.stats = BotStats()

        self._pending: dict[str, list[IncomingMessage]] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._reply_times: dict[str, deque[float]] = {}
        self._last_reply_at: dict[str, float] = {}
        self._seen_ids: deque[str] = deque(maxlen=800)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="reply")
        self._stop = threading.Event()
        self._scheduler: ProactiveScheduler | None = None
        self._pending_since: dict[str, float] = {}
        self._timer_versions: dict[str, int] = {}
        self._last_activity: dict[str, float] = {}
        self._ready: dict[str, deque] = {}
        self._running: set[str] = set()
        self._slots = threading.BoundedSemaphore(cfg.chat.max_queued_batches)
        self.metrics = UsageMeter(Path(cfg.logging.dir) / "delivery-metrics.json")
        self._status_guard = threading.Lock()
        self._status_started = False

    # ------------------------------------------------------------------ 入口
    def on_message(self, msg: IncomingMessage) -> None:
        self.stats.add("received")
        if self._stop.is_set():
            return
        with self._guard:
            if msg.msg_id and msg.msg_id in self._seen_ids:
                return
            if msg.msg_id:
                self._seen_ids.append(msg.msg_id)
            self._last_activity[msg.room_id] = time.monotonic()

        decision = self.engine.should_reply(msg)
        if not decision:
            self.stats.add("dropped")
            log.debug("忽略消息（%s）：%s | %s", decision.reason, msg.sender_name, msg.text[:60])
            return

        with self._guard:
            if len(self._pending.get(msg.room_id, [])) >= self.cfg.chat.max_pending_messages:
                self.stats.add("dropped")
                log.warning("会话 %s 待处理消息达到上限", msg.room_id)
                return
            self._pending.setdefault(msg.room_id, []).append(msg)
            first = self._pending_since.setdefault(msg.room_id, time.monotonic())
            old = self._timers.pop(msg.room_id, None)
            if old:
                old.cancel()
            delay = self.cfg.chat.debounce_seconds
            human = self.cfg.humanize
            if human.enabled and human.debounce_jitter > 0:
                delay += random.uniform(0, human.debounce_jitter)
            delay = max(0, min(delay, first + self.cfg.chat.max_debounce_seconds - time.monotonic()))
            version = self._timer_versions.get(msg.room_id, 0) + 1
            self._timer_versions[msg.room_id] = version
            timer = threading.Timer(delay, self._on_debounce, args=(msg.room_id, version))
            timer.daemon = True
            self._timers[msg.room_id] = timer
            timer.start()

    def run(self) -> None:
        self.adapter.on_message(self.on_message)
        # 适配器可能在登录成功后才知道机器人自己的 wxid（微信侧必须连上才知道）。
        # 这里把引擎的 self_wxid 更新器挂到适配器钩子上，保证 mention 触发规则可用。
        try:
            setattr(self.adapter, "on_self_wxid", self.engine.set_self_wxid)
        except Exception:  # noqa: BLE001
            pass
        wxid = self.adapter.get_self_wxid()
        if wxid:
            self.engine.set_self_wxid(wxid)
        log.info("机器人启动：适配器=%s，触发规则=%s", self.adapter.name, self.cfg.trigger_label)
        human = self.cfg.humanize
        if human.enabled:
            log.info(
                "拟人化已启用：打字停顿 %.1f~%.1fs、%.0f%% 概率分两条发、%.0f%% 概率补一句、@概率 %.0f%%",
                human.typing_min,
                human.typing_max,
                human.split_chance * 100,
                human.double_text_chance * 100,
                human.at_chance * 100,
            )
        if self.cfg.chat.time_aware:
            log.info("时间感知已启用：人设上下文会带上当前时间")
        self._scheduler = self._build_proactive()
        self._status_started = True
        threading.Thread(target=self._monitor, name="bot-metrics", daemon=True).start()
        if self._scheduler is not None:
            self._scheduler.start()
        try:
            self.adapter.start()   # 阻塞
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._scheduler is not None:
            self._scheduler.stop()
        with self._guard:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
            self._pending.clear()
            self._pending_since.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            self.adapter.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._status_started:
            try:
                self._write_status(False)
            except OSError:
                log.exception("保存停止状态失败")
        log.info("机器人已停止 %s", self.stats.snapshot())

    def _write_status(self, running):
        with self._status_guard:
            if running and self._stop.is_set():
                return
            snapshot = {"updated_at": time.time(), "running": running, **self.stats.snapshot(),
                        "adapter": self.adapter.describe(), "delivery_today": self.metrics.snapshot()}
            atomic_json(Path(self.cfg.logging.dir) / "stats.json", snapshot)

    def _monitor(self):
        while not self._stop.is_set():
            try:
                self._write_status(True)
                log.info("运行统计：%s", self.stats.snapshot())
            except Exception:
                log.exception("保存运行指标失败")
            self._stop.wait(60)

    # ------------------------------------------------------------------ 主动开场
    def _build_proactive(self) -> ProactiveScheduler | None:
        cfg = self.cfg.proactive
        if not cfg.enabled:
            return None
        sticker_store = getattr(self.adapter, "sticker_store", None)

        def targets() -> list[tuple[str, str]]:
            info = self.adapter.describe() or {}
            items: list[tuple[str, str]] = []
            for g in info.get("groups") or []:
                if g.get("name") and g.get("id"):
                    items.append((str(g["id"]), str(g["name"])))
            for p in info.get("private_chats") or []:
                if p.get("name") and p.get("wxid"):
                    items.append((str(p["wxid"]), str(p["name"])))
            if cfg.targets:
                allowed = set(cfg.targets)
                items = [it for it in items if it[1] in allowed]
            items = [it for it in items if self._proactive_allowed(it[0])]
            return items

        def pick_sticker(room_name: str) -> str | None:
            return sticker_store.pick(room_name) if sticker_store is not None else None

        return ProactiveScheduler(
            cfg=cfg,
            targets_fn=targets,
            text_fn=lambda room_id, room_name: self.engine.generate_proactive(room_id, room_name, commit=False),
            send_text=lambda room_id, text: self._safe_send(room_id, None, text),
            send_sticker=self._send_image,
            poke=lambda room_id: self._record_delivery(self._await_delivery(self.adapter.poke(room_id))),
            pick_sticker=pick_sticker,
            stop_event=self._stop,
            room_lock_fn=lambda room_id: self.engine.store.lock_for(room_id),
            eligible_fn=self._proactive_allowed,
            state_path=Path(self.cfg.wechat.state_dir) / "proactive.json",
            sticker_limit=self.cfg.stickers.max_send_per_day,
        )

    def _proactive_allowed(self, room_id: str) -> bool:
        with self._guard:
            last = self._last_activity.get(room_id, time.monotonic())
            self._last_activity.setdefault(room_id, last)
            return (not self._stop.is_set() and room_id not in self._running
                    and not self._pending.get(room_id)
                    and time.monotonic() - last >= self.cfg.proactive.min_gap_minutes * 60
                    and self._rate_ok(room_id))

    def _on_debounce(self, room_id: str, version: int | None = None) -> None:
        with self._guard:
            if version is not None and version != self._timer_versions.get(room_id):
                return
            batch = self._pending.pop(room_id, [])
            self._pending_since.pop(room_id, None)
            self._timers.pop(room_id, None)
        if not batch or self._stop.is_set():
            return
        if not self._slots.acquire(blocking=False):
            self.stats.add("dropped", len(batch))
            log.warning("回复队列已满，跳过会话 %s", room_id)
            return
        with self._guard:
            self._ready.setdefault(room_id, deque()).append(batch)
            if room_id in self._running:
                return
            self._running.add(room_id)
            self._pool.submit(self._run_room, room_id)

    def _run_room(self, room_id: str) -> None:
        while True:
            with self._guard:
                queue = self._ready.get(room_id)
                if not queue:
                    self._ready.pop(room_id, None)
                    self._running.discard(room_id)
                    return
                batch = queue.popleft()
            try:
                self._process(room_id, batch, self.engine.store.lock_for(room_id))
            except Exception:
                self.stats.add("errors")
                log.exception("会话处理异常：%s", room_id)
            finally:
                self._slots.release()

    def _process(self, room_id: str, batch: list[IncomingMessage], lock: threading.Lock) -> None:
        if self._stop.is_set():
            return
        with lock:
            if self._stop.is_set():
                return
            batch = [m for m in batch if time.time() - m.ts <= self.cfg.chat.message_ttl_seconds]
            if not batch:
                self.stats.add("expired")
                return
            if not self._rate_ok(room_id):
                self.stats.add("dropped")
                log.warning("群 %s 触发限流（%d 条/分钟），本次不回复", room_id, self.cfg.chat.max_replies_per_minute)
                return
            # 两次回复之间留最小间隔，避免连珠炮
            wait = self._last_reply_at.get(room_id, 0) + self.cfg.chat.min_interval_seconds - time.time()
            if wait > 0:
                if self._stop.wait(wait):
                    return
            human = self.cfg.humanize
            # 拟人：偶尔"已读不回"（默认关闭；被 @ 时一定回）
            if (
                human.enabled
                and human.silence_chance > 0
                and not self._was_mentioned(batch)
                and random.random() < human.silence_chance
            ):
                self.stats.add("dropped")
                log.info("拟人：这次假装没看到（silence_chance=%.0f%%）", human.silence_chance * 100)
                return
            try:
                generated = self.engine.generate(batch, commit=False)
                chunks = list(generated)
                self.stats.add("generated")
            except Exception as exc:  # noqa: BLE001
                self.stats.add("errors")
                log.exception("生成回复失败：%s", exc)
                if self.cfg.chat.error_reply:
                    self._safe_send(room_id, None, "（DeepSeek 暂时连不上，等会儿再试～）")
                return

            if time.time() - batch[-1].ts > self.cfg.chat.message_ttl_seconds:
                self.stats.add("expired")
                return

            if human.enabled:
                chunks = self._humanize_chunks(chunks, human)
                typing_delay = self._typing_delay("".join(chunks), human)
                if typing_delay > 0 and not self._stop.is_set():
                    log.debug("拟人：等 %.1fs 再发", typing_delay)
                    if self._stop.wait(typing_delay):
                        return

            # 模型生成的图片（如果有）先发图，再发文字
            for path in generated.images[:2]:
                if self._stop.is_set():
                    break
                if not self._send_image(room_id, path):
                    chunks = ["图片这次没能确认发出。"]
                    break
                self._stop.wait(0.8)

            at_id = None
            target = self.engine._target_for(batch[-1])
            at_enabled = getattr(target, "reply_with_at", None)
            if at_enabled is None:
                at_enabled = self.cfg.chat.reply_with_at
            if at_enabled and batch[-1].is_group:
                at_id = batch[-1].sender_id
            if human.enabled and at_id and human.at_chance < 1.0 and random.random() > human.at_chance:
                at_id = None  # 真人不总 @
            delivered = False
            for i, text in enumerate(chunks):
                if self._stop.is_set():
                    break
                if not self._safe_send(room_id, at_id if i == 0 else None, text):
                    break
                delivered = True
                if i < len(chunks) - 1:
                    if human.enabled:
                        self._stop.wait(random.uniform(human.split_delay_min, human.split_delay_max))
                    else:
                        self._stop.wait(0.8)
            # 拟人：发完再补一句
            if delivered and human.enabled and human.double_texts and human.double_text_chance > 0 and random.random() < human.double_text_chance:
                if not self._stop.is_set():
                    if not self._stop.wait(random.uniform(human.double_text_delay_min, human.double_text_delay_max)):
                        self._safe_send(room_id, None, random.choice(human.double_texts))

    # ------------------------------------------------------------------ 拟人
    def _was_mentioned(self, batch: list[IncomingMessage]) -> bool:
        wxid = getattr(self.engine, "self_wxid", "") or ""
        for m in batch:
            if wxid and wxid in (m.at_wxids or []):
                return True
            if wxid and f"@{wxid}" in (m.text or ""):
                return True
        return False

    def _humanize_chunks(self, chunks: list[str], human: HumanizeConfig) -> list[str]:
        if not chunks or len(chunks) > 1:
            return chunks
        text = chunks[0]
        if not (human.split_min_chars <= len(text) <= human.split_max_chars):
            return chunks
        if random.random() >= human.split_chance:
            return chunks
        return split_reply_in_two(text)

    @staticmethod
    def _typing_delay(text: str, human: HumanizeConfig) -> float:
        base = random.uniform(human.typing_min, human.typing_max)
        typing = len(text) / max(1.0, human.typing_chars_per_second)
        return max(0.0, min(human.max_typing_seconds, base + typing))

    # ------------------------------------------------------------------ 工具
    def _rate_ok(self, room_id: str) -> bool:
        limit = self.cfg.chat.max_replies_per_minute
        if limit <= 0:
            return True
        q = self._reply_times.setdefault(room_id, deque())
        cutoff = time.time() - 60
        while q and q[0] < cutoff:
            q.popleft()
        return len(q) < limit

    def _await_delivery(self, receipt, on_late=None) -> DeliveryResult:
        # Older synchronous adapters fulfill their send contract before returning.
        if receipt is None:
            return DeliveryResult("confirmed")
        if isinstance(receipt, Future):
            deadline = time.monotonic() + self.cfg.chat.delivery_timeout
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    return receipt.result(timeout=min(0.25, max(0.001, deadline - time.monotonic())))
                except TimeoutError:
                    continue
            if receipt.cancel():
                return DeliveryResult("expired", "等待送达超时，已取消排队任务")
            if on_late:
                def finalize(future):
                    try:
                        result = future.result()
                        if result.confirmed:
                            on_late()
                            self.metrics.record(confirmed=1, unknown=-1, late_confirmed=1)
                            self.stats.add("delivery_unknown", -1)
                    except Exception:
                        log.exception("保存迟到的发送回执失败")
                def completed(future):
                    # Future callbacks run on the GUI thread. Never wait for a
                    # room lock there: its owner may be waiting for the next GUI send.
                    threading.Thread(target=finalize, args=(future,), name="late-receipt", daemon=True).start()
                receipt.add_done_callback(completed)
            return DeliveryResult("unknown", "发送仍在进行，稍后以回执为准")
        return receipt if isinstance(receipt, DeliveryResult) else DeliveryResult("unknown")

    def _record_delivery(self, result: DeliveryResult) -> bool:
        self.metrics.record(send_attempts=1, confirmed=int(result.confirmed),
                            failed=int(result.status in ("failed", "expired")),
                            unknown=int(result.status == "unknown"),
                            queue_seconds=result.queue_seconds)
        if not result.confirmed:
            self.stats.add("delivery_unknown" if result.status == "unknown" else "delivery_failed")
            log.warning("消息未确认送达：%s %s", result.status, result.detail)
        return result.confirmed

    def _safe_send(self, room_id: str, at_id: str | None, text: str) -> bool:
        try:
            if self._stop.is_set() or not self._rate_ok(room_id):
                return False
            def accept():
                self.engine.commit_reply(room_id, text)
                self.stats.add("replied")
                with self._guard:
                    self._last_activity[room_id] = time.monotonic()
                    self._last_reply_at[room_id] = time.time()
                    self._reply_times.setdefault(room_id, deque()).append(time.time())
            result = self._await_delivery(self.adapter.send_text(room_id, text, at_id), on_late=accept)
            if self._record_delivery(result):
                accept()
                log.info("消息已确认：会话=%s 字数=%d", room_id, len(text))
                return True
        except Exception as exc:  # noqa: BLE001
            self.stats.add("errors")
            self._record_delivery(DeliveryResult("failed", str(exc)))
            log.exception("发送失败：%s", exc)
        return False

    def _send_image(self, room_id: str, path: str) -> bool:
        try:
            if self._stop.is_set() or not self._rate_ok(room_id):
                return False
            def accept():
                self.engine.commit_reply(room_id, "[已发送图片]")
                with self._guard:
                    self._last_activity[room_id] = time.monotonic()
                    self._last_reply_at[room_id] = time.time()
                    self._reply_times.setdefault(room_id, deque()).append(time.time())
            result = self._await_delivery(self.adapter.send_file(room_id, path), on_late=accept)
            if self._record_delivery(result):
                accept()
                return True
        except Exception as exc:  # noqa: BLE001
            self.stats.add("errors")
            self._record_delivery(DeliveryResult("failed", str(exc)))
            log.exception("发送图片失败：%s", exc)
        return False

    def feed(self, messages: Iterable[IncomingMessage]) -> None:
        """测试/模拟用：直接灌入消息。"""
        for m in messages:
            self.on_message(m)
