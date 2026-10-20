# 拟人化：怎么让它更像真人

微信自动化本身已经有拟人节奏（`wechatauto` 内置）：鼠标曲线移动、落点随机、按键
停顿不均、发消息有节流。所以这里只补「聊天行为」层面的真人感。

## 已实现

| 行为 | 说明 | 默认 |
| --- | --- | --- |
| **打字停顿** | 回复前等 1~3 秒，再按字数折算打字时间（9 字/秒，最多 8 秒） | 开 |
| **偶尔分两条发** | 回复 18~140 字时，30% 概率在标点处拆成两条，间隔 0.6~1.8 秒 | 开 |
| **偶尔补一句** | 主回复发完 2.5~6 秒后，10% 概率补一句"对了/算了 没事/你继续说" | 开 |
| **不总 @ 人** | 群里回复时只有 70% 概率 @ 发言人（`reply_with_at` 打开时才生效） | 开 |
| **合并窗口抖动** | 连续消息合并的 2.5 秒窗口随机 +0~1 秒 | 开 |
| **时间感知** | 人设上下文注入「当前时间/星期」，会说早晚安、催睡觉 | 开 |
| **已读不回** | 默认关闭；打开后每条约 5% 概率不回（被 @ 一定回），更像真人 | 关 |

## 配置

```yaml
chat:
  time_aware: true          # 注入当前时间

humanize:
  enabled: true
  typing_min: 1.0           # 打字前思考时间
  typing_max: 3.0
  typing_chars_per_second: 9.0
  max_typing_seconds: 8.0
  split_chance: 0.30        # 拆成两条的概率
  split_min_chars: 18
  split_max_chars: 140
  split_delay_min: 0.6
  split_delay_max: 1.8
  double_text_chance: 0.10  # 补一句的概率
  double_text_delay_min: 2.5
  double_text_delay_max: 6.0
  silence_chance: 0.0       # 偶尔不回（0=每条都回）
  at_chance: 0.7            # @ 人的概率
  debounce_jitter: 1.0      # 合并窗口抖动
```

## 怎么调

| 想要的效果 | 改什么 |
| --- | --- |
| 回复更快 | 调小 `typing_min/max`、`split_chance: 0`、`double_text_chance: 0` |
| 更像手打 | 调大 `typing_chars_per_second` 的倒数（改成 6~7 字/秒） |
| 更少连发两条 | 调小 `split_chance` |
| 偶尔高冷不回 | `silence_chance: 0.05`（5% 概率不理，被 @ 一定回） |
| 不想 @ 人 | `at_chance: 0` |
| 完全不搞拟人 | `humanize.enabled: false` |

## 实测示例

同一条回复，开启前后：

```
开启前：写作业呢，烦死了。你找我干嘛，不会又想让我帮你跑腿吧。   （一次发完）

开启后：写作业呢，烦死了。                                    （第 1 条）
        （等 1.2 秒）
        你找我干嘛，不会又想让我帮你跑腿吧。                    （第 2 条）
        （等 2.5~6 秒，10% 概率）
        对了                                                      （补一句）
```

## 注意

- 打字停顿是"先 sleep 再发"，不显示微信的"对方正在输入…"（wechatauto 不支持设置输入状态）；
- 拆分只在**短回复**里做；超过 `max_message_chars` 的长回复仍走原来的分条逻辑；
- 私聊和群聊共用同一套拟人参数（按群名/私聊名区分不了，也没必要）；
- 时间感知只注入当前时间，不会伪造日期。

相关代码：`src/bot.py`（`split_reply_in_two`、`_typing_delay`、`_humanize_chunks`）、`src/engine.py`（时间注入）、`tests/test_humanize.py`。
