@echo off
setlocal
title Agent Arena - set up the admin computer
rem  SET-UP-THIS-COMPUTER.cmd             makes the changes (asks for administrator approval)
rem  SET-UP-THIS-COMPUTER.cmd -CheckOnly  reports only
echo.
echo  Prepares this computer as the Arena admin computer (192.0.2.254): address, Python,
echo  stay awake, time server, ping, Remote Desktop from the dev laptop. Asks before changing
echo  the address. Then run INSTALL-ADMIN-HUB.cmd as your normal user.
echo.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File \"%~dp0Set-Up-This-Computer.ps1\" %*'"
