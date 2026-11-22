import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading

import pytest

from capstone_rag.ai_settings import Registry

ROOT=Path(__file__).resolve().parents[1]


def test_windows_runtime_prerequisite_is_explicit_and_complete():
    required={'msvcp140.dll','msvcp140_atomic_wait.dll','vcruntime140.dll','vcruntime140_1.dll','vcruntime140_threads.dll'}
    for name in ('Install-Capstone.ps1','Update-Capstone.ps1'):
        text=(ROOT/'deploy'/'windows'/name).read_text(encoding='utf-8')
        assert required <= {item for item in required if item in text}
        assert 'Microsoft Visual C++ 2015-2022 Redistributable (x64)' in text
        assert 'vc_redist.x64.exe' in text


def test_every_page_has_navigation_and_global_scrolling():
    static=ROOT/'src'/'capstone_rag'/'webapp'/'static'
    pages=list(static.glob('*.html'))
    assert {p.name for p in pages}=={'index.html','chat.html','trace.html','pressure_testing.html','testing_lab_login.html'}
    for page in pages:
        assert 'global_nav.js' in page.read_text(encoding='utf-8'),page.name
    nav=(static/'js'/'global_nav.js').read_text(encoding='utf-8')
    for label in ('Workspace','Chat','Testing Lab','Execution traces','Sign in','Sign out'):
        assert label in nav
    css=(static/'css'/'app.css').read_text(encoding='utf-8').replace(' ','')
    assert 'html,body{min-height:100%;margin:0;overflow:auto;}' in css
    assert 'overflow:hidden' not in css.split('body{',1)[0]


def test_testing_lab_controls_receive_explanatory_help():
    script=(ROOT/'src'/'capstone_rag'/'webapp'/'static'/'js'/'experiment_ui.js').read_text(encoding='utf-8')
    assert "querySelectorAll('input,select,textarea,button')" in script
    assert 'Valid values' in script and 'Recommended defaults' in script
    assert 'increases queue and head time' in script
    page=(ROOT/'src'/'capstone_rag'/'webapp'/'static'/'pressure_testing.html').read_text(encoding='utf-8')
    assert 'Getting started' in page and 'five-minute Gateway smoke preset' in page


def test_gateway_default_never_uses_rejected_qwen3(tmp_path):
    profile=Registry(tmp_path/'settings.db').get('profiles','gateway-default')
    assert {route['model'] for route in profile['routes'].values()}=={'qwen3.5:4b'}


def test_preset_refuses_models_not_allowed_to_research_identity(monkeypatch,tmp_path):
    from capstone_rag.webapp.routers import experiments as route
    class Gateway:
        def models(self):return {'resources':[{'resource_id':'head-192-0-2-6','models':['qwen3.5:4b']}]} 
    class Runtime:
        inference_registry=Registry(tmp_path/'settings.db')
    monkeypatch.setattr(route,'get_runtime',lambda:Runtime())
    from capstone_rag import gateway_transport
    monkeypatch.setattr(gateway_transport,'enabled',lambda:True)
    monkeypatch.setattr(gateway_transport,'client',lambda workload='operational':Gateway())
    values=route.presets()
    assert values['status']=='authenticated'
    assert all(p['available'] for p in values['presets'])
    profile=Runtime.inference_registry.get('profiles','gateway-default')
    for item in profile['routes'].values():item['model']='refused:1'
    with Runtime.inference_registry.connect() as db:
        db.execute("UPDATE profiles SET payload=? WHERE id='gateway-default'",(json.dumps(profile),))
    refused=next(p for p in route.presets()['presets'] if p['id']=='paired-baseline')
    assert not refused['available'] and 'refused:1' in refused['unavailable_reason']
    assert 'qwen3.5:4b' in refused['unavailable_reason']


def test_gateway_mode_blocks_all_direct_model_operations(monkeypatch):
    from capstone_rag.ai_settings import http
    monkeypatch.setenv('CAPSTONE_INFERENCE_MODE','gateway')
    for operation in ('/api/generate','/api/chat','/api/embed'):
        with pytest.raises(PermissionError,match='disabled in gateway mode'):
            http('http://127.0.0.1:11434',operation,{},1)


