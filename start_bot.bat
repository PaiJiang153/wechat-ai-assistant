@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   DeepSeek WeChat Bot - Startup
echo   1. WeChat 4.x must be logged in as the BOT account
echo   2. Keep this window open (closing it stops the bot)
echo ============================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\run_bot.ps1"
