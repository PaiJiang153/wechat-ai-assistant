# 启动本地网页测试台（不需要微信、不需要扫码）
# 用法：powershell -ExecutionPolicy Bypass -File .\scripts\run_web.ps1
param([int]$Port = 8000)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "请先运行 .\scripts\setup.ps1 安装依赖" }
Start-Process "http://127.0.0.1:$Port"
& $py -m src.main web --port $Port
