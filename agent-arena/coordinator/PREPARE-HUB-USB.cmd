@echo off
setlocal
title Arena - prepare the admin hub USB
rem  PREPARE-HUB-USB.cmd                  writes D:\AGENT-ARENA-HUB for a fresh hub
rem  PREPARE-HUB-USB.cmd E:\              another drive
rem  PREPARE-HUB-USB.cmd D:\ --move-data  also carries this service's data (heads keep their enrollment)
cd /d "%~dp0.."
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo.
"%PY%" -E -s -m arena_service.hub_usb %*
echo.
pause
