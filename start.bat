@echo off
title Ayabot Launcher
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual env not found: .venv
    echo         Run: python -m venv .venv
    pause
    exit /b 1
)

:: Delegate everything to PowerShell - no batch-level echo/escaping nightmares
:: Start web_serve.py, trap Ctrl+C to kill all python processes
powershell -ExecutionPolicy Bypass -Command ^
$e = 0; ^
try { ^
    . .venv\Scripts\Activate.ps1; ^
    Write-Host '[Ayabot] Starting WebUI...' -ForegroundColor Cyan; ^
    Write-Host '       http://localhost:19810' -ForegroundColor Magenta; ^
    Write-Host '       Press Ctrl+C to stop'; ^
    $p = Start-Process -PassThru -NoNewWindow python web_serve.py; ^
    $p.WaitForExit(); ^
    $e = $p.ExitCode; ^
} catch { ^
    Write-Host ''; ^
} finally { ^
    Get-Process -Name python -ErrorAction SilentlyContinue ^| Where-Object { $_.Id -ne $pid } ^| ForEach-Object { taskkill /F /T /PID $_.Id 2>$null }; ^
    if (Test-Path function:deactivate) { deactivate }; ^
    if ($e -ne 0) { Write-Host '[Ayabot] Exited.' -ForegroundColor Green }; ^
}
