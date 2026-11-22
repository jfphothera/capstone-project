"""Durable state: one authority, drain semantics across restart, verified backup/restore, uncertain execution."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import pytest
from inference_gateway.capacity_broker import CapacityBroker
from inference_gateway.capacity_gateway import Gateway
from inference_gateway import capacity_lifecycle as lifecycle

ROOT=Path(__file__).resolve().parents[1]


def test_authority_binding_refuses_copies_and_identity_changes_until_adopted(tmp_path):
    broker=CapacityBroker(tmp_path/'ledger.db')
    first=lifecycle.claim_authority(broker,'shared-inference')
    assert lifecycle.claim_authority(broker,'shared-inference')==first # same authority restarts freely
    with pytest.raises(RuntimeError,match='adopt'):lifecycle.claim_authority(broker,'renamed-service')
    copy=tmp_path/'copied.db';shutil.copyfile(broker.path,copy)
    copied=CapacityBroker(copy)
    with pytest.raises(RuntimeError,match='another authority'):lifecycle.claim_authority(copied,'shared-inference')
    adopted=lifecycle.adopt(copied,'shared-inference','ops')
    assert adopted['previous']==first and lifecycle.claim_authority(copied,'shared-inference')==adopted['authority']
    with copied.connect() as db:assert db.execute("SELECT count(*) FROM capacity_events WHERE event_type='service.authority_adopted'").fetchone()[0]==1


def test_server_process_refuses_a_copied_ledger(tmp_path):
    original=tmp_path/'ledger.db';broker=CapacityBroker(original);lifecycle.claim_authority(broker,'shared-inference')
    copy=tmp_path/'copy.db';shutil.copyfile(original,copy)
    environment={k:v for k,v in os.environ.items() if k!='PYTHONPATH'};environment['PYTHONPATH']=str(ROOT/'src')
    result=subprocess.run([sys.executable,'-m','inference_gateway.capacity_gateway_server','--database',str(copy),'--port','0'],
        env=environment,capture_output=True,text=True,timeout=30)
    assert result.returncode!=0 and 'another authority' in result.stderr and 'service.listening' not in result.stdout


def test_service_stop_drain_resumes_but_operator_drain_survives(tmp_path):
    broker=CapacityBroker(tmp_path/'ledger.db')
    assert lifecycle.service_stop_drain(broker) and not lifecycle.status(broker)['admission_open']
    assert lifecycle.status(broker)['resume_on_service_start']
    assert lifecycle.resume_after_service_stop(broker) and lifecycle.status(broker)['admission_open']
    lifecycle.set_admission(broker,False,'ops') # operator maintenance drain
    assert not lifecycle.service_stop_drain(broker) # already closed: the service records nothing
    assert not lifecycle.resume_after_service_stop(broker) and not lifecycle.status(broker)['admission_open']
    lifecycle.set_admission(broker,True,'ops');lifecycle.service_stop_drain(broker)
    lifecycle.set_admission(broker,False,'ops') # operator drains during a service stop: the explicit decision wins
    assert not lifecycle.resume_after_service_stop(broker) and not lifecycle.status(broker)['admission_open']


def test_online_backup_is_consistent_and_restore_is_verified_and_non_destructive(tmp_path):
    database=tmp_path/'ledger.db';broker=CapacityBroker(database)
    broker.register_client('capstone','t',models=['m:1'])
    with broker.connect() as db:broker._event(db,'infrastructure',None,'resource.authorized',{})
    stop=threading.Event()
    def write():
        while not stop.is_set():
            with broker.connect() as db:broker._event(db,'infrastructure',None,'resource.authorized',{})
    writer=threading.Thread(target=write);writer.start()
    try:record=lifecycle.backup(database,tmp_path/'backup.db')
    finally:stop.set();writer.join(5)
    assert record['sha256'] and record['ledger_id'].startswith('LEDGER-') and record['max_event_id']>0
    with pytest.raises(FileExistsError):lifecycle.backup(database,tmp_path/'backup.db')
    with lifecycle.service_owner(database):
        with pytest.raises(RuntimeError):lifecycle.restore(tmp_path/'backup.db',database,'ops',replace=True) # a running authority owns it
    with pytest.raises(FileExistsError):lifecycle.restore(tmp_path/'backup.db',database,'ops')
    restored=lifecycle.restore(tmp_path/'backup.db',database,'ops',replace=True)
    assert restored['ledger_id']==record['ledger_id'] and Path(restored['previous_kept_as']).exists()
    assert CapacityBroker(database).authenticate('capstone','t')
    tampered=tmp_path/'tampered.db';shutil.copyfile(tmp_path/'backup.db',tampered);shutil.copyfile(tmp_path/'backup.db.json',str(tampered)+'.json')
    with open(tampered,'r+b') as handle:handle.seek(200);handle.write(b'\x00\xff')
    with pytest.raises(ValueError,match='hash'):lifecycle.restore(tampered,tmp_path/'elsewhere.db','ops')


class Vanishing:
    def __init__(self):self.vanish=False;self.calls=0
    def __call__(self,endpoint,operation,payload,timeout):
        if operation=='/api/version':return {'version':'0.12.3'}
        if operation=='/api/tags':return {'models':[{'name':'m:1','digest':'D1'}]}
        if operation=='/api/ps':return {'models':[]} # 0.4.0 placement poll: nothing loaded
        if operation=='/api/show':return {'capabilities':['completion']}
        self.calls+=1
        if self.vanish:raise ConnectionResetError('head disappeared mid-request')
        return {'done':True,'model':'m:1','response':'READY'}


def test_head_disappearing_mid_request_is_uncertain_quarantined_and_never_replayed(tmp_path):
    heads=Vanishing();gateway=Gateway(tmp_path/'ledger.db',transport=heads);b=gateway.broker
    b.register_client('capstone','t',models=['m:1'])
    b.authorize_resource('head-192-0-2-3',endpoint='http://127.0.0.1:12345',resource_type='shared_inference',owner='infrastructure',applications=['capstone'],permitted_uses=['inference'])
    assert gateway.qualify('head-192-0-2-3','m:1',operator='ops')['passed']
    heads.vanish=True
    result=gateway.execute('capstone','t',{'contract':'shared.inference/1','client_key':'lost','model':'m:1','nodes':['head-192-0-2-3'],
        'deadline':time.time()+5,'operation':'/api/generate','payload':{'prompt':'x'}})
    # The request was sent: never reported as completed, and the head is quarantined, not reused.
    assert result['status']=='failed' and 'result' not in result and result['execution_started_at']
    with b.connect() as db:assert db.execute('SELECT health FROM capacity_resources').fetchone()[0]=='quarantined'
    calls=heads.calls;heads.vanish=False
    restarted=Gateway(b.path,transport=heads);restarted.recover_service_startup();restarted.refresh_health()
    assert heads.calls==calls # no replay after restart
    with b.connect() as db:assert db.execute('SELECT health FROM capacity_resources').fetchone()[0]=='quarantined' # returning head stays quarantined
    assert restarted.availability('capstone','t','m:1')['status']=='resource_unhealthy'
    # The uncertain lease is retained until expiry: reconciliation is refused before then.
    with pytest.raises(ValueError,match='active reservation'):lifecycle.reconcile_resource(b,'head-192-0-2-3','ops')
    with b.connect() as db:db.execute("UPDATE capacity_requests SET expires=? WHERE client_key='lost'",(time.time()-1,))
    b.reconcile_expired()
    # Operator verifies physical idleness, reconciles, requalifies.
    assert lifecycle.reconcile_resource(b,'head-192-0-2-3','ops')['health']=='unqualified'
    assert restarted.qualify('head-192-0-2-3','m:1',operator='ops')['passed']
    assert restarted.availability('capstone','t','m:1')['status']=='available'
    again=restarted.execute('capstone','t',{'contract':'shared.inference/1','client_key':'lost','model':'m:1','nodes':['head-192-0-2-3'],
        'deadline':time.time()+5,'operation':'/api/generate','payload':{'prompt':'x'}})
    assert again['request']['request_id']==result['request']['request_id'] and again['status']=='failed' # idempotent identity, no hidden retry


def test_lifecycle_cli_never_creates_a_ledger_and_requires_idle_confirmation(tmp_path):
    environment={k:v for k,v in os.environ.items() if k!='PYTHONPATH'};environment['PYTHONPATH']=str(ROOT/'src')
    run=lambda *args:subprocess.run([sys.executable,'-m','inference_gateway.capacity_lifecycle',*args],env=environment,capture_output=True,text=True,timeout=30)
    assert run('status','--database',str(tmp_path/'missing.db')).returncode==2 and not (tmp_path/'missing.db').exists()
    CapacityBroker(tmp_path/'ledger.db')
    assert run('reconcile-resource','--database',str(tmp_path/'ledger.db'),'--resource','head-192-0-2-3','--operator','ops').returncode==2
    status=json.loads(run('status','--database',str(tmp_path/'ledger.db')).stdout)
    assert status['admission_open'] and status['ledger_id'].startswith('LEDGER-') and status['authority'] is None
