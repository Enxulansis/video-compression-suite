# Video Compression Suite -- create the dedicated conda environment
#
# Why a separate env: the project's desktop shell (PyWebView) needs Python 3.11+,
# because pythonnet 3.2 requires it. Your Anaconda base is Python 3.9, so base
# CANNOT run it -- and we must not touch base anyway.
#
# Usage:
#   .\setup-env.ps1                 # create "vcs" env with Python 3.13 + pywebview
#   .\setup-env.ps1 -Name myenv     # use a different env name
#   .\setup-env.ps1 -Recreate       # delete and rebuild the env
#
# Safe to run twice: it detects an existing env and only installs what is missing.

[CmdletBinding()]
param(
    [string]$Name = 'vcs',
    [string]$PythonVersion = '3.13',
    [switch]$Recreate
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$CONDA_MIRROR = 'https://mirrors.tuna.tsinghua.edu.cn/anaconda'
$PIP_INDEX    = 'https://pypi.tuna.tsinghua.edu.cn/simple'

function Write-Head($t) { Write-Host "  $t" -ForegroundColor Cyan }
function Write-Ok($t)   { Write-Host "  [OK]   $t" -ForegroundColor Green }
function Write-Warn2($t){ Write-Host "  [!]    $t" -ForegroundColor Yellow }
function Write-Bad($t)  { Write-Host "  [X]    $t" -ForegroundColor Red }

function Find-Conda {
    $cmd = Get-Command conda.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @(
        'D:\anaconda3\Scripts\conda.exe',
        "$env:USERPROFILE\anaconda3\Scripts\conda.exe",
        'C:\ProgramData\anaconda3\Scripts\conda.exe',
        "$env:LOCALAPPDATA\Continuum\anaconda3\Scripts\conda.exe"
    )) { if (Test-Path -LiteralPath $p) { return $p } }
    return $null
}

function Invoke-CondaCreate {
    # Create the env, degrading through three attempts. A stray *_PROXY variable (or
    # a proxy that cannot reach the mirror) is the most common reason conda fails;
    # it reports "ProxyError ... error in your proxy configuration". So attempt 2
    # clears those variables for the child process, and attempt 3 drops the mirror.
    param([string]$Conda, [string]$Name, [string]$PythonVersion)

    $mirrorArgs = @('--channel', "$CONDA_MIRROR/cloud/conda-forge",
                    '--channel', "$CONDA_MIRROR/pkgs/main")
    $attempts = @(
        @{ Label = '清华镜像';                     Extra = $mirrorArgs; ClearProxy = $false },
        @{ Label = '清华镜像（忽略代理环境变量）'; Extra = $mirrorArgs; ClearProxy = $true },
        @{ Label = 'conda 默认源';                 Extra = @();         ClearProxy = $true }
    )

    foreach ($attempt in $attempts) {
        Write-Head "尝试：$($attempt.Label)"
        $saved = @{}
        if ($attempt.ClearProxy) {
            foreach ($v in 'HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY') {
                $saved[$v] = [Environment]::GetEnvironmentVariable($v)
                [Environment]::SetEnvironmentVariable($v, $null)
                Remove-Item "Env:$v" -ErrorAction SilentlyContinue
            }
        }
        try {
            $argv = @('create', '-n', $Name, "python=$PythonVersion", '-y') + $attempt.Extra
            & $Conda @argv 2>&1 | Select-Object -Last 6 |
                ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
            $rc = $LASTEXITCODE
        } finally {
            foreach ($v in $saved.Keys) {
                if ($saved[$v]) { [Environment]::SetEnvironmentVariable($v, $saved[$v]) }
            }
        }
        if ($rc -eq 0) { return $true }
        Write-Warn2 "该方式失败（退出码 $rc），换下一种…"
    }
    return $false
}

Write-Host ''
Write-Host '  视频压缩套件 — 创建独立 conda 环境' -ForegroundColor Cyan
Write-Host '  -----------------------------------' -ForegroundColor DarkGray

