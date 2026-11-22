@echo off
setlocal
title Ticketing Arena adapter (keep this window open)
cd /d "%~dp0.."
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "CFG=%LOCALAPPDATA%\AgentArenaService\ticketing-adapter.json"
if not exist "%CFG%" (
  echo  Not configured yet. Run SETUP-ARENA-EVENT-SERVICE.cmd first.
  pause >nul
  exit /b 2
)
echo.
echo  Publishing Ticketing (Capstone) activity to the Arena Event Service.
echo  Reads Capstone's database READ-ONLY; it never writes to Capstone.
echo  Log: %LOCALAPPDATA%\AgentArenaService\ticketing-adapter.log
echo.
"%PY%" -E -s -m arena_service.adapters.capstone --config "%CFG%"
echo.
echo  The adapter stopped (code %ERRORLEVEL%). Press any key to close.
pause >nul
