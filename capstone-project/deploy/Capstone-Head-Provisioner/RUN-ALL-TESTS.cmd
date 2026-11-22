@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Capstone Head Provisioner - Test Suite

echo.
echo   CAPSTONE HEAD PROVISIONER - TEST SUITE
echo.
echo   Runs the static checks, the unit tests and the integration tests.
echo   All three are non-destructive: no address, installation, model or
echo   firewall rule is changed on this machine or any other.
echo.
echo   Administrator rights are not required.
echo.

set "RUNNER=%~dp0tests\Run-AllTests.ps1"
set "PS51=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "RESULT=2"

if not exist "%RUNNER%" (
  echo   ERROR: %RUNNER% is missing. Re-extract the whole ZIP to one folder.
  goto :hold
)
if not exist "%PS51%" (
  echo   ERROR: Windows PowerShell was not found at %PS51%
  goto :hold
)

"%PS51%" -NoProfile -ExecutionPolicy Bypass -File "%RUNNER%" %*
set "RESULT=%ERRORLEVEL%"

echo.
if "%RESULT%"=="0" echo   RESULT: every check passed.
if not "%RESULT%"=="0" echo   RESULT: at least one check failed. See the detail above.
echo.
echo   Machine-readable results: %~dp0docs\test-results\

:hold
echo.
echo   Press any key to close this window.
pause >nul
endlocal & exit /b %RESULT%
