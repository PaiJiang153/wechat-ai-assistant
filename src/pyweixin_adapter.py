"""微信 4.1.x 适配器：基于 pyweixin 的 **UI 自动化**（不注入、不伪装、不改微信文件）。

为什么需要它
============
微信服务器已经不再放行 3.9 客户端的登录，WeChatFerry（注入方案）在真机上走不通了。
而免费的 4.x hook 项目都锁定在特定小版本（4.1.8 / 4.1.10.27），还大多没实现"收消息"。
pywechat127 里的 `pyweixin` 是一个专注微信 4.1+ 的开源 UI 自动化库（pywinauto 实现），
可以直接操作界面收发消息——虽然不如 hook 快，但**不需要降级、不需要注入、不需要管理员**。

工作原理（每轮循环）
==================
1. 打开/复用"群聊独立窗口"（`Navigator.open_seperate_dialog_window`）
2. `Monitor.listen_on_chat(duration=poll_seconds)` 监听这一小段时间内的新消息
   （内部按 UI 元素的 runtime_id 做增量识别，不会重复回放历史消息）
3. 把新消息转成 `IncomingMessage` 交给 Bot
4. Bot 把要发的回复放进发送队列，**在同一次循环末尾发送**（所有 UI 操作都在同一线程，
   避免 pywinauto 多线程访问崩溃）
5. 发送后把自己发出的文本记下来，下一轮监听时过滤掉，防止"自己回自己"死循环

限制（必须知道）
==============
* 微信必须处于**已登录、窗口未关闭**状态；电脑不能锁屏/休眠（UI 自动化需要桌面会话）
* 速度比 hook 慢：新消息最多 `poll_seconds` 秒后才被处理
* 微信 UI 大改版可能导致定位失效，此时升级 pywechat127
* 群里消息的"发送者"依赖 `wechat.group_members` 配置里的群昵称列表，尽量写全
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Any, Optional

from .adapter import WeChatAdapter
from .models import IncomingMessage

log = logging.getLogger("dsbot.pyweixin")

__all__ = ["PyweixinAdapter"]


class PyweixinAdapter(WeChatAdapter):
    """用 pyweixin（UI 自动化）操作微信 4.1.x 收发群消息。"""

    name = "pyweixin"

    def __init__(
        self,
        group_name: str,
        group_members: Optional[list[str]] = None,
        poll_seconds: float = 2.0,
        window_minimize: bool = True,
        self_name: str = "",
    ) -> None:
        super().__init__()
        self.group_name = (group_name or "").strip()
        if not self.group_name:
            raise ValueError("PyweixinAdapter 必须提供群名 group_name")
        self.group_members = [m.strip() for m in (group_members or []) if m and m.strip()]
        self.poll_seconds = max(0.5, float(poll_seconds))
        self.window_minimize = bool(window_minimize)
        self.self_name = (self_name or "").strip()

        self._dialog: Any = None
        self._stop = threading.Event()
        self._outbox: "queue.Queue[tuple[str, str, Optional[str]]]" = queue.Queue()
        # 自己刚发出去的文本，用于过滤"回声"消息（防自己回自己）
        self._sent_recent: deque[tuple[str, float]] = deque(maxlen=20)
        self._seq = 0

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """阻塞主循环；由 Bot 在独立进程里调用。"""
        try:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from pyweixin import Monitor, Navigator, Tools  # 延迟导入
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "缺少 pywechat127 依赖，请先执行：\n"
                r"  .\.venv\Scripts\python.exe -m pip install pywechat127"
            ) from exc

        # pyweixin 自带的 is_weixin_running() 依赖 psutil，权限受限时可能误报，
        # 这里再用 tasklist 兜底确认一次。
        if not Tools.is_weixin_running() and not self._weixin_process_alive():
            raise RuntimeError(
                "没有检测到正在运行的微信 4.x（Weixin.exe）。\n"
                "请先启动微信并登录机器人小号，再运行本程序。\n"
                "（注意：微信 4.x 进程名是 Weixin.exe，不是 WeChat.exe）"
            )
        version = ""
        try:
            version = str(Tools.get_weixin_version() or "")
        except Exception:  # noqa: BLE001
            pass
        log.info("已连接微信 4.x（版本 %s），准备监听群「%s」", version or "未知", self.group_name)

        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._ensure_dialog()
                self._listen_and_send(Monitor)
                backoff = 2.0
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                log.exception("UI 自动化循环异常：%s", exc)
                self._close_dialog()
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 30.0)

    def stop(self) -> None:
        self._stop.set()
        self._close_dialog()

    # ------------------------------------------------------------------ 发送
    def send_text(self, room_id: str, text: str, at_wxid: Optional[str] = None) -> None:
        """不直接操作 UI，而是入队，由监听线程在下一轮循环里统一发送。"""
        text = (text or "").strip()
        if not text:
            return
        # pyweixin 的 UI 输入框不支持可靠的"真 @ 提醒"，@ 用文本形式拼在正文前面
        if at_wxid:
            name = at_wxid
            if name and not text.startswith(f"@{name}"):
                text = f"@{name} {text}"
        self._outbox.put((room_id, text, None))

    def get_self_wxid(self) -> str:
        """UI 方案拿不到 wxid，返回群里显示的昵称，用于过滤自己发的消息。"""
        return self.self_name

    def describe(self) -> dict:
        return {
            "adapter": self.name,
            "group": self.group_name,
            "poll_seconds": self.poll_seconds,
            "members": list(self.group_members),
        }

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _weixin_process_alive() -> bool:
        """用 tasklist 兜底判断微信 4.x 是否在运行（不依赖 psutil 权限）。"""
        try:
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="ignore",
            ).stdout
            return "Weixin.exe" in out
        except Exception:  # noqa: BLE001
            return True  # 查不了就不拦

    def _ensure_dialog(self) -> None:
        """打开（或复用）群聊独立窗口。"""
        from pyweixin import Navigator, Tools

        if self._dialog is not None:
            try:
                if self._dialog.exists(timeout=0.5):
                    return
            except Exception:  # noqa: BLE001
                pass
            self._dialog = None

        # 先把主窗口调到前台，否则找不到窗口
        try:
            # pyweixin 内部用 psutil 判断进程，非管理员/权限受限时可能误报"未启动"，
            # 这里用 tasklist 兜底后的结论临时修正它的判断，避免误伤。
            if not Tools.is_weixin_running() and self._weixin_process_alive():
                import pyweixin

                pyweixin.Tools.is_weixin_running = staticmethod(lambda: True)  # type: ignore[assignment]
            Navigator.open_weixin()
        except Exception as exc:  # noqa: BLE001
            log.warning("打开微信主窗口失败（可忽略，继续尝试打开群聊窗口）：%s", exc)
        log.info("正在打开群聊窗口「%s」…", self.group_name)
        self._dialog = Navigator.open_seperate_dialog_window(
            friend=self.group_name,
            window_minimize=self.window_minimize,
            close_weixin=False,
        )
        log.info("群聊窗口已就绪")

    def _listen_and_send(self, Monitor: Any) -> None:
        """一轮：监听 poll_seconds 秒 → 分发消息 → 发送队列里的回复。"""
        assert self._dialog is not None
        duration = f"{self.poll_seconds}s"
        details = Monitor.listen_on_chat(
            self._dialog,
            duration,
            groupMembers=self.group_members,
            close_dialog_window=False,
        )
        texts = self._extract(details, "文本内容")
        senders = self._extract(details, "消息发送者")
        for i, text in enumerate(texts):
            sender = senders[i] if i < len(senders) and senders[i] else ""
            self._dispatch_text(text, sender)
        self._drain_outbox()

    @staticmethod
    def _extract(details: Any, key: str) -> list[str]:
        """兼容 pyweixin 不同版本的返回结构（有的返回 dict，有的直接返回 list）。"""
        if not isinstance(details, dict):
            return []
        value = details.get(key)
        if isinstance(value, list):
            return [str(v) for v in value if v]
        if isinstance(details.get("文本消息"), list) and key == "文本内容":
            return [str(v) for v in details["文本消息"] if v]
        return []

    def _dispatch_text(self, raw: str, sender: str) -> None:
        text = (raw or "").strip()
        if not text:
            return
        if self._is_echo(text):
            log.debug("忽略自己刚发出的消息：%s", text[:40])
            return
        if not sender and self.self_name:
            # 没有解析出发送者时，尝试按 "昵称 内容" 拆
            for name in [self.self_name, *self.group_members]:
                if text.startswith(name + " "):
                    sender, text = name, text[len(name) + 1 :].strip()
                    break
        elif sender and text.startswith(sender + " "):
            # pyweixin 的发送者列表可用时，正文里可能仍带着 "昵称 " 前缀
            text = text[len(sender) + 1 :].strip()
        if sender and self.self_name and sender == self.self_name:
            log.debug("忽略自己在群里发的消息：%s", text[:40])
            return

        self._seq += 1
        msg = IncomingMessage(
            room_id=self.group_name,
            sender_id=sender or "unknown",
            sender_name=sender or "群友",
            text=text,
            room_name=self.group_name,
            msg_id=f"ui-{int(time.time() * 1000)}-{self._seq}",
            is_group=True,
        )
        if self._on_message:
            try:
                self._on_message(msg)
            except Exception:  # noqa: BLE001
                log.exception("消息回调异常（已跳过这条）")

    def _drain_outbox(self) -> None:
        """把 Bot 排队的回复发出去（全部 UI 操作都留在本线程）。"""
        if self._dialog is None:
            return
        while True:
            try:
                _room, text, _at = self._outbox.get_nowait()
            except queue.Empty:
                return
            try:
                self._send_via_ui(text)
                self._sent_recent.append((text, time.time()))
                log.info("→ 已发送：%s", text.replace("\n", " ")[:80])
            except Exception as exc:  # noqa: BLE001
                log.exception("UI 发送失败：%s", exc)
            time.sleep(0.8)  # 多条回复之间留点间隔，别刷屏

    def _send_via_ui(self, text: str) -> None:
        import pyautogui
        from pyweixin.Uielements import Edits

        assert self._dialog is not None
        self._dialog.restore()
        edit = self._dialog.child_window(**Edits.InputEdit)
        edit.set_text(text)
        pyautogui.hotkey("alt", "s")  # pyweixin 官方示例用的发送快捷键

    def _is_echo(self, text: str) -> bool:
        now = time.time()
        for sent, ts in list(self._sent_recent):
            if now - ts < 20 and (text == sent or text.endswith(sent)):
                return True
        return False

    def _close_dialog(self) -> None:
        dialog, self._dialog = self._dialog, None
        if dialog is None:
            return
        try:
            dialog.close()
        except Exception:  # noqa: BLE001
            pass
