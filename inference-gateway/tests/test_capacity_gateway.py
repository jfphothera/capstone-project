"""Actual HTTP protocol tests with explicitly simulated model execution."""
import concurrent.futures
import json
import threading
import time
import urllib.error
import pytest
from shared_inference_client import Client
from inference_gateway.capacity_gateway import Gateway
from inference_gateway.capacity_gateway_server import Server


class Transport:
    def __init__(self):
        self.lock=threading.Lock();self.entered=threading.Event();self.release=threading.Event()
        self.hold=False;self.active=0;self.peak=0;self.calls=0
        self.content='TEST_RESULT'
    def __call__(self,endpoint,operation,payload,timeout):
        if operation=='/api/version':return {'version':'simulated-test-version'}
        if operation=='/api/tags':return {'models':[{'name':'test:1','digest':'TEST-DIGEST'}]}
        if operation=='/api/ps':return {'models':[]} # 0.4.0 placement poll: nothing loaded
        if operation=='/api/show':return {'capabilities':['completion','embedding']}
        with self.lock:self.active+=1;self.peak=max(self.peak,self.active);self.calls+=1
        try:
            self.entered.set()
            if self.hold:assert self.release.wait(10)
            if operation=='/api/embed':return {'model':'test:1','embeddings':[[.1,.2]]}
            return {'done':True,'model':'test:1','response':self.content,'eval_count':2}
        finally:
            with self.lock:self.active-=1


@pytest.fixture
def service(tmp_path):
    transport=Transport();gateway=Gateway(tmp_path/'broker.db',transport=transport)
    for app in ('alpha','beta','third'):
        gateway.broker.register_client(app,'test-'+app,models=['test:1'],max_concurrency=1)
    gateway.broker.authorize_resource('stable-head',endpoint='http://127.0.0.1:12345',resource_type='shared_inference',
        owner='test infrastructure',applications=['alpha','beta','third'],permitted_uses=['inference'])
    transport.content='READY'
    assert gateway.qualify('stable-head','test:1',operator='test administrator')['passed']
    assert gateway.qualify('stable-head','test:1',operator='test administrator',purpose='embedding')['passed']
    transport.content='TEST_RESULT';transport.calls=0;transport.peak=0;transport.entered.clear()
    server=Server(('127.0.0.1',0),gateway)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='http://127.0.0.1:'+str(server.server_port)
    yield gateway,transport,lambda app:Client(url,app,'test-'+app)
    transport.release.set();server.shutdown();server.server_close();thread.join(5)


def invoke(client,key='one',**kwargs):
    return client.infer('test:1',['stable-head'],'/api/generate',{'prompt':'Synthetic fixture','options':{'num_predict':8}},
                        client_key=key,timeout=10,**kwargs)


def test_overlapping_clients_cannot_double_execute_or_release_another_lease(service):
    gateway,transport,client=service;transport.hold=True;queued=threading.Event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(invoke,client('alpha'));assert transport.entered.wait(5)
        second=pool.submit(invoke,client('beta'),'two',progress=lambda value:queued.set() if value['request']['status']=='queued' else None)
        assert queued.wait(5)
        with gateway.broker.connect() as db:active=dict(db.execute("SELECT * FROM capacity_requests WHERE app_id='alpha'").fetchone())
        with pytest.raises(PermissionError):gateway.broker.finish('alpha','test-alpha',active['request_id'])
        assert transport.active==1
        transport.release.set()
        a=first.result(10);b=second.result(10)
    assert a['status']==b['status']=='completed' and transport.peak==1
    assert a['result']['application_id']=='alpha' and b['result']['application_id']=='beta'
    assert invoke(client('third'),'third')['status']=='completed'


