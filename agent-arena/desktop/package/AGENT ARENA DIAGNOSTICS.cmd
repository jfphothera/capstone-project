@echo off
setlocal
title Agent Arena - Diagnostics
cd /d "%~dp0"
rem Works before installation: it runs from this package if Agent Arena is not installed.
set "EXE="
if exist "%LOCALAPPDATA%\Programs\AgentArena\current.txt" (
  set /p VER=<"%LOCALAPPDATA%\Programs\AgentArena\current.txt"
)
if defined VER if exist "%LOCALAPPDATA%\Programs\AgentArena\%VER%\AgentArena.exe" set "EXE=%LOCALAPPDATA%\Programs\AgentArena\%VER%\AgentArena.exe"
if not defined EXE set "EXE=%~dp0app\AgentArena.exe"
if not exist "%EXE%" (
  echo.
  echo  ERROR: AgentArena.exe was not found. Extract the WHOLE ZIP, then try again.
  goto hold
)
echo.
echo  Opening the diagnostics window...
start "" /wait "%EXE%" --diagnostics
if "%ERRORLEVEL%"=="0" exit /b 0
echo.
echo  Diagnostics could not start (code %ERRORLEVEL%).
echo  Log file: %LOCALAPPDATA%\AgentArena\logs\agent-arena.log
:hold
echo.
echo  Press any key to close.
pause >nul
exit /b 1
