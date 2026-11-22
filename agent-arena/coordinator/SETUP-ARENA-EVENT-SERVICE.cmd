@echo off
setlocal
title Arena Event Service - one-time setup (coordinator)
cd /d "%~dp0.."
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "DATA=%LOCALAPPDATA%\AgentArenaService"
echo.
echo  Configuring the Arena Event Service in %DATA%
echo  (registers Ticketing and heads 192.0.2.2-.10; starts nothing; changes no firewall or network settings)
echo.
if not defined CAPSTONE_DB set "CAPSTONE_DB=%USERPROFILE%\PycharmProjects\Capstone\data\operations.db"
"%PY%" -E -s -m arena_service.setup_coordinator --data "%DATA%" --capstone-db "%CAPSTONE_DB%" %*
echo.
echo  Press any key to close.
pause >nul