def test_private_results_idempotency_and_input_policy(service):
    gateway,transport,client=service
    a=invoke(client('alpha'));assert invoke(client('alpha'))['request']['request_id']==a['request']['request_id']
    assert transport.calls==1
    for action in (client('beta').status,client('beta').cancel):
        with pytest.raises(urllib.error.HTTPError) as error:action(a['request']['request_id'])
        assert error.value.code==403
    with pytest.raises(urllib.error.HTTPError):
        client('alpha').infer('test:1',['stable-head'],'/api/generate',{'prompt':'DIFFERENT'},client_key='one',timeout=10)
    for payload in ({'prompt':'x','keep_alive':0},{'prompt':'x','options':{'num_predict':-1}}):
        with pytest.raises(urllib.error.HTTPError):client('alpha').infer('test:1',['stable-head'],'/api/generate',payload,timeout=10)
    assert transport.calls==1


def test_cancellation_waits_for_real_execution_then_discards_result(service):
    gateway,transport,client=service;transport.hold=True
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(invoke,client('alpha'));assert transport.entered.wait(5)
        with gateway.broker.connect() as db:rid=db.execute("SELECT request_id FROM capacity_requests WHERE app_id='alpha'").fetchone()[0]
        cancelled=client('alpha').cancel(rid);assert cancelled['request']['status']=='cancel_requested'
        assert transport.active==1
        transport.release.set();result=future.result(10)
    assert result['status']=='cancelled' and 'result' not in result
    assert invoke(client('beta'),'after-cancel')['status']=='completed'


def test_queued_cancel_keeps_terminal_state_after_worker_reconciliation(service):
    gateway,transport,client=service;transport.hold=True;queued=threading.Event();settled=threading.Event();request_id=[]
    original=gateway._run
    def observe(app,*args,**kwargs):
        try:return original(app,*args,**kwargs)
        finally:
            if app=='beta':settled.set()
    gateway._run=observe
    def progress(value):
        if value['request']['status']=='queued':request_id.append(value['request']['request_id']);queued.set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(invoke,client('alpha'));assert transport.entered.wait(5)
        second=pool.submit(invoke,client('beta'),'queued-cancel',progress=progress);assert queued.wait(5)
        client('beta').cancel(request_id[0]);assert second.result(5)['status']=='cancelled'
        assert settled.wait(5)
        deadline=time.time()+5
        while time.time()<deadline:
            with gateway.broker.connect() as db:state=db.execute('SELECT state FROM capacity_invocations WHERE request_id=?',(request_id[0],)).fetchone()[0]
            if state=='cancelled':break
            time.sleep(.02)
        assert state=='cancelled' and client('beta').status(request_id[0])['status']=='cancelled'
        transport.release.set();assert first.result(10)['status']=='completed'


def test_client_rejects_wrong_service_and_remote_plaintext(service):
    gateway,transport,client=service
    with pytest.raises(ValueError):Client('http://192.168.50.10:5000','alpha','test-alpha')
    original=client('alpha')
    wrong=Client(original.url,'alpha','test-alpha',service_id='unapproved-service')
    with pytest.raises(PermissionError):wrong.models()




def test_qualification_cannot_preempt_and_records_evidence(service):
    gateway,transport,client=service;transport.hold=True
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(invoke,client('alpha'));assert transport.entered.wait(5)
        with pytest.raises(ValueError,match='interrupt'):gateway.qualify('stable-head','test:1',operator='test-operator')
        transport.release.set();future.result(10)
    transport.content='READY'
    evidence=gateway.qualify('stable-head','test:1',operator='test-operator')
    assert evidence['passed'] and evidence['actual_execution'] is False
    with gateway.broker.connect() as db:
        assert db.execute('SELECT count(*) FROM capacity_qualification_evidence').fetchone()[0]==3
        assert db.execute("SELECT count(*) FROM capacity_requests WHERE status='leased'").fetchone()[0]==0


def test_gateway_relocation_keeps_logical_service_requests_and_client_identity(service,tmp_path,monkeypatch):
    gateway,transport,client=service
    first=invoke(client('alpha'))
    moved=Server(('127.0.0.1',0),gateway);thread=threading.Thread(target=moved.serve_forever,daemon=True);thread.start()
    try:
        bootstrap=tmp_path/'approved-service.json'
        monkeypatch.setenv('TEST_GATEWAY_TOKEN','test-alpha')
        bootstrap.write_text(json.dumps({'configuration_version':1,'contract':'shared.inference/1','service_id':'shared-inference',
            'url':'http://127.0.0.1:'+str(moved.server_port),'application_id':'alpha','token_environment':'TEST_GATEWAY_TOKEN'}))
        relocated=Client.from_config(bootstrap)
        assert relocated.status(first['request']['request_id'])['result']==first['result']
        assert invoke(relocated,'after-relocation')['status']=='completed'
        assert transport.calls==2
    finally:moved.shutdown();moved.server_close();thread.join(5)


