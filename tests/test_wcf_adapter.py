"""WcfAdapter 单元测试：用「假 wcferry / 假 WxMsg」验证消息转换与发送逻辑。

这些测试不依赖真实微信、不连接 WeChatFerry，可以在任何机器上跑：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_wcf_adapter.py -q

重点覆盖：
  * 群/私聊消息字段归一化（room_id / sender / msg_id / is_group / ts）
  * 群消息 @ 列表解析（<atuserlist>，并过滤 notify@all）
  * 非文本消息 -> [图片] / [表情] / [语音] / [视频] / [文件] / [链接/卡片] /
    [小程序] / [引用消息] 等占位文本
  * 自己发的消息被忽略
  * send_text 的 @ 构造（正文写 @群昵称、aters 只传一次、正文其它 @ 换全角）
  * 取不到昵称 / 发送抛异常时的降级，绝不向上抛异常
  * 未安装 wcferry 时 start() 给出可读的 RuntimeError
"""
from __future__ import annotations

import sys
import threading
import time
import types
from queue import Empty

import pytest

from src.wcf_adapter import WcfAdapter


# ---------------------------------------------------------------------------
# 假 WxMsg：字段/方法与真实 wcferry.WxMsg 保持一致（只实现本适配器用到的部分）
# ---------------------------------------------------------------------------
class FakeWxMsg:
    def __init__(
        self,
        msg_type: int = 1,
        msg_id: object = 1001,
        ts: int = 1700000000,
        xml: str = "",
        sender: str = "wxid_friend",
        roomid: str = "123456@chatroom",
        content: str = "",
        thumb: str = "",
        extra: str = "",
        is_self: bool = False,
        is_group: bool = True,
    ) -> None:
        self.type = msg_type
        self.id = msg_id
        self.ts = ts
        self.xml = xml
        self.sender = sender
        self.roomid = roomid
        self.content = content
        self.thumb = thumb
        self.extra = extra
        self._is_self = is_self
        self._is_group = is_group

    def from_self(self) -> bool:  # noqa: D102
        return self._is_self

    def from_group(self) -> bool:  # noqa: D102
        return self._is_group

    def is_at(self, wxid: str) -> bool:  # noqa: D102
        return wxid in (self.xml or "")

    def is_text(self) -> bool:  # noqa: D102
        return self.type == 1


# ---------------------------------------------------------------------------
# 假 Wcf：记录 RPC 调用，可按需返回群昵称 / SQL 查询结果 / 发送失败
# ---------------------------------------------------------------------------
class FakeWcf:
    def __init__(
        self,
        alias_map: dict | None = None,
        contact_rows: dict | None = None,
        send_status: int = 0,
        fail_send_times: int = 0,
        login: bool = True,
    ) -> None:
        self.alias_map = alias_map or {}
        self.contact_rows = contact_rows or {}
        self.send_status = send_status
        self.fail_send_times = fail_send_times
        self.login = login

        self.send_calls: list[tuple] = []
        self.query_calls: list[tuple[str, str]] = []
        self.alias_calls: list[tuple[str, str]] = []
        self.cleaned_up = False
        self.disabled = False

    # ---- 收消息相关 ----
    def enable_receiving_msg(self, pyq: bool = False) -> bool:
        return True

    def disable_recv_msg(self) -> int:
        self.disabled = True
        return 0

    def is_login(self) -> bool:
        return self.login

    def get_self_wxid(self) -> str:
        return "wxid_self"

    # ---- 昵称 / SQL ----
    def get_alias_in_chatroom(self, wxid: str, roomid: str) -> str:
        self.alias_calls.append((wxid, roomid))
        return self.alias_map.get((roomid, wxid), "")

    def query_sql(self, db: str, sql: str) -> list[dict]:
        self.query_calls.append((db, sql))
        for wxid, rows in self.contact_rows.items():
            if f"'{wxid}'" in sql:
                return rows
        return []

    # ---- 发送 ----
    def send_text(self, *args):
        self.send_calls.append(args)
        if self.fail_send_times > 0:
            self.fail_send_times -= 1
            raise RuntimeError("模拟 WCF 发送失败")
        return self.send_status

    def cleanup(self) -> None:
        self.cleaned_up = True


