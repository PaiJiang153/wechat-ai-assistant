@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动网页测试台（不需要微信）...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\run_web.ps1"
echo.
echo 服务已退出。
pause
