# 联网搜索

大肥鱼现在能自己上网搜了——**模型自己决定什么时候搜**，闲聊不搜，问新闻/实时数据才搜。

## 实测（真实调用）

提问：`帮我查一下今天AI圈有什么新闻`

```
INFO dsbot.search | 联网搜索「今天 AI 行业新闻」→ 8 条结果
INFO dsbot.engine | 联网搜索：今天 AI 行业新闻
INFO dsbot.search | 联网搜索「AI 公司 最新发布 动态」→ 8 条结果

🤖 查完了，就这几条：老黄说AI不是泡沫、Anthropic被它自己放话
   说十年内灭绝人类概率超10%、还有咱家DeepSeek悄悄上线新模型——这个我熟。
```

模型一次问了两个角度（中英文/主题拆分），各拿 8 条结果，再用人设口吻总结。

## 实现

```
用户消息
   │
   ▼
DeepSeek Tool Calls：模型判断要不要搜
   │ 需要 → 调用 web_search({query})
   ▼
DeepSeek 官方 Search API（POST /v1/tools/search，自研搜索引擎）
   │ 返回 title / url / summary / date
   ▼
结果整理成"参考资料"塞回对话（含来源链接）
   │
   ▼
模型基于资料 + 人设 → 简短回答（不罗列链接）
```

- 用的是 **DeepSeek 官方 Search API**，和模型同一个 Key，不用另外申请搜索服务；
- 结果在 `search_and_format` 里压缩成最多 4 条、2500 字，避免把上下文撑爆；
- 同一关键词 **5 分钟内缓存**，不重复计费；
- 模型最多搜 **2 轮**、每轮最多 2 个查询，防止钻牛角尖。

## 配置

```yaml
search:
  enabled: true
  count: 8                  # 每次搜索取多少条（1~50）
  max_results_for_model: 4  # 最终喂给模型几条
  max_chars: 2500           # 结果文本上限
  max_per_minute: 10        # 限流
  cache_ttl: 300            # 缓存 5 分钟
  max_tool_rounds: 2        # 最多搜索轮数
  need_content: false       # 是否要网页正文（更慢更贵，一般不用）
```

## 成本

| 项目 | 费用 |
| --- | --- |
| Search API | **5 元 / 1000 次成功请求**（无结果不计费） |
| 模型调用 | 每轮搜索会多一次模型调用（约 1~2k token） |

日常聊天一次搜索总成本不到 1 分钱；`max_per_minute` 和缓存能防止刷。

## 注意

- 搜索**只在模型认为需要时发生**：问"今天天气/股价/最新新闻/某事件进展"会搜；问"1+1"、闲聊不搜；
- 搜索结果属于外部网页内容，可能包含过期或错误信息，模型会标注来源链接；
- 如果 Search API 返回 429/5xx 会自动退避重试（共 3 次），最终失败会回答"没搜到"；
- 关掉：`search.enabled: false`，就回到纯模型回答。

相关文件：`src/websearch.py`、`src/engine.py`（`_generate_with_search`）、`tests/test_search.py`。
