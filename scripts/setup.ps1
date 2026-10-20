# 一键初始化：创建虚拟环境并安装依赖
# 用法：右键「使用 PowerShell 运行」，或在项目目录执行
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
param([ValidateSet("core", "wechat", "full", "legacy")][string]$Profile = "wechat")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> 项目目录: $root" -ForegroundColor Cyan

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "==> 创建虚拟环境 .venv ..." -ForegroundColor Cyan
    python -m venv .venv
    if (-not (Test-Path $py)) { throw "虚拟环境创建失败，请确认已安装 Python 3.10+ 并加入 PATH" }
}

Write-Host "==> 安装依赖 ($Profile) ..." -ForegroundColor Cyan
$requirements = @{ core = "requirements.txt"; wechat = "requirements-wechat.txt"; full = "requirements-media.txt"; legacy = "requirements-legacy.txt" }
& $py -m pip install -r $requirements[$Profile]
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败，请检查网络/代理" }

if (-not (Test-Path (Join-Path $root "config.yaml"))) {
    Copy-Item -LiteralPath (Join-Path $root "config.minimal.yaml") -Destination (Join-Path $root "config.yaml")
    Write-Host "==> 已创建最小配置 config.yaml，请填入 Key、小号昵称和目标群。" -ForegroundColor Yellow
}

Write-Host "==> 自检 ..." -ForegroundColor Cyan
& $py -m src.main check
Write-Host ""
Write-Host "完成。接下来：" -ForegroundColor Green
Write-Host "  1) 编辑 config.yaml，填入 deepseek.api_key"
Write-Host "  2) .\scripts\run_web.ps1     网页里先试玩三人群聊"
Write-Host "  3) .\scripts\run_bot.ps1     真机接入微信（保持桌面可交互）"
Write-Host "  4) 语音/文件解析需再次运行 setup.ps1 -Profile full"
