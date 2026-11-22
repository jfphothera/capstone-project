@echo off
setlocal
title Agent Arena Lab - hub: finish the Inference Gateway update (or return to 0.3.0)
cd /d "%~dp0"
rem  Run on the admin hub only, as LabOperator1. Records the service first, repairs the new release's file access, runs the
rem  Gateway's own update once more, and returns to 0.3.0 if 0.4.0 still does not start. It deletes nothing.
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Repair-HubGatewayUpdate.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
