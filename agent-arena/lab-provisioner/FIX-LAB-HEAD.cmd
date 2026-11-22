@echo off
setlocal
title Agent Arena Lab - fix this lab head (memory record, Defender, Ollama tray, Agent Arena update, restart)
cd /d "%~dp0"
rem  One double-click on a lab head: records what holds its memory, excludes the models from Defender scanning,
rem  stops Ollama's tray app, updates Agent Arena and restarts the head (asks RESTART). It deletes nothing.
rem  Everything it did is written to this stick: reports\<computer>\head-fix-<date>.
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0engine\Fix-LabHead.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
