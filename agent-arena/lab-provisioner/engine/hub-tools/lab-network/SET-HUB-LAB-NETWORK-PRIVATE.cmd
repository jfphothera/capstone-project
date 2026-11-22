@echo off
setlocal
title Agent Arena Lab - hub: make the lab network Private (Inference Gateway firewall rule)
cd /d "%~dp0"
rem  Run on the admin hub (192.0.2.254) only. Sets the lab cable's network to Private and keeps it Private after restarts,
rem  so the Inference Gateway's firewall rule for the Capstone laptop applies. It deletes nothing.
rem  What it did is written next to this file: network-fix-<computer>-<date>.txt
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Set-HubLabNetworkPrivate.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
