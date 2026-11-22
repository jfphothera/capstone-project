@echo off
title Agent Arena - 2 Install and verify
cd /d "%~dp0"
echo.
echo  Checks, installs Agent Arena with the installer window, then proves it works.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Install-And-Verify.ps1"
echo.
echo  This window stays open so you can read the results. Press any key to close.
pause >nul