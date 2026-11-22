@echo off
setlocal
title Agent Arena Lab - collect evidence from this computer
cd /d "%~dp0"
rem  One double-click: gathers everything needed to diagnose this lab computer onto this stick
rem  (reports\<computer>\evidence-<date>). It changes nothing on the computer. On the hub and the heads it asks one
rem  question: Enter = logs only (safe during an experiment); SPEED = also time the heads (loads and unloads models).
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0engine\Collect-LabEvidence.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
