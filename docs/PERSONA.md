# 设置自己的聊天人设

每个群和联系人可以引用不同的人设，未指定时使用 `persona` 默认配置。人设是模型提示词，不会修改微信账号昵称。

```yaml
persona:
  bot_name: "聊天助手"
  members: ["小明", "小红"]
  system_prompt: |
    你是群里的 AI 聊天助手，名字叫「聊天助手」。
    用自然的中文回复，一次一到三句，不用 Markdown。
    闲聊时轻松一点，讨论具体问题时认真回答。
    不确定的信息直说，不编造现实经历或群友隐私。
    群友问到身份时如实说明你是 AI。
  extra_rules: ""
```

`system_prompt` 非空时直接使用该内容；留空时使用内置模板，填入 `bot_name`、`members` 与 `extra_rules`。修改后重启机器人。

## 多个人设

```yaml
personas:
  casual:
    bot_name: "小鲸"
    system_prompt: "你是虚构的小鲸鱼 AI 聊天角色，口语、简短，认真问题认真答。"
  assistant:
    bot_name: "助手"
    system_prompt: "你是简洁可靠的 AI 助手，必要时用清楚的步骤解释。"
wechat:
  groups:
    - name: "日常聊天群"
      persona: "casual"
      trigger: "mention"
    - name: "学习讨论群"
      persona: "assistant"
      trigger: "all"
```

只修改目标配置段，保留已有的其他配置。群名须精确匹配；更多规则见 [多群配置](MULTI_GROUP.md)。先用 `python -m src.main web` 测试语气，发消息会调用已配置的模型。长期记忆也可能影响回复，可在维护台检查。

完整配置中的 `da_feiyu` 是可替换的虚构角色示例 ID，不代表模型供应商的官方形象。示例昵称和对话均为虚构。


## 全局文字规则（代码强制执行）

所有聊天（群聊、私聊、主动开场）的回复都会经过统一清洗：

- 句尾**不出现中文句号「。」**（句中的句号会转成逗号，行尾逗号会去掉）；
- 感叹号、问号、省略号保留；
- 这条规则在 `src/engine.py` 的 `sanitize_reply` 里，对所有 persona 生效，不依赖提示词。
