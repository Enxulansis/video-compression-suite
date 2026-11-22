# Video Compression Suite -- one-click launcher (PowerShell 7)
#
# Usage:  .\start.ps1 [-Port 8787] [-NoBrowser] [-Python <path>] [-Concurrency 1]
#
# Design notes (learned the hard way):
#   * Double-clicking must NEVER close the window before the user can read the
#     error, so every failure path ends in Read-Host when running interactively.
#   * Opening the browser must not depend on a single mechanism: we try the
#     ShellExecute association, then a list of known browser executables, then
#     Python's webbrowser module.
#   * We verify the HTTP endpoint ourselves instead of assuming the browser
#     worked, and warn if a system proxy is configured to intercept loopback.

[CmdletBinding()]
param(
    [int]$Port = 8787,
    [switch]$NoBrowser,
    [string]$Python = "",
    [ValidateRange(0,16)][int]$Concurrency = 0
)

$ErrorActionPreference = 'Stop'
$script:Failed = $false

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root 'backend'
$interactive = $true
try { $interactive = [Environment]::UserInteractive -and ($Host.Name -ne 'ServerRemoteHost') } catch { $interactive = $true }

function Write-Head([string]$text) { Write-Host "  $text" -ForegroundColor Cyan }
function Write-Ok([string]$text)   { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Write-Warn2([string]$text){ Write-Host "  [!]    $text" -ForegroundColor Yellow }
function Write-Bad([string]$text)  { Write-Host "  [X]    $text" -ForegroundColor Red }

function Stop-WithMessage {
    param([string]$Message)
    Write-Bad $Message
    Write-Host ''
    if ($interactive -and -not $NoBrowser) {
        Read-Host '  按回车键关闭窗口'
    }
    exit 1
}

function Find-Python {
    param([string]$Explicit)
    if ($Explicit -and (Test-Path -LiteralPath $Explicit)) { return $Explicit }
    $candidates = @(
        'D:\anaconda3\envs\vcs\python.exe',
        'D:\anaconda3\python.exe',
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        'C:\Python312\python.exe',
        'C:\Python311\python.exe'
    )
    foreach ($c in $candidates) { if (Test-Path -LiteralPath $c) { return $c } }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-PortBusy {
    param([int]$PortNumber)
    return [bool](Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction SilentlyContinue)
}

function Test-Endpoint {
    param([string]$Url)
    try {
        $resp = Invoke-WebRequest -Uri $Url -TimeoutSec 4 -UseBasicParsing
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Get-RunningVersion {
    param([string]$Url)
    try { return Invoke-RestMethod -Uri $Url -TimeoutSec 4 } catch { return $null }
}

function Test-OurEndpoint {
    param([string]$Url)
    $health = Get-RunningVersion -Url $Url
    return [bool]($health -and $health.ok -eq $true -and
        $health.service -eq 'video-compression-suite' -and
        $health.instance -ceq $script:InstanceId -and [int]$health.pid -gt 0)
}

function Get-CodeMtime {
    # Newest mtime across backend sources + front-end assets: when the code last changed.
    $newest = 0.0
    $targets = @()
    $targets += Get-ChildItem -Path (Join-Path $backend '*.py') -File -ErrorAction SilentlyContinue
    $targets += Get-ChildItem -Path (Join-Path $backend 'core\*.py') -File -ErrorAction SilentlyContinue
    $targets += Get-ChildItem -Path (Join-Path $backend 'profiles\*.json') -File -ErrorAction SilentlyContinue
    $targets += Get-ChildItem -Path (Join-Path $root 'web\*') -File -ErrorAction SilentlyContinue
    foreach ($item in $targets) {
        $seconds = $item.LastWriteTime.ToUniversalTime().Subtract([datetime]'1970-01-01').TotalSeconds
        if ($seconds -gt $newest) { $newest = $seconds }
    }
    return $newest
}

function Stop-OurServer {
    # Only ever kills a python process whose command line references app.py.
    param([int]$PortNumber)
    $stopped = $false
    Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        $ownerId = $_.OwningProcess
        try {
            $identity = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId" -ErrorAction Stop
            $entry = [regex]::Escape((Join-Path $backend 'app.py'))
            $image = [IO.Path]::GetFileNameWithoutExtension($identity.ExecutablePath)
            if ($identity.CommandLine -and $identity.CommandLine -match $entry -and $image -match '^python(w)?$') {
                Stop-Process -Id $ownerId -Force -ErrorAction Stop
                $stopped = $true
            }
        } catch { }
    }
    return $stopped
}

function Open-Browser {
    param([string]$Url)
    # 1) ShellExecute via the user's default-browser association (works when a
    #    default browser is configured -- which is NOT guaranteed).
    try {
        Start-Process $Url -ErrorAction Stop
        return $true
    } catch { }

    # 2) Known browser executables, in case the association is broken.
    $browsers = @(
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
        "$env:ProgramFiles\Mozilla Firefox\firefox.exe",
        "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe"
    )
    foreach ($exe in $browsers) {
        if (Test-Path -LiteralPath $exe) {
            try {
                Start-Process -FilePath $exe -ArgumentList $Url -ErrorAction Stop
                return $true
            } catch { }
        }
    }

    # 3) Let Python ask the OS.
    if ($script:PythonPath) {
        try {
            & $script:PythonPath -c "import webbrowser,sys; sys.exit(0 if webbrowser.open(sys.argv[1]) else 1)" $Url 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $true }
        } catch { }
    }
    return $false
}

Write-Host ''
Write-Head 'Video Compression Suite — 启动检查'
Write-Host '  ----------------------------------' -ForegroundColor DarkGray

# ---------------------------------------------------------------- prerequisites
$python = Find-Python -Explicit $Python
if (-not $python) {
    Stop-WithMessage '没找到 Python 3.9+。请安装后重试，或用 -Python <路径> 指定解释器。'
}
$script:PythonPath = $python
$pyVersion = (& $python -V 2>&1) -join ' '
Write-Ok "Python : $pyVersion  ($python)"

$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    Write-Warn2 'PATH 中找不到 ffmpeg —— 页面能打开，但压缩会失败。'
    Write-Warn2 '安装：winget install Gyan.FFmpeg   然后重开本窗口。'
} else {
    $ffVersion = (& ffmpeg -hide_banner -version 2>&1 | Select-Object -First 1)
    Write-Ok "FFmpeg : $ffVersion"
}

if (-not (Test-Path -LiteralPath (Join-Path $backend 'app.py'))) {
    Stop-WithMessage "找不到 $backend\app.py —— 目录结构不完整。"
}

# Compute the exact same data identity as the backend, without restoring its queue
# or probing media. A packaged application on this port belongs to another store.
$instanceQuery = 'import sys; sys.path.insert(0, sys.argv[1]); from core.runtime import instance_id; print(instance_id())'
$script:InstanceId = ((& $python -B -c $instanceQuery $backend) -join '').Trim()
if ($LASTEXITCODE -ne 0 -or $script:InstanceId -notmatch '^[a-f0-9]{24}$') {
    Stop-WithMessage '无法确认本项目的数据实例，未启动或复用其它程序。'
}

# ------------------------------------------------------------------ port choice
$url = "http://127.0.0.1:$Port/"
if (Test-PortBusy -PortNumber $Port) {
    if (Test-OurEndpoint -Url ($url + 'api/health')) {
        # A running instance may predate the code on disk. Without this check,
        # double-clicking start.bat silently serves the OLD build -- which is
        # exactly the "I don't see the update" report.
        $stale = $false
        $info = Get-RunningVersion -Url ($url + 'api/version')
        if ($info -and $info.started) {
            $codeMtime = Get-CodeMtime
            # Stale means the code was modified AFTER this process started.
            # (Comparing the disk mtime against the server's own reported mtime can
            # never differ -- both read the same file -- which made this check
            # useless on the first attempt.)
            if ($codeMtime -gt ([double]$info.started + 1)) {
                $stale = $true
                Write-Warn2 '旧服务早于磁盘代码，先核对任务状态。'
            }
        }
        if ($stale) {
            $active = 0
            $known = $info -and ($info.PSObject.Properties.Name -contains 'active_jobs') -and
                [int]::TryParse([string]$info.active_jobs, [ref]$active) -and $active -ge 0
            if (-not $known -or $active -gt 0) {
                Write-Warn2 '旧服务还有任务或状态无法确认，本次复用，不重启。'
                if (-not $NoBrowser) { [void](Open-Browser -Url $url) }
                exit 0
            }
            if (Stop-OurServer -PortNumber $Port) {
                $releaseBy = [DateTimeOffset]::UtcNow.AddSeconds(3)
                while ((Test-PortBusy -PortNumber $Port) -and [DateTimeOffset]::UtcNow -lt $releaseBy) {
                    Start-Sleep -Milliseconds 100
                }
                if (Test-PortBusy -PortNumber $Port) { Stop-WithMessage '旧服务端口尚未释放，没有启动第二实例。' }
                Write-Ok '已停止旧实例'
            } else {
                Stop-WithMessage '无法确认旧实例已停止，没有启动第二实例。请等待任务空闲后重试。'
            }
        } else {
            Write-Warn2 "端口 $Port 上已经有一个本程序在运行（版本已是最新）。"
            if (-not $NoBrowser) {
                Write-Head "直接打开已有实例: $url"
                if (Open-Browser -Url $url) { Write-Ok '已请求浏览器打开' }
                else { Write-Warn2 "浏览器未能自动打开，请手动访问: $url" }
            }
            Write-Host ''
            if ($interactive -and -not $NoBrowser) { Read-Host '  按回车键关闭窗口' }
            exit 0
        }
    } else {
        Stop-WithMessage "端口 $Port 被其它程序占用。请换端口：.\start.ps1 -Port 8899"
    }
}

# ----------------------------------------------------------------------- launch
# The server runs in its own detached (hidden) process so that closing this
# window does NOT stop your compression jobs.  Re-running start.bat detects the
# running instance and simply reopens the page.
Write-Head "启动服务: $url"
Write-Host ''

$pyArgs = @((Join-Path $backend 'app.py'), '--port', "$Port")
if ($Concurrency -gt 0) { $pyArgs += @('--concurrency', "$Concurrency") }
if ($NoBrowser) { $pyArgs += '--no-browser' }

$proc = Start-Process -FilePath $python -ArgumentList $pyArgs `
    -WorkingDirectory $backend -PassThru -WindowStyle Hidden -ErrorAction Stop

# Wait for the HTTP endpoint to actually answer before opening anything.
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    if ($proc.HasExited) { break }
    try {
        $health = Invoke-RestMethod -Uri ($url + 'api/health') -TimeoutSec 1
        if ($health.ok -and $health.service -eq 'video-compression-suite' -and
            $health.instance -ceq $script:InstanceId -and [int]$health.pid -eq $proc.Id) { $ready = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 200
}

if (-not $ready) {
    if ($proc.HasExited) {
        Stop-WithMessage "服务启动失败（退出码 $($proc.ExitCode)）。请用 .\start.ps1 -Verbose 或直接运行 python backend\app.py 查看原因。"
    }
    Stop-WithMessage '新服务在本次等待时限内尚未就绪，未将旧服务响应当作启动成功。'
} else {
    Write-Ok "服务已就绪（后台运行，进程 ID $($proc.Id)）"
}

Write-Host ''
Write-Host "  页面地址: $url" -ForegroundColor Green
Write-Host '  压缩任务在后端持续运行，关掉本窗口不影响；'
Write-Host "  需要停止服务时运行: .\stop.ps1    或: taskkill /PID $($proc.Id) /F"
Write-Host ''

if (-not $NoBrowser) {
    if (Open-Browser -Url $url) {
        Write-Ok '已请求浏览器打开'
    } else {
        Write-Warn2 '浏览器没有自动打开（系统可能未设置默认浏览器）。'
        Write-Warn2 '请手动复制上面的地址到浏览器打开。'
    }
    # A proxy that does not exempt loopback would break the page even though the
    # server is fine, so check for it explicitly.
    $proxy = $env:HTTP_PROXY
    if ($proxy -and $proxy -notmatch '127\.0\.0\.1|localhost') {
        Write-Warn2 "检测到系统代理 $proxy —— 若页面打不开，请把 127.0.0.1 加入代理例外。"
    }
    # Give the browser a moment, then verify the page is really being served.
    Start-Sleep -Milliseconds 900
    if (Test-Endpoint -Url ($url + 'api/profiles')) {
        Write-Ok '自检通过：页面接口可访问'
    } else {
        Write-Warn2 '自检未通过：本机无法访问该地址（通常是被代理拦截），请检查代理例外。'
    }
}

if ($interactive -and -not $NoBrowser) {
    Write-Host ''
    Read-Host '  按回车键关闭本窗口（服务继续在后台运行）' | Out-Null
}
exit 0
