@echo off
title Agent Arena - 0 Clean slate
cd /d "%~dp0"
echo.
echo  Lists everything Agent Arena placed for this Windows user, asks, then removes it.
echo  Ollama, models, network, firewall and power settings are never touched.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Clean-Slate.ps1"
echo.
echo  This window stays open so you can read the results. Press any key to close.
pause >nul
