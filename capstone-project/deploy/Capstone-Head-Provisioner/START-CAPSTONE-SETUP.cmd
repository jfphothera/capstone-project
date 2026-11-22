@echo off
setlocal EnableExtensions DisableDelayedExpansion

REM ===========================================================================
REM  Capstone Head Provisioner 1.0.0 - main launcher
REM
REM  Double-click this file, or right-click it and choose
REM  "Run as administrator". Approve the single Windows UAC prompt, then leave
REM  the machine alone. Everything else is automatic.
REM
REM  This launcher does three things and nothing else:
REM    1. obtains administrator rights once,
REM    2. runs the one deployment engine,
REM    3. keeps the window open so the result stays readable.
REM ===========================================================================

cd /d "%~dp0"
title Capstone Head Provisioner

set "ENGINE=%~dp0engine\Invoke-CapstoneProvisioner.ps1"
set "PS51=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "FALLBACKLOG=%~dp0launcher-error.log"
REM Any exit before the engine runs is a launcher failure, not a provisioning
REM result, so RESULT starts non-zero and is only cleared by the engine.
set "RESULT=2"

if not exist "%ENGINE%" (
  echo.
  echo  ERROR: the deployment engine was not found at:
  echo    %ENGINE%
  echo.
  echo  Extract the whole ZIP to one folder and run this file from inside it.
  echo  Do not run it from inside the ZIP viewer.
  echo.
  >"%FALLBACKLOG%" echo %DATE% %TIME% engine missing: %ENGINE%
  goto :hold
)

if not exist "%PS51%" (
  echo.
  echo  ERROR: Windows PowerShell was not found at:
  echo    %PS51%
  echo.
  >"%FALLBACKLOG%" echo %DATE% %TIME% powershell missing: %PS51%
  goto :hold
)

REM --- Administrator check. fltmc needs elevation, so it is a reliable probe.
fltmc >nul 2>&1
if not errorlevel 1 goto :elevated

echo.
echo  Requesting administrator rights. Approve the Windows prompt.
echo  Network and firewall changes cannot be made without it.
echo.
REM Passing an empty -ArgumentList is an error, so the no-argument case, which
REM is the normal double-click, uses a separate call.
if "%~1"=="" (
  "%PS51%" -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath '%~f0' -Verb RunAs -ErrorAction Stop } catch { exit 1 }"
) else (
  "%PS51%" -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs -ErrorAction Stop } catch { exit 1 }"
)
if errorlevel 1 (
  echo.
  echo  Administrator rights were declined, so nothing was changed.
  echo  Right-click this file and choose "Run as administrator" to try again.
  echo.
  goto :hold
)
REM The elevated copy owns the run from here; this window is finished.
endlocal
exit /b 0

:elevated
echo.
echo  ===================================================================
echo   CAPSTONE HEAD PROVISIONER
echo  ===================================================================
echo.
echo   Running with administrator rights.
echo   After at most one setup question, no further input is needed.
echo   Model downloads can take a long time. Leave this window open.
echo.

"%PS51%" -NoProfile -ExecutionPolicy Bypass -File "%ENGINE%" %*
set "RESULT=%ERRORLEVEL%"

echo.
echo  ===================================================================
if "%RESULT%"=="0"  echo   FINISHED: full success. No manual action is needed.
if "%RESULT%"=="10" echo   FINISHED: partial success. Read "WHAT STILL NEEDS ATTENTION" above.
if "%RESULT%"=="20" echo   FINISHED: failed. Read "WHAT STILL NEEDS ATTENTION" above.
if "%RESULT%"=="3"  echo   FINISHED: the package is incomplete. Re-extract the ZIP.
if "%RESULT%"=="4"  echo   FINISHED: another provisioner run is already in progress.
echo.
echo   Plain-English summary:
echo     C:\ProgramData\CapstoneProvisioner\reports\LATEST-STATUS.txt
echo   Machine-readable summary:
echo     C:\ProgramData\CapstoneProvisioner\reports\LATEST-STATUS.json
echo  ===================================================================
echo.

:hold
echo  This window stays open so the result above can be read.
echo  Press any key to close it.
pause >nul
endlocal & exit /b %RESULT%
