# 多群 + 每群独立人设

一个机器人小号可以同时挂**多个群**，每个群可以：
- 用**不同的人设**（不同性格、不同称呼、不同口气）
- 用**不同的触发规则**（每条都回 / 只被 @ 时回 / 指定前缀才回）

## 配置示例

```yaml
# 人设库：id -> 人设
personas:
  da_feiyu:
    bot_name: "大肥鱼"
    system_prompt: |
      你是「大肥鱼」……（完整人设见 docs/PERSONA.md）
  assistant:
    bot_name: "助手"
    system_prompt: |
      你是一个靠谱的助手，回复简洁专业、重点明确，不玩梗。
  tsundere:
    bot_name: "小傲娇"
    system_prompt: |
      你是个嘴硬心软的傲娇，说话带刺但会帮忙。

# 默认人设（群没指定 persona 时用）
persona:
  bot_name: "DeepSeek"
  system_prompt: ""

wechat:
  groups:
    # 群 1：大肥鱼，每条都回
    - name: "日常聊天群"
      persona: "da_feiyu"
      trigger: "all"

    # 群 2：正经助手，只在被 @ 时回
    - name: "工作交流群"
      persona: "assistant"
      trigger: "mention"

    # 群 3：傲娇人设，发 "ds" 开头才回
    - name: "朋友闲聊群"
      persona: "tsundere"
      trigger: "prefix"
      prefix: "ds"

  self_name: "机器人小号"   # 机器人小号昵称（全局唯一，防止误连主号）
```

## 规则说明

| 字段 | 说明 |
| --- | --- |
| `groups[].name` | 群名，必须和微信里**一字不差**（空格、符号都算） |
| `groups[].persona` | 引用 `personas` 里的 id；不填就用默认 `persona` |
| `groups[].trigger` | `all` / `mention` / `prefix`，不填用 `chat.trigger` |
| `groups[].prefix` | `trigger: prefix` 时的触发词，不填用 `chat.prefix` |
| `personas.<id>.bot_name` | 这个群里它叫什么、回复时 @ 谁（mention 规则也用它匹配） |
| `personas.<id>.system_prompt` | 人设正文（可用多行 `|`） |
| `personas.<id>.members` | 群里其他人的昵称（仅内置模板用得到，自定义人设可忽略） |

## 兼容旧配置

只挂一个群时，仍然可以用旧写法：

```yaml
wechat:
  group_name: "日常聊天群"
```

此时用默认 `persona`（或 `persona.system_prompt`）。

## 注意事项

1. **上下文按群隔离**：每个群有独立的对话记忆（`data/rooms/<hash>.json`），互不串味；
2. **每个群独立游标**：某个群读取失败不会影响其他群；
3. **发送按群路由**：回复会发回对应群的窗口，不会发错群；
4. **`self_name` 是全局的**：同一个微信小号登录，所以它只有一个昵称；
5. **群不存在会跳过**：`groups` 里某个群名在微信里找不到时，启动日志会警告并跳过该群，其余群照常工作；
6. **人设不存在会启动失败**：写错 `persona` id 会直接报错并列出可选人设，避免静默用错人设。

## 怎么验证

```powershell
# 看当前配置解析出的群和人设
.\.venv\Scripts\python.exe -m src.main check

# 输出示例：
# 监听群        : 2 个
#    - 日常聊天群  [人设:da_feiyu / 触发:all / 称呼:大肥鱼]
#    - 工作交流群      [人设:assistant / 触发:mention / 称呼:助手]
```

改完重启机器人（`start_bot.bat`）生效。相关代码：`src/config.py`（`GroupConfig` / `persona_for`）、`src/engine.py`（按群取人设和触发规则）、`src/wechatauto_adapter.py`（多群轮询与发送路由），测试见 `tests/test_multi_group.py`。
