@echo off
title Ayabot Launcher
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual env not found: .venv
    echo         Run: python -m venv .venv
    pause
    exit /b 1
)

:: Use start /b /wait to run PowerShell in a separate process so Ctrl+C won't trigger
:: "Terminate batch job (Y/N)?" — signal goes straight to python/uvicorn.
start /b /wait "" powershell -ExecutionPolicy Bypass -Command "& { . .venv\Scripts\Activate.ps1; Write-Host '[Ayabot] Starting WebUI...' -ForegroundColor Cyan; Write-Host '       http://localhost:19810' -ForegroundColor Magenta; Write-Host '       Press Ctrl+C to stop'; try { python web_serve.py } catch {} finally { Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $pid } | ForEach-Object { taskkill /F /T /PID $_.Id 2>$null }; if (Test-Path function:deactivate) { deactivate }; Write-Host '[Ayabot] Exited.' -ForegroundColor Green } }"
