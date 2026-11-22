"""Real loopback subprocess; no physical inference or LAN enrollment."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from inference_gateway.capacity_broker import CapacityBroker
from inference_gateway.capacity_bootstrap import apply
from inference_gateway.capacity_lifecycle import set_admission

ROOT = Path(__file__).resolve().parents[1]


def test_service_process_without_capstone_or_site_packages(tmp_path):
    database = tmp_path/'capacity.db'
    environment = {k:v for k,v in os.environ.items() if not k.startswith('CAPSTONE_') and k!='PYTHONPATH'}
    environment['PYTHONPATH'] = str(ROOT/'src')
    # Fail loudly if any unexpected application dependency is introduced later.
    entry = '''import sys, runpy
class NoApplications:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'capstone_rag','arena','agent_arena','coder','mcp','fastapi','langgraph'}:
            raise ImportError('Application dependency forbidden: '+fullname)
sys.meta_path.insert(0, NoApplications())
runpy.run_module('inference_gateway.capacity_gateway_server', run_name='__main__')
'''
    log = tmp_path/'service.log'
    with log.open('w') as output:
        process = subprocess.Popen([sys.executable,'-S','-u','-c',entry,'--database',str(database),
            '--host','127.0.0.1','--port','0'], cwd=tmp_path, env=environment,
            stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                lines = log.read_text().splitlines()
                if lines: break
                assert process.poll() is None, log.read_text()
                time.sleep(.05)
            assert lines, 'No startup metadata before deadline'
            listener = json.loads(lines[0])
            assert listener['host']=='127.0.0.1' and listener['protocol']=='http'
            origin = 'http://127.0.0.1:'+str(listener['port'])
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def get(path):
                with opener.open(origin+path,timeout=3) as response:return json.load(response)
            assert get('/health')['ok']
            assert get('/ready')['ready']
            with pytest.raises(urllib.error.HTTPError) as denied:get('/v1/models')
            assert denied.value.code==403
            broker=CapacityBroker(database)
            set_admission(broker,False,'test-operator')
            with pytest.raises(urllib.error.HTTPError) as drained:get('/ready')
            assert drained.value.code==503
            assert json.load(drained.value)['ready'] is False
            assert get('/health')['ok']
            set_admission(broker,True,'test-operator')
            assert get('/ready')['ready']
            with broker.connect() as db:
                names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'capacity_requests' in names and 'capacity_invocations' in names
            assert not {'cases','arena_events','worker_registry','qms_documents'} & names
            assert sorted(p.name for p in tmp_path.glob('*.db'))==['capacity.db']
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_bootstrap_generic_clients_no_implicit_heads_or_plaintext_secrets(tmp_path,monkeypatch):
    database=tmp_path/'ledger.db'
    policy=tmp_path/'policy.json'
    policy.write_text(json.dumps({'configuration_version':1,'contract':'shared.inference/1',
        'clients':[{'app_id':app,'token_environment':app.upper()+'_TOKEN','models':['fixture:1']}
                   for app in ['capstone','coder','future_app']], 'resources':[]}))
    for app in ['capstone','coder','future_app']:monkeypatch.setenv(app.upper()+'_TOKEN','synthetic-'+app)
    assert apply(policy,database)['clients']==3
    broker=CapacityBroker(database)
    for app in ['capstone','coder','future_app']:assert broker.authenticate(app,'synthetic-'+app)['app_id']==app
    with broker.connect() as db:
        assert db.execute('SELECT count(*) FROM capacity_resources').fetchone()[0]==0
        assert all(not r[0].startswith('synthetic-') for r in db.execute('SELECT token_hash FROM capacity_clients'))


def test_source_has_no_application_imports():
    for path in (ROOT/'src').rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [n.name for n in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
            assert not any(n.split('.')[0] in {'capstone_rag','agent_arena','arena','coder'} for n in names), path