$conda = Find-Conda
if (-not $conda) {
    Write-Bad '找不到 conda.exe。请确认 Anaconda 已安装，或用 -Name 之外的方案。'
    Read-Host '  按回车键关闭'
    exit 1
}
Write-Ok "conda: $conda"

# --- does the env already exist? ---
$envList = & $conda env list 2>&1 | Out-String
$prefix = $null
$envRoot = Split-Path (Split-Path $conda -Parent) -Parent   # D:\anaconda3
$candidate = Join-Path (Join-Path $envRoot 'envs') $Name
if (Test-Path -LiteralPath (Join-Path $candidate 'python.exe')) { $prefix = $candidate }

if ($prefix -and $Recreate) {
    Write-Warn2 "-Recreate 指定：删除已有环境 $Name"
    & $conda env remove -n $Name -y 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    $prefix = $null
}

if (-not $prefix) {
    Write-Head "创建环境 $Name (Python $PythonVersion)…"
    Write-Host '    （首次需要下载，可能要几分钟）' -ForegroundColor DarkGray
    $created = Invoke-CondaCreate -Conda $conda -Name $Name -PythonVersion $PythonVersion
    if ($created -and (Test-Path -LiteralPath (Join-Path $candidate 'python.exe'))) { $prefix = $candidate }
} else {
    Write-Ok "环境 $Name 已存在: $prefix"
}

if (-not $prefix) {
    Write-Bad '环境创建失败（三种方式都没成功）'
    Write-Host ''
    Write-Host '  请在 Anaconda Prompt 里手动执行这一条，它不受任何代理变量影响：' -ForegroundColor Yellow
    Write-Host "    conda create -n $Name python=$PythonVersion -y" -ForegroundColor White
    Write-Host '    conda activate ' + $Name -ForegroundColor White
    Write-Host '    pip install pywebview' -ForegroundColor White
    Write-Host '  然后运行 .\check-desktop.bat 自检。' -ForegroundColor DarkGray
    Write-Host ''
    Read-Host '  按回车键关闭'
    exit 2
}

$py = Join-Path $prefix 'python.exe'
$ver = (& $py -V 2>&1) -join ' '
Write-Ok "解释器: $py  ($ver)"

# --- install pywebview ---
Write-Head '安装 pywebview（原生窗口 + 拖拽真实路径）…'
& $py -m pip install --disable-pip-version-check -q -i $PIP_INDEX pywebview 2>&1 |
    Select-Object -Last 6 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }

$probe = & $py -c "import webview,sys; print(getattr(webview,'__version__','?'))" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Ok "pywebview 安装成功，版本 $probe"
} else {
    Write-Bad 'pywebview 安装失败'
    Write-Host ''
    Write-Host '  可尝试的镜像：' -ForegroundColor Yellow
    Write-Host "    $py -m pip install -i https://mirrors.aliyun.com/pypi/simple pywebview" -ForegroundColor White
    Write-Host "    $py -m pip install -i https://pypi.org/simple pywebview" -ForegroundColor White
    Write-Host ''
    Write-Host '  若报 SSL/证书错误，追加： --trusted-host mirrors.aliyun.com' -ForegroundColor DarkGray
    Read-Host '  按回车键关闭'
    exit 3
}

# --- report ---
Write-Host ''
Write-Head '环境清单'
& $py -m pip list 2>&1 | Select-String -Pattern 'pywebview|pythonnet|clr|bottle|proxy|typing' |
    ForEach-Object { Write-Host "    $($_.Line)" -ForegroundColor DarkGray }

Write-Host ''
Write-Host "  [OK]   下一步：.\check-desktop.ps1 自检，然后双击 start-desktop.bat" -ForegroundColor Cyan
Write-Host "         （脚本会自动找到 $Name 环境，无需手动 activate）" -ForegroundColor DarkGray
Write-Host ''
Read-Host '  按回车键关闭'
