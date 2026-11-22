@echo off
setlocal
title Agent Arena Lab Provisioner - tests
rem  Runs the provisioner's own unit and scenario tests. They change nothing on this computer:
rem  the firewall, Ollama and the network are stand-ins. Results go to %TEMP%.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tests\Invoke-LabTests.ps1" -ResultPath "%TEMP%\agent-arena-lab-tests.json"
echo.
echo  Results: %TEMP%\agent-arena-lab-tests.json
pause