# ---------------------------------------------------------------------------
# fixture：已「连接」的适配器 + 假 WCF
# ---------------------------------------------------------------------------
@pytest.fixture
def connected():
    adapter = WcfAdapter(port=10086)
    fake = FakeWcf(
        alias_map={
            ("123456@chatroom", "wxid_friend"): "小明",
            ("123456@chatroom", "wxid_self"): "DeepSeek",
        },
        contact_rows={
            "123456@chatroom": [{"NickName": "我 + 朋友 + DeepSeek", "Remark": ""}],
            "wxid_friend": [{"NickName": "朋友", "Remark": "老王"}],
        },
    )
    adapter._wcf = fake
    adapter.self_wxid = "wxid_self"
    return adapter, fake


def collect(adapter: WcfAdapter) -> list:
    received: list = []
    adapter.on_message(received.append)
    return received


# ===========================================================================
# 1. 消息转换
# ===========================================================================
def test_group_text_message_fields(connected):
    adapter, _ = connected
    received = collect(adapter)
    msg = FakeWxMsg(
        msg_type=1,
        msg_id=987654321,
        ts=1712345678,
        sender="wxid_friend",
        roomid="123456@chatroom",
        content="今天吃什么",
    )

    adapter._dispatch(msg)

    assert len(received) == 1
    m = received[0]
    assert m.room_id == "123456@chatroom"
    assert m.sender_id == "wxid_friend"
    assert m.sender_name == "小明"                     # 群昵称优先
    assert m.room_name == "我 + 朋友 + DeepSeek"       # Contact 表群名
    assert m.text == "今天吃什么"
    assert m.msg_id == "987654321"
    assert m.ts == 1712345678.0
    assert m.is_group is True
    assert m.msg_type == 1
    assert m.at_wxids == []
    assert m.at_me is False


def test_group_at_message_extracts_wxids(connected):
    adapter, _ = connected
    received = collect(adapter)
    xml = (
        "<msgsource>"
        "<atuserlist><![CDATA[wxid_self,wxid_other,]]></atuserlist>"
        "</msgsource>"
    )
    msg = FakeWxMsg(
        msg_type=1,
        sender="wxid_friend",
        roomid="123456@chatroom",
        content="@DeepSeek\u2005帮我看下这个",
        xml=xml,
    )

    adapter._dispatch(msg)

    m = received[0]
    assert m.at_wxids == ["wxid_self", "wxid_other"]
    assert m.at_me is True
    # 正文原样保留（Engine 的 mention 触发还会用 "@机器人名" 兜底）
    assert "@DeepSeek" in m.text


def test_group_at_all_is_not_a_member(connected):
    adapter, _ = connected
    received = collect(adapter)
    xml = "<msgsource><atuserlist><![CDATA[notify@all]]></atuserlist></msgsource>"
    msg = FakeWxMsg(msg_type=1, content="@所有人 开会了", xml=xml)

    adapter._dispatch(msg)

    assert received[0].at_wxids == []


def test_group_content_sender_prefix_is_stripped(connected):
    adapter, _ = connected
    received = collect(adapter)
    msg = FakeWxMsg(
        msg_type=1,
        sender="wxid_friend",
        roomid="123456@chatroom",
        content="wxid_friend:\n晚上吃火锅",
    )

    adapter._dispatch(msg)

    assert received[0].text == "晚上吃火锅"


def test_private_message_uses_sender_as_room(connected):
    adapter, _ = connected
    received = collect(adapter)
    # 私聊消息 WCF 里 roomid == 发送者 wxid，is_group=False
    msg = FakeWxMsg(msg_type=1, sender="wxid_friend", roomid="wxid_friend", is_group=False, content="在吗")

    adapter._dispatch(msg)

    m = received[0]
    assert m.room_id == "wxid_friend"
    assert m.sender_id == "wxid_friend"
    assert m.is_group is False
    assert m.room_name == ""
    assert m.at_wxids == []
    assert m.sender_name == "老王"                     # 私聊用备注


@pytest.mark.parametrize(
    ("msg_type", "expected"),
    [
        (0x03, "[图片]"),
        (0x22, "[语音]"),
        (0x2B, "[视频]"),
        (0x3E, "[视频]"),
        (0x2F, "[表情]"),
        (0x100031, "[表情]"),
        (0x1000031, "[链接/卡片]"),
        (0x31000031, "[引用消息]"),
        (0x41000031, "[文件]"),
        (0x2D000031, "[视频]"),
        (0x2712, "[撤回消息]"),
        (0x2710, "[系统消息]"),
        (0x2A, "[名片]"),
        (0x42, "[红包]"),
    ],
)
def test_non_text_message_placeholders(connected, msg_type, expected):
    adapter, _ = connected
    received = collect(adapter)
    msg = FakeWxMsg(msg_type=msg_type, content="<xml/>")

    adapter._dispatch(msg)

    assert received[0].text == expected


