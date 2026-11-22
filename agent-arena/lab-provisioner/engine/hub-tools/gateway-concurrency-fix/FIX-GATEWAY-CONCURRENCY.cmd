@echo off
setlocal
title Agent Arena Lab - hub: how many requests Capstone may have in the Gateway at once, and how many may wait
cd /d "%~dp0"
rem  Run on the admin hub only, as LabOperator1. Asks how many requests at once for Capstone and Capstone research (Enter =
rem  1024, the Gateway's own maximum) and how many may wait for a free head (Enter = 10000, the Gateway's maximum), and
rem  sets them through the Gateway's own bootstrap (heads and their qualifications untouched); the hub's policy copy is
rem  kept in step so later hub runs keep them. About a minute. It deletes nothing.
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Repair-HubGatewayConcurrency.ps1" -Ask -SkipRetest %*
echo.
echo  Press any key to close this window.
pause >nul
