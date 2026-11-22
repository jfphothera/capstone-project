@echo off
title Agent Arena - 3 Collect evidence (read-only)
cd /d "%~dp0"
echo.
echo  Read-only. Captures the Arena window for 2 minutes. Nothing is changed.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Collect-Evidence.ps1"
echo.
echo  This window stays open so you can read the results. Press any key to close.
pause >nul