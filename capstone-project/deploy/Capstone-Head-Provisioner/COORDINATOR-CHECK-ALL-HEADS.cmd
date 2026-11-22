@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Capstone Coordinator - Check All Heads

echo.
echo   COORDINATOR HEAD CHECK (read-only)
echo.
echo   Run this on the coordinator (192.0.2.1).
echo   It queries every head at 192.0.2.2 through 192.0.2.7, lists the models
echo   each one has, and asks one model on each head for a short reply to
echo   prove end-to-end remote inference.
echo.
echo   It changes nothing, on this machine or on any head, and needs no
echo   administrator rights. No keyboard is needed on the heads.
echo.

set "CHECK=%~dp0coordinator\Test-CapstoneHeads.ps1"
set "PS51=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "RESULT=2"

if not exist "%CHECK%" (
  echo   ERROR: %CHECK% is missing. Re-extract the whole ZIP to one folder.
  goto :hold
)
if not exist "%PS51%" (
  echo   ERROR: Windows PowerShell was not found at %PS51%
  goto :hold
)

"%PS51%" -NoProfile -ExecutionPolicy Bypass -File "%CHECK%" %*
set "RESULT=%ERRORLEVEL%"

echo.
if "%RESULT%"=="0"  echo   RESULT: every reachable head proved remote inference.
if "%RESULT%"=="10" echo   RESULT: some heads are reachable but did not prove remote inference.
if "%RESULT%"=="20" echo   RESULT: no head answered on the private LAN.

:hold
echo.
echo   Press any key to close this window.
pause >nul
endlocal & exit /b %RESULT%
