@echo off
rem ==========================================================================
rem  Capstone lab qualification (rc.25)  -  run on the Capstone laptop (the Capstone application host)
rem
rem  * Run from an elevated prompt ("Run as administrator") when NO experiment
rem    is running. The tool refuses if an experiment or a live worker is active.
rem  * Launches NO experiment and changes no policy, model, head or setting.
rem  * Sends a small set of real Gateway requests with the Testing Lab
rem    (capstone-research) identity: Gemma mediated tool selection with its real
rem    payload, Granite / Qwen structured QMS and peer budgets, native
rem    tool-argument schema checks, and a two-fixture reviewer calibration.
rem  * Writes qualification-<time>.json and .md into a folder
rem    "qualification-results" beside this file.
rem  Exit codes: 0 all passed or skipped with reason, 1 a check failed,
rem              2 refused (experiment active), 3 installation problem.
rem ==========================================================================
setlocal EnableExtensions
set "INSTALL=C:\Program Files\Capstone"
set "DATA=C:\ProgramData\Capstone"
rem Optional overrides (validation of the tool in a sandbox install only).
if defined CAPSTONE_QUALIFY_INSTALL_ROOT set "INSTALL=%CAPSTONE_QUALIFY_INSTALL_ROOT%"
if defined CAPSTONE_QUALIFY_DATA_ROOT set "DATA=%CAPSTONE_QUALIFY_DATA_ROOT%"
set "OUT=%~dp0qualification-results"
set "PY=%INSTALL%\runtime\Scripts\python.exe"
if not exist "%PY%" set "PY=%INSTALL%\runtime\python.exe"
if not exist "%PY%" (
  echo Capstone managed Python was not found under "%INSTALL%\runtime".
  exit /b 3
)
if not exist "%INSTALL%\current.json" (
  echo Capstone is not installed: "%INSTALL%\current.json" is missing.
  exit /b 3
)
if not exist "%OUT%" mkdir "%OUT%"
echo Capstone lab qualification - results go to "%OUT%"
"%PY%" -I -c "import json,sys,pathlib,runpy;r=pathlib.Path(sys.argv[1]);v=json.loads((r/'current.json').read_text(encoding='utf-8-sig'))['version'];a=r/'releases'/v/'application';src=a/'src';ok=(src/'capstone_rag'/'lab_qualification.py').exists();print('Installed Capstone release: '+v);ok or (print('This tool needs Capstone 6.0.0-rc.25 or later (lab_qualification module missing in '+v+').'),sys.exit(3));sys.path.insert(0,str(src));sys.argv=['lab_qualification','--install-root',str(r),'--data-root',sys.argv[2],'--out',sys.argv[3]];runpy.run_module('capstone_rag.lab_qualification',run_name='__main__',alter_sys=True)" "%INSTALL%" "%DATA%" "%OUT%"
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" echo Qualification finished: every check passed or was skipped with a recorded reason.
if "%RC%"=="1" echo Qualification finished: at least one check FAILED - see the results file.
if "%RC%"=="2" echo Qualification REFUSED: an experiment or a live worker is active. Nothing was sent.
if "%RC%"=="3" echo Qualification could not start: installation problem - see the message above.
endlocal & exit /b %RC%
