# 运行维护与本轮优化

日常维护现在可以通过可视化界面完成：双击 `start_manage.bat`，打开状态、记忆、清理、备份、日志和高级设置页面。见 [可视化维护台](MAINTENANCE_UI.md)。

## 快速查看状态

```powershell
.\.venv\Scripts\python.exe -m src.main status
```

输出最近的机器人状态快照、今日请求与 Token 用量、发送确认数、失败数、未确认数和累计排队秒数。
机器人运行时每分钟更新 `logs/stats.json`。用量在 `logs/usage.json`，发送指标在 `logs/delivery-metrics.json`，保留最近 90 天。
网页测试台侧栏也会显示今日文本模型 Token。该值不含独立视觉客户端和生图供应商的账单；金额以服务商后台为准。

## 消息处理规则

- 每次生成的文字与图片绑定到自己的会话，不通过全局图片列表传递。
- 同一会话按顺序处理；普通回复和主动回复使用同一会话锁。
- 数据库接收、耗时媒体解析、窗口发送分别运行；数据库连接按线程独立创建。
- 按 `sort_seq` 和 `shard + local_id` 识别新消息，分页追赶积压。同一排序值可包含多条消息。
- 消息回调成功后保存接收游标到 `data/state/receive-<账号摘要>.json`；重启恢复已经提交的游标。
- 首次启动没有游标时跳过历史消息。重启后的未处理积压仍受消息过期规则约束。
- 接收队列满时保留读取位置，下轮继续；回复队列满、限流或消息过期时明确记录跳过原因。
- 持续聊天也不无限延长合并窗口：`chat.max_debounce_seconds` 默认 10 秒。
- 文本和图片的 GUI 操作由发送线程执行，使用依赖的数据库回读验证。
- 状态为 `unknown` 时不自动重发，以避免“已发送但数据库暂未确认”造成重复消息。
- 只有确认送达的文本/图片标记才写入机器人发言历史。已超时但仍运行的文本发送若稍后确认，会补记历史。
- 旧同步适配器仍兼容；它们没有当前微信适配器的完整异步回执保证。

常用配置（已有配置会自动采用默认值，无须覆盖）：

```yaml
chat:
  max_debounce_seconds: 10
  max_pending_messages: 100
  max_queued_batches: 64
  message_ttl_seconds: 300
  delivery_timeout: 180
wechat:
  queue_size: 256
  media_workers: 2
  task_ttl_seconds: 300
  state_dir: data/state
```

`task_ttl_seconds` 限制排队任务的有效期；模型和下载另外使用各自的网络超时。
正在运行的本地语音识别和第三方窗口操作无法由线程强行中止，停止时会取消未开始的任务并等待在途操作返回。
不要把 `delivery_timeout` 调得低于正常窗口操作耗时。

## 群聊 @ 和会话选择

启动后把实际账号 ID 同步给回复引擎。接收端优先读取 @ 元数据；当前依赖缺少元数据时，按实际登录昵称和 @ 边界识别。
发送端把成员 ID 映射为群内显示名，调用 `at_member(..., verify=True)`。
群名必须精确匹配；同名群拒绝启动该目标，未知发送目标报错，不回退到第一个群。
每群的 `reply_with_at` 会覆盖全局配置，私人聊天不 @ 对方。

## 主动聊天与长期记忆

主动聊天只选择已经安静达到 `min_gap_minutes`、没有待处理回复、没有触发限流的会话。
发送时再次取得同一会话锁并检查条件。每日数量与表情使用量保存在 `data/state/proactive.json`，重启继续计数。

查看已知会话与档案：

```powershell
.\.venv\Scripts\python.exe -m src.main memory list
.\.venv\Scripts\python.exe -m src.main memory show --room "会话ID"
```

完整替换该会话的事实，或只保留希望留下的事实（省略的事实即删除）：

```powershell
.\.venv\Scripts\python.exe -m src.main memory set --room "会话ID" --fact "小明喜欢猫" --fact "小明正在准备考试"
.\.venv\Scripts\python.exe -m src.main memory clear --room "会话ID"
```

人工修改带版本号与来源；后台提取提交前检查版本，避免覆盖人工修订。
合法 JSON 数组被视为模型整理后的完整档案，`[]` 表示清空；不会把已经删掉的旧事实重新追加回来。
来源信息保存在档案的 `evidence` 字段中；自动来源记录对话时间范围，人工来源标记为 `manual`。

## 数据保留与备份

默认保留全部聊天与记忆，媒体保留策略为 30 天。**不会在后台自动删除数据**。

```yaml
retention:
  chat_days: 0
  memory_days: 0
  media_days: 30
```

清理前停止机器人，先预览，确认后执行：

```powershell
.\.venv\Scripts\python.exe -m src.main cleanup
.\.venv\Scripts\python.exe -m src.main cleanup --apply
```

清理只扫描配置的数据目录，跳过符号链接、配额记录和锁文件。`0` 表示不清理。
备份也建议在停止后执行，以获得跨文件一致的快照：

```powershell
.\.venv\Scripts\python.exe -m src.main backup --output "dist/chat-backup.zip"
```

备份包括聊天、记忆、表情索引与运行状态，不含 Key、config.yaml 和原始媒体。恢复时停止机器人，将对应目录解压回配置的数据目录。

## 依赖与验证

- `requirements.txt`：网页和控制台基础依赖。
- `requirements-wechat.txt`：增加当前微信接入。
- `requirements-media.txt`：增加语音与文件解析。
- `requirements-legacy.txt`：维护历史接入时才安装。
- `requirements-dev.txt`：基础依赖及 pytest。
- `constraints-tested.txt`：本机 Windows / Python 3.13.7 的已验证版本，不代表其他 Python 版本已验证。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

测试使用假微信、假模型与临时目录，覆盖并发、失败、游标恢复和数据维护，不调用付费 API，不向真实联系人发消息。
微信窗口切换、真实群成员 @ 选择和模型供应商连通性仍需在对应实际环境验收。
