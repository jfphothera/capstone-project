"""Shared-resource acceptance in software: Capstone, Coder and a future application on one gateway."""
import concurrent.futures
import threading
import time
import urllib.error
import pytest
from shared_inference_client import Client
from inference_gateway.capacity_broker import CapacityBroker
from inference_gateway.capacity_gateway import Gateway
from inference_gateway.capacity_gateway_server import Server

HEADS=('head-192-0-2-3','head-192-0-2-6','head-192-0-2-7')


class Heads:
    def __init__(self):self.release=threading.Event();self.hold=False;self.entered=threading.Event();self.lock=threading.Lock();self.active={};self.overlap=False
    def __call__(self,endpoint,operation,payload,timeout):
        if operation=='/api/version':return {'version':'0.12.3'}
        if operation=='/api/tags':return {'models':[{'name':m,'digest':m.upper()} for m in ('shared:1','capstone-only:1','coder-only:1','embed:1')]}
        if operation=='/api/ps':return {'models':[]} # 0.4.0 placement poll: nothing loaded
        if operation=='/api/show':return {'capabilities':['embedding'] if payload['model']=='embed:1' else ['completion']}
        with self.lock:
            self.overlap|=self.active.get(endpoint,0)>0;self.active[endpoint]=self.active.get(endpoint,0)+1
        try:
            self.entered.set()
            if self.hold and (payload.get('prompt')=='hold' or payload.get('client_marker')):assert self.release.wait(10)
            if operation=='/api/embed':return {'model':payload['model'],'embeddings':[[.1,.2]]}
            if operation=='/api/chat':return {'done':True,'model':payload['model'],'message':{'role':'assistant','content':'READY'}}
            return {'done':True,'model':payload['model'],'response':'READY','eval_count':1}
        finally:
            with self.lock:self.active[endpoint]-=1


@pytest.fixture
def service(tmp_path):
    heads=Heads();gateway=Gateway(tmp_path/'ledger.db',transport=heads);b=gateway.broker
    b.register_client('capstone','t-capstone',models=['shared:1','capstone-only:1','embed:1'],max_concurrency=2,queue_limit=4,priority=6)
    b.register_client('coder','t-coder',models=['shared:1','coder-only:1'],max_concurrency=1,queue_limit=2,priority=6)
    b.register_client('future_app','t-future',models=['embed:1'],max_concurrency=1,queue_limit=1,priority=3)
    b.set_client_limits('coder',operations=['/api/generate','/api/chat'],max_context=1024)
    b.set_client_limits('future_app',operations=['/api/embed'])
    for index,rid in enumerate(HEADS):
        b.authorize_resource(rid,endpoint='http://127.0.0.1:%d'%(12001+index),resource_type='shared_inference',owner='infrastructure',
            applications=['capstone','coder','future_app'],permitted_uses=['inference'])
        for model in ('shared:1','capstone-only:1','coder-only:1'):assert gateway.qualify(rid,model,operator='ops',max_context=2048)['passed']
        assert gateway.qualify(rid,'embed:1',operator='ops',purpose='embedding')['passed']
    server=Server(('127.0.0.1',0),gateway);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='http://127.0.0.1:'+str(server.server_port)
    yield gateway,heads,{app:Client(url,app,'t-'+app.split('_')[0]) for app in ('capstone','coder','future_app')}
    heads.release.set();server.shutdown();server.server_close();thread.join(5)


def code(call):
    with pytest.raises(urllib.error.HTTPError) as error:call()
    return error.value.code


