@echo off
setlocal
title Agent Arena Lab - memory trace (about 2 hours; leave this window open)
cd /d "%~dp0"
rem  On a lab head, as the signed-in user (no administrator): four 30-minute phases - quiet, Agent Arena open,
rem  inference on this head's own Ollama, both - recording the kernel's memory pools, their tags, handles and the
rem  programs every 2 minutes, then a summary of what grew in which phase. Changes nothing, deletes nothing.
rem  Results: reports\<computer>\memory-trace-<date> on this stick.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0engine\Trace-LabMemory.ps1" %*
echo.
echo  Press any key to close this window.
pause >nul
