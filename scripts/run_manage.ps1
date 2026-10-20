# 双击 start_manage.bat 即可；只启动维护台，不启动微信机器人。
param([int]$Port = 8010, [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$url = "http://127.0.0.1:$Port/manage"
function Test-MaintenanceReady {
    try {
        $response = Invoke-RestMethod -Uri "$url/health" -TimeoutSec 1
        return $response.application -eq "dsbot-management"
    } catch { return $false }
}
try {
    if (-not (Test-Path -LiteralPath $py)) { throw "请先运行 scripts\setup.ps1 安装运行环境。" }
    if (-not (Test-MaintenanceReady)) {
        $logDir = Join-Path $root "logs"
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        $process = Start-Process -FilePath $py -ArgumentList @("-m", "src.main", "manage", "--port", "$Port") -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir "manage-out.log") -RedirectStandardError (Join-Path $logDir "manage-error.log") -PassThru
        $deadline = (Get-Date).AddSeconds(20)
        while (-not (Test-MaintenanceReady)) {
            if ($process.HasExited -or (Get-Date) -gt $deadline) { throw "维护台未能启动。端口可能被占用，详细信息见 logs\manage-error.log。" }
            Start-Sleep -Milliseconds 200
        }
    }
    if (-not $NoBrowser) { Start-Process $url }
} catch {
    if ($NoBrowser) { Write-Error $_.Exception.Message; exit 1 }
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "维护台启动失败", "OK", "Error") | Out-Null
    exit 1
}
