@echo off
setlocal
title Agent Arena Lab Provisioner
cd /d "%~dp0"
rem  The one entry point for every lab machine: choose its role, then Inspect, Plan, Configure,
rem  Register, Verify and Report. Nothing changes before you approve the plan.
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0engine\Start-LabProvisioner.ps1" %*
echo.
echo  This window stays open so you can read the result. Press any key to close.
pause >nul
