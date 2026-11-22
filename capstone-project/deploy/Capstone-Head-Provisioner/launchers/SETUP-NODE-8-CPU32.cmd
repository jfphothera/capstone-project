@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Capstone Node 8 - CPU32
echo.
echo   NODE 8, CPU32 PROFILE - fully unattended.
echo   Sets this computer to 192.0.2.8 on the private Capstone LAN
echo   and installs the CPU32 model set. No questions are asked.
echo   Approve the Windows prompt, then leave the machine running.
echo.
if not exist "%~dp0..\START-CAPSTONE-SETUP.cmd" (
  echo  ERROR: START-CAPSTONE-SETUP.cmd is missing. Re-extract the whole ZIP to one folder.
  pause ^>nul
  endlocal
  exit /b 2
)
call "%~dp0..\START-CAPSTONE-SETUP.cmd" -NodeNumber 8 -ModelProfile CPU32 -NonInteractive
endlocal & exit /b %ERRORLEVEL%
