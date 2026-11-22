@echo off
setlocal
title Agent Arena Lab - hub: put the embedding models back on the heads
cd /d "%~dp0"
rem  Run on the admin hub only, as LabOperator1. Tests again, with the Gateway's own embedding test, every embedding model
rem  (all-minilm and the others) that hub > 3 > 2 took off a head on 2026-11-12. It deletes nothing.
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Repair-HubGatewayQualify.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
