#Requires -Version 5.1
<#
.SYNOPSIS
    Ayabot one-click launcher - starts WebUI (auto-launches room bots), stops all on exit
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $ProjectRoot '.venv'

# ---- helpers ----
function Kill-Tree($pid) {
    if (-not $pid) { return }
    & taskkill /F /T /PID $pid 2>$null | Out-Null
}
function Cleanup {
    if ($global:cleanupDone) { return }
    $global:cleanupDone = $true
    Write-Host "`n[Ayabot] Shutting down WebUI + all room bots..." -ForegroundColor Yellow
    Kill-Tree $script:webPid
    Get-Process -Name 'python' -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $pid } | ForEach-Object {
        & taskkill /F /T /PID $_.Id 2>$null | Out-Null
    }
    Write-Host "[Ayabot] All processes stopped." -ForegroundColor Green
    if (Test-Path 'function:deactivate') { deactivate }
    [Console]::CancelKeyPress.RemoveAll()
}

# ---- venv check ----
if (-not (Test-Path $VenvPath)) {
    Write-Warning "Virtual env not found: $VenvPath"
    pause; exit 1
}
$Activate = Join-Path $VenvPath 'Scripts\Activate.ps1'
. $Activate
$python = (Get-Command python).Source
Write-Host "[Ayabot] Python: $python" -ForegroundColor Cyan

# ---- register Ctrl+C handler ----
$global:cleanupDone = $false
try { [Console]::CancelKeyPress.Add({ Cleanup }) } catch { }

# ---- launch webui (auto-starts all room bots internally) ----
$WebLog = Join-Path $ProjectRoot 'webui.log'
$WebErr = "${WebLog}.err"
Write-Host "[Ayabot] Starting WebUI (will auto-launch room bots)..."
$webProc = Start-Process -PassThru -NoNewWindow -FilePath 'python' -ArgumentList @('web_serve.py') -RedirectStandardOutput $WebLog -RedirectStandardError $WebErr
$script:webPid = $webProc.Id
Write-Host "       WebUI PID: $script:webPid" -ForegroundColor DarkGray
Write-Host "       WebUI URL: http://localhost:19810" -ForegroundColor Magenta
Write-Host "       Room bots will start automatically in ~3 seconds."
Write-Host "       Use WebUI to start/stop/restart rooms anytime."
Write-Host "       Press Ctrl+C or close this window to stop everything.`n"

# ---- wait (poll + respond to Ctrl+C) ----
try {
    do {
        Start-Sleep -Milliseconds 500
        if ($global:cleanupDone) { break }
    } while (-not $webProc.HasExited)
} finally {
    Cleanup
}
