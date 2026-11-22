@echo off
setlocal
title Agent Arena Lab - hub: Gateway routing check (read-only, about 6 minutes)
cd /d "%~dp0"
rem  Run on the admin hub (192.0.2.254) as LabOperator1, while Capstone is sending work. READ-ONLY: it changes, restarts,
rem  reconfigures and re-qualifies nothing, and changes no permission.
rem    Part A: the Gateway's published observation file (one full read, then every 5 s for 5 minutes).
rem    Part B: the Gateway's ledger opened read-only - only in an administrator window; otherwise skipped and said so.
rem  Results next to this file: gateway-routing-<computer>-<date>\report.txt
fltmc >nul 2>&1
if not errorlevel 1 goto run
echo.
echo  Asking Windows for administrator rights (needed for Part B, the ledger). Approve the prompt.
echo  If you choose No, Part A still runs here and Part B is skipped.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "try { Start-Process -FilePath '%~f0' -Verb RunAs -ErrorAction Stop; exit 0 } catch { exit 1 }"
if not errorlevel 1 exit /b 0
echo  Not elevated: running Part A only.
:run
set "PY="
if exist "C:\Program Files\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY (
  echo.
  echo  No Python 3 found ^(C:\Program Files\Python312 or this account's own^). Nothing was done.
  goto done
)
"%PY%" -I "%~dp0gateway_routing_check.py" %*
:done
echo.
echo  Press any key to close this window.
pause >nul
