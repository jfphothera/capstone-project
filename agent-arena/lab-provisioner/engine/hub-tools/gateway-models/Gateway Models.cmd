@echo off
rem Gateway Models: the hub's screen for which models each orchestrator may use.
rem It needs administrator rights (the Inference Gateway's ledger and tools are administrator-only):
rem Windows asks once; click Yes. The screen opens in the browser; keep this window open while using it.
net session >nul 2>&1
if %errorlevel% neq 0 (
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
title Gateway Models - keep this window open while you use the screen
if not exist "C:\Program Files\Python312\python.exe" (
  echo The machine-wide Python 3.12 is missing. Administration hub ^> 2 installs it with the Gateway.
  pause
  exit /b 1
)
"C:\Program Files\Python312\python.exe" -I "%~dp0gateway_models.py"
if errorlevel 1 pause
