"""基于 WeChatFerry（WCF）的真实微信适配器。

设计要点
========
1. **延迟导入 wcferry**：只有 ``start()`` 里才 ``from wcferry import Wcf``。
   这样即使没装 wcferry，``python -m src.main check / mock / web`` 依然能跑。
2. **阻塞式 start()**：连接微信 → 等待登录 → 开启消息接收 → 循环 ``get_msg()``，
   把 WCF 的 ``WxMsg`` 归一化成 :class:`src.models.IncomingMessage` 后交给
   ``self._on_message``（即 ``Bot.on_message``）。
3. **失败可恢复**：``start()`` 外层带指数退避重连；单条消息转换失败只丢这一条。
4. **send_text 的 @ 实现**（已核实，见 docs/WECHAT_SETUP.md）：
   WCF 原生发送接口要求「正文里 @ 的数量 == aters 里 wxid 的数量」，
   并且正文里要写成 ``@<群昵称>``。官方示例 WeChatRobot 的 ``sendTextMsg`` 就是
   ``f" @{wcf.get_alias_in_chatroom(wxid, receiver)}\n\n{msg}"`` + ``aters=wxid``。
   所以这里会：先查群昵称 → 在正文最前面拼 ``@群昵称`` → 正文里其余 ASCII ``@``
   全部换成全角 ``＠``（防止占位符数量和 aters 对不上）。
   取不到昵称或带 @ 发送失败时，自动降级成不带 aters 的普通文本（昵称仍写在正文里）。

关于 WCF 的一些限制（别在这儿浪费时间排查）
==========================================
* WCF 客户端 ``Wcf.__init__`` 在本地模式下会调用 ``sdk.dll`` 的 ``WxInitSDK`` 注入微信
  进程；失败时它内部直接 ``os._exit(-1)``，Python 层接不住。因此：
  **先跑 ``scripts/check_wcf.py`` 确认微信版本/权限/进程都 OK，再启动机器人。**
* ``Wcf(block=True)`` 会一直等到微信登录成功；这期间 ``stop()`` 无法打断，
  只能等它登录完成，或者直接关掉进程。
* WCF 的 RPC 是同步一问一答（pynng Pair1 套接字），这里统一用一把 RLock 串行化
  所有 RPC 调用（收消息线程 + 回复线程会并发访问），避免请求/响应错位。
* 收消息线程内部出错可能 ``sys.exit``，本模块会把 ``SystemExit`` 也当作连接异常
  捕获，交给外层退避重连；另外用「空闲心跳」每 60s 调一次 ``is_login()``
  来发现「微信被关掉/退出登录」这种静默断线。
"""
from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET
from queue import Empty
from typing import Any, Callable, Optional

from .adapter import WeChatAdapter
from .models import IncomingMessage

log = logging.getLogger("dsbot.wcf")

__all__ = ["WcfAdapter", "WcfConnectionError"]


class WcfConnectionError(RuntimeError):
    """与 WeChatFerry RPC 的连接/调用异常（需要重连，而不是丢一条消息了事）。"""


