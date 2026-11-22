import inspect,json,subprocess,threading,time,sys
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from capstone_rag import gateway_transport
from capstone_rag.webapp import developer_auth
from capstone_rag.webapp.app import create_app
from capstone_rag.webapp.routers import lab

ROOT=Path(__file__).parents[1]


def _reset_cache():
    lab._cache.update(at=0,value=None,building=False,inflight=False,error=None,generation=0,started_at=None)


def test_rc14_assets_are_versioned_watchdogged_and_revalidated(tmp_path):
    html=(ROOT/'src/capstone_rag/webapp/static/pressure_testing.html').read_text(encoding='utf-8')
    assert '?v=rc12' not in html and '?v=rc14' not in html and '?v=rc16' not in html
    for asset in ('theme.js','app.css','theme.css','studio.css','testing_lab.css','testing_lab.js','global_nav.js'):
        assert f'{asset}?v=rc20' in html
    assert 'capstoneLabBootWatchdog' in html and 'Reload Testing Lab' in html
    for path in (ROOT/'src/capstone_rag/webapp/static').rglob('*'):
        if path.suffix in ('.html','.js'):
            assert '?v=rc12' not in path.read_text(encoding='utf-8')
    app_js=(ROOT/'src/capstone_rag/webapp/static/js/app.js').read_text(encoding='utf-8')
    worker=(ROOT/'src/capstone_rag/webapp/static/js/ops_shared_worker.js').read_text(encoding='utf-8')
    assert 'new SharedWorker' in app_js and 'new EventSource' not in app_js
    assert "new EventSource('/api/ops/events')" in worker and 'source.close()' in worker
    assert inspect.iscoroutinefunction(lab.mode) and inspect.iscoroutinefunction(lab.get_state)
    assert inspect.iscoroutinefunction(developer_auth.require_developer)
    app=create_app(start_workers=False,developer_token='acceptance-token')
    with TestClient(app) as client:
        assert client.get('/pressure-testing',follow_redirects=False).status_code==200
        assert client.get('/api/experiments/scenarios').status_code==200
        response=client.get('/static/js/testing_lab.js?v=rc20')
        assert response.status_code==200
        assert response.headers['cache-control']=='no-cache, max-age=0, must-revalidate'


def test_every_browser_javascript_file_parses_as_an_es_module():
    completed=subprocess.run([sys.executable,str(ROOT/'scripts/check_web_modules.py')],capture_output=True,text=True)
    assert completed.returncode==0,completed.stdout+completed.stderr
    report=json.loads(completed.stdout)
    shipped=sorted((ROOT/'src/capstone_rag/webapp/static/js').rglob('*.js'))
    assert report['passed'] and len(shipped)>=1 and len(report['modules'])==len(shipped)
    assert all(row['passed'] for row in report['modules'])


def test_inventory_timeout_becomes_partial_and_is_logged(monkeypatch,tmp_path,capsys):
    class SlowGateway:
        def models(self):time.sleep(.2);return {'resources':[]}
    monkeypatch.setattr(gateway_transport,'enabled',lambda:True)
    monkeypatch.setattr(gateway_transport,'client',lambda workload:SlowGateway())
    monkeypatch.setattr(lab,'_inventory_path',lambda:tmp_path/'missing.json')
    monkeypatch.setattr(lab,'STATE_BUILD_TIMEOUT_SECONDS',.05)
    _reset_cache();lab.state(True)
    deadline=time.time()+1;partial=None
    while time.time()<deadline:
        value=lab.state(False)
        if value['inventory_status']=='partial':partial=value;break
        time.sleep(.01)
    assert partial and 'did not finish' in partial['inventory_message']
    time.sleep(.25)
    log=capsys.readouterr().out
    assert '"event": "started"' in log and '"event": "timeout"' in log and '"event": "finished"' in log
    assert '"duration_ms"' in log and '"error"' in log


def _run_lab_heads(tmp_path,state_provider):
    started=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,value):
            data=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_GET(self):
            if self.path.startswith('/api/lab/state'):return self.reply(state_provider())
            if self.path.startswith('/api/lab/results'):
                eid=self.path.split('experiment_id=',1)[-1];head=next(head for current,head in started if current==eid)
                return self.reply({'experiments':[{'id':eid,'runs':[{'status':'completed','error':None,'model_calls':1,
                    'inference_correlations':[{'resource_id':head,'gateway_request_id':'REQ-'+eid}],
                    'gateway_request_id':'REQ-'+eid,'duration_ms':10,'arena_rows_written':1}]}]})
            self.reply({'detail':'not found'})
        def do_POST(self):
            length=int(self.headers.get('Content-Length','0'));body=json.loads(self.rfile.read(length));eid=f'EXP-{len(started)+1}'
            started.append((eid,body['heads'][0]));self.reply({'id':eid})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        root=tmp_path/'data-root';(root/'config').mkdir(parents=True);token=root/'developer.token';token.write_text('fixture-token',encoding='utf-8')
        (root/'config/appsettings.json').write_text(json.dumps({'listen_host':'127.0.0.1','listen_port':server.server_port,'developer_token_file':str(token)}),encoding='utf-8')
        return subprocess.run(['powershell.exe','-NoProfile','-File',str(ROOT/'deploy/windows/Test-CapstoneLabHeads.ps1'),'-DataRoot',str(root),'-TimeoutSeconds','5'],capture_output=True,text=True,timeout=45)
    finally:server.shutdown();server.server_close();thread.join(3)


def test_lab_heads_waits_for_ready_lists_nine_and_zero_is_failure(tmp_path):
    polls={'count':0};heads=[f'head-192-0-2-{number}' for number in range(2,11)]
    def ready_after_checking():
        polls['count']+=1
        if polls['count']<3:return {'inventory_status':'checking','inventory_message':'checking','heads':[]}
        return {'inventory_status':'ready','heads':[{'resource_id':head,'serving':True,
          'models':[{'name':'qwen3.5:4b','kind':'chat','usable':True,'size_bytes':1}]} for head in heads]}
    completed=_run_lab_heads(tmp_path/'nine',ready_after_checking)
    lines=[json.loads(line) for line in completed.stdout.splitlines() if line.strip().startswith('{')]
    assert completed.returncode==0,completed.stdout+completed.stderr
    assert len(lines)==10 and [line['resource_id'] for line in lines[:9]]==heads
    assert lines[-1]=={'passed':True,'targets':9}
    zero=_run_lab_heads(tmp_path/'zero',lambda:{'inventory_status':'ready','heads':[]})
    zero_lines=[json.loads(line) for line in zero.stdout.splitlines() if line.strip().startswith('{')]
    assert zero.returncode==1
    assert zero_lines[-1]['passed'] is False and zero_lines[-1]['targets']==0
    assert 'zero serving' in zero_lines[-1]['error']
