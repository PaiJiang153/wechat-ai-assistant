"""微信 4.x 适配器：基于 `wechatauto-replica`（wxauto 复刻版）。

为什么用它
==========
微信 4.1.x 的主界面是自绘的，UIA 树被完全隐藏（4.1.9 和 4.1.13 都验证过），
纯 UI 自动化库（pyweixin 等）读不到任何消息。
`wechatauto-replica` 换了两条路：
  * **收消息**：直接从本地 SQLCipher 数据库解密读取（从微信进程内存里取密钥），
    不需要 UI、不需要注入、不需要管理员权限，稳定可靠。
  * **发消息**：UIA + 坐标/OCR 混合驱动窗口输入框（只在发送瞬间操作窗口）。

本适配器只做"数据库轮询 → IncomingMessage"和"SendMsg → 发回群里"两件事，
其余（过滤/合并/限流/上下文/DeepSeek）全部复用项目核心。

使用前提
========
* 微信 4.x（本项目实测 4.1.9.30）已登录机器人小号
* `pip install wechatauto-replica`
"""
from __future__ import annotations

import html
import logging
import os
import queue
import re
import threading
import time
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from pathlib import Path
from typing import Any, Optional

from .adapter import WeChatAdapter
from .models import IncomingMessage
from .runtime import SendReceipt, DeliveryResult, atomic_json

log = logging.getLogger("dsbot.wechatauto")

__all__ = ["WechatautoAdapter"]

# 微信 4.x 的 DB 里，群消息 content 可能带 "<发送者wxid>:\n" 前缀
_SENDER_PREFIX_RE = re.compile(r"^wxid_[A-Za-z0-9_]+:\s*\n?")

# 消息类型 -> 占位文本
_TYPE_LABELS = {
    "图片": "[图片]",
    "动画表情": "[表情]",
    "表情": "[表情]",
    "语音": "[语音]",
    "视频": "[视频]",
    "文件": "[文件]",
    "链接": "[链接]",
    "小程序": "[小程序]",
    "引用": "[引用消息]",
    "系统": "[系统消息]",
    "位置": "[位置]",
    "红包": "[红包]",
    "转账": "[转账]",
}


def _is_image_file(path: Path) -> bool:
    """按文件头判断是不是真图片（微信表情缓存常是加密 wxgf/dat，不是图片）。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    if head[:3] == b"\xff\xd8\xff":
        return True
    if head[:4] == b"\x89PNG":
        return True
    if head[:4] in (b"GIF8", b"RIFF"):
        return True
    return False


_DOWNLOAD_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dl")


# 表情 CDN 域名兜底：vweixinf.tc.qq.com 在部分网络不可达（读超时），
# 同一 path+query 换 wxapp.tc.qq.com 可正常返回。备用域名排前面，原域名兜底。
_CDN_HOST_FALLBACK = {"vweixinf.tc.qq.com": "wxapp.tc.qq.com"}
_EMOJI_EXTS = (".gif", ".png", ".jpg")
_STICKER_DL_INFLIGHT: set[str] = set()
_STICKER_DL_LOCK = threading.Lock()


def _sticker_bg_download(md5: str, urls: list[str], media_dir: str) -> None:
    """后台把表情下全（每个地址按候选域名逐个试），成功后按真实格式改名。

    慢网络下一个表情要一分钟以上，同步等会把会话队列卡死，所以只做缓存：
    本次先返回占位符，同一张表情下次出现时命中缓存就能识别。
    """
    try:
        for url in urls:
            cands = _sticker_url_candidates(url)
            for cand in cands + cands[:1]:  # CDN 抖动大，首选域名末尾再补一次
                dest = Path(media_dir) / f"emoji_{md5}.gif"
                if _download_file(cand, dest, timeout=170.0):
                    _retype_image(dest)
                    log.info("表情包后台下载完成：%s", md5[:8])
                    return
    finally:
        with _STICKER_DL_LOCK:
            _STICKER_DL_INFLIGHT.discard(md5)


def _retype_image(path: Path) -> None:
    """按文件头改扩展名：表情 URL 不保证是 gif，错标会让识图的 MIME 失真。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
        suffix = (
            ".gif" if head[:4] == b"GIF8"
            else ".jpg" if head[:3] == b"\xff\xd8\xff"
            else ".png" if head[:4] == b"\x89PNG"
            else ""
        )
        if suffix and path.suffix.lower() != suffix:
            path.rename(path.with_suffix(suffix))
    except OSError:
        pass


def _sticker_url_candidates(url: str) -> list[str]:
    """一个表情下载地址的候选列表：备用域名优先，原地址兜底（去重）。"""
    out = [url]
    for bad, good in _CDN_HOST_FALLBACK.items():
        if bad in url:
            alt = url.replace(bad, good)
            if alt not in out:
                out.insert(0, alt)
    return out


def _download_file(url: str, dest: Path, timeout: float = 10.0) -> bool:
    """把 URL 下载到本地文件（表情包 cdnurl 下载下来就是标准 GIF/PNG）。

    requests 的 timeout 管不到 DNS 解析（getaddrinfo 可能长时间不返回），
    而表情包下载跑在会话串行队列里，一卡就把整个群的接收堵死。所以把
    GET 丢进独立线程、调用方按总时限等待，超时即放弃当前 URL。
    """
    try:
        from . import net

        future = _DOWNLOAD_POOL.submit(net.get, url, timeout=timeout)
        resp = future.result(timeout=timeout + 5)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return dest.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        log.debug("下载文件失败 %s：%s", url[:80], exc)
        return False