def test_applications_use_one_gateway_with_isolated_policy_and_records(service):
    gateway,heads,clients=service
    capstone,coder,future=clients['capstone'],clients['coder'],clients['future_app']
    mine=capstone.infer('shared:1',None,'/api/generate',{'prompt':'x'},client_key='c1',timeout=10)
    theirs=coder.infer('shared:1',None,'/api/chat',{'messages':[{'role':'user','content':'x'}]},client_key='k1',timeout=10)
    embedded=future.infer('embed:1',None,'/api/embed',{'input':['x']},client_key='f1',timeout=10)
    assert mine['status']==theirs['status']==embedded['status']=='completed'
    assert {mine['result']['application_id'],theirs['result']['application_id'],embedded['result']['application_id']}=={'capstone','coder','future_app'}
    # Each policy view is the caller's own.
    assert capstone.policy()['policy']['models']==['shared:1','capstone-only:1','embed:1']
    assert coder.policy()['policy']['operations']==['/api/chat','/api/generate'] and coder.policy()['policy']['max_context']==1024
    assert future.policy()['policy']['models']==['embed:1']
    # Another application's records cannot be read or cancelled; same request ID space, separate namespaces.
    for client,rid in ((coder,mine['request']['request_id']),(future,mine['request']['request_id']),(capstone,theirs['request']['request_id'])):
        assert code(lambda:client.status(rid))==403 and code(lambda:client.cancel(rid))==403
    assert capstone.request('/v1/usage')['requests']=={'completed':1} and coder.request('/v1/usage')['requests']=={'completed':1}
    # Policy refusals happen before any request or queue slot exists.
    assert code(lambda:coder.infer('capstone-only:1',None,'/api/generate',{'prompt':'x'},timeout=5))==403
    assert code(lambda:coder.infer('shared:1',None,'/api/embed',{'input':['x']},timeout=5))==403
    assert code(lambda:coder.infer('shared:1',None,'/api/generate',{'prompt':'x','options':{'num_ctx':2048}},timeout=5))==403
    assert code(lambda:future.infer('embed:1',None,'/api/generate',{'prompt':'x'},timeout=5))==403
    assert coder.availability('shared:1','/api/embed')['status']=='operation_not_permitted'
    assert coder.availability('shared:1',num_ctx=2048)['status']=='context_exceeds_policy'
    assert capstone.availability('coder-only:1')['status']=='not_permitted'
    with gateway.broker.connect() as db:assert db.execute("SELECT count(*) FROM capacity_requests WHERE app_id='coder'").fetchone()[0]==1


def test_busy_heads_are_never_double_allocated_under_three_concurrent_clients(tmp_path):
    broker=CapacityBroker(tmp_path/'capacity.db')
    for app in ('capstone','coder','future_app'):broker.register_client(app,'t-'+app,models=['m:1'],max_concurrency=3,queue_limit=50)
    for rid in HEADS[:2]:
        broker.authorize_resource(rid,endpoint='http://192.0.2.%s:11434'%rid[-1],resource_type='shared_inference',owner='infrastructure',
            applications=['capstone','coder','future_app'],permitted_uses=['inference'])
        broker.publish(rid,['m:1'],'healthy')
    intervals=[];lock=threading.Lock()
    def work(app,index):
        request=broker.submit(app,'t-'+app,'%s-%d'%(app,index),'m:1',list(HEADS[:2]),time.time()+60)
        while (request:=broker.admit(app,'t-'+app,request['request_id']))['status']=='queued':time.sleep(.002)
        began=time.monotonic();time.sleep(.01);ended=time.monotonic()
        with lock:intervals.append((request['resource_id'],began,ended))
        broker.finish(app,'t-'+app,request['request_id'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda job:work(*job),[(app,i) for app in ('capstone','coder','future_app') for i in range(8)]))
    assert len(intervals)==24
    for rid in HEADS[:2]:
        spans=sorted((b,e) for r,b,e in intervals if r==rid)
        assert all(later[0]>=earlier[1] for earlier,later in zip(spans,spans[1:])),rid


def test_research_identity_is_capped_and_cannot_starve_operational_work(tmp_path):
    """Operator policy, not the client: research gets its own identity, a low priority and a head cap."""
    broker=CapacityBroker(tmp_path/'capacity.db')
    broker.register_client('capstone','t-ops',models=['m:1'],max_concurrency=3,queue_limit=10,priority=8)
    broker.register_client('capstone-research','t-research',models=['m:1'],max_concurrency=1,queue_limit=50,priority=1)
    for rid in HEADS:
        broker.authorize_resource(rid,endpoint='http://192.0.2.%s:11434'%rid[-1],resource_type='shared_inference',owner='infrastructure',
            applications=['capstone','capstone-research'],permitted_uses=['inference'])
        broker.publish(rid,['m:1'],'healthy')
    research=[broker.submit('capstone-research','t-research','r%d'%i,'m:1',list(HEADS),time.time()+600) for i in range(10)]
    # Research has waited a long time: aging raises it above operational priority...
    with broker.connect() as db:db.execute("UPDATE capacity_requests SET created=? WHERE app_id='capstone-research'",(time.time()-120,))
    ops=[broker.submit('capstone','t-ops','o%d'%i,'m:1',list(HEADS),time.time()+600) for i in range(3)]
    for request in research+ops:broker.admit(request['app_id'],'t-research' if request['app_id']!='capstone' else 't-ops',request['request_id'])
    with broker.connect() as db:
        leased={r['app_id']:r['n'] for r in db.execute("SELECT app_id,count(*) n FROM capacity_requests WHERE status='leased' GROUP BY app_id")}
    # ...but its concurrency cap keeps at least two of three heads for operational work.
    assert leased=={'capstone-research':1,'capstone':2}
    with pytest.raises(ValueError):broker.register_client('capstone-research','t-research',models=['m:1'],priority=11)


