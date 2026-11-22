@echo off
setlocal
title Agent Arena Lab - put a head's models back (run on the hub)
cd /d "%~dp0"
rem  On the hub only. Reads the head's last model list from its evidence on this stick and hands the missing ones to
rem  the hub's Fleet Commit helper (download once to the hub's library over Wi-Fi, copy to the head over the lab
rem  cable, check, test). The helper shows the list and waits for APPROVE. Nothing is deleted.
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0engine\Restore-LabHeadModels.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
