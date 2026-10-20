# 设置「开机自启」：登录 Windows 后自动启动机器人
#
# 用法（右键「使用 PowerShell 运行」，或在项目目录执行）：
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
#
# 取消自启：scripts\uninstall_autostart.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$taskName = "DeepSeek-WeChat-Bot"
$ps1 = Join-Path $root "scripts\run_bot.ps1"
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) { throw "缺少 .venv，请先运行 .\scripts\setup.ps1" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ps1`" -NoPause -WaitMinutes 30" `
    -WorkingDirectory $root

# 登录后 30 秒触发：给微信/网络一点启动时间
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT30S"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host ""
Write-Host "✅ 已设置开机自启：$taskName" -ForegroundColor Green
Write-Host "   · 每次登录 Windows 后 30 秒自动启动（隐藏窗口，日志在 logs\bot.log）"
Write-Host "   · 前提：微信 4.x 已登录机器人小号（可以在它启动前登录，脚本会等最多 30 分钟）"
Write-Host "   · 取消自启：powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_autostart.ps1"
Write-Host ""
Read-Host "按回车关闭"
