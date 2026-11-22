@echo off
rem ===================================================================
rem  Video Compression Suite - launcher
rem
rem  Double-clicked  -> relaunches itself hidden, so no console window is
rem                     left on screen while the app runs.
rem  From a terminal -> runs inline so you can read the messages.
rem  Override the host with:  start.bat web   /   start.bat app
rem
rem  PURE ASCII + CRLF: cmd.exe eats bytes on LF-only batch files.
rem ===================================================================
setlocal
set "MODE=%~1"

if /i "%MODE%"=="visible" goto :run
if /i "%MODE%"=="web"     goto :run
if /i "%MODE%"=="app"     goto :run

rem Double-click detection: a double-clicked batch gets its own fresh console.
rem When launched from an existing shell, CMDCMDLINE carries extra arguments.
set "OWN_CONSOLE=0"
if /i "%~1"=="" if /i "%CMDCMDLINE%"=="%COMSPEC%" set "OWN_CONSOLE=1"
if "%OWN_CONSOLE%"=="0" goto :run

start "" /b wscript.exe "%~dp0start-silent.vbs"
exit /b 0

:run
set "PY="
for %%R in ("D:\anaconda3" "%USERPROFILE%\anaconda3" "C:\ProgramData\anaconda3") do (
  if exist "%%~R\envs\vcs\pythonw.exe" set "PY=%%~R\envs\vcs\pythonw.exe"
)
if not defined PY if exist "%~dp0.venv\Scripts\pythonw.exe" set "PY=%~dp0.venv\Scripts\pythonw.exe"

if /i "%MODE%"=="web" goto :web
if /i "%MODE%"=="app" goto :appwin
if not defined PY goto :appwin

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