@echo off
setlocal
title Agent Arena - Install
cd /d "%~dp0"
set "EXE=%~dp0app\AgentArena.exe"
if not exist "%EXE%" (
  echo.
  echo  ERROR: app\AgentArena.exe was not found next to this file.
  echo  Extract the WHOLE ZIP to a folder first, then double-click this file again.
  echo  Do not run it from inside the ZIP window.
  goto hold
)
echo.
echo  Opening the Agent Arena installer window...
echo  If Windows shows "Windows protected your PC", click "More info", then "Run anyway".
echo  This window closes by itself when the installer window is closed normally.
start "" /wait "%EXE%" --install
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" exit /b 0
echo.
echo  The installer did not start or ended with an error (code %RC%).
echo  Log file: %LOCALAPPDATA%\AgentArena\logs\agent-arena.log
:hold
echo.
echo  This window stays open so you can read it. Press any key to close.
pause >nul
exit /b 1
