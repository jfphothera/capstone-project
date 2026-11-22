@echo off
setlocal
title Agent Arena Lab - Capstone laptop: copy Capstone's logs to the stick
cd /d "%~dp0"
rem  Run on the Capstone laptop (DESKTOP-REDACT11). Copies Capstone's two log files (small) to the stick and asks Capstone
rem  itself whether the Testing Lab answers. Copies no databases. Changes nothing, deletes nothing.
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Get-CapstoneLogs.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
