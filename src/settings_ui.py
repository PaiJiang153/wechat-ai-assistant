"""Allowlisted maintenance settings; preserve unrelated YAML and secrets."""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from copy import deepcopy
from pathlib import Path

import yaml

from .config import AppConfig
from .runtime import file_lock


class SettingsConflict(ValueError):
    pass


def field(key, label, kind="number", description="", minimum=None, maximum=None, step=1, options=None):
    return dict(key=key, label=label, kind=kind, description=description,
                min=minimum, max=maximum, step=step, options=options or [])


GROUPS = [
    dict(id="model", title="模型与人设", description="控制回答风格、输出预算和默认角色。独立人设的会话仍使用自己的角色。", fields=[
        field("deepseek.model", "模型名称", "text", "填写模型服务提供的名称。"),
        field("deepseek.temperature", "回答发散程度", description="越低越稳定，越高越有变化。部分推理模型会忽略此项。", minimum=0, maximum=2, step=0.1),
        field("deepseek.max_tokens", "单次输出上限", description="包含模型可能使用的推理预算，实际可用上限取决于模型。", minimum=1, maximum=131072),
        field("deepseek.timeout", "单次请求超时（秒）", minimum=1, maximum=600),
        field("deepseek.max_retries", "失败重试次数", minimum=0, maximum=10),
        field("persona.bot_name", "默认角色名称", "text"),
        field("persona.system_prompt", "默认人设", "textarea", "留空使用内置人设；不会覆盖各会话独立的人设。"),
        field("persona.extra_rules", "额外规则", "textarea", "仅在使用内置默认人设时追加。"),
    ]),
    dict(id="chat", title="回复与上下文", description="调整触发条件、合并消息和回复长度。会话单独配置的规则优先。", fields=[
        field("chat.trigger", "默认触发方式", "select", options=[("all", "每条消息都回"), ("mention", "被 @ 才回"), ("prefix", "指定前缀触发")]),
        field("chat.prefix", "触发前缀", "text"),
        field("chat.reply_with_at", "回复时 @ 发言人", "bool"),
        field("chat.time_aware", "感知当前时间", "bool"),
        field("chat.error_reply", "连接失败时发出提示", "bool"),
        field("chat.debounce_seconds", "消息合并等待（秒）", minimum=0, maximum=120, step=0.5),
        field("chat.max_debounce_seconds", "最长合并等待（秒）", description="持续有人发言时，到此时限仍会开始处理。", minimum=0.1, maximum=300, step=0.1),
        field("chat.min_interval_seconds", "两次回复最短间隔（秒）", minimum=0, maximum=300, step=0.5),
        field("chat.max_replies_per_minute", "每个会话每分钟发送上限", description="分条和补话也计数；0 表示不限。", minimum=0, maximum=300),
        field("chat.max_message_chars", "每条回复最多字数", minimum=50, maximum=10000),
        field("chat.max_reply_chunks", "一次最多拆成几条", minimum=1, maximum=20),
        field("context.window_messages", "携带最近几条历史", minimum=1, maximum=1000),
        field("context.max_context_chars", "历史字符预算", minimum=100, maximum=200000, step=100),
    ]),
    dict(id="humanize", title="拟人节奏", description="用停顿、分条和补话调整聊天节奏。概率以百分比显示。", fields=[
        field("humanize.enabled", "启用拟人节奏", "bool"),
        field("humanize.typing_min", "最短打字停顿（秒）", minimum=0, maximum=60, step=0.1),
        field("humanize.typing_max", "最长基础停顿（秒）", minimum=0, maximum=60, step=0.1),
        field("humanize.typing_chars_per_second", "打字速度（字 / 秒）", minimum=0.1, maximum=200, step=0.1),
        field("humanize.max_typing_seconds", "打字停顿总上限（秒）", minimum=0, maximum=120, step=0.5),
        field("humanize.split_chance", "分成两条的概率", "probability", minimum=0, maximum=1, step=0.01),
        field("humanize.split_min_chars", "可分条的最短字数", minimum=8, maximum=10000),
        field("humanize.split_max_chars", "可分条的最长字数", minimum=8, maximum=10000),
        field("humanize.split_delay_min", "分条最短间隔（秒）", minimum=0, maximum=60, step=0.1),
        field("humanize.split_delay_max", "分条最长间隔（秒）", minimum=0, maximum=60, step=0.1),
        field("humanize.double_text_chance", "发送后补一句的概率", "probability", minimum=0, maximum=1, step=0.01),
        field("humanize.double_texts", "补话内容", "lines", "每行一句，随机选择一条。"),
        field("humanize.double_text_delay_min", "补话最短等待（秒）", minimum=0, maximum=120, step=0.5),
        field("humanize.double_text_delay_max", "补话最长等待（秒）", minimum=0, maximum=120, step=0.5),
        field("humanize.silence_chance", "偶尔不回复的概率", "probability", "被明确 @ 时仍会回复。", 0, 1, 0.01),
        field("humanize.at_chance", "实际 @ 对方的概率", "probability", minimum=0, maximum=1, step=0.01),
        field("humanize.debounce_jitter", "合并等待的随机增量（秒）", minimum=0, maximum=30, step=0.1),
    ]),
    dict(id="proactive", title="记忆与主动聊天", description="决定记住多少信息，以及何时主动找人聊天。", fields=[
        field("memory.enabled", "启用长期记忆", "bool"),
        field("memory.every_n_messages", "每几条消息整理一次记忆", minimum=4, maximum=1000),
        field("memory.max_facts", "每个会话最多几条事实", minimum=1, maximum=200),
        field("memory.max_inject_chars", "每次注入的记忆字符上限", minimum=1, maximum=20000),
        field("proactive.enabled", "启用主动聊天", "bool"),
        field("proactive.min_gap_minutes", "至少安静多久（分钟）", minimum=1, maximum=10080),
        field("proactive.max_gap_minutes", "最长随机检查间隔（分钟）", minimum=1, maximum=10080),
        field("proactive.active_from", "活跃时段开始", "time"),
        field("proactive.active_to", "活跃时段结束", "time", "结束早于开始表示跨午夜。"),
        field("proactive.max_per_day", "每天最多主动几次", minimum=0, maximum=100),
        field("proactive.text_chance", "主动文字的概率", "probability", "三种动作概率之和不能超过 100%。", 0, 1, 0.01),
        field("proactive.sticker_chance", "主发表情的概率", "probability", minimum=0, maximum=1, step=0.01),
        field("proactive.poke_chance", "拍一拍的概率", "probability", minimum=0, maximum=1, step=0.01),
        field("proactive.extra_prompt", "主动聊天的额外要求", "textarea"),
        field("stickers.enabled", "收集会话表情", "bool"),
        field("stickers.keyword_dir", "关键词表情池目录", "text", description="文件名即触发词（逗号分隔多个）；配置后命中才发、取代随机。"),
        field("stickers.repeat_cooldown_minutes", "同一表情重发冷却（分钟）", description="冷却内不再重发同一张表情；0 为不限制。", minimum=0, maximum=1440),
        field("stickers.max_send_per_day", "每天最多主发表情数", description="0 表示不限。", minimum=0, maximum=100),
    ]),
    dict(id="media", title="搜索与多媒体", description="按需开启。语音、文件解析和图像能力需要相应组件与服务支持。", fields=[
        field("search.enabled", "启用联网搜索", "bool"),
        field("search.count", "每次搜索结果数", minimum=1, maximum=50),
        field("search.max_results_for_model", "提供给模型的结果数", minimum=1, maximum=50),
        field("search.max_chars", "搜索材料字符上限", minimum=100, maximum=20000),
        field("search.max_per_minute", "每分钟搜索上限", minimum=0, maximum=100),
        field("search.cache_ttl", "相同搜索缓存（秒）", minimum=0, maximum=86400),
        field("search.max_tool_rounds", "最多工具调用轮数", minimum=1, maximum=5),
        field("vision.enabled", "启用图片识别", "bool"),
        field("vision.detail", "图像细节", "select", options=[("low", "低"), ("high", "高"), ("original", "原图"), ("auto", "自动")]),
        field("vision.max_chars", "图片描述最多字数", minimum=20, maximum=3000),
        field("vision.max_calls_per_minute", "每分钟识图上限", minimum=0, maximum=100),
        field("asr.enabled", "启用语音转文字", "bool"),
        field("asr.model", "本地语音模型", "select", options=[(v, v) for v in ("tiny", "base", "small", "medium", "large-v3")]),
        field("asr.max_seconds", "最长识别语音（秒）", minimum=1, maximum=600),
        field("files.enabled", "启用文件解析", "bool"),
        field("files.max_text_chars", "文件摘录最多字数", minimum=100, maximum=30000),
        field("files.max_file_mb", "最大解析文件（MB）", minimum=1, maximum=100),
        field("imagegen.enabled", "启用生图", "bool"),
        field("imagegen.provider", "生图服务", "select", "切换前请在配置文件中准备对应服务的连接与 Key。", options=[("pollinations", "Pollinations"), ("gemini", "Gemini"), ("zhipu", "智谱"), ("openai", "OpenAI 兼容接口")]),
        field("imagegen.model", "生图模型名称", "text"),
        field("imagegen.size", "图片尺寸", "text", "例如 1024x1024；须由所选服务支持。"),
        field("imagegen.max_per_day", "每天生图请求上限", minimum=0, maximum=300),
        field("imagegen.per_minute", "每分钟生图请求上限", minimum=0, maximum=30),
        field("imagegen.style_suffix", "统一追加的画面风格", "textarea"),
        field("imagegen.max_keep", "清理时保留最近几张图", minimum=0, maximum=10000),
    ]),
    dict(id="resources", title="队列与数据", description="调整排队容量、任务时限与手动清理策略。保留天数为 0 表示不清理。", fields=[
        field("wechat.poll_seconds", "读取新消息间隔（秒）", minimum=0.5, maximum=60, step=0.5),
        field("wechat.queue_size", "收发队列容量", minimum=1, maximum=10000),
        field("wechat.media_workers", "媒体处理线程数", minimum=1, maximum=16),
        field("wechat.task_ttl_seconds", "接收任务有效期（秒）", minimum=1, maximum=3600),
        field("chat.max_pending_messages", "每个会话待合并消息上限", minimum=1, maximum=1000),
        field("chat.max_queued_batches", "回复批次排队上限", minimum=1, maximum=1000),
        field("chat.message_ttl_seconds", "消息有效期（秒）", minimum=1, maximum=3600),
        field("chat.delivery_timeout", "等待发送确认（秒）", minimum=1, maximum=600),
        field("retention.chat_days", "聊天记录保留天数", minimum=0, maximum=36500),
        field("retention.memory_days", "长期记忆保留天数", minimum=0, maximum=36500),
        field("retention.media_days", "媒体保留天数", minimum=0, maximum=36500),
        field("logging.level", "日志详细程度", "select", options=[(v, v) for v in ("DEBUG", "INFO", "WARNING", "ERROR")]),
    ]),
]
FIELDS = {f["key"]: f for group in GROUPS for f in group["fields"]}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def validate_changes(changes: dict, cfg: AppConfig) -> None:
    if not isinstance(changes, dict) or not changes:
        raise ValueError("请至少修改一项设置。")
    defaults = AppConfig()
    for key, value in changes.items():
        if key not in FIELDS:
            raise ValueError("包含不可通过界面修改的设置。")
        spec = FIELDS[key]
        section, name = key.split(".")
        default = getattr(getattr(defaults, section), name)
        if isinstance(default, bool):
            valid = type(value) is bool
        elif isinstance(default, int):
            valid = type(value) is int
        elif isinstance(default, float):
            valid = type(value) in (int, float) and math.isfinite(value)
        elif isinstance(default, list):
            valid = isinstance(value, list) and len(value) <= 100 and all(isinstance(v, str) and len(v) <= 200 for v in value)
        else:
            valid = isinstance(value, str) and len(value) <= (12000 if spec["kind"] == "textarea" else 200)
        if not valid:
            raise ValueError(f"{spec['label']}的输入类型或长度不正确。")
        if spec["min"] is not None and value < spec["min"] or spec["max"] is not None and value > spec["max"]:
            raise ValueError(f"{spec['label']}应在 {spec['min']} 到 {spec['max']} 之间。")
        if spec["options"] and value not in [v[0] for v in spec["options"]]:
            raise ValueError(f"{spec['label']}不是可选值。")
        setattr(getattr(cfg, section), name, value)
    if not cfg.deepseek.model.strip():
        raise ValueError("模型名称不能为空。")
    if cfg.humanize.split_max_chars < cfg.humanize.split_min_chars:
        raise ValueError("分条的最长字数不能小于最短字数。")
    if sum((cfg.proactive.text_chance, cfg.proactive.sticker_chance, cfg.proactive.poke_chance)) > 1.000001:
        raise ValueError("主动文字、表情和拍一拍的概率之和不能超过 100%。")
    if cfg.search.max_results_for_model > cfg.search.count:
        raise ValueError("提供给模型的搜索结果数不能超过每次搜索结果数。")
    cfg._validate()


