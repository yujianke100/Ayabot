@echo off
chcp 65001 >nul
title Ayabot Launcher
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] 虚拟环境不存在: .venv
    echo         请运行: python -m venv .venv
    pause
    exit /b 1
)

powershell -ExecutionPolicy Bypass -Command ^
$ProjectRoot = '%CD%'; ^
$VenvPath = Join-Path $ProjectRoot '.venv'; ^
. (Join-Path $VenvPath 'Scripts\Activate.ps1'); ^
$script:cleanupDone = $false; ^
function Cleanup { ^
    if ($script:cleanupDone) { return }; ^
    $script:cleanupDone = $true; ^
    Write-Host \"`n[Ayabot] 正在关闭 WebUI 及所有房间 Bot...\" -ForegroundColor Yellow; ^
    if ($script:webProc -and -not $script:webProc.HasExited) { ^
        & taskkill /F /T /PID $script:webProc.Id 2>$null | Out-Null; ^
    }; ^
    Get-Process -Name 'python' -ErrorAction SilentlyContinue ^
        | Where-Object { $_.Id -ne $pid } ^
        | ForEach-Object { & taskkill /F /T /PID $_.Id 2>$null | Out-Null }; ^
    if (Test-Path 'function:deactivate') { deactivate }; ^
    [Console]::CancelKeyPress.RemoveAll(); ^
    Write-Host \"[Ayabot] 已完全退出.\" -ForegroundColor Green; ^
}; ^
try { [Console]::CancelKeyPress.Add({ Cleanup }) } catch {}; ^
Write-Host \"[Ayabot] 启动 WebUI...\" -ForegroundColor Cyan; ^
Write-Host \"       浏览器打开 http://localhost:19810\" -ForegroundColor Magenta; ^
Write-Host \"       初始账号: ayabot / 123456\"; ^
Write-Host \"       按 Ctrl+C 完全停止（不残留进程）`n\"; ^
$script:webProc = Start-Process -PassThru -NoNewWindow -FilePath 'python' ^
    -ArgumentList @('web_serve.py'); ^
do { ^
    Start-Sleep -Milliseconds 500; ^
    if ($script:cleanupDone) { break }; ^
} while (-not $script:webProc.HasExited); ^
Cleanup
