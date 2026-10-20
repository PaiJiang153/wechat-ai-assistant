# 启动、停止与自启动

## 日常使用

打开微信并登录配置中的机器人小号，保持桌面可交互，双击项目根目录的 `start_bot.bat`。`wechat.self_name` 必须与账号昵称一致；不一致时修正账号或配置。

维护台单独双击 `start_manage.bat`，地址 <http://127.0.0.1:8010/manage>。打开维护台不会启动机器人。

在机器人前台窗口按 `Ctrl+C`，等退出后再重启。高级设置保存后也需要重启机器人。维护台的退出按钮仅停止维护服务。

后台自启动实例可在任务管理器按虚拟环境的完整路径识别进程，再结束对应进程；不要误关其他项目的 Python。不要重复启动仍在运行的实例。

## 登录 Windows 后自动启动

需要时在项目目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
```

任务在 Windows 登录后运行，脚本会等待微信；不能自动完成微信扫码登录，也不能保证锁屏或远程桌面断开后的发送。移动项目目录后应重新注册。

取消：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_autostart.ps1
```

## 排查

| 现象 | 检查 |
| --- | --- |
| 账号不匹配 | 微信登录账号与 `wechat.self_name` |
| 找不到会话 | 群名、备注是否精确匹配，是否存在同名目标 |
| 收到消息但没回复 | 桌面、触发规则、限流、回执及 `logs/bot.log` |
| 提示已有实例 | 先确认旧机器人是否还在运行 |
| 维护台打不开 | 基础依赖、8010 端口及 `logs/manage-error.log` |

更多步骤见 [RELIABILITY.md](RELIABILITY.md)。
