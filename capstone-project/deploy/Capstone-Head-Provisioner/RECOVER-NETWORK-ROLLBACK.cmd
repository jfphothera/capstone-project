@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Capstone Head - Network Rollback
echo.
echo   NETWORK ROLLBACK.
echo   Restores the Ethernet adapter to the exact settings recorded before
echo   this tool changed it. An adapter that was on DHCP goes back to DHCP.
echo   Ollama and the downloaded models are not touched.
echo.
if not exist "%~dp0START-CAPSTONE-SETUP.cmd" (
  echo  ERROR: START-CAPSTONE-SETUP.cmd is missing. Re-extract the whole ZIP to one folder.
  pause ^>nul
  endlocal
  exit /b 2
)
call "%~dp0START-CAPSTONE-SETUP.cmd" -Mode Rollback
endlocal & exit /b %ERRORLEVEL%
