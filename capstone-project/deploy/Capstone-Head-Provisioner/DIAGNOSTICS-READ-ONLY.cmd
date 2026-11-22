@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Capstone Head - Read-Only Diagnostics
echo.
echo   READ-ONLY DIAGNOSTICS.
echo   Collects the full machine inventory and writes it to disk.
echo   This makes no changes of any kind.
echo.
if not exist "%~dp0START-CAPSTONE-SETUP.cmd" (
  echo  ERROR: START-CAPSTONE-SETUP.cmd is missing. Re-extract the whole ZIP to one folder.
  pause ^>nul
  endlocal
  exit /b 2
)
call "%~dp0START-CAPSTONE-SETUP.cmd" -Mode DiagnosticsOnly
endlocal & exit /b %ERRORLEVEL%
