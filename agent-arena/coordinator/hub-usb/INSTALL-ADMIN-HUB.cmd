@echo off
setlocal
title Agent Arena admin hub - install
rem  Installs the Arena event service and admin console on the admin hub (192.0.2.254).
rem  INSTALL-ADMIN-HUB.cmd                 install or update
rem  INSTALL-ADMIN-HUB.cmd --replace-data  replace this hub's data with the data this USB carries
cd /d "%~dp0app"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY set "PY=python"
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python 3.12 or later is needed and was not found. Install it from python.org
  echo  for this Windows user, then run this again. Nothing was changed.
  echo.
  pause
  exit /b 2
)
echo.
echo  Agent Arena admin hub: checking this computer first. Nothing changes until every check passes.
echo.
%PY% -E -s -m arena_service.hub_install --usb "%~dp0." %*
set "CODE=%ERRORLEVEL%"
echo.
pause
exit /b %CODE%
