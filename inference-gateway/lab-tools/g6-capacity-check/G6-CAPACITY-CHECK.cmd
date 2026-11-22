@echo off
setlocal
title Inference Gateway 0.4.6 lab tool - G6 capacity check (1 to 24 callers in 13 levels, about 60-65 minutes)
rem  Run on the hub (HUB, 192.0.2.254), where the Gateway runs, as an administrator who can read
rem  C:\ProgramData\InferenceGateway\observation. It refuses to run while the Gateway has experiment work.
rem  Results go to the results folder beside this file. Arguments are passed on (for example --client-config <path>).
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  if "%~1"=="" ( "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" ) else ( "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs" )
  exit /b 0
)
cd /d "%~dp0"
set "PY=C:\Program Files\Python312\python.exe"
if not exist "%PY%" ( echo  The machine-wide Python 3.12 is missing: this is not the hub. & pause & exit /b 2 )
"%PY%" -I "%~dp0g6_capacity_check.py" %*
set "CODE=%ERRORLEVEL%"
echo.
echo  Press any key to close this window.
pause >nul
exit /b %CODE%
