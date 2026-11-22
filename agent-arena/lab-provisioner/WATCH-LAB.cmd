@echo off
setlocal
title WATCH-LAB - live, read-only view of the lab
cd /d "%~dp0"
rem  One double-click on the hub, the Capstone laptop or a head: a live window of that machine at work.
rem    hub: the Inference Gateway's traffic   laptop: Capstone's agents step by step   head: its Ollama at work
rem  Read-only: it changes nothing and does not slow a run. Ctrl+C or closing the window stops it.
rem  It asks for administrator rights only to read the service folders (Capstone's and the Gateway's data).
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights to read the service folders. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0engine\watch\Start-Watch.ps1" %*
echo.
echo  WATCH-LAB stopped. Press any key to close this window.
pause >nul
