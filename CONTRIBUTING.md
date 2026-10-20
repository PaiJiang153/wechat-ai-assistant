# 参与贡献

欢迎通过 Issue 描述问题，或 Fork 仓库后提交 Pull Request。开始前请阅读 [README](README.md)、[AI 接手指南](docs/AI_QUICKSTART.md) 和 [接口约定](docs/CONTRACT.md)。

## 本地开发

使用 Python 3.13 创建虚拟环境，然后安装和测试：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/verify_public.py
```

开发依赖不包含真实微信桥接和语音模型。需要实际接入时另装相应依赖。测试应使用临时目录、模拟模型与模拟适配器，避免依赖个人微信、付费接口或网络。不要覆盖已有的 `config.yaml`。

## 提交要求

- 一次改动聚焦一个问题，说明修改前后的行为和验证结果。
- 对消息顺序、回执、配置写入或并发逻辑的改动，补充对应的回归测试。
- 配置或命令变化时更新示例和文档；新增第三方依赖时说明用途。
- Windows PowerShell 脚本使用 UTF-8 BOM，并保持 5.1 兼容。
- 截图与复现材料使用虚构账号、会话和密钥。勿上传真实配置、微信数据库、日志原件或聊天记录。
- 允许 AI 协助，但请核实结果，并在 PR 中说明实际执行的验证。

## 报告问题

请提供操作系统、Python 版本、安装的依赖档位、复现步骤和期望行为。涉及微信适配时补充客户端版本和适配器版本。日志请先脱敏，不要把完整 `config.yaml` 或私密截图贴到公开 Issue。

安全问题见 [SECURITY.md](SECURITY.md)。提交贡献表示你同意将该贡献按本项目的 [MIT 许可证](LICENSE) 提供。
