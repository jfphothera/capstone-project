@echo off
setlocal
title Agent Arena Lab - head: pinned Ollama and verified parallel inference (1 = update and qualify, 2 = qualify again, 0 = roll back)
cd /d "%~dp0"
rem  Run on an inference head (192.0.2.2-199) as its administrator. 1 = update Ollama to the version pinned in the
rem  provisioner (from this stick, no internet needed), set the explicit server settings, restart, prove the running
rem  server version and qualify parallel inference (about an hour). 2 = qualify again. 0 = roll back. Deletes nothing.
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  rem 1.6.17: the options (for example -AllowProtected on head 2) are passed on to the elevated window.
  if "%~1"=="" ("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs") else ("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs")
  exit /b 0
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Update-HeadOllama.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
