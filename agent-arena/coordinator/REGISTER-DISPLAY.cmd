@echo off
setlocal
title Arena - register a display address
cd /d "%~dp0.."
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "CFG=%LOCALAPPDATA%\AgentArenaService\aes.config.json"
rem  REGISTER-DISPLAY.cmd 192.0.2.11                 a new inference head (head-192-0-2-11)
rem  REGISTER-DISPLAY.cmd 192.0.2.249 coder/main     an orchestrator display showing coder/main
set "ADDR=%~1"
set "ORCH=%~2"
if "%ADDR%"=="" set /p ADDR=Address of the display (for example 192.0.2.11):
echo.
if "%ORCH%"=="" (
  "%PY%" -E -s -m arena_service.admin --config "%CFG%" head-add %ADDR%
) else (
  for /f "tokens=1,2 delims=/" %%a in ("%ORCH%") do "%PY%" -E -s -m arena_service.admin --config "%CFG%" orchestrator-display-add orch-%%a-%%b-%ADDR:.=-% orch:%%a/%%b --address %ADDR%
)
echo.
echo  The display is known by its address; installers on that computer now detect it.
echo  Next: ENROLL-DISPLAY.cmd ^<display_id^>, or PREPARE-USB.cmd ... -Enroll ^<display_id^>.
echo  To retire a head: .venv\Scripts\python.exe -m arena_service.admin --config "%CFG%" head-remove head-a-b-c-d
echo  Press any key to close.
pause >nul