class WechatautoAdapter(WeChatAdapter):
    """用 wechatauto-replica 读库收消息、驱动窗口发消息。"""

    name = "wechatauto"

    def __init__(
        self,
        group_name: str = "",
        poll_seconds: float = 2.0,
        reply_with_at: bool = True,
        skip_history: bool = True,
        expected_self_name: str = "",
        vision: Any = None,
        asr: Any = None,
        file_summarizer: Any = None,
        media_dir: str = "data/media",
        files_dir: str = "data/media/files",
        group_names: Optional[list[str]] = None,
        private_names: Optional[list[str]] = None,
        sticker_store: Any = None,
        queue_size: int = 256,
        media_workers: int = 2,
        task_ttl_seconds: float = 300,
        state_dir: str = "",
    ) -> None:
        super().__init__()
        candidates: list[str] = list(group_names or [])
        if group_name:
            candidates.insert(0, group_name)
        names: list[str] = []
        for n in candidates:
            n = (n or "").strip()
            if n and n not in names:
                names.append(n)
        self.private_names = [n.strip() for n in (private_names or []) if n and n.strip()]
        if not names and not self.private_names:
            raise ValueError("WechatautoAdapter 至少需要一个群名或私聊对象（group_name/group_names/private_names）")
        self.group_names = names
        self.group_name = names[0] if names else ""   # 兼容旧代码/日志
        self.poll_seconds = max(0.5, float(poll_seconds))
        self.reply_with_at = bool(reply_with_at)
        self.skip_history = bool(skip_history)
        # 安全校验：只允许这个昵称的账号当机器人（防止误连主号）
        self.expected_self_name = (expected_self_name or "").strip()
        # 识图 / 表情包 / 语音 / 文件
        self.vision = vision
        self.asr = asr
        self.file_summarizer = file_summarizer
        self.media_dir = str(media_dir)
        self.files_dir = str(files_dir)
        self.sticker_store = sticker_store
        self._downloader: Any = None

        self._stop = threading.Event()
        self._db: Any = None
        self._wx: Any = None
        # 每个群独立状态：{群名: {id, last_id, members, members_at}}
        self._groups: dict[str, dict[str, Any]] = {}
        # 每个私聊独立状态：{对方昵称: {id, last_id}}
        self._privates: dict[str, dict[str, Any]] = {}
        self._group_id: str = ""              # 兼容：第一个群的 id
        self._last_local_id: int = 0
        self._self_wxid: str = ""
        self._self_name: str = ""
        self._member_names: dict[str, str] = {}
        self._name_cache_at: float = 0.0
        # 发送队列：(room_id, 文本, @对象)。Bot 从工作线程只入队，
        # 真正操作窗口的发送动作统一在 start() 的轮询线程里做
        # （wechatauto 的 GUI 句柄有线程亲和性，跨线程调用会“假成功”）。
        self._outbox = queue.Queue(maxsize=queue_size)
        self.task_ttl_seconds = task_ttl_seconds
        self.state_dir = Path(state_dir) if state_dir else None
        self._state_path = None
        self._saved_cursors: dict = {}
        self._cursor_lock = threading.RLock()
        self._local = threading.local()
        self._db_factory = None
        self._downloader_factory = None
        self._media_workers = media_workers
        self._inbox_slots = threading.BoundedSemaphore(queue_size)
        self._inbox_capacity = queue_size
        self._inbox_guard = threading.Lock()
        self._inbox: dict[str, deque] = {}
        self._inbox_running: set[str] = set()
        self._media_pool = None
        self._receiver = None

    def _database(self):
        if self._db_factory is None:
            return self._db
        if not hasattr(self._local, "db"):
            self._local.db = self._db_factory()
        return self._local.db

    def _media_downloader(self):
        if self._downloader_factory is None:
            return self._downloader
        if not hasattr(self._local, "downloader"):
            self._local.downloader = self._downloader_factory(self._database(), save_dir=self.media_dir)
        return self._local.downloader

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        # 调低库的写动作节奏参数（用户已知情选择速度优先，风控风险相应上升）：
        # 1) WRITE_BURST=0：去掉"120s 内已写 N 次，冷却 45s"的突发限制（碎句连发会撞）；
        # 2) WRITE_GAP=1.0：两次写动作间隔从 natural 档的 2.5~6s 降到 1~2s。
        os.environ.setdefault("WECHATAUTO_WRITE_BURST", "0")
        os.environ.setdefault("WECHATAUTO_WRITE_GAP", "1.0")
        from wechatauto import WeChat, WeChatDB  # 延迟导入

        self._db = WeChatDB()
        self._db_factory = WeChatDB
        self._local.db = self._db
        info = self._db.get_self_info() or {}
        self._self_wxid = str(info.get("username") or "")
        self._self_name = str(info.get("nick_name") or "")
        log.info("已连接微信（账号：%s / %s）", self._self_name or "未知", self._self_wxid or "?")
        if self.expected_self_name and self._self_name != self.expected_self_name:
            raise RuntimeError(
                f"当前微信登录的是「{self._self_name or '未知'}」，"
                f"但配置要求机器人使用「{self.expected_self_name}」。\n"
                "为防止主号自动发言，已拒绝启动。\n"
                "请确认：小号已登录微信客户端，且 config.yaml 的 wechat.self_name 填的是这个小号的昵称。"
            )
        if callable(getattr(self, "on_self_wxid", None)):
            self.on_self_wxid(self._self_wxid)
        if self.state_dir:
            account = hashlib.sha256(self._self_wxid.encode()).hexdigest()[:16]
            self._state_path = self.state_dir / f"receive-{account}.json"
            if self._state_path.exists():
                try:
                    self._saved_cursors = json.loads(self._state_path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    log.warning("消息游标损坏，将按启动策略重新初始化")

        self._group_id = ""
        for gname in self.group_names:
            try:
                gid = self._resolve_group_id(gname)
            except RuntimeError as exc:
                log.warning("跳过群「%s」：%s", gname, exc)
                continue
            state: dict[str, Any] = {"id": gid, "last_id": 0, "members": {}, "members_at": 0.0}
            self._groups[gname] = state
            if not self._group_id:
                self._group_id = gid
            log.info("监听群「%s」 id=%s", gname, gid)
            self._refresh_members(gname, force=True)
            # 初始化游标：默认跳过历史消息，只回启动之后的新消息
            self._initialize_cursor(state, gid)

        for pname in self.private_names:
            try:
                pid = self._resolve_private_id(pname)
            except RuntimeError as exc:
                log.warning("跳过私聊「%s」：%s", pname, exc)
                continue
            state = {"id": pid, "last_id": 0}
            self._privates[pname] = state
            log.info("监听私聊「%s」 id=%s", pname, pid)
            self._initialize_cursor(state, pid)

        if not self._groups and not self._privates:
            raise RuntimeError(
                "配置里的群/私聊一个都找不到。请检查 config.yaml 的 wechat.groups / wechat.private_chats"
                "（或 group_name）是否和微信里完全一致。"
            )

        self._wx = WeChat()
        self._patch_gui_focus()

        if self.vision is not None or self.asr is not None or self.file_summarizer is not None:
            try:
                from wechatauto import MediaDownloader
                self._downloader_factory = MediaDownloader

                Path(self.media_dir).mkdir(parents=True, exist_ok=True)
                self._downloader = MediaDownloader(self._db, save_dir=self.media_dir)
                log.info(
                    "多媒体解析已启用：识图=%s 语音=%s 文件=%s",
                    "开" if self.vision else "关",
                    "开" if self.asr else "关",
                    "开" if self.file_summarizer else "关",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("初始化图片/语音下载器失败，多媒体解析不可用：%s", exc)

        if getattr(self, "sticker_store", None) is not None:
            log.info("表情库已启用：会记录对方常发的表情包，主动发时复用同款")

        self._media_pool = ThreadPoolExecutor(max_workers=self._media_workers, thread_name_prefix="media")
        self._receiver = threading.Thread(target=self._receive_loop, name="wechat-receive", daemon=True)
        self._receiver.start()
        try:
            while not self._stop.is_set():
                self._drain_outbox()
                self._stop.wait(0.1)
        finally:
            self.stop()

    def _patch_gui_focus(self) -> None:
        """连续给同一会话发附件时跳过"点击输入框聚焦"（省光标移动+0.6s 等待）。

        附件粘贴前库每次都会物理点击输入框。这里包一层：UIA 确认微信输入框
        仍持有键盘焦点（即上次发送后没人动过焦点）就直接复用，不点；焦点
        丢了、换了会话、或探测失败，一律回到真实点击——宁可多花 0.6 秒，
        也不能把粘贴发到别的窗口。发送本就全部经出站队列单线程串行，
        同会话连发（文字→表情）天然命中复用分支。
        """
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            return
        real_focus = gui.focus_input

        def focus_input(box=None):
            try:
                uia = gui._get_uia()
                edit = uia._chat_input() if uia is not None else None
                if edit is not None and getattr(edit, "HasKeyboardFocus", False):
                    if box is None:
                        box = gui.get_input_box()
                    return bool(box)
            except Exception:  # noqa: BLE001
                pass
            return real_focus(box)

        gui.focus_input = focus_input

    def stop(self) -> None:
        self._stop.set()
        while True:
            try:
                _, _, _, _, receipt = self._outbox.get_nowait()
                if not receipt.done():
                    receipt.set_result(DeliveryResult("expired", "机器人已停止"))
            except queue.Empty:
                break
        if self._receiver and self._receiver is not threading.current_thread():
            self._receiver.join(timeout=2)
        if self._media_pool:
            self._media_pool.shutdown(wait=False, cancel_futures=True)

    def _receive_loop(self):
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._poll_once()
                backoff = 2.0
                self._stop.wait(self.poll_seconds)
            except Exception:
                log.exception("读取微信消息失败，%.0f 秒后重试", backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)

    # ------------------------------------------------------------------ 发送
    def send_text(self, room_id: str, text: str, at_wxid: Optional[str] = None) -> None:
        """只入队；真正发送在轮询线程里执行（见 _drain_outbox）。"""
        text = (text or "").strip()
        if not text:
            return
        at = at_wxid if (self.reply_with_at and at_wxid) else None
        return self._enqueue_send(room_id, "text", text, at)

    def send_file(self, room_id: str, path: str) -> None:
        """发文件/图片（表情包 GIF 也走这里）。"""
        if not path:
            return
        return self._enqueue_send(room_id, "file", str(path), None)

    def poke(self, room_id: str) -> None:
        """戳一戳（拍一拍）。"""
        return self._enqueue_send(room_id, "poke", None, None)

    def _enqueue_send(self, room_id, kind, payload, at):
        receipt = SendReceipt()
        if self._stop.is_set():
            receipt.set_result(DeliveryResult("expired", "机器人已停止"))
            return receipt
        try:
            self._outbox.put_nowait((room_id, kind, payload, at, receipt))
        except queue.Full:
            receipt.set_result(DeliveryResult("failed", "发送队列已满"))
        return receipt

    def _drain_outbox(self, grace: float = 0.8) -> None:
        """把发送队列发完再还桌面：队列空后等 grace 秒，仍无新任务才还原窗口。

        文字回复后紧跟的表情包会落进同一轮前台占用（同会话不二次跳转），
        也避免批与批之间反复抢占/释放前台造成的等待。
        """
        if self._outbox.empty():
            return
        try:
            while not self._stop.is_set():
                try:
                    room_id, kind, payload, at, receipt = self._outbox.get_nowait()
                except queue.Empty:
                    if self._stop.wait(grace) or self._outbox.empty():
                        return
                    continue
                if not receipt.set_running_or_notify_cancel():
                    continue
                age = time.monotonic() - receipt.created_at
                try:
                    if self._stop.is_set() or age > self.task_ttl_seconds:
                        result = DeliveryResult("expired", "发送任务过期")
                    elif kind == "file":
                        result = self._send_file_now(room_id, str(payload))
                    elif kind == "poke":
                        result = self._send_poke_now(room_id)
                    else:
                        result = self._send_now(room_id, str(payload), at)
                except Exception as exc:
                    result = DeliveryResult("unknown", str(exc))
                result = result or DeliveryResult("unknown", "发送接口未返回确认")
                receipt.set_result(DeliveryResult(result.status, result.detail, age))
                self._stop.wait(0.4)
        finally:
            self._restore_desktop()

    def _send_file_now(self, room_id: str, path: str) -> None:
        if self._wx is None:
            log.warning("微信尚未连接，跳过发送文件：%s", path)
            return
        chat = self._name_for_room(room_id)
        gui = getattr(self._wx, "_gui", None)
        suffix = Path(path).suffix.lower()
        try:
            # 图片/GIF 走 send_image（剪贴板粘贴，微信按图片消息插入），
            # 这样表情包在聊天里显示为动图，而不是一个 .gif 文件。
            if gui is not None and suffix in (".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                # 图片走粘贴路线，粘贴是否落框已有 UIA 回读校验；DB 轮询回执
                # （图片最长 6s）会拖慢同批后续发送，这里不再等库确认
                result = gui.send_image(path, who=chat, verify=False)
            else:
                result = self._wx.SendFiles([path], who=chat)
            # WxResponse 失败时 __bool__ 为 False，不能写 `result or {}`，
            # 否则失败对象会被空字典吞掉，真实原因只剩下 "None"。
            status = str(result.get("status") or "") if isinstance(result, dict) else ""
            detail = str(result.get("message") or "") if isinstance(result, dict) else ""
            if "成功" in status:
                label = "私聊" if self._is_private_room(room_id) else "群"
                kind = "表情/图片" if suffix in (".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp") else "文件"
                log.info("→ 已发送%s到%s「%s」：%s", kind, label, chat, Path(path).name)
            else:
                log.warning("发送文件未确认：%s（%s）", detail or status or result, Path(path).name)
            verified_image = gui is not None and suffix in (".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp")
            return DeliveryResult("confirmed" if verified_image and "成功" in status else "unknown", status)
        except Exception as exc:  # noqa: BLE001
            log.warning("发送文件异常：%s", exc)
            return DeliveryResult("unknown", str(exc))

    def _send_poke_now(self, room_id: str) -> None:
        if self._wx is None:
            return
        chat = self._name_for_room(room_id)
        try:
            result = self._wx.Poke(who=chat)
            status = str(result.get("status") or "") if isinstance(result, dict) else ""
            if "成功" in status:
                log.info("→ 已戳了戳「%s」", chat)
            else:
                log.warning("戳一戳失败（%s）：%s", chat, result.get("message") if isinstance(result, dict) else result)
            return DeliveryResult("confirmed" if "成功" in status else "unknown", status)
        except Exception as exc:  # noqa: BLE001
            log.warning("戳一戳异常：%s", exc)
            return DeliveryResult("unknown", str(exc))

    def _send_now(self, room_id: str, text: str, at: Optional[str]) -> None:
        if self._wx is None:
            log.warning("微信尚未连接，跳过发送：%s", text[:40])
            return
        group_name = self._name_for_room(room_id)
        gui = getattr(self._wx, "_gui", None)
        # The dependency already retries UI steps and verifies DB landing. An
        # unconfirmed send must not be sent a second time by this layer.
        for attempt in (1,):
            try:
                member = self._member_for_at(room_id, at) if at else None
                if gui is not None and member:
                    result = gui.at_member(member, text, who=group_name, verify=True)
                elif gui is not None:
                    # verify=True：发送后回读数据库确认，避免"接口报成功但没发出去"
                    result = gui.send_msg(text, who=group_name, verify=True)
                else:
                    result = self._wx.SendMsg(text, who=group_name, **({"at": member} if member else {}))
                status = str(result.get("status") or "") if isinstance(result, dict) else ""
                detail = str(result.get("message") or "") if isinstance(result, dict) else ""
                if "成功" in status:
                    label = "私聊" if self._is_private_room(room_id) else "群"
                    log.info("→ 已发送到%s「%s」：%s", label, group_name, text.replace("\n", " ")[:60])
                    return DeliveryResult("confirmed" if gui is not None else "unknown", "接口报告成功")
                log.warning("发送未确认（第 %d 次）：%s", attempt, detail or status or result)
            except Exception as exc:  # noqa: BLE001
                log.warning("发送消息异常（第 %d 次）：%s", attempt, exc)
        return DeliveryResult("unknown", "发送未确认；为避免重复消息，不自动重发")

    def _member_for_at(self, room_id, at):
        for state in self._groups.values():
            if state.get("id") == room_id:
                return (state.get("members") or {}).get(at) or at
        return None

    def _restore_desktop(self) -> None:
        """把发送时被最小化的窗口还原，避免占用你的桌面。"""
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            return
        try:
            restored = gui.release_foreground(with_blockers=True)
            if restored:
                log.info("已还原被临时最小化的窗口 %d 个", restored)
        except Exception as exc:  # noqa: BLE001
            log.debug("还原窗口失败（可忽略）：%s", exc)

    def _verify_sent(self, room_id: str, text: str) -> bool:
        gid = self._id_for_room(room_id)
        try:
            rows = self._db.get_messages(gid, limit=5) or []
        except Exception:  # noqa: BLE001
            return True  # 查不了就不当成失败
        needle = text.strip()[:30]
        for row in rows:
            if str(row.get("sender_username") or "") != self._self_wxid:
                continue
            if needle and needle in str(row.get("content") or ""):
                return True
        return False

    def _name_for_room(self, room_id: str) -> str:
        for gname, state in self._groups.items():
            if gname == room_id or state.get("id") == room_id:
                return gname
        for pname, state in self._privates.items():
            if pname == room_id or state.get("id") == room_id:
                return pname
        if room_id == self._group_id and self.group_name:
            return self.group_name
        raise ValueError(f"未配置的会话：{room_id}")

    def _id_for_room(self, room_id: str) -> str:
        for gname, state in self._groups.items():
            if gname == room_id or state.get("id") == room_id:
                return str(state.get("id") or "")
        for pname, state in self._privates.items():
            if pname == room_id or state.get("id") == room_id:
                return str(state.get("id") or "")
        return self._group_id

    def _is_private_room(self, room_id: str) -> bool:
        return any(state.get("id") == room_id or name == room_id for name, state in self._privates.items())

    def get_self_wxid(self) -> str:
        return self._self_wxid

    def describe(self) -> dict:
        return {
            "adapter": self.name,
            "groups": [
                {"name": name, "id": state.get("id")} for name, state in self._groups.items()
            ],
            "private_chats": [
                {"name": name, "wxid": state.get("id")} for name, state in self._privates.items()
            ],
            "poll_seconds": self.poll_seconds,
            "send_queue": self._outbox.qsize(),
            "receive_queue": sum(len(q) for q in self._inbox.values()),
        }

    # ------------------------------------------------------------------ 内部
    def _resolve_group_id(self, group_name: str) -> str:
        groups = self._db.get_groups() or []
        matches = [g for g in groups if str(g.get("name", "")).strip() == group_name]
        if len(matches) == 1:
            return str(matches[0]["username"])
        if len(matches) > 1:
            raise RuntimeError(f"存在多个同名群「{group_name}」，请先修改群名以避免误发")
        raise RuntimeError(
            f"在微信里找不到群「{group_name}」。请确认群名与微信聊天列表里显示的完全一致（含空格/符号）。"
        )

    def _resolve_private_id(self, name: str) -> str:
        """把私聊对象的昵称/备注解析成 wxid。"""
        candidates: list[dict] = []
        try:
            candidates = self._db.search_contact(name) or []
        except Exception as exc:  # noqa: BLE001
            log.debug("search_contact 失败：%s", exc)
        for c in candidates:
            nick = str(c.get("nick_name") or "").strip()
            remark = str(c.get("remark") or "").strip()
            if name in (nick, remark):
                wxid = str(c.get("username") or "").strip()
                if wxid:
                    return wxid
        try:
            wxid = self._db.username_by_nickname(name)
            if wxid:
                return str(wxid)
        except Exception as exc:  # noqa: BLE001
            log.debug("username_by_nickname 失败：%s", exc)
        raise RuntimeError(f"在微信通讯录里找不到「{name}」，请确认昵称/备注和配置一致")

    def _refresh_members(self, group_name: str, force: bool = False) -> None:
        state = self._groups.get(group_name)
        if not state:
            return
        now = time.time()
        if not force and now - float(state.get("members_at") or 0) < 300:
            return
        try:
            members = self._database().get_group_members(str(state["id"])) or []
        except Exception:  # noqa: BLE001
            members = []
        mapping: dict[str, str] = {}
        for m in members:
            wxid = str(m.get("username") or "")
            name = str(m.get("remark") or m.get("nick_name") or "").strip()
            if wxid and name:
                mapping[wxid] = name
        state["members"] = mapping
        state["members_at"] = now
        if state.get("id") == self._group_id:
            # 兼容旧字段
            self._member_names = mapping
            self._name_cache_at = now

    @staticmethod
    def _row_seq(row):
        return int(row.get("sort_seq") or row.get("local_id") or 0)

    @staticmethod
    def _row_id(row):
        return f"{row.get('shard', '')}:{row.get('local_id', '')}"

    def _initialize_cursor(self, state, room_id):
        saved = self._saved_cursors.get(room_id)
        if saved:
            state.update(last_seq=saved["seq"], boundary=list(saved["ids"]))
        elif self.skip_history:
            rows = self._db.get_messages(room_id, limit=50) or []
            seq = max((self._row_seq(r) for r in rows), default=0)
            state.update(last_seq=seq, boundary=[self._row_id(r) for r in rows if self._row_seq(r) == seq])
            self._saved_cursors[room_id] = {"seq": seq, "ids": state["boundary"]}
        else:
            state.update(last_seq=0, boundary=[])
        state["last_id"] = state["last_seq"]

    def _poll_once(self) -> None:
        for private, targets in ((False, self._groups), (True, self._privates)):
            for name, state in list(targets.items()):
                try:
                    self._poll_room(name, state, private)
                except Exception:
                    log.exception("读取会话失败（其他会话继续）：%s", name)

    def _poll_room(self, name, state, private):
        room_id = str(state.get("id") or "")
        floor = state.get("last_seq", int(state.get("last_id") or 0))
        boundary = set(state.get("boundary", []))
        collected = {}
        previous_page = None
        offset = 0
        while not self._stop.is_set():
            rows = self._database().get_messages(room_id, limit=50, offset=offset) or []
            signature = tuple(self._row_id(r) for r in rows)
            if not rows or signature == previous_page:
                break
            previous_page = signature
            for row in rows:
                seq, identity = self._row_seq(row), self._row_id(row)
                if seq > floor or (seq == floor and identity not in boundary):
                    collected[identity] = row
            if len(collected) > self._inbox_capacity:
                oldest = sorted(collected.values(), key=lambda r: (self._row_seq(r), self._row_id(r)))[:self._inbox_capacity]
                collected = {self._row_id(r): r for r in oldest}
            if len(rows) < 50 or any(self._row_seq(r) < floor for r in rows):
                break
            offset += 50
        for row in sorted(collected.values(), key=lambda r: (self._row_seq(r), self._row_id(r))):
            if not self._enqueue_incoming(row, name, state, private):
                break
            seq = self._row_seq(row)
            if seq > floor:
                boundary, floor = set(), seq
            boundary.add(self._row_id(row))
            state.update(last_seq=floor, last_id=floor, boundary=list(boundary))

    def _enqueue_incoming(self, row, name, state, private):
        if self._media_pool is None:
            self._deliver_row(row, name, state, private)
            return True
        if not self._inbox_slots.acquire(blocking=False):
            return False
        room = str(state["id"])
        with self._inbox_guard:
            self._inbox.setdefault(room, deque()).append((row, name, state, private, time.monotonic()))
            if room not in self._inbox_running:
                self._inbox_running.add(room)
                self._media_pool.submit(self._consume_room, room)
        return True

    def _consume_room(self, room):
        while not self._stop.is_set():
            with self._inbox_guard:
                items = self._inbox.get(room)
                if not items:
                    self._inbox.pop(room, None)
                    self._inbox_running.discard(room)
                    return
                row, name, state, private, queued = items.popleft()
            try:
                while not self._stop.is_set():
                    if time.monotonic() - queued > self.task_ttl_seconds:
                        log.warning("接收任务过期：会话=%s 消息=%s", room, self._row_id(row))
                        self._commit_cursor(room, row)
                        break
                    try:
                        self._deliver_row(row, name, state, private)
                        break
                    except Exception:
                        log.exception("消息处理失败，稍后重试：%s", room)
                        self._stop.wait(2)
            finally:
                self._inbox_slots.release()

    def _deliver_row(self, row, name, state, private):
        if private:
            self._dispatch_private(row, name, str(state["id"]))
        else:
            self._dispatch(row, name)
        self._commit_cursor(str(state["id"]), row)

    def _commit_cursor(self, room, row):
        with self._cursor_lock:
            cursor = self._saved_cursors.setdefault(room, {"seq": 0, "ids": []})
            seq = self._row_seq(row)
            if seq > cursor["seq"]:
                cursor.update(seq=seq, ids=[])
            if seq == cursor["seq"] and self._row_id(row) not in cursor["ids"]:
                cursor["ids"].append(self._row_id(row))
            if self._state_path:
                atomic_json(self._state_path, self._saved_cursors)

    def _dispatch_private(self, row: dict, contact: str, wxid: str) -> None:
        sender_wxid = str(row.get("sender_username") or "")
        if sender_wxid and sender_wxid == self._self_wxid:
            return  # 自己发的
        text = self._normalise_content(row, wxid, contact)
        if not text:
            return
        msg = IncomingMessage(
            room_id=wxid,
            sender_id=wxid,
            sender_name=contact,
            text=text,
            room_name=contact,
            msg_id=f"wechatauto-private-{wxid}-{self._row_id(row)}",
            ts=float(row.get("create_time") or time.time()),
            is_group=False,
        )
        if self._on_message:
            try:
                self._on_message(msg)
            except Exception:  # noqa: BLE001
                log.exception("私聊消息回调异常（已跳过）")
                raise

    def _dispatch(self, row: dict, group_name: str) -> None:
        state = self._groups.get(group_name) or {}
        members: dict[str, str] = state.get("members") or {}
        sender_wxid = str(row.get("sender_username") or "")
        if sender_wxid and sender_wxid == self._self_wxid:
            return  # 自己发的
        text = self._normalise_content(row, str(state.get("id") or self._group_id), group_name)
        if not text:
            return
        self._refresh_members(group_name)
        members = state.get("members") or members
        sender_name = members.get(sender_wxid) or sender_wxid or "群友"
        msg = IncomingMessage(
            room_id=str(state.get("id") or self._group_id),
            sender_id=sender_wxid or sender_name,
            sender_name=sender_name,
            text=text,
            room_name=group_name,
            msg_id=f"wechatauto-{state.get('id')}-{self._row_id(row)}",
            ts=float(row.get("create_time") or time.time()),
            is_group=True,
            at_wxids=self._extract_mentions(row, text),
        )
        if self._on_message:
            try:
                self._on_message(msg)
            except Exception:  # noqa: BLE001
                log.exception("消息回调异常（已跳过）")
                raise

    def _extract_mentions(self, row, text):
        raw = str(row.get("msg_source") or row.get("source") or "") + str(row.get("content") or "")
        found = list(row.get("at_wxids") or [])
        for match in re.findall(r"<atuserlist>(.*?)</atuserlist>", raw, re.S):
            found.extend(x.strip() for x in match.replace("<![CDATA[", "").replace("]]>", "").split(",") if x.strip())
        # get_messages() in the installed library omits msg_source. Match the
        # actual logged-in nickname at a mention boundary as its fallback.
        if self._self_name and re.search(r"@" + re.escape(self._self_name) + r"(?=$|[\s\u2005,，:：])", text):
            found.append(self._self_wxid)
        return list(dict.fromkeys(found))

    def _normalise_content(self, row: dict, room_id: str = "", room_name: str = "") -> str:
        msg_type = str(row.get("type") or "")
        if msg_type in ("系统", "系统消息", "10000", "10002", "撤回"):
            return ""
        raw = str(row.get("content") or "").strip()
        room_id = room_id or self._group_id
        if msg_type in ("", "文本", "text", "1"):
            # 群消息 content 里可能带 "<wxid>:\n" 前缀，去掉
            return _SENDER_PREFIX_RE.sub("", raw).strip()
        if msg_type == "图片":
            return self._describe_image(row, room_id)
        if msg_type in ("动画表情", "表情"):
            return self._describe_sticker(row, raw, room_name)
        if msg_type == "语音":
            return self._describe_voice(row, room_id)
        if msg_type == "文件/链接/卡片":
            return self._describe_card(row, raw, room_id)
        return _TYPE_LABELS.get(msg_type, f"[{msg_type}]")

    # ------------------------------------------------------------------ 识图
    def _describe_image(self, row: dict, room_id: str = "") -> str:
        """下载并解密图片，交给视觉模型描述。失败则退回占位符。"""
        downloader = self._media_downloader()
        if self.vision is None or downloader is None:
            return "[图片]"
        try:
            local_id = int(row.get("local_id", 0))
        except (TypeError, ValueError):
            return "[图片]"
        try:
            path = downloader.download_image(
                room_id or self._group_id, local_id, save_dir=self.media_dir,
                **({"shard": row["shard"]} if row.get("shard") else {})
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("下载图片失败（local_id=%s）：%s", local_id, exc)
            return "[图片]"
        if not path or not Path(path).exists():
            return "[图片]"
        desc = self.vision.describe(path, kind="image")
        return f"[图片] {desc}" if desc else "[图片]"

    def _describe_sticker(self, row: dict, raw: str, room_name: str = "") -> str:
        """表情包：优先从消息里的下载地址取标准 GIF；取不到就放弃（不把加密缓存喂给模型）。
        成功时顺便登记进该群/私聊的表情库，供主动发表情包时复用。"""
        if self.vision is None:
            return "[表情包]"
        match = re.search(r'md5="([0-9a-fA-F]{32})"', raw or "")
        if not match:
            return "[表情包]"
        md5 = match.group(1).lower()

        # 之前可能按真实格式存成 .gif/.png/.jpg，命中任一扩展名就算已缓存
        path = None
        for ext in _EMOJI_EXTS:
            cand = Path(self.media_dir) / f"emoji_{md5}{ext}"
            if cand.exists() and _is_image_file(cand):
                path = cand
                break
        if path is None:
            # 依次尝试消息里可能出现过的下载地址（cdnurl 是明文图，最优先）
            urls: list[str] = []
            for attr in ("cdnurl", "cdnthumburl", "thumburl", "cdnbigurl"):
                for m in re.finditer(rf'{attr}\s*=\s*"([^"]+)"', raw or ""):
                    url = m.group(1).replace("&amp;", "&").strip()
                    if url.startswith("http") and url not in urls:
                        urls.append(url)
            if urls:
                # 快速尝试一次（只走首选候选域名）；慢网络一分钟起步，不能同步干等
                dest = Path(self.media_dir) / f"emoji_{md5}.gif"
                if _download_file(_sticker_url_candidates(urls[0])[0], dest, timeout=15.0):
                    _retype_image(dest)
                    for ext in _EMOJI_EXTS:
                        cand = dest.with_suffix(ext)
                        if cand.exists() and _is_image_file(cand):
                            path = cand
                            break
            if path is None:
                cached = self._find_sticker_file(md5)  # 只返回能识别的图片
                if cached:
                    path = cached
            if path is None and urls:
                # 交给后台慢慢下并缓存；同一张表情下次出现即可识别
                with _STICKER_DL_LOCK:
                    inflight = md5 in _STICKER_DL_INFLIGHT
                    if not inflight:
                        _STICKER_DL_INFLIGHT.add(md5)
                if not inflight:
                    _DOWNLOAD_POOL.submit(_sticker_bg_download, md5, urls, str(self.media_dir))
                log.debug("表情包 %s 转后台下载，本次先用占位符", md5[:8])
                return "[表情包]"
            if path is None:
                return "[表情包]"

        if self.sticker_store is not None and room_name:
            try:
                self.sticker_store.record(
                    room_name, md5, path, str(row.get("sender_username") or "")
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("登记表情库失败：%s", exc)

        desc = self.vision.describe(path, kind="sticker")
        return f"[表情包] {desc}" if desc else "[表情包]"

    # ------------------------------------------------------------------ 语音
    def _describe_voice(self, row: dict, room_id: str = "") -> str:
        """语音：下载 SILK → 本地 whisper 转文字。"""
        downloader = self._media_downloader()
        if self.asr is None or downloader is None:
            return "[语音]"
        try:
            local_id = int(row.get("local_id", 0))
        except (TypeError, ValueError):
            return "[语音]"
        try:
            voice_dir = Path(self.media_dir) / "voice"
            voice_dir.mkdir(parents=True, exist_ok=True)
            path = downloader.download_voice(room_id or self._group_id, local_id, save_dir=str(voice_dir),
                                             **({"shard": row["shard"]} if row.get("shard") else {}))
        except Exception as exc:  # noqa: BLE001
            log.warning("下载语音失败（local_id=%s）：%s", local_id, exc)
            return "[语音]"
        if not path or not Path(path).exists():
            return "[语音]"
        text = self.asr.transcribe_silk(path)
        return f"[语音] {text}" if text else "[语音]"

    # ------------------------------------------------------------------ 文件/链接/卡片
    _CARD_LABELS = {
        3: "音乐",
        4: "链接",
        5: "链接",
        19: "合并转发",
        33: "小程序",
        36: "小程序",
        57: "引用消息",
        62: "拍一拍",
        2001: "红包",
    }

    def _describe_card(self, row: dict, raw: str, room_id: str = "") -> str:
        """appmsg（文件/链接/引用/拍一拍/小程序…）解析成可读文本。"""
        title_match = re.search(r"<title>(.*?)</title>", raw or "", re.S)
        title = html.unescape(title_match.group(1)).strip() if title_match else ""
        type_match = re.search(r"<type>(\d+)</type>", raw or "")
        try:
            app_type = int(type_match.group(1)) if type_match else 0
        except ValueError:
            app_type = 0

        if app_type == 6:  # 文件
            fallback = f"[文件] {title}" if title else "[文件]"
            downloader = self._media_downloader()
            if self.file_summarizer is None or downloader is None:
                return fallback
            try:
                local_id = int(row.get("local_id", 0))
            except (TypeError, ValueError):
                return fallback
            try:
                Path(self.files_dir).mkdir(parents=True, exist_ok=True)
                path = downloader.download_media(room_id or self._group_id, local_id, save_dir=self.files_dir)
            except Exception as exc:  # noqa: BLE001
                log.warning("下载文件失败（local_id=%s）：%s", local_id, exc)
                return fallback
            if not path or not Path(path).exists():
                return fallback
            desc = self.file_summarizer.summarize(path, display_name=title)
            return f"[文件] {desc}" if desc else fallback

        label = self._CARD_LABELS.get(app_type, "卡片")
        return f"[{label}] {title}" if title else f"[{label}]"

    def _find_sticker_file(self, md5: str) -> Optional[Path]:
        """微信 4.x 的表情缓存路径：<账号目录>/cache/<年-月>/Emoticon/<md5前2位>/<md5>。

        注意：缓存文件经常是加密的（wxgf/加密 dat），必须跳过非图片文件，
        否则会喂给 PIL 报 "cannot identify image file"。
        """
        account_dir = getattr(self._db, "account_dir", None)
        if not account_dir:
            return None
        base = Path(str(account_dir))
        patterns = [
            f"cache/*/Emoticon/{md5[:2]}/{md5}",
            f"cache/*/Emoticon/*/{md5}",
            f"cache/*/emoticon/{md5[:2]}/{md5}",
        ]
        for pattern in patterns:
            try:
                for p in base.glob(pattern):
                    if p.is_file() and _is_image_file(p):
                        return p
            except Exception:  # noqa: BLE001
                continue
        return None
