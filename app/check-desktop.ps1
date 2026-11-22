# Video Compression Suite -- desktop (PyWebView) readiness check
#
# Tells you, in one shot, whether the native-drag-drop window can start, and the
# exact command to run if it cannot.
#
# Usage:  .\check-desktop.ps1 [-EnvName vcs]

[CmdletBinding()]
param([string]$EnvName = 'vcs')

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root 'backend'

Write-Host ''
Write-Host '  桌面窗口（原生拖拽）自检' -ForegroundColor Cyan
Write-Host '  -------------------------' -ForegroundColor DarkGray

function Ok($m)   { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Bad($m)  { Write-Host "  [X]    $m" -ForegroundColor Red }
function Info($m) { Write-Host "  [i]    $m" -ForegroundColor DarkGray }

# 1) backend present
if (Test-Path -LiteralPath (Join-Path $backend 'desktop.py')) { Ok '桌面宿主 backend\desktop.py 存在' }
else { Bad '找不到 backend\desktop.py'; exit 1 }

# 2) locate an interpreter: dedicated conda env first, then a plain venv.
#    Base Anaconda is Python 3.9, which pythonnet 3.2 refuses -- never use it.
function Find-DedicatedPython {
    param([string]$Name)
    $candidates = @()
    foreach ($root in @('D:\anaconda3', "$env:USERPROFILE\anaconda3",
                        'C:\ProgramData\anaconda3', "$env:LOCALAPPDATA\Continuum\anaconda3")) {
        $candidates += (Join-Path $root "envs\$Name\python.exe")
    }
    $candidates += (Join-Path $root '.venv\Scripts\python.exe')
    foreach ($c in $candidates) { if (Test-Path -LiteralPath $c) { return $c } }
    return $null
}

$venvPy = Find-DedicatedPython -Name $EnvName
if ($venvPy) {
    $ver = (& $venvPy -V 2>&1) -join ' '
    Ok "解释器: $venvPy  ($ver)"
    if ($ver -match '3\.(\d+)') {
        $minor = [int]$Matches[1]
        if ($minor -lt 11) {
            Bad "Python 3.$minor 太旧：pythonnet 3.2 需要 3.11+，拖拽窗口无法使用"
            Info "请运行 .\setup-env.ps1 创建一个 Python 3.13 的独立环境"
        }
    }
} else {
    Bad "没有找到独立环境（conda 环境名: $EnvName，或 .venv）"
    Write-Host ''
    Write-Host '  请先执行（一次性）：' -ForegroundColor Yellow
    Write-Host '    cd E:\视频压缩\app' -ForegroundColor White
    Write-Host '    .\setup-env.ps1' -ForegroundColor White
    Write-Host ''
    exit 2
}

# 3) pywebview importable + version
& $venvPy (Join-Path $backend 'desktop.py') --check
$rc = $LASTEXITCODE
switch ($rc) {
    0 { Ok 'pywebview 可用 —— 可以启动桌面窗口' }
    3 {
        Bad 'pywebview 未安装（或导入失败）'
        Write-Host ''
        Write-Host '  安装（一次性，会自动建/复用独立环境）：' -ForegroundColor Yellow
        Write-Host '    cd E:\视频压缩\app' -ForegroundColor White
        Write-Host '    .\setup-env.ps1' -ForegroundColor White
        Write-Host ''
        Write-Host '  或手动只装依赖到现有环境：' -ForegroundColor DarkGray
        Write-Host "    `"$venvPy`" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pywebview" -ForegroundColor DarkGray
        Write-Host ''
    }
    default { Bad "自检脚本返回 $rc" }
}

# 4) WebView2 runtime
$wv = Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -ErrorAction SilentlyContinue
if ($wv) { Ok "WebView2 运行时: $($wv.pv)" }
else { Bad '未检测到 WebView2 运行时（Windows 11 通常自带；缺失需安装 Edge WebView2 Runtime）' }

Write-Host ''
if ($rc -eq 0) {
    Write-Host '  下一步：双击 start-desktop.bat 启动桌面窗口' -ForegroundColor Cyan
} else {
    Write-Host '  按上面的命令装好 pywebview 后，重新运行本脚本自检' -ForegroundColor Cyan
}
Write-Host ''
