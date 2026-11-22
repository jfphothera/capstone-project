@echo off
title Agent Arena - 1 Discover (read-only)
cd /d "%~dp0"
echo.
echo  Read-only check of this head. Nothing is changed.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Discover.ps1"
echo.
echo  This window stays open so you can read the results. Press any key to close.
pause >nul