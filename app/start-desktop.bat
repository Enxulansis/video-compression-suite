@echo off
rem ===================================================================
rem  Video Compression Suite - explicit desktop-window entry (same as start.bat default).
rem
rem  Runs the PyWebView desktop window when available, else the --app window.
rem    1. Desktop window (PyWebView)  -> real drag & drop with real paths
rem    2. Edge/Chrome --app window    -> native dialogs, but no drag paths
rem    3. Plain browser tab           -> last resort
rem  Use start.bat to auto-pick, or start-web.bat for a browser tab.
rem
rem  PURE ASCII + CRLF only: cmd.exe eats bytes on LF-only batch files.
rem ===================================================================
setlocal
set "MODE=%~1"
set "PY="
for %%R in ("D:\anaconda3" "%USERPROFILE%\anaconda3" "C:\ProgramData\anaconda3") do (
  if exist "%%~R\envs\vcs\python.exe" set "PY=%%~R\envs\vcs\python.exe"
)
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

if /i "%MODE%"=="web" goto :web
if /i "%MODE%"=="app" goto :appwin
if not defined PY goto :appwin

rem --- desktop host: verify pywebview before committing to it ---
"%PY%" "%~dp0backend\desktop.py" --check >nul 2>nul
if errorlevel 1 goto :appwin
"%PY%" "%~dp0backend\desktop.py" %*
set "RC=%ERRORLEVEL%"
goto :done

:appwin
pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-app.ps1"
set "RC=%ERRORLEVEL%"
goto :done

:web
pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "RC=%ERRORLEVEL%"

:done
if not "%MODE%"=="quiet" pause
exit /b %RC%