import ast
import json
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

import pytest

from capstone_rag import gateway_transport
from shared_inference_client import Client


def test_approved_bootstrap_uses_shared_sdk(tmp_path,monkeypatch):
    bootstrap=tmp_path/'client.json'
    bootstrap.write_text(json.dumps({
        'configuration_version':1,
        'contract':'shared.inference/1',
        'service_id':'shared-inference',
        'url':'http://127.0.0.1:8765',
        'application_id':'capstone',
        'token_environment':'TEST_CAPSTONE_INFERENCE_TOKEN',
    }),encoding='utf-8')
    monkeypatch.setenv('CAPSTONE_GATEWAY_CLIENT_CONFIG',str(bootstrap))
    monkeypatch.setenv('TEST_CAPSTONE_INFERENCE_TOKEN','fixture-secret')
    configured=gateway_transport.client()
    assert configured.url=='http://127.0.0.1:8765'
    assert configured.app=='capstone'
    assert configured.service_id=='shared-inference'


def test_remote_plaintext_bootstrap_fails_closed(tmp_path,monkeypatch):
    bootstrap=tmp_path/'client.json'
    bootstrap.write_text(json.dumps({
        'configuration_version':1,'contract':'shared.inference/1',
        'service_id':'shared-inference','url':'http://192.0.2.254:9999',
        'application_id':'capstone','token_environment':'TEST_CAPSTONE_INFERENCE_TOKEN',
    }),encoding='utf-8')
    monkeypatch.setenv('CAPSTONE_GATEWAY_CLIENT_CONFIG',str(bootstrap))
    monkeypatch.setenv('TEST_CAPSTONE_INFERENCE_TOKEN','fixture-secret')
    with pytest.raises(ValueError,match='verified HTTPS'):
        gateway_transport.client()


def test_adapter_preserves_contract_and_removes_server_owned_controls(monkeypatch):
    calls=[]
    class FakeClient:
        def availability(self,*args,**kwargs):return {'status':'available'}
        def infer(self,*args,**kwargs):
            calls.append((args,kwargs))
            return {'status':'completed','result':{'response':'READY','resource_id':'head-1'}}
    monkeypatch.setattr(gateway_transport,'client',lambda workload='operational':FakeClient())
    result=gateway_transport.invoke(None,{'url':'https://gateway.invalid'},'model:1','/api/generate',{
        'model':'other','stream':True,'keep_alive':'forever','prompt':'safe',
    },10,request_id='CALL-1')
    assert result['response']=='READY'
    args,kwargs=calls[0]
    assert args[:3]==('model:1',None,'/api/generate')
    assert args[3]=={'prompt':'safe'}
    assert kwargs['client_key']=='CALL-1'


def test_authenticated_sdk_submit_poll_round_trip():
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,body):
            data=json.dumps(body).encode()
            self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('X-Inference-Service-ID','shared-inference')
            self.end_headers();self.wfile.write(data)
        def authenticate(self):
            seen.append((self.command,self.path))
            assert self.headers['X-Application-ID']=='capstone'
            assert self.headers['X-Infrastructure-Token']=='fixture-token'
        def do_GET(self):
            self.authenticate()
            if self.path=='/v1/models':
                return self.reply(200,{'contract':'shared.inference/1','resources':[{'resource_id':'head-1','models':['model:1']}]})
            if self.path.startswith('/v1/requests/'):
                return self.reply(200,{'contract':'shared.inference/1','status':'completed',
                    'request':{'request_id':'REQ-1','status':'completed','resource_id':'head-1'},
                    'result':{'response':{'response':'READY'},'resource_id':'head-1'}})
            self.reply(404,{'error':'not found'})
        def do_POST(self):
            self.authenticate();length=int(self.headers.get('Content-Length','0'))
            body=json.loads(self.rfile.read(length))
            assert body['contract']=='shared.inference/1'
            assert body['client_key']=='CALL-1' and body['nodes']==['@authorized']
            assert body['operation']=='/api/generate' and body['payload']=={'prompt':'safe'}
            self.reply(202,{'contract':'shared.inference/1','status':'queued',
                'request':{'request_id':'REQ-1','status':'queued','resource_id':None}})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        configured=Client(f'http://127.0.0.1:{server.server_port}','capstone','fixture-token')
        assert configured.models()['resources'][0]['resource_id']=='head-1'
        result=configured.infer('model:1',None,'/api/generate',{'prompt':'safe'},client_key='CALL-1',timeout=3)
        assert result['status']=='completed' and result['result']['resource_id']=='head-1'
        assert len(seen)>=3
    finally:
        server.shutdown();server.server_close();thread.join(3)


