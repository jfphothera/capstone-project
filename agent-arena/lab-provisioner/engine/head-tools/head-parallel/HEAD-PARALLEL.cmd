@echo off
setlocal
title Agent Arena Lab - head: Ollama runs several requests at once (1 = on, 0 = off)
cd /d "%~dp0"
rem  Run on an inference head (192.0.2.2-199) as its administrator. 1 = let Ollama run several requests at once
rem  (for the hub's pressure test and for parallel work), 0 = back to how it was. Restarts Ollama; deletes nothing.
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Set-HeadOllamaParallel.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
