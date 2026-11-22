@echo off
setlocal
title Agent Arena Lab - hub: requests at once per head, by machine class (all heads)
cd /d "%~dp0"
rem  Run on the admin hub only, as LabOperator1. Asks how many requests at once for the 8 GB, 16 GB, 32 GB and GPU heads
rem  (Enter = the pressure test's result) and applies it to every head through the Gateway. It deletes nothing.
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Set-ParallelPerHead.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
