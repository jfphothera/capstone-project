@echo off
setlocal
title Agent Arena Lab - hub: pressure test (how many requests at once each head handles best)
cd /d "%~dp0"
rem  Run on the admin hub only, as LabOperator1. 1 = start, 2 = status, 0 = stop and put every head back.
rem  The test runs by itself in the background; its logs go to this stick under reports\<computer>.
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-PressureTest.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
