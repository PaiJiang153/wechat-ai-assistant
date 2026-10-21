"""配置加载：config.yaml + 环境变量覆盖。

设计要点：
  * 所有可调行为都在 YAML 里，不用改代码
  * API Key 支持从环境变量 DEEPSEEK_API_KEY 注入，避免明文写进文件
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, get_type_hints, get_origin

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


DEFAULT_SYSTEM_PROMPT = """你叫「{bot_name}」，是微信群里的一名成员，和 {members} 是朋友。

群聊规则：
1. 这是闲聊群，别人发消息你就自然接话，像真人朋友一样，别像客服。
2. 回复要短：一般 1~3 句，最多不超过 120 字；微信不渲染 Markdown，不要用 **、#、表格、代码块。
3. 说人话，可以用口语、网络用语，可以接梗，但不要油腻、不要过度吹捧。
4. 有人直接问你问题时给准确答案；闲聊时不要长篇大论，也不要每次都反问。
5. 涉及你不确定的事实，直说不确定，不要编。
6. 不要自称「AI 助手」，不要提「根据我的训练数据」这类话，你就是群里这个人。
{extra_rules}"""


@dataclass
class DeepSeekConfig:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = 1.3
    max_tokens: int = 800
    timeout: float = 60.0
    max_retries: int = 3

    def resolve_api_key(self) -> str:
        key = (os.environ.get("DEEPSEEK_API_KEY") or self.api_key or "").strip()
        return "" if key in ("sk-你的key", "sk-your-key") else key


@dataclass
class PersonaConfig:
    bot_name: str = "DeepSeek"
    members: list[str] = field(default_factory=lambda: ["我", "朋友"])
    system_prompt: str = ""       # 留空则用 DEFAULT_SYSTEM_PROMPT
    extra_rules: str = ""

    def build_system_prompt(self) -> str:
        if self.system_prompt.strip():
            return self.system_prompt
        names = "、".join(self.members) if self.members else "其他人"
        return DEFAULT_SYSTEM_PROMPT.format(
            bot_name=self.bot_name, members=names, extra_rules=self.extra_rules
        ).strip()


@dataclass
class ImageGenConfig:
    """生图：模型决定要画图时，调用生图服务并发送图片。"""

    enabled: bool = True
    provider: str = "pollinations"        # pollinations | gemini | openai
    model: str = "flux"                   # gemini: gemini-2.5-flash-image；openai: 各家模型名
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""                     # 空则读环境变量 IMAGEGEN_API_KEY / GEMINI_API_KEY
    size: str = "1024x1024"
    timeout: float = 120.0
    max_per_day: int = 10
    per_minute: int = 1                   # Pollinations 免费版：每 45 秒 1 张
    out_dir: str = "data/media/generated"
    style_suffix: str = ""                # 统一追加的风格词，如 "digital illustration"
    translate_prompt: bool = True         # 中文描述先翻成英文提示词（仅 Pollinations 需要）
    max_keep: int = 100                   # 本地最多保留多少张生成图
    extra_params: dict = field(default_factory=dict)

    def resolve_api_key(self) -> str:
        for env in ("IMAGEGEN_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ZHIPU_API_KEY"):
            value = os.environ.get(env, "").strip()
            if value:
                return value
        return (self.api_key or "").strip()


@dataclass
class NetworkConfig:
    """网络出口：默认直连，避免系统代理（Clash 等）没开时 API 调用失败。"""

    use_system_proxy: bool = False   # true = 跟随系统代理（代理软件需保持运行）


@dataclass
class MemoryConfig:
    """长期记忆：定期把聊天里的事实摘成「人物档案」，注入人设。"""

    enabled: bool = True
    dir: str = "data/memory"
    every_n_messages: int = 40            # 每收到多少条消息更新一次档案
    max_facts: int = 24                   # 档案最多保留多少条事实
    max_inject_chars: int = 900           # 注入提示词的最大长度
    model: str = ""                       # 空 = 用 deepseek.model


@dataclass
class StickerConfig:
    """表情包库：记录对方常发的表情，主动发时复用（按风格/频率选）。"""

    enabled: bool = True
    dir: str = "data/stickers"
    max_send_per_day: int = 5


@dataclass
class ProactiveConfig:
    """主动开场：随机间隔主动找人聊天（文本 / 表情包 / 戳一戳）。"""

    enabled: bool = True
    min_gap_minutes: float = 45.0         # 两次主动消息的最小间隔
    max_gap_minutes: float = 180.0        # 最大间隔
    active_from: str = "08:30"            # 只在活跃时段发（HH:MM）
    active_to: str = "23:30"
    max_per_day: int = 8
    text_chance: float = 0.80
    sticker_chance: float = 0.15
    poke_chance: float = 0.05
    targets: list[str] = field(default_factory=list)  # 空 = 所有已配置群/私聊
    extra_prompt: str = ""                # 额外要求（可选）


@dataclass
class ChatConfig:
    trigger: str = "all"                  # all | mention | prefix
    prefix: str = "ds"
    reply_with_at: bool = True            # 回复时 @ 发言人
    only_groups: bool = True              # 只处理群聊
    debounce_seconds: float = 2.5         # 连续消息合并的静默窗口
    min_interval_seconds: float = 1.0     # 两次回复的最小间隔
    max_replies_per_minute: int = 12      # 限流，防止刷屏/烧钱
    max_message_chars: int = 800          # 单条消息上限，超长自动分条
    max_reply_chunks: int = 3             # 一次回复最多拆成几条
    error_reply: bool = False             # API 出错时是否在群里吱一声
    time_aware: bool = False              # 把人设上下文里注入当前时间（更会接早/晚安）
    max_debounce_seconds: float = 10.0
    max_pending_messages: int = 100
    max_queued_batches: int = 64
    message_ttl_seconds: float = 300.0
    delivery_timeout: float = 180.0


@dataclass
class HumanizeConfig:
    """拟人化行为：让回复的节奏、条数、随机性更像真人。"""

    enabled: bool = False
    # 打字停顿：base 抖动 + 按字数估算打字时间
    typing_min: float = 1.0
    typing_max: float = 3.0
    typing_chars_per_second: float = 9.0
    max_typing_seconds: float = 8.0
    # 把一条回复拆成两条（真人经常分两次发）
    split_chance: float = 0.30
    split_min_chars: int = 18
    split_max_chars: int = 140
    split_delay_min: float = 0.6
    split_delay_max: float = 1.8
    # 发完再补一句（"对了…"）
    double_text_chance: float = 0.10
    double_texts: list[str] = field(
        default_factory=lambda: ["对了", "算了 没事", "你继续说", "我刚想起来个事", "行吧"]
    )
    double_text_delay_min: float = 2.5
    double_text_delay_max: float = 6.0
    # 偶尔已读不回（默认关：保持"每条都回"）
    silence_chance: float = 0.0
    # 回复时 @ 人的概率（群里真人不总 @）
    at_chance: float = 0.7
    # 合并窗口随机抖动（±秒）
    debounce_jitter: float = 1.0


@dataclass
class ContextConfig:
    window_messages: int = 24
    max_context_chars: int = 6000
    persist_dir: str = "data/rooms"
    summarize_after: int = 60             # 超过这么多条后，旧消息滚动丢弃（保留摘要位置）


@dataclass
class PrivateChatConfig:
    """一个要挂机器人的私聊对象（对方昵称/备注）。"""

    name: str
    persona: str = ""                     # personas 里的 id；空则用默认 persona
    trigger: str = ""                     # all | mention | prefix；空则用 chat.trigger
    prefix: str = ""


@dataclass
class GroupConfig:
    """一个要挂机器人的群（可单独指定人设和触发规则）。"""

    name: str
    persona: str = ""                     # personas 字典里的 id；空则用默认 persona
    trigger: str = ""                     # all | mention | prefix；空则用 chat.trigger
    prefix: str = ""                      # trigger=prefix 时的前缀；空则用 chat.prefix
    reply_with_at: bool | None = None     # 空则用 chat.reply_with_at


@dataclass
class WeChatConfig:
    adapter: str = "wcf"                  # wechatauto | pyweixin | wcf | mock
    allowed_groups: list[str] = field(default_factory=list)  # 空=所有群；支持群名或 roomid
    ignore_senders: list[str] = field(default_factory=list)  # 不响应的发送者（wxid 或昵称）
    wcf_port: int = 10086
    self_wxid: str = ""                   # 留空自动获取
    wcf_debug: bool = False

    # ---- wechatauto（微信 4.x，读数据库收消息 + 窗口发送）----
    group_name: str = ""                  # 单群模式的群名（向后兼容；多群用 groups）
    groups: list[dict] = field(default_factory=list)  # 多群：[{name, persona, trigger, prefix}]
    private_chats: list[dict] = field(default_factory=list)  # 私聊：[{name, persona, trigger}]
    group_members: list[str] = field(default_factory=list)
    poll_seconds: float = 2.0
    window_minimize: bool = True
    self_name: str = ""
    queue_size: int = 256
    media_workers: int = 2
    task_ttl_seconds: float = 300.0
    state_dir: str = "data/state"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    dir: str = "logs"


@dataclass
class RetentionConfig:
    # 0 disables automatic deletion. Cleanup is an explicit CLI operation.
    chat_days: int = 0
    memory_days: int = 0
    media_days: int = 30


@dataclass
class VisionConfig:
    """识图 / 看表情包（deepseek-flash 支持图像理解）。"""

    enabled: bool = True
    detail: str = "low"                   # low 最省 token；需要看清细节改 original
    max_chars: int = 160                  # 描述文字上限
    image_prompt: str = (
        "这是微信群聊里别人发的一张图片。用一句中文说清它是什么内容、有没有文字，"
        "80 字以内，不要客套话。"
    )
    sticker_prompt: str = (
        "这是微信群聊里的一个表情包（可能是动图）。用一句中文说明它画的是什么、"
        "传达什么情绪或网络梗，60 字以内。"
    )
    media_dir: str = "data/media"         # 下载的图片/表情包落盘位置
    cache_size: int = 200                 # 最多缓存多少张图的识别结果
    max_calls_per_minute: int = 20        # 防止图片刷屏把额度打爆


@dataclass
class ASRConfig:
    """语音转文字（本地 faster-whisper，离线、免费、不外传）。"""

    enabled: bool = True
    model: str = "base"                   # tiny/base/small/medium；越大约准越慢
    language: str = "zh"
    device: str = "cpu"
    compute_type: str = "int8"
    models_dir: str = "data/models"
    max_seconds: float = 120.0            # 超过这个时长的语音不识别


@dataclass
class FilesConfig:
    """文件消息：解析成文字描述后进上下文。"""

    enabled: bool = True
    max_text_chars: int = 2000            # 每个文件最多读多少字
    max_file_mb: float = 20.0             # 超过这个大小只报文件名
    dir: str = "data/media/files"         # 下载的文件落盘位置


@dataclass
class SearchConfig:
    """联网搜索（DeepSeek 官方 Search API，由模型通过 Tool Calls 自行决定）。"""

    enabled: bool = True
    count: int = 8                        # 每次搜索取多少条结果（1~50）
    need_content: bool = False            # 是否需要网页正文（更贵更慢，一般不用）
    max_results_for_model: int = 4        # 最终喂给模型几条
    max_chars: int = 2500                 # 搜索结果文本上限
    timeout: float = 60.0
    max_per_minute: int = 10              # 限流，防刷
    cache_ttl: int = 300                  # 同一 query 缓存秒数
    max_tool_rounds: int = 2              # 最多几轮"搜索→再回答"


@dataclass
class AppConfig:
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    personas: dict[str, PersonaConfig] = field(default_factory=dict)
    chat: ChatConfig = field(default_factory=ChatConfig)
    humanize: HumanizeConfig = field(default_factory=HumanizeConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    wechat: WeChatConfig = field(default_factory=WeChatConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    imagegen: ImageGenConfig = field(default_factory=ImageGenConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    stickers: StickerConfig = field(default_factory=StickerConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    source_path: str = ""

    # ---------- 构造 ----------
    @classmethod
    def from_dict(cls, data: dict[str, Any], source_path: str = "") -> "AppConfig":
        unknown_root = set(data) - (set(cls.__dataclass_fields__) - {"source_path"})
        if unknown_root:
            raise ValueError(f"配置根节点存在未知字段: {', '.join(sorted(unknown_root))}")
        cfg = cls(source_path=source_path)
        for key, section_cls in (
            ("deepseek", DeepSeekConfig),
            ("persona", PersonaConfig),
            ("chat", ChatConfig),
            ("humanize", HumanizeConfig),
            ("context", ContextConfig),
            ("wechat", WeChatConfig),
            ("vision", VisionConfig),
            ("asr", ASRConfig),
            ("files", FilesConfig),
            ("search", SearchConfig),
            ("imagegen", ImageGenConfig),
            ("network", NetworkConfig),
            ("memory", MemoryConfig),
            ("stickers", StickerConfig),
            ("proactive", ProactiveConfig),
            ("logging", LoggingConfig),
            ("retention", RetentionConfig),
        ):
            raw = data.get(key) or {}
            if not isinstance(raw, dict):
                raise ValueError(f"配置节 {key} 必须是字典")
            known = {f for f in section_cls.__dataclass_fields__}  # type: ignore[attr-defined]
            unknown = set(raw) - known
            if unknown:
                raise ValueError(f"配置节 {key} 里有未知字段: {', '.join(sorted(unknown))}")
            for name, value in raw.items():
                expected = get_type_hints(section_cls)[name]
                kind = get_origin(expected) or expected
                if kind in (str, bool, int, float, list, dict):
                    valid = isinstance(value, (int, float)) and not isinstance(value, bool) if kind is float else isinstance(value, kind)
                    if kind is int and isinstance(value, bool):
                        valid = False
                    if not valid:
                        raise ValueError(f"{key}.{name} 类型错误，需要 {kind.__name__}")
            setattr(cfg, key, section_cls(**raw))

        # personas：id -> 人设（动态键，单独解析）
        raw_personas = data.get("personas") or {}
        if raw_personas:
            if not isinstance(raw_personas, dict):
                raise ValueError("配置节 personas 必须是「人设id: 人设配置」的字典")
            known_persona = set(PersonaConfig.__dataclass_fields__)  # type: ignore[attr-defined]
            for pid, pdata in raw_personas.items():
                if not isinstance(pdata, dict):
                    raise ValueError(f"人设 {pid} 必须是字典")
                unknown = set(pdata) - known_persona
                if unknown:
                    raise ValueError(f"人设 {pid} 里有未知字段: {', '.join(sorted(unknown))}")
                cfg.personas[str(pid)] = PersonaConfig(**pdata)

        cfg._validate()
        return cfg

    def _validate(self) -> None:
        for name in ("max_pending_messages", "max_queued_batches", "max_reply_chunks"):
            if getattr(self.chat, name) < 1:
                raise ValueError(f"chat.{name} 必须大于 0")
        for name in ("max_debounce_seconds", "message_ttl_seconds", "delivery_timeout"):
            if getattr(self.chat, name) <= 0:
                raise ValueError(f"chat.{name} 必须大于 0")
        if self.chat.debounce_seconds < 0 or self.chat.min_interval_seconds < 0:
            raise ValueError("chat 的等待时间不能为负数")
        if min(self.wechat.queue_size, self.wechat.media_workers) < 1 or self.wechat.task_ttl_seconds <= 0:
            raise ValueError("wechat 的队列容量、工作线程和任务时限必须大于 0")
        if min(self.retention.chat_days, self.retention.memory_days, self.retention.media_days) < 0:
            raise ValueError("retention 保留天数不能为负数")
        if self.context.window_messages < 1 or self.context.max_context_chars < 1:
            raise ValueError("context 的消息数和字符预算必须大于 0")
        if self.deepseek.max_tokens < 1 or self.deepseek.timeout <= 0 or self.deepseek.max_retries < 0:
            raise ValueError("deepseek 的输出预算、超时或重试次数不合法")
        if self.vision.cache_size < 1 or self.memory.max_facts < 1 or self.memory.max_inject_chars < 1:
            raise ValueError("缓存和记忆容量必须大于 0")
        for low, high in (("typing_min", "typing_max"), ("split_delay_min", "split_delay_max"),
                          ("double_text_delay_min", "double_text_delay_max")):
            if getattr(self.humanize, low) < 0 or getattr(self.humanize, high) < getattr(self.humanize, low):
                raise ValueError(f"humanize.{low}/{high} 范围不合法")
        if self.chat.trigger not in ("all", "mention", "prefix"):
            raise ValueError("chat.trigger 只能是 all / mention / prefix")
        if self.chat.max_message_chars < 50:
            raise ValueError("chat.max_message_chars 太小（至少 50）")
        if self.chat.trigger == "prefix" and not self.chat.prefix:
            raise ValueError("trigger=prefix 时必须配置 chat.prefix")
        if self.wechat.adapter not in ("wcf", "pyweixin", "wechatauto", "mock"):
            raise ValueError("wechat.adapter 只能是 wcf / pyweixin / wechatauto / mock")
        if self.wechat.adapter in ("pyweixin", "wechatauto") and not (
            self.wechat.group_name.strip() or self.wechat.groups or self.wechat.private_chats
        ):
            raise ValueError(
                f"用 {self.wechat.adapter} 适配器时至少要配置一个群或私聊"
                "（wechat.group_name / wechat.groups / wechat.private_chats）"
            )
        for g in self.wechat.groups:
            if not isinstance(g, dict) or not str(g.get("name") or "").strip():
                raise ValueError("wechat.groups 每一项都必须有 name（群名）")
            extra = set(g) - {"name", "persona", "trigger", "prefix", "reply_with_at"}
            if extra:
                raise ValueError(f"wechat.groups「{g.get('name')}」有未知字段: {', '.join(sorted(extra))}")
            trigger = g.get("trigger") or self.chat.trigger
            if trigger not in ("all", "mention", "prefix"):
                raise ValueError(f"wechat.groups「{g.get('name')}」的 trigger 非法：{trigger}")
            if trigger == "prefix" and not str(g.get("prefix") or self.chat.prefix).strip():
                raise ValueError("群 prefix 触发必须配置非空前缀")
            persona_id = str(g.get("persona") or "")
            if persona_id and persona_id not in self.personas:
                raise ValueError(
                    f"wechat.groups「{g.get('name')}」引用了不存在的人设「{persona_id}」，"
                    f"可选：{', '.join(self.personas) or '（无，先配置 personas）'}"
                )
        for p in self.wechat.private_chats:
            if not isinstance(p, dict) or not str(p.get("name") or "").strip():
                raise ValueError("wechat.private_chats 每一项都必须有 name（对方昵称/备注）")
            extra = set(p) - {"name", "persona", "trigger", "prefix"}
            if extra:
                raise ValueError(f"wechat.private_chats「{p.get('name')}」有未知字段: {', '.join(sorted(extra))}")
            trigger = p.get("trigger") or "all"
            if trigger not in ("all", "mention", "prefix"):
                raise ValueError(f"wechat.private_chats「{p.get('name')}」的 trigger 非法：{trigger}")
            if trigger == "prefix" and not str(p.get("prefix") or self.chat.prefix).strip():
                raise ValueError("私聊 prefix 触发必须配置非空前缀")
            persona_id = str(p.get("persona") or "")
            if persona_id and persona_id not in self.personas:
                raise ValueError(
                    f"wechat.private_chats「{p.get('name')}」引用了不存在的人设「{persona_id}」，"
                    f"可选：{', '.join(self.personas) or '（无，先配置 personas）'}"
                )
        if self.wechat.poll_seconds < 0.5:
            raise ValueError("wechat.poll_seconds 不能小于 0.5 秒")
        if self.vision.detail not in ("low", "high", "original", "auto"):
            raise ValueError("vision.detail 只能是 low / high / original / auto")
        if self.vision.max_chars < 20:
            raise ValueError("vision.max_chars 太小（至少 20）")
        if self.asr.model not in ("tiny", "base", "small", "medium", "large-v3"):
            raise ValueError("asr.model 只能是 tiny / base / small / medium / large-v3")
        if self.asr.max_seconds <= 0:
            raise ValueError("asr.max_seconds 必须大于 0")
        if not 1 <= self.search.count <= 50:
            raise ValueError("search.count 必须在 1~50 之间")
        if self.humanize.typing_chars_per_second <= 0:
            raise ValueError("humanize.typing_chars_per_second 必须大于 0")
        for name in ("split_chance", "double_text_chance", "silence_chance", "at_chance"):
            value = getattr(self.humanize, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"humanize.{name} 必须在 0~1 之间")
        if self.memory.enabled and self.memory.every_n_messages < 4:
            raise ValueError("memory.every_n_messages 不能小于 4")
        for name in ("text_chance", "sticker_chance", "poke_chance"):
            value = getattr(self.proactive, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"proactive.{name} 必须在 0~1 之间")
        for name in ("active_from", "active_to"):
            raw = str(getattr(self.proactive, name))
            try:
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw):
                    raise ValueError(raw)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"proactive.{name} 必须是 HH:MM 格式，当前 {raw!r}") from exc
        if self.proactive.min_gap_minutes <= 0 or self.proactive.max_gap_minutes < self.proactive.min_gap_minutes:
            raise ValueError("proactive 的间隔配置不合法（min_gap 应大于 0 且不超过 max_gap）")
        if self.imagegen.provider not in ("pollinations", "gemini", "openai", "zhipu"):
            raise ValueError("imagegen.provider 只能是 pollinations / gemini / zhipu / openai")
        if not re.fullmatch(r"\d{2,4}x\d{2,4}", str(self.imagegen.size or "")):
            raise ValueError(f"imagegen.size 格式应为 宽x高，例如 1024x1024，当前 {self.imagegen.size!r}")

    # ---------- 群 / 人设 ----------
    def group_list(self) -> list[GroupConfig]:
        """统一的群列表：多群模式用 wechat.groups，单群模式回落到 group_name。"""
        if self.wechat.groups:
            result = []
            for g in self.wechat.groups:
                cleaned = {k: v for k, v in g.items() if v is not None and v != ""}
                result.append(GroupConfig(**cleaned))
            return result
        if self.wechat.group_name.strip():
            return [GroupConfig(name=self.wechat.group_name.strip())]
        return []

    def group_for(self, room_name_or_id: str) -> GroupConfig | None:
        key = (room_name_or_id or "").strip()
        if not key:
            return None
        for g in self.group_list():
            if g.name == key:
                return g
        return None

    def private_chat_list(self) -> list[PrivateChatConfig]:
        result = []
        for p in self.wechat.private_chats:
            cleaned = {k: v for k, v in p.items() if v is not None and v != ""}
            result.append(PrivateChatConfig(**cleaned))
        return result

    def private_chat_for(self, name: str) -> PrivateChatConfig | None:
        key = (name or "").strip()
        if not key:
            return None
        for p in self.private_chat_list():
            if p.name == key:
                return p
        return None

    def persona_for(self, target: GroupConfig | PrivateChatConfig | None = None) -> PersonaConfig:
        if target is not None and target.persona:
            return self.personas[target.persona]
        return self.persona

    def trigger_for(self, target: GroupConfig | PrivateChatConfig | None) -> str:
        return target.trigger if (target and target.trigger) else self.chat.trigger

    def prefix_for(self, target: GroupConfig | PrivateChatConfig | None) -> str:
        return target.prefix if (target and target.prefix) else self.chat.prefix

    # ---------- 工具 ----------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("source_path", None)
        return d

    def masked_dict(self) -> dict[str, Any]:
        """脱敏后的配置，用于日志/诊断输出。"""
        d = self.to_dict()
        key = d["deepseek"].get("api_key") or ""
        if key:
            d["deepseek"]["api_key"] = key[:6] + "***" + key[-4:] if len(key) > 12 else "***"
        if d["imagegen"].get("api_key"):
            d["imagegen"]["api_key"] = "***"
        return d

    def resolve_paths(self, base: Path) -> None:
        """把配置里的相对路径按项目根目录展开。"""
        self.context.persist_dir = str((base / self.context.persist_dir).resolve())
        self.vision.media_dir = str((base / self.vision.media_dir).resolve())
        self.imagegen.out_dir = str((base / self.imagegen.out_dir).resolve())
        self.asr.models_dir = str((base / self.asr.models_dir).resolve())
        self.files.dir = str((base / self.files.dir).resolve())
        self.memory.dir = str((base / self.memory.dir).resolve())
        self.stickers.dir = str((base / self.stickers.dir).resolve())
        self.logging.dir = str((base / self.logging.dir).resolve())
        self.wechat.state_dir = str((base / self.wechat.state_dir).resolve())

    @property
    def trigger_label(self) -> str:
        return {"all": "每条消息都回", "mention": "只在被 @ 时回", "prefix": f"以 {self.chat.prefix} 开头时回"}[
            self.chat.trigger
        ]


def load_config(path: str | Path | None = None) -> AppConfig:
    """读取 YAML 配置。path 为空时依次找 config.yaml / config.example.yaml。"""
    base = Path(__file__).resolve().parent.parent
    if path is None:
        for cand in (base / "config.yaml", base / "config.example.yaml"):
            if cand.exists():
                path = cand
                break
        if path is None:
            raise FileNotFoundError("找不到 config.yaml，请复制 config.example.yaml 为 config.yaml 并填写")
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    if yaml is None:
        raise RuntimeError("缺少 PyYAML，请先 pip install -r requirements.txt")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是字典")
    cfg = AppConfig.from_dict(data, source_path=str(p))
    cfg.resolve_paths(p.parent)
    return cfg
