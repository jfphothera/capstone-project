@echo off
setlocal
title Arena Event Service (keep this window open)
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
echo  Arena Event Service is starting. It is independent of Capstone:
echo  stopping Capstone does not stop this service. Close this window to stop it.
echo  Log: %LOCALAPPDATA%\AgentArenaService\arena-event-service.log
echo.
"%PY%" -E -s -m arena_service --config "%CFG%"
echo.
echo  The service stopped (code %ERRORLEVEL%). See the log above. Press any key to close.
pause >nul
