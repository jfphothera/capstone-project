@echo off
setlocal
title Arena - bring an existing head in line with the hub
rem  FIX-EXISTING-HEAD.cmd            make the changes (asks for administrator approval)
rem  FIX-EXISTING-HEAD.cmd -CheckOnly report only
echo.
echo  Widens this head's Ollama firewall rule to the hub (192.0.2.254) and dev laptop (192.0.2.1),
echo  sets Eastern time with the hub as time source, and keeps the head awake and on the LAN.
echo  Nothing else changes. Details: README.txt next to this file.
echo.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File \"%~dp0Fix-ExistingHead.ps1\" %*'"
