"""Assemble the Gateway 0.4.6 lab tools (lab-tools/<tool>/) from lab-tools/src and the c922 fixtures.

Each tool is one self-contained Python file (standard library only: the common part is copied in, fixtures are
embedded compressed) plus one .cmd entry. The fixtures are read from lab-tools/src/fixtures/*.json, which were
extracted from Capstone's outputs/experiment-evaluation-c922/calls.json (see their 'source' field).

    python scripts/build_lab_tools.py
"""
import base64,hashlib,json,zlib
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'lab-tools'/'src'
TOOLS={
    'g6-capacity-check':('g6_capacity_check.py','g6-fixture.json','G6-CAPACITY-CHECK.cmd','G6 capacity check (1 to 24 callers in 13 levels, about 60-65 minutes)'),
    'g4-gemma500-repro':('g4_gemma500_repro.py','g4-fixture.json','G4-GEMMA500-REPRO.cmd','G4 Gemma HTTP 500 reproduction on one idle head (about 10 minutes)'),
    'launch-storm':('launch_storm.py',None,'LAUNCH-STORM.cmd','G1/G3 launch-storm measurement (discovery only, a few minutes)'),
}
CMD='''@echo off
setlocal
title Inference Gateway 0.4.6 lab tool - {title}
rem  Run on the hub (HUB, 192.0.2.254), where the Gateway runs, as an administrator who can read
rem  C:\\ProgramData\\InferenceGateway\\observation. It refuses to run while the Gateway has experiment work.
rem  Results go to the results folder beside this file. Arguments are passed on (for example --client-config <path>).
fltmc >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Asking Windows for administrator rights. Approve the prompt to continue.
  if "%~1"=="" ( "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" ) else ( "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs" )
  exit /b 0
)
cd /d "%~dp0"
set "PY=C:\\Program Files\\Python312\\python.exe"
if not exist "%PY%" ( echo  The machine-wide Python 3.12 is missing: this is not the hub. & pause & exit /b 2 )
"%PY%" -I "%~dp0{script}" %*
set "CODE=%ERRORLEVEL%"
echo.
echo  Press any key to close this window.
pause >nul
exit /b %CODE%
'''


def build():
    common=(SRC/'common.py').read_text(encoding='utf-8')
    out={}
    for folder,(script,fixture,cmd,title) in TOOLS.items():
        body=(SRC/script).read_text(encoding='utf-8')
        doc,rest=body.split('"""',2)[1],body.split('"""',2)[2]
        if fixture:
            raw=json.dumps(json.loads((SRC/'fixtures'/fixture).read_text(encoding='utf-8')),separators=(',',':'),sort_keys=True).encode()
            rest=rest.replace("'@@FIXTURE@@'",repr(base64.b64encode(zlib.compress(raw,9)).decode()))
        text='"""%s"""\n%s\n%s'%(doc,common,rest.lstrip('\n'))
        target=ROOT/'lab-tools'/folder;target.mkdir(parents=True,exist_ok=True)
        (target/script).write_text(text,encoding='utf-8',newline='\n')
        (target/cmd).write_text(CMD.format(title=title,script=script).replace('\n','\r\n'),encoding='utf-8',newline='')
        out[folder]={'script':str(target/script),'cmd':str(target/cmd),'sha256':hashlib.sha256((target/script).read_bytes()).hexdigest()}
    return out


if __name__=='__main__':print(json.dumps(build(),indent=2))
