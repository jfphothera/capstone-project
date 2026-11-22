@echo off
setlocal
title Agent Arena admin hub - uninstall
rem  Removes the program files, start at sign-in and the desktop shortcut. Service data is kept.
cd /d "%~dp0app"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY set "PY=python"
echo.
echo  Close the Arena Event Service and admin console windows first.
pause
%PY% -E -s -m arena_service.hub_install --usb "%~dp0." --uninstall
echo.
pause
