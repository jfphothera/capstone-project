@echo off
setlocal
title Arena admin console (keep this window open)
cd /d "%~dp0.."
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "CFG=%LOCALAPPDATA%\AgentArenaService\aes.config.json"
if not exist "%CFG%" (
  echo  Not configured yet. Run SETUP-ARENA-EVENT-SERVICE.cmd first.
  pause >nul
  exit /b 2
)
echo.
echo  The admin hub's screen opens in your browser. It works only on this computer,
echo  and only in the tab this window opens. Close this window to stop it.
echo  The event service itself keeps running in its own window.
echo.
"%PY%" -E -s -m arena_service.admin_console --config "%CFG%"
echo.
echo  The console stopped (code %ERRORLEVEL%). Press any key to close.
pause >nul
