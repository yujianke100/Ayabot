@echo off
chcp 65001 >nul
title Ayabot Launcher

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual env not found: .venv
    echo         Run: python -m venv .venv
    pause
    exit /b 1
)

powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0start.ps1"
pause
