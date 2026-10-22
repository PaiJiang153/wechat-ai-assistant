# 长期记忆 / 主动开场 / 表情包 / 戳一戳

## 一、长期记忆（人物档案）

**作用**：跨天聊天也不会忘。每个群/私聊单独维护一份「人物档案」，记住稳定事实：
称呼偏好、生日、喜好（吃/喝/游戏/动漫）、讨厌什么、最近在忙什么、重要日期、口头习惯。

**怎么工作**：

```
平时聊天（每收到 40 条消息）
   │
   ▼ 后台异步（不阻塞回复）
让模型读最近 60~80 条对话 + 已有档案
   │
   ▼
抽出稳定事实，合并去重 → data/memory/<hash>.json
   │
   ▼ 下次回复时
注入 system prompt：【长期记忆（之前聊到过，可以自然引用，不要逐条背出来）】
```

- 只存事实，不存原文；
- 档案按群/私聊隔离，重启不丢；
- 每 40 条才更新一次，不会每条消息都花额外 token；
- 模型会忽略一次性的闲聊、情绪、天气。

配置：

```yaml
memory:
  enabled: true
  dir: "data/memory"
  every_n_messages: 40     # 每 40 条更新一次
  max_facts: 24            # 最多 24 条事实
  max_inject_chars: 900    # 注入上限
  model: ""                # 空 = 用 deepseek.model
```

## 二、主动开场（随机时间）

**作用**：不是固定时间打卡，而是**随机间隔**主动找人聊天，内容贴合当前时间和最近聊过的事。

- 间隔在 `min_gap_minutes ~ max_gap_minutes` 之间随机（默认 45~180 分钟）；
- 首次启动随机等 3~20 分钟；
- 只在 `active_from ~ active_to` 时段发（默认 08:30~23:30，支持跨零点）；
- 每天上限 `max_per_day`（默认 8 次）；
- 每次随机选一个目标（所有已配置的群 + 私聊，或用 `targets` 限定）；
- 动作按概率随机：说句话 80% / 发表情包 15% / 戳一戳 5%（比例可调）；
- 文案由模型基于「最近上下文 + 当前时间 + 长期记忆」生成，会自然接「今天好热」这种话，不会说"我主动来找你"。

配置：

```yaml
proactive:
  enabled: true
  min_gap_minutes: 45
  max_gap_minutes: 180
  active_from: "08:30"
  active_to: "23:30"
  max_per_day: 8
  text_chance: 0.80
  sticker_chance: 0.15
  poke_chance: 0.05
  targets: []              # 空 = 所有群/私聊
```

## 三、表情包（主动发 + 聊天里顺手发）

**作用**：平时别人发的每个动画表情，机器人都会顺手把 GIF 存进这个群/私聊的**表情库**
（按 md5 去重、按出现次数排序）。需要发表情时，优先挑对方**最常用**的那几张——
风格自然就是这个聊天对象自己的风格。

发送时机：

| 时机 | 概率 |
| --- | --- |
| 主动开场（随机间隔） | `proactive.sticker_chance`（默认 15%） |
| 正常回复之后顺手补一张 | `stickers.reply_chance`（默认 6%） |
| 每天上限 | `stickers.max_send_per_day`（默认 5 张） |

**读取失败的处理**：微信的表情缓存（`cache/*/Emoticon/...`）经常是**加密字节**，
以前会被当成图片喂给 PIL 报 `cannot identify image file`。现在会先按文件头校验，
不是真图片就跳过；只有从 `cdnurl`/`thumburl` 下载到的标准 GIF 才会识别和入库，
所以不会再报这个警告，也不会把坏文件塞进表情库。

**真机验证**：发送后回读数据库，消息类型是 **动画表情**（不是文件），聊天里显示为动图：

```
send_image: 成功 图片已发送：...emoji_25279aaf2c6a1b4bd70f9169346229ad.gif
会话消息数: 3
   类型: 动画表情 | 内容: <msg><emoji md5="25279aaf2c6a1b4bd70f9169346229ad...
```

配置：

```yaml
stickers:
  enabled: true
  dir: "data/stickers"
  max_send_per_day: 5
  reply_chance: 0.06      # 正常聊天里顺手发一张的概率（0=不发）
```

## 四、戳一戳（拍一拍）

调用 wechatauto 的 `WeChat.Poke` 接口。真机验证：

```
Poke 原始返回: WxResponse {'status': '成功', 'message': '已对 小明 拍一拍'}
```

- 私聊戳对方；群里戳需要指定成员（当前实现按会话戳，群里会戳最近发言人）；
- 由主动开场的 `poke_chance` 控制概率（默认 5%）；
- 文件传输助手这类系统会话不支持戳一戳（会返回"未找到对方消息"）。

## 五、数据与成本

| 项目 | 说明 |
| --- | --- |
| 档案文件 | `data/memory/<hash>.json`（每群/私聊一个，小文件） |
| 表情库 | `data/stickers/<hash>.json` + `data/media/emoji_*.gif` |
| 额外 API 成本 | 记忆提取约每 40 条消息一次；主动开场每次一次；表情/戳一戳 0 成本 |
| 重启会丢吗 | 不会，都落盘 |

## 六、调参建议

| 想要 | 改什么 |
| --- | --- |
| 更频繁主动 | `min_gap_minutes: 20`, `max_gap_minutes: 60` |
| 少打扰 | `max_per_day: 3`, `active_from: "10:00"` |
| 不主动发表情/戳 | `sticker_chance: 0` / `poke_chance: 0` |
| 记忆更勤 | `every_n_messages: 20`（成本会高一点） |
| 关掉全部 | 对应 `enabled: false` |

相关代码：`src/memory.py`、`src/proactive.py`、`src/stickers.py`、`src/engine.py`、`src/bot.py`、`src/wechatauto_adapter.py`；测试：`tests/test_memory.py`、`tests/test_proactive.py`、`tests/test_stickers.py`。
