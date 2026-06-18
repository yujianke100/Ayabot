@echo off
title Ayabot Launcher
cd /d "%~dp0"

:: 退出 conda base 环境（如有），避免 .venv 路径被 conda 干扰
call conda deactivate 2>nul

if not exist ".venv\Scripts\python.exe" (
    echo [Ayabot] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

:: 确保依赖已安装
echo [Ayabot] Checking dependencies...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo [Ayabot] Installing dependencies (first time may take a while)...
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

:: Run powershell directly so Ctrl+C reaches python/uvicorn.
powershell -ExecutionPolicy Bypass -Command "& { . .venv\Scripts\Activate.ps1; Write-Host '[Ayabot] Starting WebUI...' -ForegroundColor Cyan; Write-Host '       http://localhost:19810' -ForegroundColor Magenta; Write-Host '       Press Ctrl+C to stop'; try { python web_serve.py } catch {} finally { Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $pid } | ForEach-Object { taskkill /F /T /PID $_.Id 2>$null }; if (Test-Path function:deactivate) { deactivate }; Write-Host '[Ayabot] Exited.' -ForegroundColor Green } }"
