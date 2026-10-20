# 取消「开机自启」
$ErrorActionPreference = "Continue"
$taskName = "DeepSeek-WeChat-Bot"
try {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
    Write-Host "✅ 已取消开机自启：$taskName" -ForegroundColor Green
} catch {
    Write-Host "没有找到该计划任务（可能本来就没设置）。" -ForegroundColor Yellow
}
Read-Host "按回车关闭"
