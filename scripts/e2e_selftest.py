r"""端到端自测：不需要真人发消息，直接灌一条「群友发言」走完整链路。

流程：
  真实适配器(wechatauto) + 真实 DeepSeek + 真实 Bot
    → 伪造一条 IncomingMessage（假装群里有人说话）
    → Bot 合并/限流 → DeepSeek 生成回复
    → 适配器真实发送到微信群
    → 回读数据库确认消息已落在群里

用法：
    .\.venv\Scripts\python.exe scripts\\e2e_selftest.py
    .\.venv\Scripts\python.exe scripts\\e2e_selftest.py --text "帮我测一下"
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.bot import Bot                      # noqa: E402
from src.config import load_config            # noqa: E402
from src.context import ContextStore          # noqa: E402
from src.engine import ReplyEngine            # noqa: E402
from src.llm import DeepSeekClient            # noqa: E402
from src.models import IncomingMessage        # noqa: E402
from src.wechatauto_adapter import WechatautoAdapter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="在吗？随便聊两句")
    parser.add_argument("--sender", default="小明")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config()
    if not cfg.group_list():
        print("[自测] 请先配置至少一个群聊目标；本脚本会向第一个群发送真实消息。")
        return 2
    llm = DeepSeekClient(cfg.deepseek)
    store = ContextStore(cfg.context.persist_dir)
    engine = ReplyEngine(cfg, llm, store)
    adapter = WechatautoAdapter(
        group_names=[group.name for group in cfg.group_list()],
        private_names=[contact.name for contact in cfg.private_chat_list()],
        poll_seconds=cfg.wechat.poll_seconds,
        reply_with_at=False,
        expected_self_name=cfg.wechat.self_name,
    )
    bot = Bot(cfg, adapter, engine)

    adapter.on_message(bot.on_message)
    adapter.on_self_wxid = engine.set_self_wxid
    baseline = _last_self_local_id(adapter)
    print(f"[自测] 目标群: {adapter.group_name}")
    print(f"[自测] 基线 self local_id = {baseline}")

    thread = threading.Thread(target=_run_adapter, args=(adapter,), daemon=True)
    thread.start()
    ready_deadline = time.time() + args.timeout
    while adapter._wx is None and thread.is_alive() and time.time() < ready_deadline:
        time.sleep(0.1)
    if adapter._wx is None:
        adapter.stop()
        bot.shutdown()
        llm.close()
        print("[自测] 适配器未能就绪")
        return 1

    fake = IncomingMessage(
        room_id=adapter._group_id,
        sender_id="selftest-user",
        sender_name=args.sender,
        text=args.text,
        room_name=adapter.group_name,
        msg_id=f"selftest-{int(time.time())}",
        is_group=True,
    )
    print(f"[自测] 伪造群消息：{args.sender}: {args.text}")
    bot.on_message(fake)

    deadline = time.time() + args.timeout
    reply_row = None
    while time.time() < deadline:
        time.sleep(1.0)
        row = _find_new_self_message(adapter, baseline)
        if row:
            reply_row = row
            break

    adapter.stop()
    bot.shutdown()
    thread.join(timeout=5)
    llm.close()

    if reply_row:
        print("\n[自测] ✅ 端到端成功：机器人已经把回复发到群里")
        print(f"        local_id={reply_row.get('local_id')} content={reply_row.get('content')!r}")
        return 0
    print("\n[自测] ❌ 超时：数据库里没看到机器人发出的新消息")
    return 1


def _run_adapter(adapter: WechatautoAdapter) -> None:
    try:
        adapter.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[自测] 适配器异常退出：{exc}")


def _last_self_local_id(adapter: WechatautoAdapter):
    db = adapter._database()
    if db is None:
        from wechatauto import WeChatDB

        adapter._db = db = WeChatDB()
    info = db.get_self_info() or {}
    adapter._self_wxid = str(info.get("username") or "")
    adapter._group_id = adapter._resolve_group_id(adapter.group_name)
    rows = db.get_messages(adapter._group_id, limit=300) or []
    return {adapter._row_id(row) for row in rows}


def _find_new_self_message(adapter: WechatautoAdapter, baseline):
    db = adapter._database()
    rows = db.get_messages(adapter._group_id, limit=50) or []
    for row in rows:
        if adapter._row_id(row) not in baseline and str(row.get("sender_username") or "") == adapter._self_wxid:
            return row
    return None


if __name__ == "__main__":
    raise SystemExit(main())