def patch_yaml(text: str, changes: dict) -> str:
    """Patch only selected value spans; retain comments, personas and all keys."""
    if any(isinstance(t, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)) for t in yaml.scan(text)):
        raise ValueError("该配置含 YAML 引用，请先在配置文件中展开引用后再使用表单保存。")
    root = yaml.compose(text)
    if root is not None and not isinstance(root, yaml.MappingNode):
        raise ValueError("配置根节点必须是字典。")
    sections = {k.value: v for k, v in root.value} if root else {}
    newline = "\r\n" if "\r\n" in text else "\n"
    additions = {}
    replacements = []
    for key, value in changes.items():
        section, name = key.split(".")
        node = sections.get(section)
        values = {k.value: v for k, v in node.value} if isinstance(node, yaml.MappingNode) else {}
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if name in values:
            old = values[name]
            original = text[old.start_mark.index:old.end_mark.index]
            replacements.append((old.start_mark.index, old.end_mark.index,
                                 encoded + (newline if original.endswith("\n") else "")))
        else:
            additions.setdefault(section, []).append(f"  {name}: {encoded}{newline}")
    for section, lines in additions.items():
        node = sections.get(section)
        if isinstance(node, yaml.MappingNode):
            pos = node.end_mark.index
            replacements.append((pos, pos, (newline if pos and text[pos-1] != "\n" else "") + "".join(lines)))
        elif node is not None:
            replacements.append((node.start_mark.index, node.end_mark.index, newline + "".join(lines)))
        else:
            replacements.append((len(text), len(text), newline + section + ":" + newline + "".join(lines)))
    for start, end, value in sorted(replacements, key=lambda op: (op[0], op[1]), reverse=True):
        text = text[:start] + value + text[end:]
    return text