def test_lightweight_service_health_auth_and_capstone_round_trip(tmp_path,monkeypatch):
    token=tmp_path/'token';token.write_text('fixture-service-token',encoding='utf-8')
    data=tmp_path/'data';data.mkdir()
    for name in ('customers','devices','orders','shipments'):(data/(name+'.json')).write_text('[]',encoding='utf-8')
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    process=subprocess.Popen([sys.executable,str(ROOT/'services'/'service_host.py'),'--kind','erp','--data-root',str(data),'--host','127.0.0.1','--port',str(port),'--token-file',str(token)])
    try:
        for _ in range(50):
            try:
                health=json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health',timeout=.2));break
            except Exception:time.sleep(.05)
        else:pytest.fail('service did not start')
        assert health=={'ok':True,'service':'erp','format':'capstone-business-service/1'}
        with pytest.raises(urllib.error.HTTPError) as denied:urllib.request.urlopen(f'http://127.0.0.1:{port}/api/erp/customers')
        assert denied.value.code==401
        request=urllib.request.Request(f'http://127.0.0.1:{port}/api/erp/customers',headers={'Authorization':'Bearer fixture-service-token'})
        assert json.load(urllib.request.urlopen(request))=={'rows':[]}
        config=tmp_path/'erp-client.json';config.write_text(json.dumps({'url':f'http://127.0.0.1:{port}','token_file':str(token),'execution_node':'head-192-0-2-4'}),encoding='utf-8')
        monkeypatch.setenv('CAPSTONE_ERP_SERVICE_CONFIG',str(config))
        # A config file alone stays local; remote needs the explicit switch.
        from capstone_rag import remote_services
        assert remote_services.configured('erp') is False
        monkeypatch.setenv('CAPSTONE_BUSINESS_SERVICES','remote')
        assert remote_services.configured('erp') is True
        from capstone_rag.erp import ERP
        from capstone_rag import activity,task_context,store as store_mod
        st=store_mod.Store(str(tmp_path/'operations.db'))
        with activity.investigation(st),task_context.bind(task_id='TASK-ERP',workflow_id='WF-ERP',agent_id='erp_research'):
            remote=ERP()
        assert remote.customers=={} and remote.devices=={} and remote.orders==[]
        events=st.query("SELECT * FROM arena_events WHERE event_type LIKE 'tool.%' ORDER BY sequence")
        assert events and {event['execution_node'] for event in events}=={'head-192-0-2-4'}
        assert {event['tool_node'] for event in events}=={'head-192-0-2-4'}
        st.close()
    finally:
        process.terminate();process.wait(5)


def test_installed_end_to_end_tool_uses_both_identities_and_embedding(tmp_path):
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,value,status=200):
            data=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.send_header('X-Inference-Service-ID','shared-inference');self.end_headers();self.wfile.write(data)
        def do_GET(self):
            app=self.headers.get('X-Application-ID');seen.append((app,self.path))
            if self.path=='/v1/models':return self.reply({'resources':[{'resource_id':'head-192-0-2-6','models':['qwen3.5:4b','embed:fixture'],'model_details':{'qwen3.5:4b':{'qualified_operations':['completion']},'embed:fixture':{'qualified_operations':['embedding']}}}]})
            if self.path=='/v1/policy':return self.reply({'application_id':app,'models':['qwen3.5:4b','embed:fixture']})
            if self.path.startswith('/v1/availability?'):return self.reply({'status':'available'})
            return self.reply({'error':'not found'},404)
        def do_POST(self):
            app=self.headers.get('X-Application-ID');seen.append((app,self.path));body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            operation=body['operation'];model=body['model'];response={'embeddings':[[1.0,0.0]]} if operation=='/api/embed' else {'response':'READY'}
            return self.reply({'status':'completed','request':{'request_id':'REQ-'+str(len(seen)),'status':'completed','resource_id':'head-192-0-2-6'},'result':{'request_id':'REQ-'+str(len(seen)),'resource_id':'head-192-0-2-6','model':model,'model_digest':'sha256:fixture','queue_ms':3,'duration_ms':7,'response':response}})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        token=tmp_path/'token';token.write_text('fixture-token',encoding='utf-8');configs=[]
        for app in ('capstone','capstone-research'):
            path=tmp_path/(app+'.json');path.write_text(json.dumps({'configuration_version':1,'contract':'shared.inference/1','service_id':'shared-inference','url':f'http://127.0.0.1:{server.server_port}','application_id':app,'token_file':str(token)}),encoding='utf-8');configs.append(path)
        result=subprocess.run([sys.executable,'-I',str(ROOT/'scripts'/'capstone_end_to_end.py'),'--operational',str(configs[0]),'--research',str(configs[1])],capture_output=True,text=True,timeout=20)
        output=json.loads(result.stdout)
        assert result.returncode==0 and output['passed'] and len(output['calls'])==3
        assert [call['operation'] for call in output['calls']]==['/api/generate','/api/embed','/api/generate']
        assert {call['resource_id'] for call in output['calls']}=={'head-192-0-2-6'}
        submit_apps=[app for app,path in seen if path=='/v1/submit']
        assert submit_apps==['capstone','capstone','capstone-research']
    finally:
        server.shutdown();server.server_close();thread.join(3)


def test_rc4_update_script_preserves_program_data_contract():
    text=(ROOT/'deploy'/'windows'/'Update-Capstone.ps1').read_text(encoding='utf-8')
    assert "Join-Path $DataRoot 'data\\operations.db'" in text
    assert "Join-Path $DataRoot 'data\\ai_configuration.db'" in text
    assert 'seed-data' not in text
    assert 'gateway' not in '\n'.join(line.lower() for line in text.splitlines() if 'remove-item' in line)


def test_service_packages_have_fixed_lifecycle_and_no_embedded_secret():
    templates=ROOT/'deploy'/'services'/'templates'
    expected={'Install-Service.ps1','Configure-Service.ps1','Start-Service.ps1','Register-ServiceTask.ps1','Test-ServiceHealth.ps1','Update-Service.ps1','Rollback-Service.ps1','Import-ServiceData.ps1'}
    assert expected=={p.name for p in templates.glob('*.ps1')}
    merged='\n'.join(p.read_text(encoding='utf-8') for p in templates.glob('*.ps1'))
    assert 'NT AUTHORITY\\LOCAL SERVICE' in merged and 'credentials_issued=$false' in merged
    assert '{{PORT}}' in merged and '11434' not in merged
