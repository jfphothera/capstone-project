@echo off
setlocal
title Agent Arena Lab - hub: give the Gateway each head's measured per-model capacity (HEAD-OLLAMA reports)
cd /d "%~dp0"
rem  Run on the admin hub only, as LabOperator1. Reads every head's HEAD-OLLAMA report on this stick, shows the qualification
rem  matrix and, after APPLY, gives the Gateway (0.4.4+) the per-model limits through its own bootstrap. It deletes nothing.
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Apply-ModelCapacity.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