def test_capstone_runtime_has_no_gateway_server_imports():
    root=Path(__file__).resolve().parents[1]/'src'/'capstone_rag'
    excluded={
        'capacity_admin.py','capacity_bootstrap.py','capacity_broker.py',
        'capacity_gateway.py','capacity_gateway_server.py','capacity_lifecycle.py',
    }
    assert not [name for name in excluded if (root/name).exists()]
    forbidden={name.removesuffix('.py') for name in excluded}
    offenders=[]
    for path in root.rglob('*.py'):
        if path.name in excluded:
            continue
        tree=ast.parse(path.read_text(encoding='utf-8'),filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom) and node.module:
                module=node.module.rsplit('.',1)[-1]
                if module in forbidden:offenders.append(f'{path.name}:{node.lineno}:{module}')
    assert offenders==[]


def test_client_template_is_non_operational_and_contract_pinned():
    template=Path(__file__).resolve().parents[1]/'config'/'inference-gateway.client.template.json'
    value=json.loads(template.read_text(encoding='utf-8'))
    assert value['contract']=='shared.inference/1'
    assert value['configuration_version']==1
    assert value['application_id']=='capstone'
    assert value['url']=='https://192.0.2.254:8443'
    assert value['service_id']=='shared-inference'
    assert value['token_file']==r'C:\ProgramData\Capstone\gateway\capstone.token'
    assert value['ca_file']==r'C:\ProgramData\Capstone\gateway\inference-gateway-ca.pem'
    assert 'token' not in value and 'token_environment' not in value
    research=json.loads((template.parent/'inference-gateway.research-client.template.json').read_text(encoding='utf-8'))
    assert research['application_id']=='capstone-research'
    assert research['token_file']!=value['token_file']


def test_production_adapter_requires_config_and_separates_identities(tmp_path,monkeypatch):
    monkeypatch.delenv('CAPSTONE_GATEWAY_CLIENT_CONFIG',raising=False)
    monkeypatch.delenv('CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG',raising=False)
    monkeypatch.setenv('CAPSTONE_INFRASTRUCTURE_TOKEN','obsolete-must-not-be-used')
    with pytest.raises(PermissionError,match='from_config'):
        gateway_transport.client()
    with pytest.raises(PermissionError,match='from_config'):
        gateway_transport.client('research')


def test_policy_availability_capability_pin_and_unknown_state(monkeypatch):
    calls=[]
    class FakeClient:
        def availability(self,*args,**kwargs):return {'status':'available'}
        def infer(self,*args,**kwargs):
            calls.append((args,kwargs))
            return {'status':'unknown','request':{'request_id':'REQ-uncertain'},'client_key':'TRIAL-7'}
    monkeypatch.setattr(gateway_transport,'client',lambda workload='operational':FakeClient())
    with pytest.raises(gateway_transport.GatewayUnknownExecution) as caught:
        gateway_transport.invoke(None,{},'model:1','/api/generate',{'prompt':'x'},30,
            request_id='TRIAL-7',expected_digest='sha256:abc',required_capabilities=['structured_output'],workload='research')
    assert len(calls)==1
    assert calls[0][1]['client_key']=='TRIAL-7'
    assert calls[0][1]['expected_digest']=='sha256:abc'
    assert calls[0][1]['required_capabilities']==['structured_output']
    assert caught.value.envelope['request']['request_id']=='REQ-uncertain'
