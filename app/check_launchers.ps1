# Video Compression Suite -- launcher files sanity check
#
# Why this exists: two encoding bugs already shipped, both silent and both
# invisible to PowerShell 7 alone:
#
#   1. start.bat had LF-only line endings with Chinese text. cmd.exe mis-tracks
#      its parse offset on LF-only batch files and eats bytes (`chcp` -> `cher.`,
#      `pwsh` -> `user.`), producing a cascade of "is not recognized" errors.
#   2. setup-env.ps1 was saved as BOM-less UTF-8. PowerShell 7 reads that fine,
#      but Windows PowerShell 5.1 (what `powershell` and an Anaconda Prompt give
#      you) decodes it as ANSI and shreds the Chinese strings:
#      "The string is missing the terminator". We only parsed with pwsh 7, so we
#      missed it until the user hit it.
#
# Usage:  .\check_launchers.ps1 [-Fix] [-Quiet]
#   -Fix    rewrite CRLF, add the UTF-8 BOM to .ps1, strip non-ASCII from .bat/.cmd

[CmdletBinding()]
param(
    [switch]$Fix,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Result {
    param([string]$Status, [string]$File, [string]$Message, [string]$Colour = 'Gray')
    if ($Quiet -and $Status -eq 'OK') { return }
    Write-Host ("  [{0,-4}] {1,-22} {2}" -f $Status, $File, $Message) -ForegroundColor $Colour
}

function Convert-ToCrlf {
    param([string]$Path, [switch]$WithBom)
    # ReadAllText honours an existing BOM, so re-encoding never corrupts the text.
    $text = [System.IO.File]::ReadAllText($Path)
    $text = ($text -replace "`r`n", "`n") -replace "`n", "`r`n"
    $encoding = New-Object System.Text.UTF8Encoding($WithBom.IsPresent)
    [System.IO.File]::WriteAllText($Path, $text, $encoding)
}

Write-Host ''
Write-Host '  Launcher file check' -ForegroundColor Cyan
Write-Host '  -------------------' -ForegroundColor DarkGray

# Every launcher-ish file in the app tree.
$patterns = @('*.bat', '*.cmd', '*.ps1')
$files = @()
foreach ($pattern in $patterns) {
    $files += Get-ChildItem -Path $root -Recurse -File -Filter $pattern -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\\.venv\\' }
}

$problems = 0
foreach ($file in $files) {
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    $loneLf = [regex]::Matches($text, "(?<!`r)`n").Count
    $nonAscii = ($bytes | Where-Object { $_ -gt 127 } | Measure-Object).Count
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    $isBatch = $file.Extension -in '.bat', '.cmd'

    $issues = @()
    if ($loneLf -gt 0) { $issues += "LF-only x$loneLf" }
    # .bat MUST NOT have a BOM (cmd would try to run it); .ps1 with non-ASCII MUST
    # have one so Windows PowerShell 5.1 does not decode it as ANSI.
    if ($isBatch -and $hasBom) { $issues += 'UTF-8 BOM (cmd 会报错)' }
    if (-not $isBatch -and $nonAscii -gt 0 -and -not $hasBom) { $issues += '缺少 UTF-8 BOM（PS 5.1 会乱码）' }
    if ($isBatch -and $nonAscii -gt 0) { $issues += "non-ASCII x$nonAscii" }

    if (-not $issues) {
        $note = if ($isBatch) { 'CRLF, no BOM, ASCII-safe' } else { 'CRLF, BOM ok' }
        Write-Result 'OK' $file.Name $note 'Green'
        continue
    }

    $problems++
    if ($Fix) {
        $needsBom = (-not $isBatch -and $nonAscii -gt 0)
        if ($isBatch -and $nonAscii -gt 0) {
            Write-Result 'FAIL' $file.Name ("非 ASCII 内容需人工移到 .ps1：" + ($issues -join ', ')) 'Red'
            Write-Host "         批处理文件必须保持纯 ASCII，中文提示请写在 .ps1 里。" -ForegroundColor Yellow
            continue
        }
        Convert-ToCrlf -Path $file.FullName -WithBom:$needsBom
        Write-Result 'FIXED' $file.Name ($issues -join ', ') 'Yellow'
    } else {
        Write-Result 'FAIL' $file.Name ($issues -join ', ') 'Red'
    }
}

# Final gate: every .ps1 must also parse under Windows PowerShell 5.1, which is
# what an Anaconda Prompt or a bare `powershell` call gives you.
$ps51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (Test-Path -LiteralPath $ps51) {
    $bad = 0
    foreach ($file in $files | Where-Object { $_.Extension -eq '.ps1' }) {
        $out = & $ps51 -NoProfile -Command "`$e=`$null; `$null=[System.Management.Automation.Language.Parser]::ParseFile('$($file.FullName)',[ref]`$null,[ref]`$e); if(`$e.Count){'BAD'}else{'OK'}" 2>&1
        if (($out -join '') -match 'BAD') {
            $bad++
            Write-Result 'FAIL' $file.Name 'PowerShell 5.1 解析失败（编码问题）' 'Red'
        }
    }
    if ($bad -eq 0) { Write-Result 'OK' '.ps1 (5.1 解析)' '全部通过 Windows PowerShell 5.1 解析' 'Green' }
    else { $problems += $bad }
}

Write-Host ''
if ($problems -eq 0) {
    Write-Host '  All launcher files are fine.' -ForegroundColor Green
    exit 0
}
if ($Fix) {
    Write-Host "  Rewrote $problems file(s)." -ForegroundColor Yellow
    exit 0
}
Write-Host "  $problems file(s) need attention. Re-run with -Fix to repair." -ForegroundColor Red
exit 1
