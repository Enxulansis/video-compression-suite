# Video Compression Suite -- stop the background server
#
# Usage:  .\stop.ps1 [-Port 8787]
#
# start.ps1 launches the server as a detached background process so that closing
# the launcher window does not kill a running batch.  This is the matching "off"
# switch.

[CmdletBinding()]
param([int]$Port = 8787)

$ErrorActionPreference = 'Stop'
# Whether the "press Enter" hold makes sense. `[Environment]::UserInteractive` alone is
# NOT a reliable test: it stays True for a process spawned without a usable stdin, where
# Read-Host raises a terminating error -- and under $ErrorActionPreference='Stop' that
# aborted the script (exit 1) AFTER the service had already been stopped, so a scripted
# restart looked like a failure and never ran its next step. Also require real stdin.
$interactive = $true
try {
    $interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
} catch { $interactive = $false }

Write-Host ''
Write-Host '  Video Compression Suite — 停止服务' -ForegroundColor Cyan
Write-Host '  ----------------------------------' -ForegroundColor DarkGray

$found = $false
$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($conn in $conns) {
    $procId = $conn.OwningProcess
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if (-not $proc) { continue }
    # Only stop our own processes. Two shapes exist:
    #   python  app.py    -- server started on its own (start.ps1)
    #   pythonw desktop.py -- the native window host, which starts the server
    #                         in-process, so the listening PID is "pythonw".
    # Matching only app.py meant the desktop window was reported as
    # "occupied by another program" and left running, so the next launch kept
    # reusing the old backend (and thus the old code).
    $isOurs = $false
    $ours = ''
    try {
        $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction Stop).CommandLine
        if ($cmdline -and $cmdline -match 'app\.py') { $isOurs = $true; $ours = 'app.py' }
        elseif ($cmdline -and $cmdline -match 'desktop\.py') { $isOurs = $true; $ours = 'desktop.py' }
    } catch { }

    if ($isOurs) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Write-Host "  [OK]   已停止端口 $Port 上的服务（$ours / 进程 $procId）" -ForegroundColor Green
        $found = $true
    } else {
        Write-Host "  [!]    端口 $Port 被其它程序占用（$($proc.ProcessName) / $procId），未动它。" -ForegroundColor Yellow
        $found = $true
    }
}

# A desktop host can lose the port race but still hold a window open; stop it too
# so the next launch really starts fresh instead of reusing a stale process.
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'desktop\.py' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  [OK]   已关闭原生窗口（desktop.py / 进程 $($_.ProcessId)）" -ForegroundColor Green
        $found = $true
    }

if (-not $found) {
    Write-Host "  [i]    端口 $Port 上没有正在运行的服务。" -ForegroundColor DarkGray
}

Write-Host ''
# Cosmetic hold so a double-clicked window does not vanish before the result is read.
# The try/catch is deliberate: whether a prompt is possible must never decide the exit
# code of a script whose actual work already succeeded (see the $interactive note above).
if ($interactive) {
    try { Read-Host '  按回车键关闭窗口' | Out-Null } catch { }
}
