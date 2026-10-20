# 启动微信机器人（关机重启后就用这个）
#
# 它会自动：
#   1. 等微信客户端启动并登录（最多等 5 分钟）
#   2. 检查配置里的机器人小号昵称
#   3. 启动机器人（Ctrl+C 停止）
#
# 用法（任选其一）：
#   双击项目根目录的  start_bot.bat
#   powershell -ExecutionPolicy Bypass -File .\scripts\run_bot.ps1
param(
    [switch]$NoPause,          # 开机自启时用：结束不等待按键
    [int]$WaitMinutes = 5      # 等微信登录的最长时间
)

$ErrorActionPreference = "Stop"
# 让 PowerShell 按 UTF-8 解码第三方库（wechatauto）写到 stderr 的中文日志，避免乱码
try {
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    $OutputEncoding = [Text.Encoding]::UTF8
} catch { }
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "缺少 .venv，请先运行 .\scripts\setup.ps1" }

$adapter = & $py -c "from src.config import load_config; print(load_config().wechat.adapter)" 2>$null
Write-Host "==> 接入方式: $adapter" -ForegroundColor Cyan

if ($adapter -eq "wcf") {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    if (-not $isAdmin) {
        Write-Host "==> WeChatFerry 需要管理员权限，正在请求提权 ..." -ForegroundColor Yellow
        $argList = @("-NoExit", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $argList
        exit
    }
    & $py "scripts\check_wcf.py"
}

# ---- 等微信客户端就绪（wechatauto / pyweixin 都需要）----
function Test-WeixinRunning {
    $out = & tasklist /FI "IMAGENAME eq Weixin.exe" /NH 2>$null
    return ($out -join " ") -match "Weixin\.exe"
}

if ($adapter -in @("wechatauto", "pyweixin")) {
    if (-not (Test-WeixinRunning)) {
        Write-Host ""
        Write-Host "== 没有检测到微信 4.x 在运行 ==" -ForegroundColor Yellow
        Write-Host "   请先打开微信，并登录【机器人小号】。"
        Write-Host "   我会在这里等你最多 5 分钟，登录后自动继续..." -ForegroundColor DarkGray
    }
    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    while (-not (Test-WeixinRunning)) {
        if ((Get-Date) -gt $deadline) {
            Write-Host ""
            Write-Host "等待超时：$WaitMinutes 分钟内没检测到微信。请登录微信后重新双击 start_bot.bat。" -ForegroundColor Red
            if (-not $NoPause) { Read-Host "按回车退出" }
            exit 1
        }
        Start-Sleep -Seconds 2
    }
    Start-Sleep -Seconds 2
}

# ---- 启动前的账号提示 ----
if ($adapter -eq "wechatauto") {
    $expected = & $py -c "from src.config import load_config; print(load_config().wechat.self_name)" 2>$null
    if ($expected) {
        Write-Host "==> 机器人应使用的小号昵称: $expected" -ForegroundColor Cyan
        Write-Host "    （如果微信登录的是主号，程序会自动拒绝启动，防止主号乱发言）" -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "==> 启动机器人（按 Ctrl+C 停止）..." -ForegroundColor Green
Write-Host "    日志文件：$root\logs\bot.log"
Write-Host ""
& $py -m src.main run

$code = $LASTEXITCODE
Write-Host ""
if ($code -ne 0) {
    Write-Host "机器人退出，代码 $code。常见原因见 logs\bot.log 最后几行。" -ForegroundColor Yellow
}
if (-not $NoPause) { Read-Host "按回车关闭窗口" }
