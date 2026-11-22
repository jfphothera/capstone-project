@echo off
setlocal
title Agent Arena Lab - Inference Gateway 0.4.0 live head test (acceptance row 20)
rem  Run on the admin hub (192.0.2.254) as LabOperator1, BEFORE the Gateway update. It sends one idle head a few short Ollama
rem  requests with the Gateway's own code and test file, and writes the result to reports\<computer>. It changes nothing.
cd /d "%~dp0"
set "PY=C:\Program Files\Python312\python.exe"
if not exist "%PY%" ( echo  The machine-wide Python 3.12 is missing: this is not the hub. & pause & exit /b 2 )
set "HEAD=%~1"
if "%HEAD%"=="" set "HEAD=192.0.2.5"
for %%F in ("%~dp0..\..\..\packages\gateway\inference-gateway-*.zip") do set "PKG=%%~fF"
for %%R in ("%~dp0..\..\..\..") do set "STICK=%%~fR"
echo.
echo  Gateway 0.4.0 live test against head %HEAD% (about 3 minutes). Do not start Capstone work meanwhile.
echo.
"%PY%" -I "%~dp0run_live_test.py" --head %HEAD% --package "%PKG%" --out "%STICK%\reports\%COMPUTERNAME%"
echo.
echo  Press any key to close this window.
pause >nul
