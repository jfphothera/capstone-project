"""Released 0.1.0 SDK (frozen fixture) and 0.1.0-style policy against the current server."""
import hashlib
import importlib.util
import json
from pathlib import Path
import threading
import pytest
from inference_gateway.capacity_bootstrap import apply
from inference_gateway.capacity_gateway import Gateway
from inference_gateway.capacity_gateway_server import Server

FIXTURE=Path(__file__).parent/'fixtures'/'sdk_0_1_0'/'shared_inference_client.py'
RELEASED_SHA256='43f693ccef5a3be528d3457ef3159a085013702d18b89d5e22e34c57b48fbf57'


def released_sdk():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest()==RELEASED_SHA256
    spec=importlib.util.spec_from_file_location('released_shared_inference_client',FIXTURE)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def transport(endpoint,operation,payload,timeout):
    if operation=='/api/version':return {'version':'simulated'}
    if operation=='/api/tags':return {'models':[{'name':'fixture:1','digest':'D1'}]}
    if operation=='/api/ps':return {'models':[]} # 0.4.0 placement poll: nothing loaded
    if operation=='/api/show':return {'capabilities':['completion','embedding']}
    if operation=='/api/embed':return {'model':'fixture:1','embeddings':[[.5,.5]]}
    if operation=='/api/chat':return {'done':True,'model':'fixture:1','message':{'role':'assistant','content':'READY'}}
    return {'done':True,'model':'fixture:1','response':'READY','eval_count':1}


@pytest.fixture
def server(tmp_path,monkeypatch):
    database=tmp_path/'ledger.db';gateway=Gateway(database,transport=transport)
    policy=tmp_path/'policy.json'
    # The 0.1.0 policy format: token_environment per client, explicit resources.
    policy.write_text(json.dumps({'configuration_version':1,'contract':'shared.inference/1',
        'clients':[{'app_id':'capstone','token_environment':'CAPSTONE_TOKEN','models':['fixture:1']}],
        'resources':[{'resource_id':'head-192-0-2-3','endpoint':'http://127.0.0.1:12345','resource_type':'shared_inference',
                      'owner':'infrastructure','applications':['capstone'],'permitted_uses':['inference']}]}))
    monkeypatch.setenv('CAPSTONE_TOKEN','test-capstone')
    assert apply(policy,database)=={'clients':1,'resources':1,'qualification':'required separately','implicit_revocations':False}
    for purpose in ('completion','embedding'):assert gateway.qualify('head-192-0-2-3','fixture:1',operator='test-operator',purpose=purpose)['passed']
    instance=Server(('127.0.0.1',0),gateway);thread=threading.Thread(target=instance.serve_forever,daemon=True);thread.start()
    yield 'http://127.0.0.1:'+str(instance.server_port)
    instance.shutdown();instance.server_close();thread.join(5)


def test_released_sdk_full_flow_against_current_server(server,tmp_path,monkeypatch):
    sdk=released_sdk()
    bootstrap=tmp_path/'client.json';monkeypatch.setenv('APPLICATION_INFERENCE_TOKEN','test-capstone')
    bootstrap.write_text(json.dumps({'configuration_version':1,'contract':'shared.inference/1','service_id':'shared-inference',
        'url':server,'application_id':'capstone','token_environment':'APPLICATION_INFERENCE_TOKEN'}))
    client=sdk.Client.from_config(bootstrap)
    [resource]=client.models()['resources']
    detail=resource['model_details']['fixture:1']
    # Every 0.1.0 discovery key is still present with its 0.1.0 meaning.
    assert {'capabilities','digest','qualified_operations','qualification_purpose','observed_at','execution_policy'}<=set(detail)
    assert detail['qualified_operations']==['completion','embedding'] and detail['digest']=='D1'
    for operation,payload in (('/api/generate',{'prompt':'x'}),('/api/chat',{'messages':[{'role':'user','content':'x'}]}),('/api/embed',{'input':['x']})):
        result=client.infer('fixture:1',None,operation,payload,client_key='key'+operation,expected_digest='D1',timeout=10)
        assert result['status']=='completed' and result['result']['model_digest']=='D1'
        assert {'response','resource_id','model','model_digest','queued_at','started_at','finished_at','queue_ms','duration_ms','lease_id','application_id'}<=set(result['result'])
        assert client.status(result['request']['request_id'])['status']=='completed'
    first=client.status(client.infer('fixture:1',None,'/api/generate',{'prompt':'x'},client_key='key/api/generate',expected_digest='D1',timeout=10)['request']['request_id'])
    # Identical retry returns the same durable request; a changed pin under the same key is refused.
    assert first['request']['client_key']=='key/api/generate' and first['status']=='completed'
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as changed:
        client.infer('fixture:1',None,'/api/generate',{'prompt':'x'},client_key='key/api/generate',timeout=10)
    assert changed.value.code==400
    assert client.cancel(result['request']['request_id'])['status']=='completed'
    assert client.request('/v1/usage')['requests']=={'completed':3}