class SettingsEditor:
    def __init__(self, cfg: AppConfig):
        self.fallback = cfg
        self.path = Path(cfg.source_path).resolve() if cfg.source_path else None
        self.undo = None

    def read(self):
        raw = self.path.read_bytes() if self.path else b""
        data = yaml.safe_load(raw.decode("utf-8-sig")) or {} if raw else self.fallback.to_dict()
        cfg = AppConfig.from_dict(data)
        return raw, data, cfg

    def snapshot(self):
        raw, _, cfg = self.read()
        defaults = AppConfig()
        groups = deepcopy(GROUPS)
        for group in groups:
            for spec in group["fields"]:
                section, name = spec["key"].split(".")
                spec["value"] = getattr(getattr(cfg, section), name)
                spec["default"] = getattr(getattr(defaults, section), name)
        return dict(groups=groups, revision=digest(raw), writable=self.path is not None,
                    can_undo=self.undo is not None and self.undo[1] == digest(raw), restart_required=True)

    def _write(self, raw: bytes):
        temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_bytes(raw)
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def save(self, revision: str, changes: dict):
        if self.path is None:
            raise ValueError("请通过配置文件启动维护台后再保存设置。")
        with file_lock(self.path.with_suffix(".manage.lock")):
            raw, _, cfg = self.read()
            if revision != digest(raw):
                raise SettingsConflict("配置已被其他窗口或程序修改，请重新读取后再保存。")
            validate_changes(changes, cfg)
            updated = patch_yaml(raw.decode("utf-8-sig"), changes).encode("utf-8")
            # Validate the actual edited document, including preserved sections.
            AppConfig.from_dict(yaml.safe_load(updated.decode("utf-8")) or {})
            self._write(updated)
            self.undo = (raw, digest(updated))
        return self.snapshot()

    def restore(self, revision: str):
        if self.path is None or self.undo is None:
            raise ValueError("没有可恢复的上一次设置。")
        with file_lock(self.path.with_suffix(".manage.lock")):
            current = self.path.read_bytes()
            if revision != digest(current) or digest(current) != self.undo[1]:
                raise SettingsConflict("配置已再次变化，无法直接恢复，请重新读取。")
            self._write(self.undo[0])
            self.undo = None
        return self.snapshot()