# ---------------------------------------------------------------------------
# 微信消息类型 -> 占位文本
#
# 来源：.venv/Lib/site-packages/wcferry 对应的 WCF 源码
#       WeChatFerry/spy/message_handler.cpp 里的 GetMsgTypes()
#       （0x01 文字、0x03 图片、0x22 语音、0x2B 视频、0x3E 小视频、
#         0x2F 表情、0x31 链接/文件/引用等 appmsg、0x1000031 链接 ...）
# 群聊消息里带 @ 时，实际被 @ 的 wxid 在 XML 的 <atuserlist> 里。
# ---------------------------------------------------------------------------
_MESSAGE_TYPE_LABELS: dict[int, str] = {
    0x00: "[朋友圈]",
    0x03: "[图片]",
    0x22: "[语音]",
    0x25: "[好友申请]",
    0x28: "[好友推荐]",
    0x2A: "[名片]",
    0x2B: "[视频]",
    0x2F: "[表情]",              # 石头剪刀布 / 表情图片
    0x30: "[位置]",
    0x32: "[系统消息]",          # VOIPMSG
    0x33: "[系统消息]",          # 微信初始化
    0x34: "[系统消息]",          # VOIPNOTIFY
    0x35: "[系统消息]",          # VOIPINVITE
    0x3E: "[视频]",              # 小视频
    0x42: "[红包]",
    0x270F: "[系统消息]",        # SYSNOTICE
    0x2710: "[系统消息]",        # 红包 / 系统消息（>=10000）
    0x2712: "[撤回消息]",
    0x100031: "[表情]",          # 搜狗表情
    0x1000031: "[链接/卡片]",    # 链接
    0x1A000031: "[红包]",
    0x20010031: "[红包]",
    0x2D000031: "[视频]",        # 视频号视频
    0x2E000031: "[链接/卡片]",   # 视频号名片
    0x31000031: "[引用消息]",
    0x37000031: "[拍一拍]",
    0x3A000031: "[链接/卡片]",   # 视频号直播
    0x3A100031: "[链接/卡片]",   # 商品链接
    0x3A200031: "[链接/卡片]",   # 视频号直播
    0x3E000031: "[链接/卡片]",   # 音乐链接
    0x41000031: "[文件]",
}

# 0x31 是一个「大杂烩」类型：文字/文件/链接/小程序/引用都在里面，
# 具体要看消息 XML 里 <appmsg><type> 的数值。
_APPMSG_BASE_TYPES = frozenset({0x31})
_APPMSG_TYPE_LABELS: dict[int, str] = {
    1: "[链接/卡片]",    # 文本型 appmsg
    3: "[链接/卡片]",    # 音乐
    4: "[链接/卡片]",    # 链接
    5: "[链接/卡片]",    # 链接
    6: "[文件]",
    8: "[表情]",
    19: "[合并转发]",
    33: "[小程序]",
    36: "[小程序]",
    51: "[视频]",        # 视频号
    57: "[引用消息]",
    62: "[视频]",
    63: "[链接/卡片]",   # 视频号直播
    74: "[文件]",
    2001: "[红包]",
}


