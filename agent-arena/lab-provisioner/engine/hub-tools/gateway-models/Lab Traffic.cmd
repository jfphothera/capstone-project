@echo off
rem Lab Traffic: realistic TEST work through the Inference Gateway to the heads, shown live in Agent Arena.
rem It needs administrator rights (the Gateway's tools are administrator-only): Windows asks once; click Yes.
net session >nul 2>&1
if %errorlevel% neq 0 (
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
title Lab Traffic - TEST work through the Gateway
if not exist "C:\Program Files\Python312\python.exe" (
  echo The machine-wide Python 3.12 is missing. Administration hub ^> 2 installs it with the Gateway.
  pause
  exit /b 1
)
"C:\Program Files\Python312\python.exe" -I "%~dp0lab_traffic.py"
pause
