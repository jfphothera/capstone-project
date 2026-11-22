@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Capstone Head - Verify Only
echo.
echo   VERIFICATION ONLY.
echo   Checks the network, Ollama, the installed models and remote serving,
echo   and tests one real inference per installed model.
echo   Nothing is installed, downloaded, changed or removed.
echo.
if not exist "%~dp0START-CAPSTONE-SETUP.cmd" (
  echo  ERROR: START-CAPSTONE-SETUP.cmd is missing. Re-extract the whole ZIP to one folder.
  pause ^>nul
  endlocal
  exit /b 2
)
call "%~dp0START-CAPSTONE-SETUP.cmd" -Mode VerifyOnly
endlocal & exit /b %ERRORLEVEL%