class WcfAdapter(WeChatAdapter):
    """WeChatFerry 实现：小号登录微信，收群消息、发文本回复。"""

    name = "wechatferry"

    # 空闲多久没消息就调一次 is_login() 探活（秒）
    _IDLE_PING_SECONDS = 60.0
    # 昵称/群名缓存有效期（秒）
    _CACHE_TTL = 300.0
    # 重连最大退避（秒）
    _MAX_BACKOFF = 60.0

    def __init__(self, port: int = 10086, debug: bool = False) -> None:
        super().__init__()
        self.port = int(port) if port else 10086
        self.debug = bool(debug)

        # WCF 客户端对象，start() 成功后才有值；没连上时为 None。
        self._wcf: Any = None
        # 自己（小号）的 wxid，连接成功后从 WCF 拿。
        self.self_wxid: str = ""

        self._stop_event = threading.Event()
        # WCF 的 RPC 套接字是同步一问一答，必须串行化（收消息线程 + 回复线程并发）。
        self._rpc_lock = threading.RLock()

        # 缓存：避免每条消息都去查库（微群场景下完全够用）
        self._alias_cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._room_name_cache: dict[str, tuple[str, float]] = {}
        self._contact_name_cache: dict[str, tuple[str, float]] = {}

        # 可选钩子：连接成功拿到 self_wxid 时回调一次。
        # 核心 Bot 目前是在 start() 之前调 get_self_wxid()（那时还没连上），
        # 所以 trigger=mention 时建议在 config.yaml 里预先填 wechat.self_wxid；
        # 这个钩子留给外部需要时接线（不改核心模块）。
        self.on_self_wxid: Optional[Callable[[str], None]] = None

    # ==================================================================
    # 生命周期
    # ==================================================================
    def start(self) -> None:
        """阻塞启动：注入/连接微信 → 等登录 → 循环收消息，直到 stop()。

        wcferry 在这里才导入，保证没安装依赖时 check/mock/web 子命令照常可用。
        """
        try:
            from wcferry import Wcf  # noqa: F401  （延迟导入，见模块说明）
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "没有安装依赖 wcferry，无法启动真实微信。\n"
                "请先执行：.\\\\.venv\\\\Scripts\\\\python.exe -m pip install wcferry\n"
                "安装/微信版本要求见 docs/WECHAT_SETUP.md"
            ) from exc

        self._stop_event.clear()
        backoff = 2.0

        while not self._stop_event.is_set():
            if self._wcf is not None:      # 上一轮残留
                self._close_wcf()

            log.info("正在启动 WeChatFerry（端口 %d，debug=%s）…", self.port, self.debug)
            log.info("提示：需要微信已登录、以管理员身份运行、微信版本与 wcferry 匹配")
            try:
                # block=True：内部会等到微信登录成功才返回（可能一直阻塞）
                wcf = Wcf(port=self.port, debug=self.debug)
            except Exception as exc:  # noqa: BLE001
                # 注意：wcferry 多数初始化失败会直接 os._exit()，走不到这里；
                # 能走到这里的异常（如端口被占、依赖缺失）可以退避重试。
                log.exception("启动 WeChatFerry 失败：%s", exc)
                if self._wait(backoff):
                    break
                backoff = min(backoff * 2, self._MAX_BACKOFF)
                continue

            self._wcf = wcf
            backoff = 2.0
            try:
                # Wcf(block=True) 构造完成后已经登录，self_wxid 属性已就绪。
                self.self_wxid = str(getattr(wcf, "self_wxid", "") or "")
                if not self.self_wxid:
                    self.self_wxid = str(self._rpc("get_self_wxid") or "")
                log.info("微信已连接，登录账号 wxid=%s", self.self_wxid or "（未知）")
                self._notify_self_wxid()

                if not self._rpc("enable_receiving_msg"):
                    raise WcfConnectionError("enable_receiving_msg() 返回 False，开启收消息失败")

                log.info("开始接收微信群消息（Ctrl+C 退出）")
                self._receive_loop(wcf)
            except WcfConnectionError as exc:
                log.error("与微信的连接中断：%s", exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("接收消息循环异常退出：%s", exc)
            finally:
                self._close_wcf()

            if self._stop_event.is_set():
                break
            log.warning("%.0f 秒后尝试重连微信…", backoff)
            if self._wait(backoff):
                break
            backoff = min(backoff * 2, self._MAX_BACKOFF)

        log.info("WcfAdapter.start() 已退出")

    def stop(self) -> None:
        """通知 start() 退出，并关闭 WCF 连接（可重复调用）。"""
        self._stop_event.set()
        self._close_wcf()

    # ==================================================================
    # 发送
    # ==================================================================
    def send_text(self, room_id: str, text: str, at_wxid: Optional[str] = None) -> None:
        """发送文本；``at_wxid`` 非空时在群里 @ 这个人。

        实现细节（已核实，见 docs/WECHAT_SETUP.md「@ 的实现」）：
        WCF 原生 ``send_text(msg, receiver, aters)`` 的要求是
        「正文里 ``@`` 的个数 == aters 里 wxid 的个数」，且正文写成 ``@群昵称``。
        所以这里先在正文最前面拼 ``@群昵称``，再把正文里其它 ``@`` 换成全角 ``＠``。
        """
        body = (text or "").strip()
        if not body and not at_wxid:
            log.debug("空消息，跳过发送")
            return
        if not room_id:
            log.warning("send_text 缺少 room_id，跳过发送")
            return
        wcf = self._wcf
        if wcf is None:
            log.warning("微信尚未连接，跳过发送：%s", body[:40])
            return

        aters = ""
        outgoing = body
        if at_wxid:
            alias = self._get_alias(at_wxid, room_id)
            if alias:
                # 防御：正文里若还有 ASCII @，会让 WCF 的占位符数量对不上，统一换全角。
                safe_body = body.replace("@", "＠")
                outgoing = f"@{alias}\n{safe_body}" if safe_body else f"@{alias}"
                aters = at_wxid
            else:
                # 取不到群昵称就不带 aters，避免发出「裸 @」或 @ 错人。
                log.warning("取不到 %s 在群 %s 的昵称，本次不 @，改为普通文本", at_wxid, room_id)

        try:
            status = self._send_once(wcf, outgoing, room_id, aters)
        except BaseException as exc:  # noqa: BLE001  兜底：绝不能把回复线程/收消息循环搞崩
            log.error("发送消息失败：%s", exc)
            if aters:
                # 带 @ 的发送整体失败时，仍尝试降级为普通文本（正文里已有 "@昵称"）
                log.warning("带 @ 发送异常，尝试降级为普通文本重发")
                try:
                    self._send_once(wcf, outgoing, room_id, "")
                except BaseException as exc2:  # noqa: BLE001
                    log.error("降级重发也失败：%s", exc2)
            return

        if aters and status not in (0, None):
            # 带 @ 发送失败时降级：正文里已经有 "@昵称" 文本，直接当普通消息再发一次。
            log.warning("带 @ 发送失败（status=%s），降级为普通文本重发", status)
            try:
                self._send_once(wcf, outgoing, room_id, "")
            except BaseException as exc:  # noqa: BLE001
                log.error("降级重发也失败：%s", exc)
        elif status not in (0, None):
            log.warning("发送返回非 0 状态（status=%s），消息可能没发出去", status)

    def _send_once(self, wcf: Any, body: str, room_id: str, aters: str) -> Any:
        """执行一次底层发送；所有异常统一包装成 WcfConnectionError。"""
        with self._rpc_lock:
            try:
                if aters:
                    return wcf.send_text(body, room_id, aters)
                # at_wxid 为空时只传两个参数（不要传空字符串 aters）
                return wcf.send_text(body, room_id)
            except KeyboardInterrupt:
                raise
            except BaseException as exc:  # noqa: BLE001  包含 SystemExit
                raise WcfConnectionError(f"send_text 调用失败：{type(exc).__name__}: {exc}") from exc

    # ==================================================================
    # 信息
    # ==================================================================
    def get_self_wxid(self) -> str:
        """机器人小号的 wxid；未连接时返回空字符串。"""
        if self.self_wxid:
            return self.self_wxid
        if self._wcf is not None:
            try:
                self.self_wxid = str(self._rpc("get_self_wxid") or "")
            except WcfConnectionError as exc:
                log.debug("获取 self_wxid 失败：%s", exc)
        return self.self_wxid

    def describe(self) -> dict:
        return {
            "adapter": self.name,
            "self_wxid": self.get_self_wxid(),
            "wcf_port": self.port,
            "connected": self._wcf is not None,
        }

    # ==================================================================
    # 收消息
    # ==================================================================
    def _receive_loop(self, wcf: Any) -> None:
        """阻塞循环：从 WCF 消息队列取消息并分发。

        `get_msg()` 内部是 ``msgQ.get(block=True, timeout=1)``，超时抛 queue.Empty。
        空闲超过 60 秒时探活一次 is_login()，用于发现「微信被关掉/退出登录」这种
        静默断线（此时 socket 可能还活着，只是没消息）。
        """
        last_activity = time.time()
        last_ping = 0.0
        while not self._stop_event.is_set():
            try:
                msg = wcf.get_msg()
            except Empty:
                now = time.time()
                if (
                    now - last_activity >= self._IDLE_PING_SECONDS
                    and now - last_ping >= self._IDLE_PING_SECONDS
                ):
                    last_ping = now
                    try:
                        if not self._rpc("is_login"):
                            raise WcfConnectionError("微信已退出登录（is_login() == False）")
                    except WcfConnectionError:
                        raise
                    log.debug("空闲探活成功（微信仍在线）")
                continue
            except BaseException as exc:  # noqa: BLE001
                if self._stop_event.is_set():
                    break
                raise WcfConnectionError(f"get_msg 异常：{type(exc).__name__}: {exc}") from exc

            last_activity = time.time()
            try:
                self._dispatch(msg)
            except Exception:  # noqa: BLE001
                # 一条消息转换/回调失败，只丢这一条，绝不能终止收消息循环
                log.exception("处理消息失败，已跳过（id=%s）", getattr(msg, "id", "?"))

    def _dispatch(self, msg: Any) -> None:
        """把 WCF WxMsg 转成 IncomingMessage 并交给 Bot。"""
        if self._stop_event.is_set():
            return
        # 自己发出去的消息：直接忽略（Bot/Engine 也会按 self_wxid 过滤，这里是提前省事）
        if _msg_from_self(msg):
            if not self.self_wxid:
                sender = str(getattr(msg, "sender", "") or "")
                if sender:
                    self.self_wxid = sender
                    self._notify_self_wxid()
            log.debug("忽略自己发送的消息 id=%s", getattr(msg, "id", "?"))
            return

        handler = self._on_message
        if handler is None:
            log.debug("没有注册 on_message 回调，丢弃消息")
            return
        incoming = self._convert_message(msg)
        if incoming is not None:
            handler(incoming)

    def _convert_message(self, msg: Any) -> Optional[IncomingMessage]:
        """WxMsg -> IncomingMessage（纯转换，不抛异常给调用方）。"""
        msg_type = _as_int(getattr(msg, "type", 0))
        is_group = _msg_from_group(msg)
        sender_id = str(getattr(msg, "sender", "") or "")

        room_id = str(getattr(msg, "roomid", "") or "") if is_group else sender_id
        if not room_id:
            log.debug("消息没有 roomid/sender，跳过：%s", msg)
            return None

        text = self._message_text(msg, is_group)
        room_name = self._room_name(room_id) if is_group else ""
        sender_name = self._sender_name(sender_id, room_id if is_group else "")
        at_wxids = self._extract_at_wxids(getattr(msg, "xml", "") or "") if is_group else []

        raw_id = getattr(msg, "id", "")
        msg_id = str(raw_id) if raw_id not in (None, "") else f"wcf-{int(time.time() * 1000)}"

        ts = _as_int(getattr(msg, "ts", 0))
        return IncomingMessage(
            room_id=room_id,
            sender_id=sender_id,
            text=text,
            sender_name=sender_name,
            room_name=room_name,
            msg_id=msg_id,
            ts=float(ts) if ts else time.time(),
            is_group=is_group,
            msg_type=msg_type,
            at_wxids=at_wxids,
        )

    # ------------------------------------------------------------------
    # 文本 / 占位符
    # ------------------------------------------------------------------
    def _message_text(self, msg: Any, is_group: bool) -> str:
        """文本消息取 content；非文本消息转成「[图片]」这类占位符。"""
        msg_type = _as_int(getattr(msg, "type", 0))
        if msg_type == 0x01:  # 普通文本
            content = str(getattr(msg, "content", "") or "")
            if is_group:
                # 某些微信/WCF 版本群消息 content 会带 "<发送者wxid>:\n" 前缀，
                # 只在前缀确实是发送者/自己时才剥掉，避免误伤正常文本。
                content = _strip_sender_prefix(content, str(getattr(msg, "sender", "") or ""), self.self_wxid)
            return content.strip()

        label = _MESSAGE_TYPE_LABELS.get(msg_type)
        if label is None and msg_type in _APPMSG_BASE_TYPES:
            label = _appmsg_label(str(getattr(msg, "xml", "") or ""))
        return label or f"[消息类型 {msg_type}]"

    # ------------------------------------------------------------------
    # @ 列表 / 显示名 / 群名（带缓存）
    # ------------------------------------------------------------------
    def _extract_at_wxids(self, xml_text: str) -> list[str]:
        """从消息 XML 的 <atuserlist> 里取出被 @ 的 wxid 列表。

        例：``<msgsource><atuserlist><![CDATA[wxid_a,wxid_b,]]></atuserlist></msgsource>``
        @所有人 不是具体成员，``notify@all`` 这类值会被过滤掉。
        """
        if not xml_text:
            return []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []
        node = root.find(".//atuserlist")
        raw = (node.text or "") if node is not None else ""
        raw = raw.replace(" ", "").replace("\n", "").replace("\r", "").replace("\t", "")
        if not raw:
            return []
        result: list[str] = []
        for part in raw.split(","):
            part = part.strip()
            if not part or part.startswith("notify@"):
                continue
            if part not in result:
                result.append(part)
        return result

    def _sender_name(self, sender_id: str, room_id: str) -> str:
        """群聊优先取「群昵称」，私聊取备注/昵称，最后兜底 wxid。"""
        if not sender_id:
            return ""
        if room_id:
            alias = self._get_alias(sender_id, room_id)
            if alias:
                return alias
        return self._contact_name(sender_id) or sender_id

    def _get_alias(self, wxid: str, room_id: str) -> str:
        """取群昵称（WCF 提供 get_alias_in_chatroom；取不到返回空字符串）。"""
        if not wxid or not room_id:
            return ""
        key = (room_id, wxid)
        cached = self._cache_get(self._alias_cache, key)
        if cached is not None:
            return cached
        alias = ""
        if room_id.endswith("@chatroom"):
            alias = str(self._safe_rpc("get_alias_in_chatroom", wxid, room_id, default="") or "").strip()
        if not alias:
            alias = self._contact_name(wxid)
        self._cache_set(self._alias_cache, key, alias)
        return alias

    def _contact_name(self, wxid: str) -> str:
        """从 MicroMsg.db 的 Contact 表取备注/昵称（WCF 没有 get_contact 单查接口）。"""
        if not wxid:
            return ""
        cached = self._cache_get(self._contact_name_cache, wxid)
        if cached is not None:
            return cached
        name = ""
        rows = self._safe_rpc(
            "query_sql",
            "MicroMsg.db",
            f"SELECT Remark, NickName FROM Contact WHERE UserName = '{_sql_escape(wxid)}';",
            default=None,
        )
        if rows:
            row = rows[0] or {}
            name = str(row.get("Remark") or row.get("NickName") or "").strip()
        self._cache_set(self._contact_name_cache, wxid, name)
        return name

    def _room_name(self, room_id: str) -> str:
        """群名：WCF 没有直接的 get_chatroom 接口，群名在 Contact 表里。"""
        if not room_id:
            return ""
        cached = self._cache_get(self._room_name_cache, room_id)
        if cached is not None:
            return cached
        name = ""
        rows = self._safe_rpc(
            "query_sql",
            "MicroMsg.db",
            f"SELECT NickName, Remark FROM Contact WHERE UserName = '{_sql_escape(room_id)}';",
            default=None,
        )
        if rows:
            row = rows[0] or {}
            name = str(row.get("NickName") or row.get("Remark") or "").strip()
        self._cache_set(self._room_name_cache, room_id, name)
        return name

    # ------------------------------------------------------------------
    # WCF RPC 封装（串行 + 统一异常）
    # ------------------------------------------------------------------
    def _rpc(self, method: str, *args: Any) -> Any:
        """串行调用 WCF 的 RPC 方法；任何异常（含 SystemExit）转成 WcfConnectionError。"""
        with self._rpc_lock:
            wcf = self._wcf
            if wcf is None:
                raise WcfConnectionError("尚未连接微信")
            try:
                return getattr(wcf, method)(*args)
            except KeyboardInterrupt:
                raise
            except BaseException as exc:  # noqa: BLE001  含 WCF 内部 sys.exit(-1)
                raise WcfConnectionError(f"{method} 调用失败：{type(exc).__name__}: {exc}") from exc

    def _safe_rpc(self, method: str, *args: Any, default: Any = None) -> Any:
        """容错版 _rpc：查昵称/查群名这种辅助查询失败时返回默认值，不打断主流程。"""
        try:
            return self._rpc(method, *args)
        except WcfConnectionError as exc:
            log.warning("WCF 查询 %s 失败（不影响收消息）：%s", method, exc)
            return default
        except Exception as exc:  # noqa: BLE001
            log.warning("WCF 查询 %s 出现异常（不影响收消息）：%s", method, exc)
            return default

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _close_wcf(self) -> None:
        """关闭当前 WCF 连接（收消息线程 + 套接字 + SDK）。"""
        wcf, self._wcf = self._wcf, None
        if wcf is None:
            return
        with self._rpc_lock:
            try:
                wcf.disable_recv_msg()
            except BaseException as exc:  # noqa: BLE001
                log.debug("关闭消息接收失败（可忽略）：%s", exc)
            try:
                wcf.cleanup()
            except BaseException as exc:  # noqa: BLE001
                log.debug("清理 WCF 连接失败（可忽略）：%s", exc)
        log.info("WCF 连接已关闭")

    def _notify_self_wxid(self) -> None:
        if not self.self_wxid:
            return
        hook = self.on_self_wxid
        if hook is None:
            return
        try:
            hook(self.self_wxid)
        except Exception:  # noqa: BLE001
            log.exception("on_self_wxid 回调执行失败")

    def _wait(self, seconds: float) -> bool:
        """可被 stop() 打断的 sleep；返回 True 表示期间收到了停止信号。"""
        return self._stop_event.wait(timeout=seconds)

    def _cache_get(self, cache: dict, key: Any) -> Optional[str]:
        item = cache.get(key)
        if not item:
            return None
        value, ts = item
        if time.time() - ts > self._CACHE_TTL:
            cache.pop(key, None)
            return None
        return value

    def _cache_set(self, cache: dict, key: Any, value: str) -> None:
        cache[key] = (value, time.time())


# ==========================================================================
# 模块级小工具（便于单测，不依赖 wcferry）
# ==========================================================================
def _msg_from_group(msg: Any) -> bool:
    """兼容真实 WxMsg.from_group() 和测试里手写的假消息对象。"""
    fn = getattr(msg, "from_group", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return False
    return bool(getattr(msg, "_is_group", getattr(msg, "is_group", False)))


def _msg_from_self(msg: Any) -> bool:
    fn = getattr(msg, "from_self", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return False
    return bool(getattr(msg, "_is_self", getattr(msg, "is_self", False)))


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sql_escape(value: str) -> str:
    """单引号转义，防止拼 SQL 时被奇怪昵称搞坏。"""
    return str(value).replace("'", "''")


def _strip_sender_prefix(content: str, sender: str, self_wxid: str) -> str:
    """剥掉部分版本群消息 content 开头的 ``<wxid>:\\n`` 前缀。

    只在前缀确实是发送者 wxid 或机器人自己 wxid 时才剥，避免误删正常文本。
    """
    if not content:
        return content
    for prefix in (sender, self_wxid):
        if not prefix:
            continue
        for sep in (f"{prefix}:\n", f"{prefix}:\r\n"):
            if content.startswith(sep):
                return content[len(sep):]
    return content


def _appmsg_label(xml_text: str) -> str:
    """解析 0x31（appmsg）消息 XML 里的 <type>，判断是文件/链接/小程序/引用…"""
    if not xml_text:
        return "[链接/卡片]"
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "[链接/卡片]"
    appmsg = root.find(".//appmsg")
    if appmsg is None:
        return "[链接/卡片]"
    raw_type = (appmsg.findtext("type") or "").strip()
    app_type = _as_int(raw_type, -1)
    return _APPMSG_TYPE_LABELS.get(app_type, "[链接/卡片]")
