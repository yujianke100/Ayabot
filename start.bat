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

:: 生成临时 .ps1 → 执行 → 清理
:: 逐行 echo 写入，^& ^| ^> 等特殊字符用 ^ 转义
set _PS_=%TEMP%\ayabot_launch.ps1
if exist "%_PS_%" del "%_PS_%"

>  "%_PS_%" echo $ProjectRoot = '%CD%'
>> "%_PS_%" echo $VenvPath = Join-Path $ProjectRoot '.venv'
>> "%_PS_%" echo . (Join-Path $VenvPath 'Scripts\Activate.ps1')
>> "%_PS_%" echo $script:cleanupDone = $false
>> "%_PS_%" echo function Cleanup {
>> "%_PS_%" echo     if ($script:cleanupDone^) { return }
>> "%_PS_%" echo     $script:cleanupDone = $true
>> "%_PS_%" echo     Write-Host "`n[Ayabot] 正在关闭 WebUI 及所有房间 Bot..." -ForegroundColor Yellow
>> "%_PS_%" echo     if ($script:webProc -and -not $script:webProc.HasExited^) {
>> "%_PS_%" echo         ^& taskkill /F /T /PID $script:webProc.Id 2^>$null ^| Out-Null
>> "%_PS_%" echo     }
>> "%_PS_%" echo     Get-Process -Name 'python' -ErrorAction SilentlyContinue ^| Where-Object { $_.Id -ne $pid } ^| ForEach-Object { ^& taskkill /F /T /PID $_.Id 2^>$null ^| Out-Null }
>> "%_PS_%" echo     if (Test-Path 'function:deactivate'^) { deactivate }
>> "%_PS_%" echo     [Console]::CancelKeyPress.RemoveAll(^)
>> "%_PS_%" echo     Write-Host "[Ayabot] 已完全退出." -ForegroundColor Green
>> "%_PS_%" echo }
>> "%_PS_%" echo try { [Console]::CancelKeyPress.Add({ Cleanup }) } catch {}
>> "%_PS_%" echo Write-Host "[Ayabot] 启动 WebUI..." -ForegroundColor Cyan
>> "%_PS_%" echo Write-Host "       浏览器打开 http://localhost:19810" -ForegroundColor Magenta
>> "%_PS_%" echo Write-Host "       初始账号: ayabot / 123456"
>> "%_PS_%" echo Write-Host "       按 Ctrl+C 完全停止（不残留进程）`n"
>> "%_PS_%" echo $script:webProc = Start-Process -PassThru -NoNewWindow -FilePath 'python' -ArgumentList @('web_serve.py')
>> "%_PS_%" echo do {
>> "%_PS_%" echo     Start-Sleep -Milliseconds 500
>> "%_PS_%" echo     if ($script:cleanupDone^) { break }
>> "%_PS_%" echo } while (-not $script:webProc.HasExited^)
>> "%_PS_%" echo Cleanup

powershell -ExecutionPolicy Bypass -File "%_PS_%"
del "%_PS_%" 2>nul
