# 让你的 AI 拉取复刻

仓库：<https://github.com/PaiJiang153/wechat-ai-assistant>

把下面这段话直接交给能操作本地文件和终端的 AI：

```text
请克隆 https://github.com/PaiJiang153/wechat-ai-assistant 到新目录，
阅读 AGENTS.md、README.md、docs/AI_QUICKSTART.md 和 docs/CONTRACT.md。
先用 Python 3.13 创建虚拟环境，安装 requirements-dev.txt，运行全部测试。
在 config.yaml 不存在时复制 config.minimal.yaml，启动本地维护台，
检查 /manage/health 并告诉我打开地址。暂时不连接微信、不发送消息。
保留已有配置和数据，不把密钥、日志、聊天记录提交到 Git。
完成后，根据我提供的模型 Key、小号昵称和目标会话配置实际使用环境。
```

## 最小可验证流程（Windows PowerShell）

```powershell
git clone https://github.com/PaiJiang153/wechat-ai-assistant.git
cd wechat-ai-assistant
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
if (-not (Test-Path config.yaml)) { Copy-Item config.minimal.yaml config.yaml }
.\.venv\Scripts\python.exe -m src.main manage --port 8010
```

打开 <http://127.0.0.1:8010/manage>。健康检查为 <http://127.0.0.1:8010/manage/health>，返回 `application: dsbot-management`。维护台不需要模型 Key，也不会启动自动回复。端口被占用时换成 `--port 8011`，并使用对应地址。按 `Ctrl+C` 停止前台服务。

只体验维护台时，把安装文件改成 `requirements.txt` 即可。Linux/macOS 上可使用 `.venv/bin/python` 执行同一流程；真实微信桥接和 PowerShell 启动脚本只面向 Windows。

## 接入自己的服务与微信

1. 在终端设置 `DEEPSEEK_API_KEY`，或在本地 `config.yaml` 填写 `deepseek.api_key`。其他 OpenAI 兼容服务同时配置 `deepseek.base_url`、`model`，能力以供应商为准。
2. 用 `python -m src.main web` 打开本地聊天测试台；发消息会调用所配置的模型。
3. 需要真实微信时，在 Windows 安装 `requirements-wechat.txt`，登录专用小号；填写 `wechat.self_name` 和精确匹配的 `wechat.groups` / `private_chats`。
4. 用户确定要开启自动回复后，执行 `python -m src.main run`。可选语音、文件功能另安装 `requirements-media.txt` 并显式开启配置。

高级设置提供行为参数；密钥、路径、目标会话和完整人设仍在本地配置文件中设置。保存高级参数后需要重启机器人才能应用到正在运行的机器人进程。

## 验收清单

- 全部模拟测试通过；没有下载模型、调用付费服务或给真实联系人发消息。
- 本地维护台能打开，健康检查成功，未填写密钥也可使用维护功能。
- 真实微信接入应单独验证账号、会话和发送回执；模拟测试通过不代表所有微信版本均兼容。
- `git status --short` 中没有实际配置、聊天数据、备份或日志；运行 `python scripts/verify_public.py` 复查。

## 修改入口

| 需求 | 首先查看 |
| --- | --- |
| 改默认参数 / 增加配置项 | `src/config.py`、两个配置模板 |
| 让参数出现在维护台 | `src/settings_ui.py`、`web/static/js/manage.js` |
| 改回复和人设行为 | `src/engine.py`、`docs/PERSONA.md` |
| 改发送、限流或消息合并 | `src/bot.py`、`src/runtime.py` |
| 适配新版微信 | `src/wechatauto_adapter.py`、适配器模拟测试 |
| 改维护接口与页面 | `web/manage.py`、`web/templates/manage.html`、对应 CSS/JS |

接口约定见 [CONTRACT.md](CONTRACT.md)，部署见 [DEPLOY.md](DEPLOY.md)，维护见 [MAINTENANCE_UI.md](MAINTENANCE_UI.md)。