def test_appmsg_placeholder_from_xml(connected):
    adapter, _ = connected
    received = collect(adapter)

    cases = {
        "6": "[文件]",
        "33": "[小程序]",
        "36": "[小程序]",
        "57": "[引用消息]",
        "19": "[合并转发]",
        "5": "[链接/卡片]",
    }
    for app_type, expected in cases.items():
        xml = f"<msg><appmsg><type>{app_type}</type></appmsg></msg>"
        adapter._dispatch(FakeWxMsg(msg_type=0x31, xml=xml, content=""))
        assert received[-1].text == expected, app_type

    # 没有 appmsg/解析失败时兜底成链接卡片
    adapter._dispatch(FakeWxMsg(msg_type=0x31, xml="not-xml", content=""))
    assert received[-1].text == "[链接/卡片]"


def test_self_message_is_ignored(connected):
    adapter, _ = connected
    received = collect(adapter)
    msg = FakeWxMsg(msg_type=1, sender="wxid_self", content="我自己发的", is_self=True)

    adapter._dispatch(msg)

    assert received == []


def test_self_wxid_is_learned_from_self_message():
    adapter = WcfAdapter()
    fake = FakeWcf()
    adapter._wcf = fake
    received = collect(adapter)

    adapter._dispatch(FakeWxMsg(msg_type=1, sender="wxid_robot", is_self=True))

    assert adapter.self_wxid == "wxid_robot"
    assert received == []


def test_missing_msg_id_gets_fallback_id(connected):
    adapter, _ = connected
    received = collect(adapter)

    adapter._dispatch(FakeWxMsg(msg_type=1, msg_id="", content="你好"))

    assert received[0].msg_id.startswith("wcf-")


def test_display_name_and_room_name_are_cached(connected):
    adapter, fake = connected
    received = collect(adapter)
    for _ in range(3):
        adapter._dispatch(FakeWxMsg(msg_type=1, content="hi"))

    assert len(received) == 3
    # 同一个发送者/同一个群，昵称和群名都只查一次
    assert len(fake.alias_calls) == 1
    assert sum(1 for _, sql in fake.query_calls if "NickName" in sql) == 1


def test_get_self_wxid_returns_cached_value(connected):
    adapter, _ = connected
    assert adapter.get_self_wxid() == "wxid_self"
    assert adapter.describe()["adapter"] == "wechatferry"
    assert adapter.describe()["connected"] is True


def test_get_self_wxid_queries_when_empty(connected):
    adapter, fake = connected
    adapter.self_wxid = ""
    assert adapter.get_self_wxid() == "wxid_self"
    assert adapter.get_self_wxid() == "wxid_self"


# ===========================================================================
# 2. 发送 / @
# ===========================================================================
def test_send_text_without_at_uses_two_args(connected):
    adapter, fake = connected

    adapter.send_text("123456@chatroom", "你好呀")

    assert len(fake.send_calls) == 1
    assert fake.send_calls[0] == ("你好呀", "123456@chatroom")


def test_send_text_with_at_builds_mention_and_aters(connected):
    adapter, fake = connected

    adapter.send_text("123456@chatroom", "你好呀", at_wxid="wxid_friend")

    assert len(fake.send_calls) == 1
    body, receiver, aters = fake.send_calls[0]
    assert receiver == "123456@chatroom"
    assert aters == "wxid_friend"
    assert body == "@小明\n你好呀"
    assert body.count("@") == 1


def test_send_text_with_at_sanitizes_other_at_signs(connected):
    adapter, fake = connected

    adapter.send_text("123456@chatroom", "邮箱 a@b.com 收到了吗", at_wxid="wxid_friend")

    body, _, aters = fake.send_calls[0]
    assert aters == "wxid_friend"
    # 只有开头那个 @ 是 WCF 的 @ 占位符，正文里其它 @ 必须换成全角
    assert body.count("@") == 1
    assert "a＠b.com" in body


