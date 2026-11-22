import hashlib
import json
import sqlite3
import time
import pytest
from inference_gateway.capacity_gateway import Gateway
from inference_gateway.capacity_observe import snapshot


def test_observation_is_read_only_and_omits_private_fields(tmp_path):
    path=tmp_path/'ledger.db'
    gateway=Gateway(path)
    broker=gateway.broker
    broker.register_client('app','PRIVATE-TOKEN',models=['fixture:1'])
    broker.authorize_resource('head',endpoint='http://127.0.0.1:12345',resource_type='shared_inference',owner='private-owner',applications=['app'],permitted_uses=['inference'])
    request=broker.submit('app','PRIVATE-TOKEN','PRIVATE-CLIENT-KEY','fixture:1',['head'],time.time()+60)
    with broker.connect() as db:
        db.execute("INSERT INTO capacity_invocations(request_id,app_id,payload_hash,execution_owner,state,result,error,updated) VALUES(?,?,?,?,?,?,?,?)",
            (request['request_id'],'app','PRIVATE-HASH','PRIVATE-OWNER','failed','PRIVATE-RESPONSE','PRIVATE-ERROR',time.time()))
        broker._event(db,'app',request['request_id'],'queued',{'prompt':'PRIVATE-PROMPT'})
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    result=snapshot(path,limit=1)
    encoded=json.dumps(result)
    assert 'PRIVATE-' not in encoded and 'private-owner' not in encoded
    assert not {'token_hash','result','error','payload_hash','client_key','endpoint'} & set(encoded.split('"'))
    assert result['requests']['queued']==1 and result['invocations']['failed']==1
    assert result['applications'][0]['app_id']=='app'
    assert len(snapshot(path,after_event_id=result['next_event_id'])['events'])==2
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    with pytest.raises(ValueError):snapshot(path,limit=1001)


def test_expiry_revocation_failure_and_restart_are_evented(tmp_path):
    def failing(endpoint,operation,payload,timeout):raise ConnectionError('PRIVATE-UPSTREAM-ERROR')
    gateway=Gateway(tmp_path/'ledger.db',transport=failing);broker=gateway.broker
    for app in ('app','other'):broker.register_client(app,'token-'+app,models=['fixture:1'])
    broker.authorize_resource('head-192-0-2-3',endpoint='http://127.0.0.1:12345',resource_type='shared_inference',owner='infrastructure',applications=['app','other'],permitted_uses=['inference'])
    broker.publish('head-192-0-2-3',['fixture:1'],'healthy')
    executing=broker.submit('app','token-app','executing','fixture:1',['head-192-0-2-3'],time.time()+60)
    broker.admit('app','token-app',executing['request_id']);broker.executing('app','token-app',executing['request_id'])
    waiting=broker.submit('app','token-app','waiting','fixture:1',['head-192-0-2-3'],time.time()+60)
    with broker.connect() as db:
        db.execute('UPDATE capacity_requests SET expires=? WHERE request_id=?',(time.time()-1,executing['request_id']))
        db.execute('UPDATE capacity_requests SET deadline=? WHERE request_id=?',(time.time()-1,waiting['request_id']))
    broker.reconcile_expired()
    revoked=broker.submit('other','token-other','revoked','fixture:1',['head-192-0-2-3'],time.time()+60)
    broker.revoke('other');broker.revoke('never-registered')
    broker.reconcile_resource('head-192-0-2-3',confirmed_idle=True);broker.publish('head-192-0-2-3',['fixture:1'],'healthy')
    failed=gateway.execute('app','token-app',{'contract':'shared.inference/1','client_key':'failing','model':'fixture:1','nodes':['head-192-0-2-3'],
        'deadline':time.time()+10,'operation':'/api/generate','payload':{'prompt':'Synthetic fixture'}})
    assert failed['status']=='failed'
    interrupted=broker.submit('app','token-app','interrupted','fixture:1',['head-192-0-2-3'],time.time()+60)
    with broker.connect() as db:
        db.execute("INSERT INTO capacity_invocations(request_id,app_id,payload_hash,execution_owner,state,updated) VALUES(?,?,?,?,'active',?)",(interrupted['request_id'],'app','h','lost',time.time()))
    gateway.recover_service_startup()
    result=snapshot(tmp_path/'ledger.db',limit=1000)
    events=[(e['event_type'],e['app_id'],e['request_id']) for e in result['events']]
    assert ('lease.expired','app',executing['request_id']) in events
    assert ('resource.quarantined','infrastructure',executing['request_id']) in events
    assert ('request.expired','app',waiting['request_id']) in events
    assert ('cancelled','other',revoked['request_id']) in events and ('authorization.revoked','other',None) in events
    assert not any(e[1]=='never-registered' for e in events)
    assert ('invocation.failed','app',failed['request']['request_id']) in events
    assert ('invocation.unknown','app',interrupted['request_id']) in events
    assert result['last_hour']['events']['lease.expired']==1 and result['last_hour']['invocations_updated']['failed']==1
    [failure]=result['last_hour']['invocation_failures']
    assert (failure['resource_id'],failure['model'],failure['app_id'],failure['count'])==('head-192-0-2-3','fixture:1','app',1)
    assert 'PRIVATE-' not in json.dumps(result)


