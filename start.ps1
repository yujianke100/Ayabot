#Requires -Version 5.1
<#
.SYNOPSIS
    Ayabot one-click launcher - starts WebUI (auto-launches room bots), stops all on Ctrl+C
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $ProjectRoot '.venv'

# ---- venv check ----
if (-not (Test-Path $VenvPath)) {
    Write-Warning "Virtual env not found: $VenvPath"
    pause; exit 1
}
$Activate = Join-Path $VenvPath 'Scripts\Activate.ps1'
. $Activate
$python = (Get-Command python).Source
Write-Host "[Ayabot] Python: $python" -ForegroundColor Cyan

# ---- helper: kill process + all descendants via taskkill ----
function Kill-Tree($pid) {
    if (-not $pid) { return }
    # /T = kill tree (children), /F = force
    & taskkill /F /T /PID $pid 2>$null | Out-Null
}

# ---- launch webui (auto-starts all room bots internally) ----
$WebLog = Join-Path $ProjectRoot 'webui.log'
$WebErr = "${WebLog}.err"
Write-Host "[Ayabot] Starting WebUI (will auto-launch room bots)..."
$webProc = Start-Process -PassThru -NoNewWindow -FilePath 'python' -ArgumentList @('web_serve.py') -RedirectStandardOutput $WebLog -RedirectStandardError $WebErr
$webPid = $webProc.Id
Write-Host "       WebUI PID: $webPid" -ForegroundColor DarkGray
Write-Host "       WebUI URL: http://localhost:19810" -ForegroundColor Magenta
Write-Host "       Room bots will start automatically in ~3 seconds."
Write-Host "       Use WebUI to start/stop/restart rooms anytime."
Write-Host "       Press Ctrl+C or close this window to stop everything.`n"

# ---- wait ----
try {
    $webProc.WaitForExit()
    Write-Host "[Ayabot] WebUI has exited on its own." -ForegroundColor Yellow
} finally {
    Write-Host "`n[Ayabot] Shutting down WebUI + all room bots..." -ForegroundColor Yellow
    Kill-Tree $webPid
    # Kill any stray python processes that might be leftover child bots
    Get-Process -Name 'python' -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $pid } | ForEach-Object {
        & taskkill /F /T /PID $_.Id 2>$null | Out-Null
    }
    Write-Host "[Ayabot] All processes stopped." -ForegroundColor Green
    if (Test-Path 'function:deactivate') { deactivate }
}