def test_send_text_without_alias_degrades_to_plain(connected):
    adapter, fake = connected
    adapter.send_text("123456@chatroom", "你好", at_wxid="wxid_unknown")

    assert len(fake.send_calls) == 1
    assert fake.send_calls[0] == ("你好", "123456@chatroom")


def test_send_text_nonzero_status_is_logged_not_raised(connected):
    adapter, fake = connected
    fake.send_status = -1

    adapter.send_text("123456@chatroom", "你好")   # 不应抛异常

    assert len(fake.send_calls) == 1


def test_send_text_retries_plain_when_at_send_fails(connected):
    adapter, fake = connected
    fake.fail_send_times = 1

    adapter.send_text("123456@chatroom", "你好", at_wxid="wxid_friend")

    assert len(fake.send_calls) == 2
    assert len(fake.send_calls[0]) == 3      # 第一次带 aters，抛异常
    assert len(fake.send_calls[1]) == 2      # 降级重发不带 aters


def test_send_text_never_raises(connected):
    adapter, fake = connected
    fake.fail_send_times = 99

    adapter.send_text("123456@chatroom", "你好", at_wxid="wxid_friend")  # 不应抛异常


def test_send_text_before_connection_is_safe():
    adapter = WcfAdapter()
    received = collect(adapter)
    adapter.send_text("123456@chatroom", "你好")   # 未连接也不应抛异常
    assert received == []


def test_stop_closes_connection():
    adapter = WcfAdapter()
    fake = FakeWcf()
    adapter._wcf = fake

    adapter.stop()

    assert adapter._wcf is None
    assert fake.disabled is True
    assert fake.cleaned_up is True


# ===========================================================================
# 3. 延迟导入：没装 wcferry 时 start() 给出可读错误
# ===========================================================================
def test_start_raises_helpful_error_without_wcferry(monkeypatch):
    monkeypatch.setitem(sys.modules, "wcferry", None)  # 模拟「没安装」
    adapter = WcfAdapter()

    with pytest.raises(RuntimeError) as excinfo:
        adapter.start()

    message = str(excinfo.value)
    assert "wcferry" in message
    assert "WECHAT_SETUP" in message


def test_importing_adapter_does_not_import_wcferry():
    """模块顶层不能 import wcferry，否则没装依赖时 check/mock/web 会直接挂。"""
    import src.wcf_adapter as mod  # noqa: F401

    assert "wcferry" not in sys.modules or sys.modules.get("wcferry") is not None


# ===========================================================================
# 4. start() 端到端（假 wcferry 模块）：能收消息、能 stop() 退出
# ===========================================================================
def test_start_receives_messages_and_stops(monkeypatch):
    """用假 wcferry 模块跑一遍 start()/stop()，确认阻塞循环能收消息并优雅退出。"""

    class FakeWcfClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.self_wxid = "wxid_self"
            self.cleaned = False
            self._queue = [
                FakeWxMsg(msg_type=1, msg_id=1, sender="wxid_friend", content="第一条"),
                FakeWxMsg(msg_type=1, msg_id=2, sender="wxid_friend", content="第二条"),
            ]

        def enable_receiving_msg(self, pyq: bool = False) -> bool:
            return True

        def disable_recv_msg(self) -> int:
            return 0

        def cleanup(self) -> None:
            self.cleaned = True

        def get_msg(self):
            if self._queue:
                return self._queue.pop(0)
            time.sleep(0.01)
            raise Empty

        def get_self_wxid(self) -> str:
            return "wxid_self"

        def is_login(self) -> bool:
            return True

        def get_alias_in_chatroom(self, wxid: str, roomid: str) -> str:
            return "小明"

        def query_sql(self, db: str, sql: str) -> list:
            return []

    fake_module = types.ModuleType("wcferry")
    fake_module.Wcf = FakeWcfClient
    monkeypatch.setitem(sys.modules, "wcferry", fake_module)

    adapter = WcfAdapter(port=10086)
    received: list = []
    adapter.on_message(received.append)

    thread = threading.Thread(target=adapter.start, name="fake-wcf-start", daemon=True)
    thread.start()

    deadline = time.time() + 5
    while len(received) < 2 and time.time() < deadline:
        time.sleep(0.01)

    adapter.stop()
    thread.join(timeout=5)

    assert len(received) == 2
    assert received[0].text == "第一条"
    assert received[1].text == "第二条"
    assert received[0].room_id == "123456@chatroom"
    assert thread.is_alive() is False
