@echo off
rem Stop the background server.
rem Runs the paired .ps1 with PowerShell 7 when available, falling back to
rem Windows PowerShell 5.1. The .ps1 files carry a UTF-8 BOM so 5.1 reads the
rem Chinese text correctly.
setlocal
set "PS=pwsh"
where pwsh >nul 2>nul
if errorlevel 1 set "PS=powershell"

"%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
set "RC=%ERRORLEVEL%"
exit /b %RC%
