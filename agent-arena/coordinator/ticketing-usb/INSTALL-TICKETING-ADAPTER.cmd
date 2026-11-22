@echo off
setlocal
title Agent Arena - install the Ticketing adapter
rem  Installs the Arena's Ticketing adapter on the Capstone laptop (192.0.2.200-.249), after Capstone.
rem  INSTALL-TICKETING-ADAPTER.cmd                                  asks for the app code
rem  INSTALL-TICKETING-ADAPTER.cmd --capstone-db D:\path\operations.db   Capstone's database elsewhere
cd /d "%~dp0app"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY set "PY=python"
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python 3.12 or later is needed and was not found. Nothing was changed.
  echo.
  pause
  exit /b 2
)
echo.
echo  Ticketing adapter: checking this laptop first. Nothing changes until every check passes.
echo.
%PY% -E -s -m arena_service.app_install --usb "%~dp0." %*
set "CODE=%ERRORLEVEL%"
echo.
pause
exit /b %CODE%
