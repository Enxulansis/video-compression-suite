[CmdletBinding()]
param(
    [ValidateSet('Bundle', 'Installer')][string]$Stage = 'Bundle',
    [string]$Python = 'D:\anaconda3\envs\vcs\python.exe',
    [string]$BuildRoot = '',
    [string]$BundleDir = '',
    [string]$MediaDir = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if (-not $BuildRoot) { $BuildRoot = Join-Path $projectRoot '_temp\packaging' }
$downloads = Join-Path $BuildRoot 'downloads'
$tools = Join-Path $BuildRoot 'tools'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $BuildRoot 'tools\pyinstaller-cache'
$env:PYTHONPATH = Join-Path $tools 'python'
$buildClock = Get-Date
$stamp = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId($buildClock.ToUniversalTime(), 'China Standard Time').ToString('yyyyMMdd-HHmmss')
$runDir = Join-Path $BuildRoot ('runs\' + $stamp)
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$runner = Join-Path $PSScriptRoot 'run_bounded.py'

if ($Stage -eq 'Bundle') {
    if (-not $MediaDir) { $MediaDir = Join-Path $downloads 'ffmpeg-8.0.1-full_build-shared\bin' }
    $payload = Join-Path $runDir 'payload'
    & $Python -B $runner --timeout 90 --reserve 256 --log (Join-Path $runDir 'prepare.log') -- $Python -B (Join-Path $PSScriptRoot 'prepare_payload.py') --output $payload --downloads $downloads --media-dir $MediaDir
    if ($LASTEXITCODE -ne 0) { throw ('Payload preparation failed: {0}; {1}' -f $LASTEXITCODE, $runDir) }
    $env:VCS_BUILD_PAYLOAD = $payload
    & $Python -B $runner --timeout 600 --reserve 768 --log (Join-Path $runDir 'bundle.log') -- $Python -B -m PyInstaller --noconfirm --workpath (Join-Path $runDir 'work') --distpath (Join-Path $runDir 'dist') (Join-Path $PSScriptRoot 'video_compression.spec')
    if ($LASTEXITCODE -ne 0) { throw ('Executable build failed: {0}; {1}' -f $LASTEXITCODE, $runDir) }
    $BundleDir = Join-Path $runDir 'dist\VideoCompressionSuite'
    $record = [ordered]@{ bundle = $BundleDir; run = $runDir; payload = $payload }
} else {
    if (-not $BundleDir -or -not (Test-Path -LiteralPath (Join-Path $BundleDir 'VideoCompressionSuite.exe'))) { throw 'Pass an existing validated BundleDir' }
    $compiler = Join-Path $tools 'inno\ISCC.exe'
    $offlineRuntime = Join-Path $downloads 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe'
    $signature = Get-AuthenticodeSignature -LiteralPath $offlineRuntime
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Microsoft Corporation*') { throw 'Offline WebView2 Microsoft signature validation failed' }
    $compilerArgs = @('/Qp', ('/DBundleDir={0}' -f $BundleDir), ('/DDependencyDir={0}' -f $downloads), ('/DReleaseDir={0}' -f $runDir), (Join-Path $PSScriptRoot 'installer.iss'))
    & $Python -B $runner --timeout 600 --reserve 512 --log (Join-Path $runDir 'installer.log') -- $compiler @compilerArgs
    if ($LASTEXITCODE -ne 0) { throw ('Installer compilation failed: {0}; {1}' -f $LASTEXITCODE, $runDir) }
    $record = [ordered]@{ bundle = $BundleDir; run = $runDir; installer = (Join-Path $runDir '视频压缩套件_0.7.1_x64_离线完整版.exe') }
}
$record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $BuildRoot 'last-build.json') -Encoding utf8
$record | ConvertTo-Json
