"""Independent simulated clients; no claim that Coder is deployed."""
import concurrent.futures
import time
import pytest
from inference_gateway.capacity_broker import CapacityBroker


def setup(tmp_path):
    broker=CapacityBroker(tmp_path/'capacity.db')
    for app in ('alpha','beta','gamma'):
        broker.register_client(app,'test-'+app,models=['model:1'],max_concurrency=1,queue_limit=4)
    for resource in ('head-a','head-b'):
        broker.authorize_resource(resource,endpoint='http://192.0.2.'+('3' if resource=='head-a' else '7')+':11434',
            resource_type='shared_inference',owner='infrastructure',applications=['alpha','beta','gamma'],permitted_uses=['inference'])
        broker.publish(resource,['model:1'],'healthy')
    return broker


def submit(broker,app,key,nodes=None):
    return broker.submit(app,'test-'+app,key,'model:1',nodes or ['head-a'],time.time()+120)


def test_atomic_capacity_and_namespace(tmp_path):
    broker=setup(tmp_path)
    requests=[submit(broker,app,'one') for app in ('alpha','beta')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda pair:broker.admit(pair[0],'test-'+pair[0],pair[1]['request_id']),zip(('alpha','beta'),requests)))
    with broker.connect() as db:
        assert db.execute("SELECT count(*) FROM capacity_requests WHERE status='leased'").fetchone()[0]==1
    with pytest.raises(PermissionError):broker.get('beta','test-beta',requests[0]['request_id'])
    with pytest.raises(PermissionError):broker.authenticate('alpha','test-beta')
    broker.finish('alpha','test-alpha',requests[0]['request_id'])
    assert broker.admit('beta','test-beta',requests[1]['request_id'])['status']=='leased'


def test_app_limits_queue_and_duplicate_keys(tmp_path):
    broker=setup(tmp_path)
    one=submit(broker,'alpha','one',['head-a']);two=submit(broker,'alpha','two',['head-b'])
    assert submit(broker,'alpha','one',['head-a'])['request_id']==one['request_id']
    with pytest.raises(ValueError):submit(broker,'alpha','one',['head-b'])
    broker.admit('alpha','test-alpha',one['request_id'])
    assert broker.get('alpha','test-alpha',two['request_id'])['status']=='queued'
    broker.finish('alpha','test-alpha',one['request_id'],cancel=True)
    assert broker.admit('alpha','test-alpha',two['request_id'])['status']=='leased'


def test_executing_expiry_quarantines_and_rejects_stale_completion(tmp_path):
    broker=setup(tmp_path);one=submit(broker,'alpha','one')
    broker.admit('alpha','test-alpha',one['request_id']);broker.executing('alpha','test-alpha',one['request_id'])
    with broker.connect() as db:db.execute('UPDATE capacity_requests SET expires=? WHERE request_id=?',(time.time()-1,one['request_id']))
    two=submit(broker,'beta','two');assert broker.admit('beta','test-beta',two['request_id'])['status']=='queued'
    assert broker.finish('alpha','test-alpha',one['request_id'])['status']=='expired'
    broker.publish('head-a',['model:1'],'healthy')
    with broker.connect() as db:assert db.execute("SELECT health FROM capacity_resources WHERE resource_id='head-a'").fetchone()[0]=='quarantined'


def test_priority_aging_and_third_client(tmp_path):
    broker=setup(tmp_path)
    broker.register_client('beta','test-beta',models=['model:1'],priority=10)
    low=submit(broker,'alpha','old');high=submit(broker,'beta','new')
    with broker.connect() as db:db.execute('UPDATE capacity_requests SET created=? WHERE request_id=?',(time.time()-120,low['request_id']))
    broker.admit('beta','test-beta',high['request_id'])
    assert broker.get('alpha','test-alpha',low['request_id'])['status']=='leased'
    broker.finish('alpha','test-alpha',low['request_id'])
    three=submit(broker,'gamma','third',['head-b'])
    assert broker.admit('gamma','test-gamma',three['request_id'])['status']=='leased'


def test_exclusive_devices_are_never_shared_by_heartbeat(tmp_path):
    broker=setup(tmp_path)
    broker.authorize_resource('coder-laptop',resource_type='exclusive_test_device',owner='coder',applications=[],permitted_uses=['build','test'])
    broker.publish('coder-laptop',['model:1'],'healthy')
    with pytest.raises(PermissionError):submit(broker,'alpha','intrusion',['coder-laptop'])
    with pytest.raises(PermissionError):broker.publish('new-lan-device',['model:1'],'healthy')
    broker.revoke('gamma')
    with pytest.raises(PermissionError):submit(broker,'gamma','revoked')






def test_coordinator_adapter_does_not_preempt_another_client(tmp_path):
    import threading
    from inference_gateway.capacity_broker import shared_slot
    broker=setup(tmp_path);entered=threading.Event();release=threading.Event()
    def first():
        with shared_slot(broker.path,'alpha','test-alpha','http://192.0.2.3:11434','model:1',time.monotonic()+10):
            entered.set();assert release.wait(5)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(first);assert entered.wait(5)
        with pytest.raises(TimeoutError):
            with shared_slot(broker.path,'beta','test-beta','http://192.0.2.3:11434','model:1',time.monotonic()+.2):
                pytest.fail('Double admission')
        with broker.connect() as db:assert db.execute("SELECT count(*) FROM capacity_requests WHERE status='leased'").fetchone()[0]==1
        release.set();future.result(timeout=5)
    with broker.connect() as db:assert db.execute("SELECT count(*) FROM capacity_requests WHERE status='leased'").fetchone()[0]==0
