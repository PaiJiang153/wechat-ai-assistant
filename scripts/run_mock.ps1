# 启动控制台模拟群聊（不需要微信）
# 用法：powershell -ExecutionPolicy Bypass -File .\scripts\run_mock.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "请先运行 .\scripts\setup.ps1 安装依赖" }
& $py -m src.main mock