def test_clients_cannot_self_raise_priority_or_spoof_application(service):
    gateway,transport,client=service
    for extra in ({'priority':1000},{'application_id':'beta'},{'max_concurrency':1000}):
        body={'contract':'shared.inference/1','client_key':'policy','model':'test:1','nodes':['stable-head'],
            'deadline':time.time()+10,'operation':'/api/generate','payload':{'prompt':'Synthetic fixture'},**extra}
        with pytest.raises(urllib.error.HTTPError) as error:client('alpha').request('/v1/submit',body)
        assert error.value.code==400
    assert transport.calls==0


def test_model_version_change_and_context_escalation_fail_before_execution(service):
    gateway,transport,client=service
    original=gateway.transport
    gateway.transport=lambda endpoint,operation,payload,timeout: {'version':'changed'} if operation=='/api/version' else original(endpoint,operation,payload,timeout)
    assert invoke(client('alpha'),'changed-version')['status']=='failed'
    assert transport.calls==0
    gateway.transport=original;gateway.refresh_health()
    result=client('alpha').infer('test:1',['stable-head'],'/api/generate',{'prompt':'Fixture','options':{'num_ctx':2048}},timeout=10)
    assert result['status']=='failed' and 'allocation' in result['error']
    assert transport.calls==0


def test_dynamic_health_disconnect_recovery_and_model_removal(service):
    gateway,transport,client=service;original=gateway.transport
    def offline(*args):raise ConnectionError('Simulated disconnect')
    gateway.transport=offline;gateway.refresh_health()
    assert client('alpha').models()['resources']==[]
    gateway.transport=original;gateway.refresh_health()
    assert client('alpha').models()['resources'][0]['models']==['test:1']
    gateway.transport=lambda endpoint,operation,payload,timeout: {'models':[]} if operation=='/api/tags' else original(endpoint,operation,payload,timeout)
    gateway.refresh_health();assert client('alpha').models()['resources']==[]
    gateway.transport=original;gateway.refresh_health()
    assert client('alpha').models()['resources']==[] # removal requires requalification
    assert transport.calls==0


def test_restart_does_not_replay_expired_execution_or_reuse_uncertain_capacity(service):
    gateway,transport,client=service
    request=gateway.broker.submit('alpha','test-alpha','interrupted','test:1',['stable-head'],time.time()+20)
    request=gateway.broker.admit('alpha','test-alpha',request['request_id'])
    gateway.broker.executing('alpha','test-alpha',request['request_id'])
    with gateway.broker.connect() as db:
        db.execute('UPDATE capacity_requests SET expires=? WHERE request_id=?',(time.time()-1,request['request_id']))
    restarted=Gateway(gateway.broker.path,transport=transport)
    assert restarted.broker.get('alpha','test-alpha',request['request_id'])['status']=='expired'
    restarted.refresh_health()
    with restarted.broker.connect() as db:assert db.execute("SELECT health FROM capacity_resources WHERE resource_id='stable-head'").fetchone()[0]=='quarantined'
    assert transport.calls==0




def test_embedding_qualification_does_not_authorize_generation(service):
    gateway,transport,client=service
    gateway.broker.authorize_resource('embedding-only',endpoint='http://127.0.0.1:12346',resource_type='shared_inference',
        owner='test infrastructure',applications=['alpha'],permitted_uses=['inference'])
    assert gateway.qualify('embedding-only','test:1',operator='test operator',purpose='embedding')['passed']
    before=transport.calls
    result=client('alpha').infer('test:1',['embedding-only'],'/api/generate',{'prompt':'Synthetic fixture'},timeout=10)
    assert result['status']=='failed' and 'operation-specific' in result['error']
    assert transport.calls==before and 'execution_started_at' not in result




