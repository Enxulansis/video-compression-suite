# Video Compression Suite -- desktop window launcher (PowerShell 7)
#
# Opens the app in its own window using Edge's --app mode: no address bar, no tabs,
# its own taskbar entry. WebView2 is already installed on this machine, so this
# needs no extra package (pywebview/pythonnet are unreachable: pip is blocked by
# the network policy here).
#
# Usage:  .\start-app.ps1 [-Port 8787] [-NoBrowserFallback] [-Fullscreen]

[CmdletBinding()]
param(
    [int]$Port = 8787,
    [switch]$NoBrowserFallback,
    [switch]$Fullscreen
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$url = "http://127.0.0.1:$Port/?app=1"

function Find-Browser {
    $candidates = @(
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
    )
    foreach ($exe in $candidates) { if (Test-Path -LiteralPath $exe) { return $exe } }
    return $null
}

Write-Host ''
Write-Host '  Video Compression Suite — 独立窗口' -ForegroundColor Cyan
Write-Host '  ----------------------------------' -ForegroundColor DarkGray

# 1) Make sure the backend is up. start.ps1 already handles staleness detection
#    (restarting an instance older than the code) and waits for readiness, so we
#    just delegate to it with a windowless host.
$startScript = Join-Path $root 'start.ps1'
& pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File $startScript -Port $Port -NoBrowser 2>&1 |
    ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [X] 后端没有就绪，无法打开窗口。请先运行 .\start.ps1 看报错。" -ForegroundColor Red
    Read-Host '  按回车键关闭'
    exit 1
}
Write-Host "  [OK]   服务已就绪: http://127.0.0.1:$Port" -ForegroundColor Green

$browser = Find-Browser
if (-not $browser) {
    Write-Host '  [!]   没找到 Edge/Chrome，无法用 --app 模式开窗。' -ForegroundColor Yellow
    Write-Host "        请手动打开浏览器访问 $url" -ForegroundColor Yellow
    Read-Host '  按回车键关闭'
    exit 1
}
Write-Host "  [OK]   窗口程序: $browser" -ForegroundColor Green

# 2) A dedicated profile keeps this window independent of the user's normal
#    browsing session (and its extensions), which also avoids the normal browser's
#    Remote Debugging being disabled by policy.
$profileDir = Join-Path $env:LOCALAPPDATA 'VideoCompressSuite\appwindow'
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

$browserArgs = @(
    "--app=$url",
    "--user-data-dir=$profileDir",
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-features=Translate,msEdgeTranslate',
    '--window-size=1500,1000'
)
if ($Fullscreen) { $browserArgs += '--start-fullscreen' }

# Delete any previous app-window profile singleton lock so a stale lock cannot stop
# the new window from opening.
foreach ($lock in @('SingletonLock', 'SingletonCookie', 'SingletonSocket')) {
    $lockPath = Join-Path $profileDir $lock
    if (Test-Path -LiteralPath $lockPath) { Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue }
}

Write-Host '  正在打开独立窗口…' -ForegroundColor Cyan
Write-Host ''
Start-Process -FilePath $browser -ArgumentList $browserArgs
Write-Host '  窗口已启动。压缩任务在后台运行，关闭窗口不影响任务。' -ForegroundColor DarkGray
Write-Host '  需要停止服务时运行: .\stop.ps1' -ForegroundColor DarkGray
Write-Host ''
exit 0
