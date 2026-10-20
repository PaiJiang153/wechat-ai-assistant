# 部署与更新

真实微信接入需要 **Windows、已登录的微信客户端和可交互桌面**。本地维护台、网页测试台和模拟测试可独立运行。已验证 Python 3.13.7（64 位）；不保证所有微信版本均兼容。

## 从仓库安装

```powershell
git clone https://github.com/PaiJiang153/wechat-ai-assistant.git
cd wechat-ai-assistant
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Profile core
.\start_manage.bat
```

打开 <http://127.0.0.1:8010/manage>。安装脚本只在配置不存在时创建 `config.yaml`，已有配置不覆盖。

| 安装档位 | 用途 |
| --- | --- |
| `core` | 本地维护台、网页和控制台测试 |
| `wechat`（默认） | 基础功能 + 当前 Windows 微信桥接 |
| `full` | 微信接入 + 语音和文件解析 |
| `legacy` | 历史适配器依赖，供兼容排查 |

真实微信另运行 `setup.ps1 -Profile wechat`。从官方渠道安装微信，登录专用小号，填写自己的配置：

```yaml
deepseek:
  api_key: ""  # 建议通过环境变量 DEEPSEEK_API_KEY 设置
  model: "deepseek-chat"
wechat:
  adapter: "wechatauto"
  self_name: "机器人小号昵称"
  groups:
    - name: "你的群名"
      trigger: "mention"
```

群名必须精确匹配，同名群先改名。模型与可选功能以供应商支持为准。

```powershell
$env:DEEPSEEK_API_KEY = "你的 Key"
.\.venv\Scripts\python.exe -m src.main check
.\start_bot.bat
```

环境变量须在启动进程可继承的环境设置；维护台不会保存 API Key。网页和控制台发送消息会调用模型。

## Windows 服务器

选择带桌面的 Windows 环境，先验证微信能登录、目标会话能正确收发。锁屏、休眠或远程桌面断开可能影响 GUI 发送，应在自己的环境实测。

维护台保持绑定本机地址，不需要开放公网端口。模型、搜索、生图和可选模型下载需要对应的出站网络。自启动见 [STARTUP.md](STARTUP.md)；登录触发的任务不能代替 Windows 登录和微信登录。

## 更新与备份

1. 停止机器人和维护台，使用维护功能备份聊天与记忆，另行妥善保存自己的配置。
2. 查看 `git status`，处理自己修改的代码后执行 `git pull --ff-only`。
3. 重新运行对应档位的安装脚本，再自检、启动。

新配置请对照模板合并，不要覆盖实际配置。备份可能含私密对话，不应上传公开仓库或 Issue。

## 源码打包

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\package.ps1
```

默认部署包不含实际配置、数据、日志、虚拟环境、个人历史排查材料或旧聊天截图。首次运行仍需联网安装依赖。`-WithConfig` 只适合自己迁移，包可能含密钥，不能公开。

运行状态、备份和清理见 [RELIABILITY.md](RELIABILITY.md)。
