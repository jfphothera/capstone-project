@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Capstone GPU Workstation - Inspect and Optionally Add Models
echo.
echo   GPU WORKSTATION MODE.
echo   For the existing RTX 4060 Ti machine at 192.0.2.2 only.
echo   Additive: it never changes the network, never reinstalls Ollama,
echo   never moves model storage and never removes an existing model.
echo.
if not exist "%~dp0START-CAPSTONE-SETUP.cmd" (
  echo  ERROR: START-CAPSTONE-SETUP.cmd is missing. Re-extract the whole ZIP to one folder.
  pause ^>nul
  endlocal
  exit /b 2
)
call "%~dp0START-CAPSTONE-SETUP.cmd" -ModelProfile GPU -SkipNetwork -NonInteractive
endlocal & exit /b %ERRORLEVEL%
