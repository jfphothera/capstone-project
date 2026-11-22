"""0.4.4: ledger contention, transport symptoms, runtime-aware per-model capacity, and the request
lifecycle under 12, 16 and 20 concurrent workflows.

Field, 2026-11-16/17 (pilot EXP-7f55845c..., hub on 0.4.3, Ollama 0.40.13): 48 'database is locked'
events on the Gateway's own SQLite ledger (27 HTTP 503, 20 lease renewals, 1 recovery cycle) and
167 SSLEOFError events, 159 of them on /v1/submit. The 0.4.1 client gives up on one submit or
status attempt after 10 s; the ledger's busy timeout was 30 s.
"""
import functools
import importlib.util
import json
import socket
import sqlite3
import ssl
import threading
import time
import urllib.request
from pathlib import Path
import pytest
from inference_gateway import capacity_broker,capacity_gateway,model_capacity,recovery
from inference_gateway.capacity_broker import CapacityBroker,LedgerBusy
from inference_gateway.capacity_gateway import Gateway

ROOT=Path(__file__).resolve().parents[1]
_spec=importlib.util.spec_from_file_location('fp_helpers',Path(__file__).with_name('test_failures_and_placement.py'))
fp=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(fp)
endpoint,events,run,body,background=fp.endpoint,fp.events,fp.run,fp.body,fp.background


