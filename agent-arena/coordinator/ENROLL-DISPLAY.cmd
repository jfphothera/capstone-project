@echo off
setlocal
title Arena - issue a one-time enrollment code
cd /d "%~dp0.."
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "CFG=%LOCALAPPDATA%\AgentArenaService\aes.config.json"
set "DISPLAY=%~1"
if "%DISPLAY%"=="" set /p DISPLAY=Display to enroll (for example head-192-0-2-3):
echo.
"%PY%" -E -s -m arena_service.admin --config "%CFG%" enroll %DISPLAY%
echo.
echo  Give this code to the person installing Agent Arena on %DISPLAY%.
echo  It works once, only from that head's own address, for 24 hours.
echo  Press any key to close.
pause >nul
