"""Closure: coherent status snapshots and host-controlled lifecycle only."""
import json,time
from contextlib import contextmanager
from inference_gateway.capacity_gateway import Gateway
from inference_gateway.capacity_broker import CapacityBroker
from inference_gateway.capacity_lifecycle import set_admission,status,service_owner
import pytest

def fixture(tmp_path):
    gateway=Gateway(tmp_path/'ledger.db',transport=lambda *args:None)
    gateway.broker.register_client('fixture','fixture-token',models=['fixture:1'])
    gateway.broker.authorize_resource('fixture-head',endpoint='http://127.0.0.1:12345',resource_type='shared_inference',owner='fixture infrastructure',applications=['fixture'],permitted_uses=['inference'])
    gateway.broker.publish('fixture-head',['fixture:1'],'healthy')
    return gateway

def test_status_request_and_dispatch_metadata_share_a_database_snapshot(tmp_path,monkeypatch):
    gateway=fixture(tmp_path);broker=gateway.broker
    with broker.connect() as db:db.execute('PRAGMA journal_mode=WAL')
    request=broker.submit('fixture','fixture-token','snapshot','fixture:1',['fixture-head'],time.time()+60);rid=request['request_id']
    with broker.connect() as db:db.execute('INSERT INTO capacity_invocations(request_id,app_id,payload_hash,execution_owner,state,updated) VALUES(?,?,?,?,?,?)',(rid,'fixture','fixture-hash','fixture-owner','active',time.time()))
    original=broker.connect;interleaved=[]
    class Cursor:
        def __init__(self,cursor):self.cursor=cursor
        def fetchone(self):
            row=self.cursor.fetchone()
            if not interleaved:
                with original() as writer:
                    writer.execute("UPDATE capacity_requests SET status='leased',resource_id='fixture-head',lease_id='fixture-lease',expires=?,executing=1 WHERE request_id=?",(time.time()+50,rid))
                    writer.execute('UPDATE capacity_invocations SET started_at=? WHERE request_id=?',(time.time(),rid))
                interleaved.append(True)
            return row
    class Connection:
        def __init__(self,db):self.db=db
        def execute(self,sql,args=()):
            cursor=self.db.execute(sql,args)
            return Cursor(cursor) if 'FROM capacity_requests WHERE request_id=? AND app_id=?' in sql else cursor
    @contextmanager
    def connection():
        with original() as db:yield Connection(db)
    monkeypatch.setattr(broker,'connect',connection)
    result=gateway.result('fixture','fixture-token',rid)
    assert interleaved and result['request']['status']=='queued' and 'execution_started_at' not in result
    result=gateway.result('fixture','fixture-token',rid)
    assert result['request']['resource_id']=='fixture-head' and result['execution_started_at']

def test_drain_survives_restart_and_allows_existing_allocation_to_finish(tmp_path):
    gateway=fixture(tmp_path);b=gateway.broker
    first=b.submit('fixture','fixture-token','first','fixture:1',['fixture-head'],time.time()+60)
    b.admit('fixture','fixture-token',first['request_id']);b.executing('fixture','fixture-token',first['request_id'])
    drained=set_admission(b,False,'fixture operator');assert not drained['safe_to_stop'] and drained['active_allocations']==1
    second=b.submit('fixture','fixture-token','second','fixture:1',['fixture-head'],time.time()+60)
    assert b.admit('fixture','fixture-token',second['request_id'])['status']=='queued'
    b.finish('fixture','fixture-token',first['request_id'])
    restarted=CapacityBroker(b.path)
    assert status(restarted)['safe_to_stop'] and not status(restarted)['admission_open']
    set_admission(restarted,True,'fixture operator')
    assert restarted.admit('fixture','fixture-token',second['request_id'])['status']=='leased'

def test_exclusive_service_owner_lock_is_released_after_context_exit(tmp_path):
    database=tmp_path/'ledger.db'
    with service_owner(database):
        with pytest.raises(RuntimeError):
            with service_owner(database):pass
    with service_owner(database):pass

def test_startup_does_not_replay_or_allocate_orphaned_private_requests(tmp_path):
    gateway=fixture(tmp_path);b=gateway.broker
    request=b.submit('fixture','fixture-token','orphan','fixture:1',['fixture-head'],time.time()+60);rid=request['request_id']
    with b.connect() as db:db.execute('INSERT INTO capacity_invocations(request_id,app_id,payload_hash,execution_owner,state,updated) VALUES(?,?,?,?,?,?)',(rid,'fixture','fixture-hash','lost-owner','active',time.time()))
    gateway.recover_service_startup()
    result=gateway.result('fixture','fixture-token',rid)
    assert result['status']=='unknown' and result['request']['status']=='cancelled' and 'result' not in result