def script(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def hold_externally(path,seconds,started=None):
    """Another connection (not this process's gate) holds the write lock: the operator CLI, or a stall."""
    def hold():
        db=sqlite3.connect(path,timeout=10)
        try:
            db.execute('BEGIN IMMEDIATE')
            if started:started.set()
            time.sleep(seconds);db.rollback()
        finally:db.close()
    thread=threading.Thread(target=hold,daemon=True);thread.start();return thread


# ---------------------------------------------------------------- 1. the ledger write path

def test_writers_queue_in_arrival_order_and_a_refused_writer_names_the_holder(tmp_path):
    broker=CapacityBroker(tmp_path/'ledger.db');logs=[];broker.log=lambda e,**f:logs.append((e,f))
    entered=threading.Event();order=[]
    def slow():
        with broker.write('slow_holder') as db:entered.set();time.sleep(1.0)
    threading.Thread(target=slow,daemon=True).start();assert entered.wait(5)
    with pytest.raises(LedgerBusy) as refused:
        with broker.write('impatient',wait=.3):pass
    assert refused.value.holder['label']=='slow_holder' and refused.value.holder['held_ms']>=250
    assert any(e=='ledger.wait_timeout' and f['label']=='impatient' and f['holder']['label']=='slow_holder' for e,f in logs)
    def queued(name):
        with broker.write(name,wait=10):order.append(name)
    waiters=[]
    for name in ('first','second','third'):
        waiters.append(threading.Thread(target=queued,args=(name,)));waiters[-1].start();time.sleep(.05)
    for t in waiters:t.join(10)
    assert order==['first','second','third'] # FIFO hand-off, no barging
    stats=broker.ledger_stats()['labels']
    assert stats['impatient']['timeouts']==1 and stats['slow_holder']['hold_ms_max']>=900
    assert any(e=='ledger.slow_transaction' and f['label']=='slow_holder' for e,f in logs)


def test_another_process_holding_the_lock_is_retried_within_the_budget_then_refused_as_external(tmp_path):
    path=tmp_path/'ledger.db';broker=CapacityBroker(path);started=threading.Event()
    hold_externally(path,.6,started);assert started.wait(5)
    t=time.monotonic()
    with broker.write('patient',wait=5) as db:db.execute("UPDATE capacity_service_policy SET value=value WHERE key='admission_open'")
    assert time.monotonic()-t>=.4 and broker.ledger_stats()['labels']['patient']['external_busy']>=1
    started.clear();hold_externally(path,2,started);assert started.wait(5)
    with pytest.raises(LedgerBusy) as refused:
        with broker.write('impatient',wait=.5):pass
    assert refused.value.holder=='external'


def test_every_ledger_write_of_a_request_goes_through_the_gate_and_submit_writes_once(tmp_path):
    gateway,transport=fp.lab(tmp_path,['head-192-0-2-7'])
    before=gateway.broker.ledger_stats()['labels']
    assert run(gateway,'one')['status']=='completed'
    after=gateway.broker.ledger_stats()['labels']
    used={k for k in after if after[k]['count']>before.get(k,{'count':0})['count']}
    assert {'submit','invocation','place','executing','started','complete'}<=used
    assert 'record_operation' not in used # 0.4.3: a separate write; now part of the submission
    with gateway.broker.connect() as db:
        assert db.execute("SELECT operation FROM capacity_request_routing").fetchone()[0]=='completion'


def test_the_status_and_slot_queries_use_indexes(tmp_path):
    gateway,_=fp.lab(tmp_path,['head-192-0-2-7'])
    plans={}
    with gateway.broker.connect() as db:
        for name,sql in (('expiry',"SELECT * FROM capacity_requests WHERE status IN ('leased','cancel_requested') AND expires<1"),
                         ('queued',"SELECT * FROM capacity_requests WHERE status='queued'"),
                         ('slot_guard',"SELECT count(*) FROM capacity_requests WHERE resource_id='x' AND status IN ('leased','cancel_requested')"),
                         ('pending',"SELECT count(*) FROM capacity_invocations WHERE state='active'")):
            plans[name]=' '.join(r[-1] for r in db.execute('EXPLAIN QUERY PLAN '+sql))
    assert all('INDEX' in plan for plan in plans.values()),plans


def test_status_reads_take_no_write_lock_for_a_call_still_running_past_its_lease(tmp_path):
    gateway,transport=fp.lab(tmp_path,['head-192-0-2-7'])
    transport.hold=True
    threads=background(gateway,['slow'],prompt='hold');assert transport.wait_inflight(1)
    with gateway.broker.connect() as db:rid=db.execute("SELECT request_id FROM capacity_requests WHERE client_key='slow'").fetchone()[0]
    with gateway.broker.write('test') as db:db.execute('UPDATE capacity_requests SET expires=?,deadline=? WHERE request_id=?',(time.time()-5,time.time()-1,rid))
    assert not gateway.broker.has_expired() # 0.4.3: True, so every poll ran an empty expiry transaction
    count=gateway.broker.ledger_stats()['labels'].get('expire',{'count':0})['count']
    gateway.result('capstone','t-capstone',rid)
    assert gateway.broker.ledger_stats()['labels'].get('expire',{'count':0})['count']==count
    transport.release.set()
    for t in threads:t.join(10)


def test_a_finished_reply_is_recorded_through_a_busy_ledger(tmp_path):
    gateway,transport=fp.lab(tmp_path,['head-192-0-2-7']);path=tmp_path/'ledger.db'
    inner=fp.Lab.__call__
    def busy_on_reply(url,operation,payload,timeout):
        reply=inner(transport,url,operation,payload,timeout)
        if operation=='/api/generate' and payload.get('prompt')=='busy':
            started=threading.Event();hold_externally(path,1.5,started);started.wait(5)
        return reply
    gateway.transport=busy_on_reply
    result=run(gateway,'busy',prompt='busy')
    assert result['status']=='completed' and gateway.broker.ledger_stats()['labels']['complete']['wait_ms_max']>=1000 # waited, then recorded


def test_renewal_rides_through_contention_without_expiring_or_holding(tmp_path,monkeypatch):
    monkeypatch.setattr(capacity_broker,'LEASE_SECONDS',2)
    monkeypatch.setattr(capacity_gateway,'renew_until_stopped',functools.partial(capacity_broker.renew_until_stopped,interval=.3))
    gateway,transport=fp.lab(tmp_path,['head-192-0-2-7']);path=tmp_path/'ledger.db'
    transport.hold=True
    threads=background(gateway,['long'],prompt='hold');assert transport.wait_inflight(1)
    for _ in range(3):
        started=threading.Event();hold_externally(path,1.2,started).join(5)
    transport.release.set()
    for t in threads:t.join(10)
    names=[e[0] for e in events(gateway)]
    assert names.count('completed')==1 and 'lease.expired' not in names and 'resource.held' not in names


def test_a_client_cancel_before_dispatch_fails_no_head(tmp_path):
    # Found by the 20-workflow run: 0.4.0-0.4.3 raised TimeoutError when the lease ended during the pre-dispatch
    # checks; in that phase it classifies as head_unreachable, so a client's cancel marked a healthy head offline
    # and the other calls being checked on it failed with 'Resource admission withdrawn before dispatch'.
    rid='head-192-0-2-7';gateway,transport=fp.lab(tmp_path,[rid]);inner=fp.Lab.__call__
    def cancel_during_checks(url,operation,payload,timeout):
        if operation=='/api/show':
            with gateway.broker.connect() as db:req=db.execute("SELECT request_id FROM capacity_requests WHERE client_key='c'").fetchone()[0]
            gateway.cancel('capstone','t-capstone',req)
        return inner(transport,url,operation,payload,timeout)
    gateway.transport=cancel_during_checks
    assert run(gateway,'c')['status']=='cancelled'
    names=[e[0] for e in events(gateway)]
    assert fp.health(gateway,rid)=='healthy' and 'resource.health_changed' not in names and transport.generations()==[]
    with gateway.broker.connect() as db:assert db.execute('SELECT failure_class FROM capacity_request_attempts').fetchone()[0]=='request_fault'
    gateway.transport=transport
    assert run(gateway,'next')['status']=='completed'

# ---------------------------------------------------------------- 2. transport: a client that left

def test_a_busy_ledger_answers_503_inside_the_clients_attempt_and_the_retry_runs_once(tmp_path):
    from inference_gateway.capacity_gateway_server import Server
    import shared_inference_client as client_lib
    gateway,transport=fp.lab(tmp_path,['head-192-0-2-7']);logs=[];gateway.emit=lambda e,**f:logs.append((e,f))
    server=Server(('127.0.0.1',0),gateway);threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        started=threading.Event();hold_externally(tmp_path/'ledger.db',7,started);assert started.wait(5)
        client=client_lib.Client('http://127.0.0.1:%d'%server.server_port,'capstone','t-capstone')
        t=time.monotonic();result=client.infer('m:1',None,'/api/generate',{'prompt':'once'},timeout=60)
        assert result['status']=='completed' and time.monotonic()-t>=6
        assert client.retries['submit']>=1 and gateway.counters.get('ledger_busy_503',0)>=1
        assert len(transport.generations(prompt='once'))==1 # one execution, however many attempts
        assert not [e for e,_ in logs if e=='http.unavailable']
    finally:server.shutdown();server.server_close()


def test_a_client_that_gives_up_is_logged_as_client_gone_with_timing_not_as_unavailable(tmp_path,certificates,monkeypatch):
    from inference_gateway import capacity_gateway_server as srv
    monkeypatch.setattr(srv,'CLIENT_ATTEMPT_SECONDS',1)
    gateway,_=fp.lab(tmp_path,['head-192-0-2-7']);logs=[];gateway.emit=lambda e,**f:logs.append((e,f))
    real=gateway.policy
    def slow_policy(app,token):time.sleep(2);return real(app,token)
    gateway.policy=slow_policy
    good=certificates['good']
    server=srv.Server(('127.0.0.1',0),gateway,tls=srv.server_tls(good['server_certificate'],good['server_key']))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        context=ssl.create_default_context(cafile=good['ca_file_for_clients'])
        request=urllib.request.Request('https://127.0.0.1:%d/v1/policy'%server.server_port,headers={'X-Application-ID':'capstone','X-Infrastructure-Token':'t-capstone'})
        with pytest.raises(Exception):urllib.request.urlopen(request,timeout=.5,context=context) # the client's attempt timeout
        deadline=time.monotonic()+10
        while not [f for e,f in logs if e=='http.client_gone'] and time.monotonic()<deadline:time.sleep(.1)
        gone=[f for e,f in logs if e=='http.client_gone']
        assert len(gone)==1 and gone[0]['phase']=='responding' and gone[0]['elapsed_ms']>=1900 and gone[0]['likely']=='client_attempt_timeout'
        assert gone[0]['error'].split(':')[0] in ('SSLEOFError','ConnectionAbortedError','ConnectionResetError','BrokenPipeError')
        assert not [e for e,_ in logs if e=='http.unavailable'] and gateway.counters.get('unavailable_503',0)==0
    finally:server.shutdown();server.server_close()


# ---------------------------------------------------------------- 3. per-model capacity after the Ollama upgrade

ENTRY={'parallel':3,'digest':'DIGEST-qwen3.5:4b','runtime_version':'0.40.15','num_ctx':4096,'evidence':'arena-matrix-2026-11-17'}


def test_policy_defaults_and_validation():
    placement=recovery.validate_policy('placement_policy',None)
    assert placement['parallel_runtime_min']=='0.40.15' and placement['serial_families_need_evidence'] is True
    assert placement['serial_families']==['qwen35','qwen35moe'] # kept; now applied by runtime and evidence
    for bad in ({'parallel_runtime_min':'latest'},{'parallel_runtime_min':40},{'serial_families_need_evidence':'yes'}):
        with pytest.raises(ValueError):recovery.validate_policy('placement_policy',bad)
    for bad in ({'m':{'parallel':0,'digest':'d','runtime_version':'0.40.15'}},{'m':{'parallel':2,'digest':'d'}},
                {'m':{'parallel':2,'digest':'d','runtime_version':'0.40.15','extra':1}},{'m':{'parallel':True,'digest':'d','runtime_version':'v'}},[]):
        with pytest.raises(ValueError):CapacityBroker.validate_model_capacity(bad)


def test_effective_limit_uses_only_current_evidence_and_keeps_conservative_defaults():
    policy=recovery.validate_policy('placement_policy',None)
    def eff(version,digest='DIGEST-q',declared=None,measured=None,families=('qwen35',),slots=4,**over):
        row=lambda p,v='0.40.15',d='DIGEST-q':{'parallel':p,'digest':d,'runtime_version':v,'num_ctx':4096,'evidence':'e','recorded_at':1.0}
        return model_capacity.effective('q',slots=slots,families=families,runtime_version=version,digest=digest,
            declared=row(*declared) if declared else None,measured=row(*measured) if measured else None,policy={**policy,**over})
    assert (lambda r:(r['limit'],r['source']))(eff('0.40.15',declared=(3,)))==(3,'declared')
    assert (lambda r:(r['limit'],r['source']))(eff('0.40.15',declared=(3,),measured=(2,)))==(2,'measured')   # min(declared, measured)
    assert (lambda r:(r['limit'],r['source']))(eff('0.40.15',declared=(8,)))==(4,'declared')                 # capped at slots
    r=eff('0.40.16',declared=(3,))                                                                           # runtime changed: requalify
    assert (r['limit'],r['source'],r['reason'],r['declared']['stale_because'])==(1,'serial_family','no_current_parallel_evidence','runtime_changed')
    r=eff('0.40.15',digest='sha256:OTHER',declared=(3,));assert (r['limit'],r['declared']['stale_because'])==(1,'digest_changed')
    assert eff('0.40.15',digest='sha256:digest-q',declared=(3,))['limit']==3                                  # sha256: prefix and case ignored
    assert (lambda r:(r['limit'],r['reason']))(eff('0.40.13'))==(1,'runtime_below_parallel_min')
    assert (lambda r:(r['limit'],r['reason']))(eff(None))==(1,'runtime_version_unknown')
    assert (lambda r:(r['limit'],r['source']))(eff('0.40.15'))==(1,'serial_family')                           # upgraded, unverified: still 1
    assert (lambda r:(r['limit'],r['source']))(eff('0.40.15',serial_families_need_evidence=False))==(4,'slots') # the contract's literal default
    assert (lambda r:(r['limit'],r['source']))(eff('0.40.13',families=('gemma3',)))==(4,'slots')
    assert (lambda r:(r['limit'],r['source']))(eff('0.40.15',declared=(3,),model_parallel={'q':2}))==(2,'policy')
    assert model_capacity.at_least('0.40.15','0.40.15') and model_capacity.at_least('0.41','0.40.15') and not model_capacity.at_least('0.40.15-rc1','0.40.15')


class UpgradedLab(fp.Lab):
    """The 0.4.0 fake heads with /api/tags families and a settable runtime version."""
    version='0.40.15'
    def __call__(self,url,operation,payload,timeout):
        if operation=='/api/version':return {'version':self.version}
        reply=super().__call__(url,operation,payload,timeout)
        if operation=='/api/tags':
            for m in reply['models']:m['details']={'family':'qwen35' if m['name'].startswith('qwen3.5') else 'llama','families':['qwen35' if m['name'].startswith('qwen3.5') else 'llama']}
        return reply


def upgraded(tmp_path,rid,slots,model_capacity_value=None):
    transport=UpgradedLab();gateway=Gateway(tmp_path/'ledger.db',transport=transport);b=gateway.broker
    b.register_client('capstone','t-capstone',models=['qwen3.5:4b'],max_concurrency=16,queue_limit=32,priority=6)
    b.authorize_resource(rid,endpoint=endpoint(rid),resource_type='shared_inference',owner='infrastructure',applications=['capstone'],
                         permitted_uses=['inference'],slots=slots,memory_gb=31.4,model_capacity=model_capacity_value)
    assert gateway.qualify(rid,'qwen3.5:4b',operator='ops')['passed']
    with b.write('test') as db:recovery.store_policy(db,'failure_policy',{'ps_settle_seconds':0})
    transport.calls.clear();gateway.refresh_health()
    return gateway,transport


def test_a_current_declaration_lets_an_upgraded_head_run_the_serial_family_in_parallel_and_a_runtime_change_withdraws_it(tmp_path):
    rid='head-192-0-2-7';gateway,transport=upgraded(tmp_path,rid,4,{'qwen3.5:4b':ENTRY})
    transport.hold=True
    threads=background(gateway,['a','b','c','d'],model='qwen3.5:4b',prompt='hold');assert transport.wait_inflight(3)
    time.sleep(.5);assert transport.inflight==3 # declared 3 of 4 slots; the fourth waits in the Gateway queue
    transport.release.set()
    for t in threads:t.join(10)
    transport.version='0.40.16';transport.release.clear();gateway.refresh_health() # head upgraded again: the declaration is stale
    with gateway.broker.connect() as db:
        [limit]=model_capacity.head_limits(db,rid,['qwen3.5:4b'],recovery.load_policy(db)['placement'])
    assert (limit['limit'],limit['source'],limit['declared']['stale_because'])==(1,'serial_family','runtime_changed')


def test_declared_capacity_survives_reapply_and_inventory_import(tmp_path):
    from inference_gateway import capacity_bootstrap,head_inventory
    rid='head-192-0-2-7';db_path=tmp_path/'ledger.db'
    resource={'resource_id':rid,'endpoint':endpoint(rid),'resource_type':'shared_inference','owner':'infrastructure','applications':['capstone'],
              'permitted_uses':['inference'],'slots':4,'memory_gb':31.4,'model_capacity':{'qwen3.5:4b':ENTRY}}
    policy={'configuration_version':1,'contract':'shared.inference/1','clients':[],'resources':[resource]}
    (tmp_path/'p.json').write_text(json.dumps(policy));out=capacity_bootstrap.apply(tmp_path/'p.json',db_path)
    assert out['capacity'][rid]['model_capacity']==['qwen3.5:4b']
    without=dict(resource);del without['model_capacity'] # a policy without the key keeps the declarations
    (tmp_path/'p2.json').write_text(json.dumps({**policy,'resources':[without]}));capacity_bootstrap.apply(tmp_path/'p2.json',db_path)
    with CapacityBroker(db_path).connect() as db:
        row=db.execute("SELECT * FROM capacity_model_capacity WHERE resource_id=? AND source='declared'",(rid,)).fetchone()
    assert (row['parallel'],row['digest'],row['runtime_version'],row['num_ctx'],row['evidence'])==(3,ENTRY['digest'],'0.40.15',4096,ENTRY['evidence'])
    bad={**policy,'resources':[{**resource,'model_capacity':{'qwen3.5:4b':{**ENTRY,'parallel':65}}}]}
    (tmp_path/'bad.json').write_text(json.dumps(bad))
    with pytest.raises(ValueError):capacity_bootstrap.apply(tmp_path/'bad.json',db_path)
    inventory={'schema':head_inventory.SCHEMA,'heads':[{'resource_id':rid,'arena_node_id':rid,'address':'192.0.2.7','endpoint':'http://192.0.2.7:11434',
               'verified':{'ollama_version':'0.40.15'},'capacity':{'slots':4,'model_capacity':{'qwen3.5:4b':ENTRY}}}]}
    accepted,refused=head_inventory.resources(inventory,['capstone'])
    assert not refused and accepted[0]['model_capacity']=={'qwen3.5:4b':ENTRY}
    del inventory['heads'][0]['capacity']['model_capacity']
    merged=head_inventory.merge({'resources':accepted},head_inventory.resources(inventory,['capstone'])[0])
    assert merged['resources'][0]['model_capacity']=={'qwen3.5:4b':ENTRY} # a re-import without it keeps it


class Timed(UpgradedLab):
    """Generation replies with Ollama timings; overlap=True: the calls computed at the same time."""
    overlap=True
    def __call__(self,url,operation,payload,timeout):
        reply=super().__call__(url,operation,payload,timeout)
        if operation=='/api/generate' and payload.get('prompt')==Gateway.MEASURE_PROMPT:
            with self.lock:self.n=getattr(self,'n',0)+1;n=self.n
            end=1_800_000_000+(5 if self.overlap else 5*n) # serial: each call ends 5 s after the previous one
            reply.update(created_at=time.strftime('%Y-%m-%dT%H:%M:%S',time.gmtime(end))+'.250000000Z',eval_count=48,
                         total_duration=5_000_000_000*n if not self.overlap else 5_000_000_000,load_duration=0,prompt_eval_duration=500_000_000,eval_duration=4_000_000_000)
        return reply


@pytest.mark.parametrize('overlap,expected',[(True,4),(False,1)])
def test_measure_parallel_judges_overlap_from_ollama_timings_and_min_with_the_declaration(tmp_path,overlap,expected):
    rid='head-192-0-2-7';transport=Timed();transport.overlap=overlap
    gateway=Gateway(tmp_path/'ledger.db',transport=transport);b=gateway.broker
    b.register_client('capstone','t-capstone',models=['qwen3.5:4b'],max_concurrency=16,queue_limit=32,priority=6)
    b.authorize_resource(rid,endpoint=endpoint(rid),resource_type='shared_inference',owner='infrastructure',applications=['capstone'],
                         permitted_uses=['inference'],slots=8,memory_gb=31.4,model_capacity={'qwen3.5:4b':{**ENTRY,'parallel':6}})
    assert gateway.qualify(rid,'qwen3.5:4b',operator='ops',max_context=4096)['passed']
    out=gateway.measure_parallel(rid,'qwen3.5:4b',parallel=4,operator='ops')
    assert out['stored'] and out['measured_parallel']==expected and out['runtime_version']=='0.40.15' and out['num_ctx']==4096
    with b.connect() as db:
        [limit]=model_capacity.head_limits(db,rid,['qwen3.5:4b'],recovery.load_policy(db)['placement'])
        health=db.execute('SELECT health FROM capacity_resources WHERE resource_id=?',(rid,)).fetchone()[0]
    assert (limit['limit'],limit['source'])==(expected,'measured') and limit['declared']['parallel']==6 and health=='healthy'
    assert ('model_capacity.measured',{'resource_id':rid,'model':'qwen3.5:4b','requested':4,'measured':expected,'stored':True}) in events(gateway)


def test_observation_lifecycle_and_models_show_each_limit_and_its_source(tmp_path):
    from inference_gateway import capacity_lifecycle,capacity_observe
    rid='head-192-0-2-7';gateway,_=upgraded(tmp_path,rid,4,{'qwen3.5:4b':ENTRY})
    snap=capacity_observe.snapshot(tmp_path/'ledger.db')
    [limit]=[r for r in snap['resources'] if r['resource_id']==rid][0]['capacity']['model_limits']
    assert (limit['model'],limit['limit'],limit['source'],limit['runtime_version'])==('qwen3.5:4b',3,'declared','0.40.15')
    status=capacity_lifecycle.capacity(gateway.broker)
    assert status['resources'][0]['model_limits'][0]['source']=='declared' and status['parallel_runtime_min']=='0.40.15'
    detail=gateway.models('capstone','t-capstone')['resources'][0]['model_details']['qwen3.5:4b']
    assert detail['parallel']=={'limit':3,'source':'declared','reason':None}
    assert 'ledger' in gateway.publish_counters() # write-lock statistics reach the observation


# ---------------------------------------------------------------- 4. lifecycle under 12, 16 and 20 workflows

@pytest.mark.parametrize('workflows',[12,16,20])
def test_workflows_with_mixed_heads_busy_resources_and_injected_ledger_contention(tmp_path,workflows):
    report=script('lifecycle_load').run(workflows,calls=3,work=tmp_path/'w')
    assert report['errors']==[] and report['duplicate_dispatch']==[] and report['lost_results']==[] and report['over_model_limit']=={}
    assert report['lease_expired']==0 and report['resource_held']==0 and report['database_locked_logged']==0
    assert set(report['statuses'])<={'completed','cancelled'},report['failed_requests']
    assert sum(report['statuses'].values())==workflows*3
    assert report['injected_external_locks']>0


def test_a_restart_with_calls_in_flight_never_replays_them(tmp_path):
    gateway,transport=fp.lab(tmp_path,['head-192-0-2-7','head-192-0-2-8'],slots={'head-192-0-2-7':2,'head-192-0-2-8':2})
    transport.hold=True
    threads=background(gateway,['r1','r2','r3'],prompt='hold');assert transport.wait_inflight(3)
    # The service starts again on the same ledger (the old process's calls are still at the heads).
    restarted=Gateway(tmp_path/'ledger.db',transport=transport)
    assert restarted.recover_service_startup()=={'interrupted_requests':3}
    restarted.refresh_health();restarted.broker.place();time.sleep(.5)
    assert len(transport.generations(prompt='hold'))==3 # nothing dispatched again
    with restarted.broker.connect() as db:
        rids=[r[0] for r in db.execute("SELECT request_id FROM capacity_invocations WHERE state='unknown'")]
    assert len(rids)==3 and {restarted.result('capstone','t-capstone',r)['status'] for r in rids}=={'unknown'}
    transport.release.set() # (the old process is gone in the field; here its threads are just let go)
    for t in threads:t.join(10)
