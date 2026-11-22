import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from capstone_rag.ai_settings import Registry, canonical


ROOT=Path(__file__).resolve().parents[1]


def deployment_script(name):
    candidates=(ROOT/'deploy'/'windows'/name,ROOT.parents[1]/'tools'/name,ROOT.parents[2]/'tools'/name)
    return next(path for path in candidates if path.is_file())


def test_start_script_downgrades_native_stderr_before_python_launch():
    script=deployment_script('Start-Capstone.ps1').read_text(encoding='utf-8')
    launch="& $python (Join-Path $app 'run_app.py')"
    assert "$ErrorActionPreference='Continue'" in script
    assert script.index("$ErrorActionPreference='Continue'") < script.index(launch)
    assert '1>>$log 2>>$errors' in script


@pytest.mark.skipif(not shutil.which('powershell.exe'),reason='Windows PowerShell 5.1 is unavailable')
def test_windows_powershell_native_stderr_stand_in_keeps_running(tmp_path):
    stand_in=tmp_path/'stand_in.py'
    stand_in.write_text("import sys,time\nprint('Started server process',file=sys.stderr,flush=True)\ntime.sleep(1.0)\n",encoding='utf-8')
    error_log=tmp_path/'error.log';output_log=tmp_path/'output.log';launcher=tmp_path/'launch.ps1'
    launcher.write_text(
        "$ErrorActionPreference='Stop'\n"
        "$ErrorActionPreference='Continue'\n"
        f"& '{sys.executable}' '{stand_in}' 1>>'{output_log}' 2>>'{error_log}'\n"
        "exit $LASTEXITCODE\n",encoding='utf-8')
    started=time.monotonic()
    result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(launcher)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0
    assert time.monotonic()-started>=0.9
    # Windows PowerShell 5.1 writes redirected native stderr as UTF-16.
    assert 'Started server process' in error_log.read_text(encoding='utf-16')


def test_gateway_verifier_imports_from_installed_layout_under_isolated_python(tmp_path):
    token=tmp_path/'token';token.write_text('fixture-only',encoding='utf-8')
    configs=[]
    for application in ('capstone','capstone-research'):
        path=tmp_path/f'{application}.json'
        path.write_text(json.dumps({'configuration_version':1,'contract':'shared.inference/1',
            'service_id':'shared-inference','url':'http://127.0.0.1:1',
            'application_id':application,'token_file':str(token)}),encoding='utf-8')
        configs.append(path)
    result=subprocess.run([sys.executable,'-I',str(ROOT/'scripts'/'verify_gateway_clients.py'),
        '--operational',str(configs[0]),'--research',str(configs[1])],capture_output=True,text=True,timeout=10)
    output=json.loads(result.stdout)
    assert result.returncode==1
    assert output['status']=='offline_or_unverified'
    assert output['error_class']!='ModuleNotFoundError'


def test_release_reseeds_gateway_default_and_preserves_run_snapshots(tmp_path):
    path=tmp_path/'settings.db';registry=Registry(path)
    baseline=registry.get('profiles','gateway-default')
    assert {route['model'] for route in baseline['routes'].values()}=={'qwen3.5:4b'}
    for route in baseline['routes'].values():route['model']='qwen3:4b'
    snapshot={'id':'RUN-OLD','experiment_id':'EXP-OLD','profile':baseline}
    with registry.connect() as db:
        db.execute("UPDATE profiles SET payload=? WHERE id='gateway-default'",(canonical(baseline),))
        db.execute('INSERT INTO experiment_runs VALUES (?,?,?)',('RUN-OLD','EXP-OLD',canonical(snapshot)))
    migrated=Registry(path)
    assert {route['model'] for route in migrated.get('profiles','gateway-default')['routes'].values()}=={'qwen3.5:4b'}
    with migrated.connect() as db:preserved=json.loads(db.execute("SELECT payload FROM experiment_runs WHERE id='RUN-OLD'").fetchone()[0])
    assert {route['model'] for route in preserved['profile']['routes'].values()}=={'qwen3:4b'}
