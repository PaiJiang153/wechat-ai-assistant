# 模块接口契约

## 数据与会话隔离

适配器只产生 `IncomingMessage`，包含稳定的 `room_id`、发送者 ID、消息 ID、时间和可用的 @ 信息。
群名用作显示和配置匹配；微信消息身份由会话、分片和 local_id 组合，排序使用 sort_seq。

`ContextStore.lock_for(room_id)` 返回该会话的可重入锁。普通回复、主动回复、重置和确认发送后的历史提交共享该锁。
同一会话的接收媒体任务和回复任务均按队列顺序执行；不同会话可并行。

## 生成与发送分离

```python
result = engine.generate(batch, commit=False)
result.room_id  # 当前会话
list(result)    # 文本分条
result.images  # 仅属于本次请求的图片
```

`GenerationResult` 兼容原有文本列表。机器人使用结果自带的图片，不使用全局图片缓冲。
`generate(..., commit=True)` 是网页/旧调用者的同步兼容入口；真实机器人必须使用 `commit=False`。
只有发送确认后，才调用 `engine.commit_reply(room_id, delivered_text)`。

`engine.pop_images()` 仅保留为旧调用者的线程局部兼容入口，新代码不得依赖其跨请求传递附件。
工具调用轮数和每轮执行数量均受限，每个返回的 tool_call 都必须有对应的工具结果。

## 适配器发送契约

```python
receipt = adapter.send_text(room_id, text, at_wxid=None)
receipt = adapter.send_file(room_id, path)
receipt = adapter.poke(room_id)
```

当前微信适配器返回 `SendReceipt`（Future），由 GUI 线程执行后完成，结果为 `DeliveryResult`：

- `confirmed`：当前发送调用完成验证。
- `failed`：明确失败，例如队列已满。
- `unknown`：已尝试发送但未确认，禁止在机器人层自动重发。
- `expired`：过期、停止或尚未执行的任务已取消。

同步适配器可以返回 `DeliveryResult`。历史同步适配器返回 `None` 时按其旧发送契约兼容，不能据此声称具有微信数据库确认能力。
GUI 句柄只在创建它的线程使用；数据库和媒体下载器按线程独立建立。
未知发送会话必须报错；群名模糊匹配和回退到第一个群均不允许。
启动校验账号后调用 `on_self_wxid(actual_wxid)`，同步真实身份给引擎。

## 配置与维护

YAML 由 `AppConfig` 解析与校验。新增配置必须有默认值，兼容用户已有 config.yaml，更新两个示例和使用文档。
环境变量 DEEPSEEK_API_KEY 优先于文件，日志和网页不得返回完整 Key。
记忆保存使用原子替换，人工修订和后台提交共享文件锁并比较 revision。
维护清理默认只预览，不在正常机器人运行流程删除历史数据。

## 测试要求

使用 `.venv/Scripts/python.exe -m pytest -q`。测试必须使用临时目录、假微信和假模型；并发测试用事件/屏障明确控制交错。
接收队列、发送回执、恢复游标、同序号多消息、人工记忆修订、模板可加载和清理边界均属于回归范围。
真机端到端脚本会发送真实消息，应与离线测试分开执行。
