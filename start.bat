@echo off
title Ayabot Launcher
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual env not found: .venv
    echo         Run: python -m venv .venv
    pause
    exit /b 1
)

:: Run python directly in foreground (not Start-Process) so Ctrl+C goes straight to uvicorn.
:: After uvicorn exits, kill any leftover python processes (orphaned bots).
powershell -ExecutionPolicy Bypass -Command "& { . .venv\Scripts\Activate.ps1; Write-Host '[Ayabot] Starting WebUI...' -ForegroundColor Cyan; Write-Host '       http://localhost:19810' -ForegroundColor Magenta; Write-Host '       Press Ctrl+C to stop'; try { python web_serve.py } catch {} finally { Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $pid } | ForEach-Object { taskkill /F /T /PID $_.Id 2>$null }; if (Test-Path function:deactivate) { deactivate }; Write-Host '[Ayabot] Exited.' -ForegroundColor Green } }"