def test_dynamic_model_permission_revocation_blocks_queued_and_pre_dispatch_work(service):
    gateway,transport,client=service
    request=gateway.broker.submit('beta','test-beta','queued-revoke','test:1',['stable-head'],time.time()+20)
    gateway.broker.register_client('beta','test-beta',models=['another:1'])
    assert gateway.broker.admit('beta','test-beta',request['request_id'])['status']=='cancelled'
    original=gateway.transport
    def revoke_during_metadata(endpoint,operation,payload,timeout):
        if operation=='/api/show':gateway.broker.register_client('alpha','test-alpha',models=['another:1'])
        return original(endpoint,operation,payload,timeout)
    gateway.transport=revoke_during_metadata
    result=invoke(client('alpha'),'dispatch-revoke')
    assert result['status']=='failed' and 'authorization withdrawn' in result['error']
    assert transport.calls==0 and 'execution_started_at' not in result


def test_global_overload_does_not_leave_an_orphan_capacity_request(service):
    gateway,transport,client=service;gateway.max_pending=1;transport.hold=True
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(invoke,client('alpha'));assert transport.entered.wait(5)
        with pytest.raises(urllib.error.HTTPError):invoke(client('beta'),'overload')
        with gateway.broker.connect() as db:
            assert db.execute("SELECT status FROM capacity_requests WHERE app_id='beta'").fetchone()[0]=='cancelled'
        transport.release.set();assert first.result(10)['status']=='completed'
    assert invoke(client('third'),'after-overload')['status']=='completed'


def test_incomplete_upstream_response_cannot_publish_success(service):
    gateway,transport,client=service;original=gateway.transport
    def incomplete(endpoint,operation,payload,timeout):
        if operation=='/api/generate':return {'model':'test:1','done':False,'response':'partial'}
        return original(endpoint,operation,payload,timeout)
    gateway.transport=incomplete
    result=invoke(client('alpha'),'partial-response')
    assert result['status']=='failed' and 'result' not in result
    assert 'Incomplete generation' in result['error']
    assert client('alpha').request('/v1/usage')['completed_output_tokens']==0


def test_missing_digest_cannot_establish_a_qualification(service):
    gateway,transport,client=service;original=gateway.transport;before=transport.calls
    gateway.transport=lambda endpoint,operation,payload,timeout: {'models':[{'name':'test:1'}]} if operation=='/api/tags' else original(endpoint,operation,payload,timeout)
    evidence=gateway.qualify('stable-head','test:1',operator='test operator')
    assert not evidence['passed'] and 'digest' in evidence['error'] and transport.calls==before


def test_dynamic_request_uses_newly_authorized_qualified_capacity_without_client_reconfiguration(service):
    gateway,transport,client=service;queued=threading.Event()
    gateway.broker.publish('stable-head',['test:1'],'offline')
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(client('alpha').infer,'test:1',None,'/api/generate',{'prompt':'Synthetic dynamic fixture'},
            client_key='dynamic-capacity',timeout=10,progress=lambda value:queued.set() if value['request']['status']=='queued' else None)
        assert queued.wait(5)
        gateway.broker.authorize_resource('enrolled-late',endpoint='http://127.0.0.1:12347',resource_type='shared_inference',
            owner='test infrastructure',applications=['alpha'],permitted_uses=['inference'])
        assert client('alpha').models()['resources']==[] and transport.calls==0
        transport.content='READY'
        assert gateway.qualify('enrolled-late','test:1',operator='test operator')['passed']
        result=future.result(10)
    assert result['status']=='completed' and result['result']['resource_id']=='enrolled-late'
    assert result['request']['node_policy']=='authorized_dynamic'
    repeated=client('alpha').infer('test:1',None,'/api/generate',{'prompt':'Synthetic dynamic fixture'},client_key='dynamic-capacity',timeout=10)
    assert repeated['request']['request_id']==result['request']['request_id'] and transport.calls==2
    assert client('beta').models()['resources']==[]
