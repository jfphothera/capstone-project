@echo off
setlocal
title Agent Arena - Launch
set "VER="
if exist "%LOCALAPPDATA%\Programs\AgentArena\current.txt" set /p VER=<"%LOCALAPPDATA%\Programs\AgentArena\current.txt"
set "EXE=%LOCALAPPDATA%\Programs\AgentArena\%VER%\AgentArena.exe"
if not defined VER goto missing
if not exist "%EXE%" goto missing
start "" "%EXE%"
exit /b 0
:missing
echo.
echo  Agent Arena is not installed for this Windows user yet.
echo  Double-click "INSTALL AGENT ARENA" first.
echo.
echo  Press any key to close.
pause >nul
exit /b 2
