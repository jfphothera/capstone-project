@echo off
setlocal
title Arena - prepare an install USB
cd /d "%~dp0.."
echo.
echo  Writes AGENT-ARENA-^<version^> to a USB drive: launchers, tools, the built package and
echo  the event service identity. Example:
echo    PREPARE-USB.cmd -Drive D: -Version 2.1.0 -Enroll head-192-0-2-3 -Wipe
echo  -Wipe backs up whatever is on the stick to %%LOCALAPPDATA%%\AgentArenaService\usb-backups first.
echo.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%CD%\desktop\usb\New-ArenaUsb.ps1" %*
echo.
echo  Press any key to close.
pause >nul