def test_last_hour_failures_name_head_model_and_class_without_the_error(tmp_path, monkeypatch):
    # Shaped like the hub evidence of 2026-11-18: reviewer qwen3:4b timing out on two healthy heads.
    from inference_gateway import capacity_observe
    gateway=Gateway(tmp_path/'ledger.db');broker=gateway.broker
    broker.register_client('capstone-research','token',models=['qwen3:4b','other:1'])
    heads=['head-192-0-2-3','head-192-0-2-5']
    for n,head in enumerate(heads):
        broker.authorize_resource(head,endpoint=f'http://127.0.0.1:1234{n}',resource_type='shared_inference',owner='infrastructure',applications=['capstone-research'],permitted_uses=['inference'])
    now=time.time()
    def invocation(key,model,head,state,failure_class,updated,attempt=1):
        rid=broker.submit('capstone-research','token',key,model,heads,now+60)['request_id']
        with broker.connect() as db:
            db.execute('UPDATE capacity_requests SET resource_id=? WHERE request_id=?',(head,rid))
            if failure_class!='no-attempt':broker.record_attempt(db,rid,attempt,head,failure_class)
            db.execute("INSERT INTO capacity_invocations(request_id,app_id,payload_hash,execution_owner,state,error,updated) VALUES(?,?,'h','o',?,'PRIVATE-ERROR timed out',?)",
                       (rid,'capstone-research',state,updated))
    for i in range(6):invocation(f'five-{i}','qwen3:4b','head-192-0-2-5','failed','deadline_exceeded',now-10-i)
    for i in range(5):invocation(f'three-{i}','qwen3:4b','head-192-0-2-3','failed','deadline_exceeded',now-100-i)
    invocation('ok','qwen3:4b','head-192-0-2-3','completed',None,now-5)                  # not a failure
    invocation('old','qwen3:4b','head-192-0-2-3','failed','deadline_exceeded',now-7200)  # outside the hour
    invocation('unplaced','other:1',None,'failed','no-attempt',now-50)                   # never reached a head
    result=snapshot(tmp_path/'ledger.db',limit=1)['last_hour']
    assert result['invocations_updated']['failed']==12 and result['invocation_failures_omitted']==0
    rows=[(r['resource_id'],r['model'],r['app_id'],r['class'],r['count']) for r in result['invocation_failures']]
    assert rows==[('head-192-0-2-5','qwen3:4b','capstone-research','deadline_exceeded',6),
                  ('head-192-0-2-3','qwen3:4b','capstone-research','deadline_exceeded',5),
                  (None,'other:1','capstone-research',None,1)]
    assert abs(result['invocation_failures'][0]['last_at']-(now-10))<1
    assert 'PRIVATE-' not in json.dumps(result) and 'timed out' not in json.dumps(result)
    monkeypatch.setattr(capacity_observe,'FAILURE_ROWS',1)                                # bounded: the rest is counted
    capped=snapshot(tmp_path/'ledger.db',limit=1)['last_hour']
    assert len(capped['invocation_failures'])==1 and capped['invocation_failures_omitted']==6


def test_observer_never_creates_an_authority(tmp_path):
    missing=tmp_path/'missing.db'
    with pytest.raises(sqlite3.OperationalError):snapshot(missing)
    assert not missing.exists()