def test_a_failing_client_cannot_create_uncontrolled_consumption(service):
    gateway,heads,clients=service;coder=clients['coder'];heads.hold=True;heads.entered.clear()
    body=lambda key:{'contract':'shared.inference/1','client_key':key,'model':'shared:1','nodes':['@authorized'],'deadline':time.time()+2,
                     'operation':'/api/generate','payload':{'prompt':'hold'}}
    # Coder may hold one head (max_concurrency 1) and queue two more; everything after that is refused.
    accepted=[coder.request('/v1/submit',body('flood-0'))];assert heads.entered.wait(5)
    accepted+=[coder.request('/v1/submit',body('flood-%d'%i)) for i in (1,2)]
    assert len(accepted)==3
    assert code(lambda:coder.request('/v1/submit',body('flood-overflow')))==400
    with gateway.broker.connect() as db:
        assert db.execute("SELECT count(*) FROM capacity_requests WHERE app_id='coder' AND status='leased'").fetchone()[0]==1
    # Other applications still get the remaining heads while Coder floods.
    assert clients['capstone'].infer('shared:1',None,'/api/generate',{'prompt':'x'},client_key='during-flood',timeout=10)['status']=='completed'
    heads.release.set()
    deadline=time.time()+10
    while time.time()<deadline:
        with gateway.broker.connect() as db:
            if not db.execute("SELECT count(*) FROM capacity_requests WHERE app_id='coder' AND status IN ('queued','leased')").fetchone()[0]:break
        time.sleep(.1)
    assert not heads.overlap


def test_revocation_releases_capacity_without_degrading_heads(service):
    gateway,heads,clients=service;heads.hold=True;heads.entered.clear()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        running=pool.submit(clients['coder'].infer,'shared:1',['head-192-0-2-3'],'/api/generate',{'prompt':'hold'},client_key='executing',timeout=10)
        assert heads.entered.wait(5)
        queued=gateway.broker.submit('coder','t-coder','queued','shared:1',['head-192-0-2-3'],time.time()+30)
        gateway.broker.revoke('coder')
        heads.release.set()
        with pytest.raises(urllib.error.HTTPError):running.result(10)
    with gateway.broker.connect() as db:
        states={r['client_key']:r['status'] for r in db.execute("SELECT client_key,status FROM capacity_requests WHERE app_id='coder'")}
        healths={r['resource_id']:r['health'] for r in db.execute('SELECT resource_id,health FROM capacity_resources')}
    # Dispatched work returned, so its late result is discarded and the lease released; queued work is cancelled.
    assert states['queued']=='cancelled' and states['executing'] in {'cancelled','completed'}
    assert set(healths.values())=={'healthy'}
    assert code(lambda:clients['coder'].models())==403
    assert clients['capstone'].infer('shared:1',['head-192-0-2-3'],'/api/generate',{'prompt':'x'},client_key='after-revoke',timeout=10)['status']=='completed'


def test_revoked_before_dispatch_releases_the_lease_immediately(service):
    gateway,heads,clients=service;original=gateway.transport
    def revoke_during_checks(endpoint,operation,payload,timeout):
        if operation=='/api/show':gateway.broker.revoke('coder')
        return original(endpoint,operation,payload,timeout)
    gateway.transport=revoke_during_checks
    with pytest.raises(urllib.error.HTTPError):clients['coder'].infer('shared:1',['head-192-0-2-3'],'/api/generate',{'prompt':'x'},client_key='pre-dispatch',timeout=10)
    deadline=time.time()+5
    while time.time()<deadline:
        with gateway.broker.connect() as db:
            row=db.execute("SELECT status FROM capacity_requests WHERE client_key='pre-dispatch'").fetchone()
            health=db.execute("SELECT health FROM capacity_resources WHERE resource_id='head-192-0-2-3'").fetchone()[0]
        if row['status']=='cancelled':break
        time.sleep(.05)
    assert row['status']=='cancelled' and health=='healthy'
    gateway.transport=original
    assert clients['capstone'].infer('shared:1',['head-192-0-2-3'],'/api/generate',{'prompt':'x'},client_key='next',timeout=10)['status']=='completed'
