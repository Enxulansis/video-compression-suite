@echo off
rem ===================================================================
rem  Video Compression Suite - double-click launcher
rem
rem  Opens the app in a normal browser tab.
rem
rem  For the dedicated app window with native dialogs use start.bat.
rem
rem  This file is deliberately PURE ASCII with CRLF line endings.
rem  Do not add non-ASCII text here, and do not let an editor rewrite it
rem  with LF-only endings: cmd.exe mis-tracks its parse offset on
rem  LF-only batch files and silently eats bytes, turning commands like
rem  "chcp" into "cher." - the exact failure observed in the field.
rem  All Chinese output is emitted by start.ps1 instead.
rem ===================================================================
setlocal
set "SILENT="
set "ARGS="

rem "silent" (first argument only) suppresses the trailing pause; it is a
rem launcher-only switch and must NOT be forwarded to the script.
if /i "%~1"=="silent" (set "SILENT=1") else (set "ARGS=%*")

where pwsh >nul 2>nul
if errorlevel 1 goto :no_pwsh

pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %ARGS%
set "RC=%ERRORLEVEL%"
goto :done

:no_pwsh
echo.
echo   [X] PowerShell 7 (pwsh) was not found on PATH.
echo       Install it with:  winget install Microsoft.PowerShell
echo.
set "RC=1"

:done
if not defined SILENT pause
exit /b %RC%
