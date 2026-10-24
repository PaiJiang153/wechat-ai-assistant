# WeChat AI Assistant · 微信 AI 助手

[![Tests](https://github.com/PaiJiang153/wechat-ai-assistant/actions/workflows/tests.yml/badge.svg)](https://github.com/PaiJiang153/wechat-ai-assistant/actions/workflows/tests.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Windows WeChat AI assistant with per-conversation personas, persistent memory, and a local maintenance dashboard. Includes isolated tests and an [AI reproduction guide](docs/AI_QUICKSTART.md).

**让你的 AI 直接复刻：**把 [AI 接手指南](docs/AI_QUICKSTART.md) 中的提示词交给它即可。[AGENTS.md](AGENTS.md) 说明安装、验证、模块入口和配置保护规则。

![本地维护台，使用虚构测试数据](docs/screenshots/maintenance-overview-desktop.png)

在 Windows 微信小号上接收消息、调用模型，并向已配置的群或联系人回复。也可以先用本地网页或控制台测试，无需连接微信。

当前接入是 `wechatauto-replica`：读取本地微信数据库收消息，通过微信窗口发送。历史 `pyweixin` / WeChatFerry 代码保留在独立模块，默认不安装其依赖。

## 已实现

- 多群与私人聊天，每个会话可使用独立人设和触发规则。
- 消息去重、连续发言合并、最长合并等待、限流和有界队列。
- 上下文持久化、长期记忆，以及记忆查看、修订和清空。
- 可选识图、语音转文字、文件解析、搜索、生图、表情和主动聊天。
- 每个请求独立传递图片，按会话保持顺序，发送回执区分确认、失败和未确认。
- 本地日志、运行指标、今日文本模型 Token 统计、清理预览和备份。

## 首次安装

本机已验证环境：Windows、Python 3.13.7（64 位）。真实微信接入需要可交互的 Windows 桌面与已登录的微信小号。网页模式不依赖微信。

```powershell
git clone https://github.com/PaiJiang153/wechat-ai-assistant.git
cd wechat-ai-assistant

# 默认安装基础及当前微信接入依赖，首次生成 config.yaml
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1

# 仅体验网页 / 控制台
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Profile core

# 需要语音和文件解析
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Profile full
```

已有 `config.yaml` 会保留。首次配置可参考 [最小配置](config.minimal.yaml)，完整选项见 [配置模板](config.example.yaml)。

修改 `config.yaml` 中的小号昵称、目标群名，并设置模型 Key：

```powershell
$env:DEEPSEEK_API_KEY = "你的 Key"
```

也可以填入 `deepseek.api_key`。环境变量优先；不要把实际配置或备份配置公开。

```yaml
wechat:
  adapter: "wechatauto"
  self_name: "机器人小号昵称"
  groups:
    - name: "你的群名"
      trigger: "all"
```

群名精确匹配；同名群先改名。最小配置默认关闭可选功能，安装相应依赖后再按需开启。模型是否支持视觉、工具调用和生图，以实际配置的服务为准。

## 启动

**可视化维护：双击 `start_manage.bat`**，在浏览器里查看状态、管理记忆、清理数据、备份、查看日志，并调整 91 项高级参数。无需模型 Key，也不会启动自动回复。
维护台地址为 `http://127.0.0.1:8010/manage`，详细步骤见 [可视化维护台](docs/MAINTENANCE_UI.md)。

| 模式 | 操作 | 用途 |
| --- | --- | --- |
| 网页 | 双击 `start_web.bat` | 在浏览器里模拟两个人聊天，验证模型和人设 |
| 控制台 | `.\.venv\Scripts\python.exe -m src.main mock` | 无微信测试核心消息逻辑 |
| 真实微信 | 登录小号，再双击 `start_bot.bat` | 向配置的群和联系人自动回复 |
| 自检 | `.\.venv\Scripts\python.exe -m src.main check` | 配置与本机依赖检查，不调用模型 |
| 可视化维护 | 双击 `start_manage.bat` | 本地管理界面，包含维护功能和高级参数设置 |

网页默认地址为 `http://127.0.0.1:8000`。发消息会调用配置的模型服务；它不是离线模型。微信自动发送时需要桌面可交互，锁屏或远程桌面断开可能影响发送。

## 配置重点

| 配置 | 行为 |
| --- | --- |
| `chat.trigger` | `all` 每条回复、`mention` 被 @ 才回复、`prefix` 前缀触发 |
| `wechat.groups` / `private_chats` | 明确允许自动回复的目标 |
| `personas` | 可供各会话引用的人设库 |
| `chat.debounce_seconds` | 连续发言合并的安静窗口，默认 2.5 秒 |
| `chat.max_debounce_seconds` | 持续发言时最多等 10 秒 |
| `chat.max_replies_per_minute` | 每个会话每分钟发送上限，分条回复也计数 |
| `chat.split_on_comma` | 开启后把逗号/顿号也当切分点，每个片段单独发一条（拟人碎句） |
| `stickers.keyword_dir` | 关键词表情池目录：文件名即触发词（逗号分隔多个），配置后命中才发、取代随机 |
| `stickers.repeat_cooldown_minutes` | 同一张表情的最短重发间隔（默认 30 分钟，0 = 不限制） |
| `chat.message_ttl_seconds` | 过期消息不再生成或发送回复，默认 300 秒 |
| `context.window_messages` / `max_context_chars` | 历史条数与字符预算；最新超长消息同样裁剪 |
| `proactive` | 安静时主动发言；启用后遵守会话冷却与每日上限 |
| `retention` | 手动清理时的数据保留天数，0 为不清理 |

模型按实际输入、输出和工具调用计费。上下文长度、图片、搜索及生图都会影响用量；费用以供应商账单为准。

## 运行维护

```powershell
.\.venv\Scripts\python.exe -m src.main status
.\.venv\Scripts\python.exe -m src.main memory list
.\.venv\Scripts\python.exe -m src.main memory show --room "会话ID"
.\.venv\Scripts\python.exe -m src.main memory set --room "会话ID" --fact "小明喜欢猫"
.\.venv\Scripts\python.exe -m src.main memory clear --room "会话ID"

# 默认只预览；停止机器人后，再按需要添加 --apply 执行
.\.venv\Scripts\python.exe -m src.main cleanup
.\.venv\Scripts\python.exe -m src.main backup --output "dist/chat-backup.zip"
```

状态每分钟保存到 `logs/stats.json`。未确认发送不会自动重发，也不会先记成机器人已经说过的话。接收游标按账号保存，重启后继续处理未提交的消息；消息仍受过期规则限制。

完整语义与维护步骤见 [运行维护与可靠性](docs/RELIABILITY.md)。

## 项目结构

```text
src/                  消息模型、适配器、编排、模型、记忆及可选能力
web/                  Flask 本地测试台
scripts/              安装、启动、体检、自启、打包
tests/                模拟单元测试与并发、失败回归测试
config.minimal.yaml   初次使用的最小配置
config.example.yaml   完整配置示例
constraints-tested.txt 已验证的依赖版本
data/                 聊天、记忆、媒体、接收游标（自动生成）
logs/                 日志及运行指标（自动生成）
```

## 开发与打包

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
powershell -ExecutionPolicy Bypass -File .\scripts\package.ps1
```

测试使用模拟依赖，不向真实微信发消息，不调用付费模型。默认部署包排除实际配置、聊天数据、日志和虚拟环境。历史接入依赖单独放在 `requirements-legacy.txt`。

## 进一步阅读

- [可视化维护与高级设置](docs/MAINTENANCE_UI.md)

- [部署到 Windows 服务器](docs/DEPLOY.md)
- [重启和开机自启](docs/STARTUP.md)
- [模块接口](docs/CONTRACT.md)
- [多群和独立人设](docs/MULTI_GROUP.md)
- [识图](docs/VISION.md)、[语音和文件](docs/VOICE_FILE.md)
- [搜索](docs/SEARCH.md)、[生图](docs/IMAGE_GEN.md)
- [拟人节奏](docs/HUMANIZE.md)、[记忆与主动聊天](docs/MEMORY_PROACTIVE.md)

真实微信接入依赖个人客户端的非官方能力，请使用专门的小号和明确配置的会话。生成回复会把会话上下文发送到配置的模型服务。

## 开源与贡献

本项目采用 [MIT 许可证](LICENSE)。参与方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，隐私与漏洞报告见 [SECURITY.md](SECURITY.md)。

公开仓库不含个人配置、密钥、微信数据、日志或个人环境的历史排查记录。维护台截图使用模拟数据，真实客户端兼容性需在自己的 Windows 环境验证。
