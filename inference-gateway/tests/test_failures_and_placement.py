"""0.4.0: failure classes, automatic recovery and load balancing (fake Ollama transport).

Heads are named and sized like the lab (docs/FAILURES-AND-PLACEMENT.md). Errors are the
exception types urllib really raises: HTTPError for any 4xx/5xx reply, URLError when the
connection fails before the request is sent, raw TimeoutError after it was sent.
"""
import io
import json
import subprocess
import sys
import threading
import time
import urllib.error
import zipfile
from pathlib import Path
import pytest
from inference_gateway import recovery
from inference_gateway.capacity_gateway import Gateway
from inference_gateway.capacity_observe import snapshot

ROOT=Path(__file__).resolve().parents[1]
GIB=1024**3
RAM={'head-192-0-2-2':63.9,'head-192-0-2-3':15.4,'head-192-0-2-4':15.4,'head-192-0-2-5':15.4,'head-192-0-2-6':7.4,
     'head-192-0-2-7':31.4,'head-192-0-2-8':31.4,'head-192-0-2-9':31.4,'head-192-0-2-10':31.4}
SIZES={'gemma3:270m':291*1024**2,'all-minilm:latest':46*1024**2,'qwen3.5:4b':int(3.4*GIB),'small:1':1*GIB,'m:1':2*GIB,
       'other:1':1*GIB,'big:1':16*GIB,'mid:1':12*GIB}
ALL=['gemma3:270m','all-minilm:latest','qwen3.5:4b','m:1','other:1','big:1','mid:1','small:1']


def endpoint(rid):return 'http://127.0.0.1:%d'%(20000+int(rid.rsplit('-',1)[1]))


class Lab:
    """Fake Ollama heads: installed models, loaded models (/api/ps) and a behaviour per head and model."""
    def __init__(self):
        self.loaded={};self.behaviour={};self.down=set();self.calls=[];self.lock=threading.Lock()
        self.hold=False;self.release=threading.Event();self.inflight=0;self.entered=threading.Condition(self.lock)
    def __call__(self,url,operation,payload,timeout):
        if url in self.down:raise urllib.error.URLError(ConnectionRefusedError(10061,'No connection could be made'))
        if operation=='/api/version':return {'version':'0.40.13'}
        if operation=='/api/tags':return {'models':[{'name':n,'digest':'DIGEST-'+n,'size':SIZES[n]} for n in ALL]}
        if operation=='/api/show':return {'capabilities':['embedding'] if payload['model']=='all-minilm:latest' else ['completion']}
        if operation=='/api/ps':
            return {'models':[{'name':n,'digest':'DIGEST-'+n,'size':int(SIZES[n]*1.1),'expires_at':'2026-11-11T18:40:00Z'} for n in self.loaded.get(url,[])]}
        model=payload['model']
        with self.lock:self.calls.append((url,operation,model,payload.get('prompt')));self.inflight+=1;self.entered.notify_all()
        try:
            behaviour=self.behaviour.get((url,model),self.behaviour.get((None,model),'ok'))
            if behaviour in ('500','501'):raise urllib.error.HTTPError(url+operation,int(behaviour),'Internal Server Error',{},io.BytesIO(b'{"error":"model runner stopped"}'))
            if behaviour=='400':raise urllib.error.HTTPError(url+operation,400,'Bad Request',{},io.BytesIO(b'{"error":"invalid request"}'))
            if behaviour=='timeout':raise TimeoutError('timed out')
            if self.hold and payload.get('prompt')=='hold':assert self.release.wait(10)
            if operation=='/api/embed':return {'model':model,'embeddings':[[.1,.2]]}
            return {'done':True,'model':model,'response':'NOT READY' if behaviour=='not_ready' else 'READY'}
        finally:
            with self.lock:self.inflight-=1
    def generations(self,rid=None,prompt=None):
        return [c for c in self.calls if c[1]!='/api/embed' and (rid is None or c[0]==endpoint(rid)) and (prompt is None or c[3]==prompt)]
    def wait_inflight(self,n,timeout=10):
        with self.entered:return self.entered.wait_for(lambda:self.inflight>=n,timeout)


def lab(tmp_path,heads,*,models=('m:1','other:1'),clients=None,slots=None,memory=RAM,embed=False):
    transport=Lab();gateway=Gateway(tmp_path/'ledger.db',transport=transport);b=gateway.broker
    for app,(limit,priority) in (clients or {'capstone':(5,6)}).items():
        b.register_client(app,'t-'+app,models=list(models),max_concurrency=limit,queue_limit=32,priority=priority)
    for rid in heads:
        b.authorize_resource(rid,endpoint=endpoint(rid),resource_type='shared_inference',owner='infrastructure',applications=list(clients or ['capstone']),
                             permitted_uses=['inference'],slots=(slots or {}).get(rid,1),memory_gb=memory.get(rid) if memory else None)
        for model in models:
            if model=='all-minilm:latest':assert gateway.qualify(rid,model,operator='ops',purpose='embedding')['passed']
            else:assert gateway.qualify(rid,model,operator='ops')['passed']
    with b.connect() as db:recovery.store_policy(db,'failure_policy',{'ps_settle_seconds':0})
    transport.calls.clear();gateway.refresh_health()
    return gateway,transport


def body(key,model='m:1',nodes=None,prompt='x',deadline=30,operation='/api/generate'):
    payload={'input':['x']} if operation=='/api/embed' else {'prompt':prompt}
    return {'contract':'shared.inference/1','client_key':key,'model':model,'nodes':nodes or ['@authorized'],'deadline':time.time()+deadline,
            'operation':operation,'payload':payload}


def run(gateway,key,app='capstone',**kwargs):return gateway.execute(app,'t-'+app,body(key,**kwargs))


def leases(gateway):
    with gateway.broker.connect() as db:
        return [dict(r) for r in db.execute("SELECT request_id,app_id,resource_id,model FROM capacity_requests WHERE status IN ('leased','cancel_requested') AND app_id NOT LIKE 'maintenance:%'")]


def events(gateway,request_id=None):
    with gateway.broker.connect() as db:
        rows=db.execute('SELECT event_type,request_id,payload FROM capacity_events ORDER BY event_id').fetchall()
    return [(r['event_type'],json.loads(r['payload'])) for r in rows if request_id is None or r['request_id']==request_id]


def health(gateway,rid):
    with gateway.broker.connect() as db:return db.execute('SELECT health FROM capacity_resources WHERE resource_id=?',(rid,)).fetchone()[0]


def suspensions(gateway):
    with gateway.broker.connect() as db:return recovery.active(db)


def background(gateway,keys,**kwargs):
    threads=[threading.Thread(target=run,args=(gateway,key),kwargs=kwargs,daemon=True) for key in keys]
    for t in threads:t.start()
    return threads


# ---------------------------------------------------------------- 3. load balancing

def test_five_concurrent_requests_go_to_five_different_free_heads(tmp_path):
    heads=['head-192-0-2-%d'%n for n in (3,4,5,7,8)]
    gateway,transport=lab(tmp_path,heads)
    transport.hold=True
    threads=background(gateway,['c%d'%i for i in range(5)],prompt='hold')
    assert transport.wait_inflight(5)
    placed=leases(gateway)
    assert len(placed)==5 and {l['resource_id'] for l in placed}==set(heads) # five different heads, none queued
    transport.release.set()
    for t in threads:t.join(10)
    with gateway.broker.connect() as db:assert db.execute("SELECT count(*) FROM capacity_requests WHERE status='completed' AND app_id='capstone'").fetchone()[0]==5


def test_loaded_head_first_then_an_idle_head_where_the_model_fits(tmp_path):
    heads=['head-192-0-2-7','head-192-0-2-8','head-192-0-2-9']
    gateway,transport=lab(tmp_path,heads,slots={'head-192-0-2-7':2})
    # 0.4.3: the 0.4.0 order is now placement_policy.strategy loaded_first (default least_busy: tests/test_release_0_4_3.py).
    with gateway.broker.connect() as db:recovery.store_policy(db,'placement_policy',{'strategy':'loaded_first'})
    transport.loaded[endpoint('head-192-0-2-7')]=['m:1'];gateway.refresh_health() # /api/ps: m:1 resident on head 7 only
    transport.hold=True
    first=background(gateway,['a','b'],prompt='hold');assert transport.wait_inflight(2)
    assert [l['resource_id'] for l in leases(gateway)]==['head-192-0-2-7']*2 # both slots of the head that has it loaded
    third=background(gateway,['c'],prompt='hold');assert transport.wait_inflight(3) # head 7 full: no queueing behind it
    placed={l['resource_id'] for l in leases(gateway)}
    assert len(leases(gateway))==3 and placed-{'head-192-0-2-7'} and placed<=set(heads)
    tiers=[p.get('placement') for e,p in events(gateway) if e=='admitted']
    assert tiers==['loaded','loaded','fits']
    transport.release.set()
    for t in first+third:t.join(10)


def test_two_slots_on_a_31_gb_head_never_more_and_never_unload_a_model_in_use(tmp_path):
    rid='head-192-0-2-7';gateway,transport=lab(tmp_path,[rid],models=('big:1','mid:1'),slots={rid:2})
    b=gateway.broker;deadline=time.time()+60
    submit=lambda key,model:b.submit('capstone','t-capstone',key,model,['@authorized'],deadline)
    def place(request):
        with b.connect() as db:db.execute('UPDATE capacity_requests SET nodes=? WHERE request_id=?',(json.dumps([rid]),request['request_id']))
        return b.admit('capstone','t-capstone',request['request_id'])['status']
    big1=submit('big-1','big:1');assert place(big1)=='leased'              # 16 GiB x1.2 fits in 31.4 GiB
    mid=submit('mid-1','mid:1');assert place(mid)=='queued'                # 19.2 + 14.4 > 31.4: would unload big:1 in use
    big2=submit('big-2','big:1');assert place(big2)=='leased'              # same model, second slot
    big3=submit('big-3','big:1');assert place(big3)=='queued'              # never more than slots
    assert len(leases(gateway))==2
    with pytest.raises(Exception,match='slots exhausted'):                 # the ledger itself refuses a third lease
        with b.connect() as db:db.execute("UPDATE capacity_requests SET status='leased',resource_id=? WHERE request_id=?",(rid,big3['request_id']))
    b.finish('capstone','t-capstone',big1['request_id']);b.finish('capstone','t-capstone',big2['request_id'])
    assert b.admit('capstone','t-capstone',mid['request_id'])['status']=='leased' # nothing in use any more: it may load


def test_research_identity_stays_capped_on_multi_slot_heads(tmp_path):
    heads=['head-192-0-2-7','head-192-0-2-8','head-192-0-2-9']
    gateway,transport=lab(tmp_path,heads,clients={'capstone':(3,8),'capstone-research':(1,1)},slots={h:2 for h in heads})
    b=gateway.broker
    research=[b.submit('capstone-research','t-capstone-research','r%d'%i,'m:1',heads,time.time()+600) for i in range(10)]
    with b.connect() as db:db.execute("UPDATE capacity_requests SET created=? WHERE app_id='capstone-research'",(time.time()-120,))
    ops=[b.submit('capstone','t-capstone','o%d'%i,'m:1',heads,time.time()+600) for i in range(3)]
    for r in research+ops:b.admit(r['app_id'],'t-'+r['app_id'],r['request_id'])
    counts={}
    for l in leases(gateway):counts[l['app_id']]=counts.get(l['app_id'],0)+1
    assert counts=={'capstone-research':1,'capstone':3} # research holds max_concurrency leases (and so heads); row 8 unchanged


# ---------------------------------------------------------------- 1. failure classes

def test_a_4xx_reply_fails_only_the_request(tmp_path):
    rid='head-192-0-2-3';gateway,transport=lab(tmp_path,[rid])
    transport.behaviour[(endpoint(rid),'m:1')]='400'
    result=run(gateway,'bad')
    assert result['status']=='failed' and 'HTTP Error 400' in result['error']
    request=gateway.broker.get('capstone','t-capstone',result['request']['request_id'])
    assert request['status']=='cancelled' and request['executing']==0 # a definite answer: nothing left executing
    assert health(gateway,rid)=='healthy' and suspensions(gateway)==[]
    assert gateway.availability('capstone','t-capstone','m:1')['status']=='available'
    del transport.behaviour[(endpoint(rid),'m:1')]
    assert run(gateway,'good')['status']=='completed'
    assert not [e for e,_ in events(gateway) if e in ('resource.quarantined','resource.held','model.suspended','request.replaced')]


def test_a_5xx_on_head_3_suspends_only_that_model_there_and_the_request_completes_elsewhere(tmp_path):
    gateway,transport=lab(tmp_path,['head-192-0-2-3','head-192-0-2-7'])
    transport.loaded[endpoint('head-192-0-2-3')]=['m:1'];gateway.refresh_health() # head 3 is the first choice
    transport.behaviour[(endpoint('head-192-0-2-3'),'m:1')]='500'
    result=run(gateway,'one-request')
    rid=result['request']['request_id']
    assert result['status']=='completed' and result['result']['resource_id']=='head-192-0-2-7'
    assert [e for e,_ in events(gateway,rid)].count('completed')==1 and [e for e,_ in events(gateway,rid)].count('admitted')==2
    assert ('request.replaced',{'from_resource_id':'head-192-0-2-3','failure_class':'model_on_head','attempt':2}) in events(gateway,rid)
    assert [(s['scope'],s['resource_id'],s['model'],s['operation']) for s in suspensions(gateway)]==[('resource_model','head-192-0-2-3','m:1','completion')]
    assert health(gateway,'head-192-0-2-3')=='healthy'
    assert gateway.availability('capstone','t-capstone','m:1')['resources']=={'head-192-0-2-3':'model_suspended_on_resource','head-192-0-2-7':'available'}
    assert {r['resource_id']:r['models'] for r in gateway.models('capstone','t-capstone')['resources']}=={'head-192-0-2-3':['other:1'],'head-192-0-2-7':['m:1','other:1']}
    other=run(gateway,'other-model',model='other:1',nodes=['head-192-0-2-3'])
    assert other['status']=='completed' and other['result']['resource_id']=='head-192-0-2-3' # head 3 keeps serving other models
    assert len(transport.generations('head-192-0-2-3'))==2 and len(transport.generations('head-192-0-2-7'))==1


def test_the_same_model_failing_on_two_heads_is_suspended_everywhere_and_both_heads_stay(tmp_path):
    heads=['head-192-0-2-3','head-192-0-2-5','head-192-0-2-7']
    gateway,transport=lab(tmp_path,heads)
    for h in heads[:2]:transport.loaded[endpoint(h)]=['m:1'];transport.behaviour[(endpoint(h),'m:1')]='500'
    gateway.refresh_health()
    result=run(gateway,'cascade')
    assert result['status']=='failed' # second attempt also failed: one re-placement only
    scopes=sorted((s['scope'],s['resource_id']) for s in suspensions(gateway))
    assert scopes==[('model',None),('resource_model','head-192-0-2-3'),('resource_model','head-192-0-2-5')]
    assert gateway.availability('capstone','t-capstone','m:1')['status']=='model_suspended'
    assert all('m:1' not in r['models'] for r in gateway.models('capstone','t-capstone')['resources'])
    for h in heads:
        assert health(gateway,h)=='healthy' and run(gateway,'other-'+h,model='other:1',nodes=[h])['status']=='completed'
    doc=snapshot(gateway.broker.path,limit=1000)
    assert [(a['kind'],a['model'],a['reason_code']) for a in doc['alerts']]==[('model.suspended','m:1','model_suspended_everywhere')]
    assert [e for e,_ in events(gateway)].count('model.suspended')==3 and 'resource.quarantined' not in [e for e,_ in events(gateway)]


def test_an_unreachable_head_goes_offline_and_the_request_completes_on_another(tmp_path):
    gateway,transport=lab(tmp_path,['head-192-0-2-3','head-192-0-2-7'])
    transport.loaded[endpoint('head-192-0-2-3')]=['m:1'];gateway.refresh_health()
    original=transport.__call__
    def refuse_inference(url,operation,payload,timeout):
        if url==endpoint('head-192-0-2-3') and operation=='/api/generate':raise urllib.error.URLError(ConnectionRefusedError(10061,'refused'))
        return original(url,operation,payload,timeout)
    gateway.transport=refuse_inference
    result=run(gateway,'unreachable')
    assert result['status']=='completed' and result['result']['resource_id']=='head-192-0-2-7'
    assert health(gateway,'head-192-0-2-3')=='offline' and suspensions(gateway)==[]
    gateway.transport=transport;gateway.refresh_health()
    assert health(gateway,'head-192-0-2-3')=='healthy' # back when the heartbeat returns


# ---------------------------------------------------------------- 2. hold, probe, release or escalate

def test_a_timeout_after_dispatch_holds_the_head_until_the_deadline_then_a_probe_returns_it(tmp_path):
    rid='head-192-0-2-3';gateway,transport=lab(tmp_path,[rid,'head-192-0-2-7'])
    transport.loaded[endpoint(rid)]=['m:1'];gateway.refresh_health()
    transport.behaviour[(endpoint(rid),'m:1')]='timeout'
    result=run(gateway,'timed-out',deadline=20)
    assert result['status']=='failed' and health(gateway,rid)=='quarantined' # never replayed elsewhere
    assert len(transport.generations())==1
    [hold]=suspensions(gateway);deadline=result['request']['deadline']
    assert (hold['scope'],hold['state'],hold['failure_class'],hold['model'])==('resource','held','uncertain_execution','m:1') and hold['hold_until']==deadline
    assert [r['outcome'] for r in gateway.run_recovery(now=deadline-1)]==[] # not probed before the deadline
    # Even when due (e.g. an operator's probe now), a held head waits for the original deadline.
    with gateway.broker.connect() as db:db.execute('UPDATE capacity_suspensions SET next_probe=? WHERE suspension_id=?',(deadline-1,hold['suspension_id']))
    assert gateway.run_recovery(now=deadline-1)[0]['waiting']=='deadline' and len(transport.generations(rid))==1
    with gateway.broker.connect() as db:db.execute("UPDATE capacity_requests SET expires=? WHERE request_id=?",(time.time()-1,result['request']['request_id']))
    gateway.broker.reconcile_expired() # the uncertain lease expires: the hold is not raised again
    assert [e for e,_ in events(gateway)].count('resource.quarantined')==1
    del transport.behaviour[(endpoint(rid),'m:1')]
    # A model still resident (busy, or within its keep_alive) is not safe: look again later.
    assert gateway.run_recovery(now=deadline+61)[0]['waiting']=='runner_loaded' and len(transport.generations(rid))==1
    transport.loaded[endpoint(rid)]=[] # keep_alive ended: Ollama unloaded the idle runner
    [outcome]=gateway.run_recovery(now=deadline+61+15)
    assert outcome['outcome']=='recovered' and [p[0] for p in outcome['probes']]==['other:1','m:1'] # a small qualified model first, then the failed one
    assert health(gateway,rid)=='healthy' and suspensions(gateway)==[]
    assert {'resource.held','probe.passed','resource.recovered'}<={e for e,_ in events(gateway)}
    assert run(gateway,'after',nodes=[rid])['status']=='completed'


def test_three_failed_probes_set_needs_attention_and_raise_an_alert(tmp_path):
    rid='head-192-0-2-3';gateway,transport=lab(tmp_path,[rid])
    transport.behaviour[(endpoint(rid),'m:1')]='timeout'
    result=run(gateway,'timed-out',deadline=5);deadline=result['request']['deadline']
    with gateway.broker.connect() as db:db.execute("UPDATE capacity_requests SET expires=? WHERE request_id=?",(time.time()-1,result['request']['request_id']))
    transport.behaviour[(endpoint(rid),'other:1')]='not_ready' # the small-model probe keeps failing
    clock=deadline+61;outcomes=[]
    for wait in (300,900,0):
        [outcome]=gateway.run_recovery(now=clock);outcomes.append(outcome['outcome']);clock+=wait+1
        assert gateway.run_recovery(now=clock-2)==[] or wait==0 # nothing due before the backoff ends
    assert outcomes==['probe_failed','probe_failed','needs_attention']
    [item]=suspensions(gateway);assert item['state']=='needs_attention' and item['probes_failed']==3 and health(gateway,rid)=='quarantined'
    doc=snapshot(gateway.broker.path,limit=1000)
    [alert]=doc['alerts'];assert (alert['kind'],alert['resource_id'],alert['reason_code'])==('needs_attention',rid,'probes_exhausted')
    assert [e for e,_ in events(gateway)].count('probe.failed')==3 and 'resource.needs_attention' in [e for e,_ in events(gateway)]
    # The operator can still probe or release by hand.
    with pytest.raises(ValueError,match='confirmation'):recovery.release(gateway.broker,item['suspension_id'],'ops',confirmed=False)
    released=recovery.release(gateway.broker,item['suspension_id'],'ops',confirmed=True)
    assert released['resource_health']=='degraded' and suspensions(gateway)==[]
    gateway.refresh_health();assert health(gateway,rid)=='healthy'


def test_operator_commands_list_probe_and_release_print_json(tmp_path):
    rid='head-192-0-2-3';gateway,transport=lab(tmp_path,[rid,'head-192-0-2-7'])
    transport.loaded[endpoint(rid)]=['m:1'];gateway.refresh_health();transport.behaviour[(endpoint(rid),'m:1')]='500'
    assert run(gateway,'r')['status']=='completed'
    import os
    environment={k:v for k,v in os.environ.items() if k!='PYTHONPATH'};environment['PYTHONPATH']=str(ROOT/'src')
    cli=lambda *a:subprocess.run([sys.executable,'-m','inference_gateway.capacity_lifecycle',*a,'--database',str(gateway.broker.path)],env=environment,capture_output=True,text=True,timeout=60)
    listed=json.loads(cli('suspensions').stdout);[item]=listed['suspensions']
    assert (item['scope'],item['resource_id'],item['model'],item['state'])==('resource_model',rid,'m:1','suspended') and listed['alerts']==[]
    del transport.behaviour[(endpoint(rid),'m:1')]
    done=threading.Event()
    def service():
        while not done.is_set():gateway.run_recovery();time.sleep(.2) # stands in for the service's recovery thread
    worker=threading.Thread(target=service,daemon=True);worker.start()
    try:probed=json.loads(cli('probe','--suspension',item['suspension_id'],'--operator','ops','--wait-seconds','20').stdout)
    finally:done.set();worker.join(5)
    assert probed['probe']['passed'] and probed['suspension']['state']=='recovered' and not probed['pending']
    assert cli('release','--suspension',item['suspension_id'],'--operator','ops').returncode==2 # --confirmed required
    gone=cli('release','--suspension',item['suspension_id'],'--operator','ops','--confirmed')
    assert gone.returncode==1 and 'already recovered' in json.loads(gone.stdout)['error']


# ---------------------------------------------------------------- the lab incident

def test_replay_2026_11_11_gemma3_270m_failing_on_head_3_then_head_5(tmp_path):
    """18:33:44 gemma3:270m fails fast on head 3; 18:35:07 again on head 5 (0.3.0: both heads quarantined twice)."""
    heads=sorted(RAM)
    gateway,transport=lab(tmp_path,heads,models=('gemma3:270m','all-minilm:latest','qwen3.5:4b'),
                          clients={'capstone':(1,6),'capstone-research':(1,2)})
    transport.loaded[endpoint('head-192-0-2-3')]=['gemma3:270m'];gateway.refresh_health()
    research=lambda key,**k:run(gateway,key,app='capstone-research',**k)
    transport.behaviour[(endpoint('head-192-0-2-3'),'gemma3:270m')]='500'
    first=research('18:33:44',model='gemma3:270m')
    assert first['status']=='completed' and first['result']['resource_id']!='head-192-0-2-3' # re-placed, same request ID
    assert research('18:33:45',model='all-minilm:latest',operation='/api/embed',nodes=['head-192-0-2-3'])['status']=='completed'
    transport.loaded[endpoint('head-192-0-2-5')]=['gemma3:270m'];gateway.refresh_health() # 18:34:54: work moved to head 5
    transport.behaviour[(endpoint('head-192-0-2-5'),'gemma3:270m')]='500'
    second=research('18:35:07',model='gemma3:270m')
    assert second['status']=='failed' # its second head would need a model now suspended everywhere: no hidden retry
    with gateway.broker.connect() as db:db.execute("UPDATE capacity_requests SET expires=? WHERE status IN ('leased','cancel_requested')",(time.time()-1,))
    gateway.broker.reconcile_expired() # 18:34:45 / 18:36:07 lease expiry: nothing is executing, so no second quarantine
    for h in ('head-192-0-2-3','head-192-0-2-5'):
        assert health(gateway,h)=='healthy'
        assert research('minilm-'+h,model='all-minilm:latest',operation='/api/embed',nodes=[h])['status']=='completed'
        assert run(gateway,'qwen-'+h,model='qwen3.5:4b',nodes=[h])['status']=='completed'
    assert gateway.availability('capstone-research','t-capstone-research','gemma3:270m')['status']=='model_suspended'
    names=[e for e,_ in events(gateway)]
    assert 'resource.quarantined' not in names and 'lease.expired' not in names
    doc=snapshot(gateway.broker.path,limit=1000)
    assert [(r['resource_id'],r['health']) for r in doc['resources'] if r['health']!='healthy']==[]
    assert {(s['scope'],s['resource_id'],s['model'],s['state']) for s in doc['suspensions']}=={
        ('model',None,'gemma3:270m','suspended'),('resource_model','head-192-0-2-3','gemma3:270m','suspended'),
        ('resource_model','head-192-0-2-5','gemma3:270m','suspended')}
    assert [a['kind'] for a in doc['alerts']]==['model.suspended']


# ---------------------------------------------------------------- policy

def test_example_lab_policy_applies_and_a_head_reimport_keeps_capacity(tmp_path):
    from inference_gateway.capacity_bootstrap import apply
    from inference_gateway import head_inventory
    example=ROOT/'config'/'policy.lab-example.json';policy=json.loads(example.read_text(encoding='utf-8'))
    result=apply(example,tmp_path/'ledger.db')
    assert result['resources']==9 and result['clients']==0 and result['capacity']['head-192-0-2-2']=={'slots':1,'memory_gb':63.9}
    assert result['failure_policy']['probe_backoff_seconds']==[60,300,900] and result['placement_policy']['loaded_fresh_seconds']==30
    gateway=Gateway(tmp_path/'ledger.db')
    with gateway.broker.connect() as db:
        assert {r['resource_id']:(r['slots'],r['memory_gb']) for r in db.execute('SELECT * FROM capacity_resource_capacity')}=={
            r['resource_id']:(r['slots'],r['memory_gb']) for r in policy['resources']}
    for bad in ({'probe_backoff_seconds':[]},{'cascade_heads':1},{'unknown':1}):
        with pytest.raises(ValueError):recovery.validate_policy('failure_policy',bad)
    with pytest.raises(ValueError,match='slots'):gateway.broker.authorize_resource('head-192-0-2-9',endpoint='http://192.0.2.9:11434',resource_type='shared_inference',
        owner='infrastructure',applications=['capstone'],permitted_uses=['inference'],slots=0)
    # Re-importing Arena's verified inventory without capacity keeps each head's slots and memory_gb.
    inventory={'schema':head_inventory.SCHEMA,'heads':[{'resource_id':'head-192-0-2-7','arena_node_id':'head-192-0-2-7','address':'192.0.2.7',
               'endpoint':'http://192.0.2.7:11434','verified':{'ollama_version':'0.40.13'}}]}
    accepted,refused=head_inventory.resources(inventory,['capstone']);merged=head_inventory.merge(policy,accepted)
    seven=next(r for r in merged['resources'] if r['resource_id']=='head-192-0-2-7')
    assert not refused and (seven['slots'],seven['memory_gb'],seven['applications'])==(1,31.4,['capstone'])
    inventory['heads'][0]['capacity']={'slots':2,'memory_gb':30}
    seven=next(r for r in head_inventory.merge(policy,head_inventory.resources(inventory,['capstone'])[0])['resources'] if r['resource_id']=='head-192-0-2-7')
    assert (seven['slots'],seven['memory_gb'])==(2,30)


# ---------------------------------------------------------------- 4. observation, 5. upgrade

V030_TOP={'contract','observed_at','service_id','ledger_id','admission_open','applications','resources','requests','invocations','leases',
          'qualification_observations','events','next_event_id','last_hour','inventory','scope'}
V030_RESOURCE={'resource_id','resource_type','qualified_models','health','last_seen','fresh'}


def test_observation_keeps_every_0_3_0_field_and_adds_capacity_suspensions_alerts(tmp_path):
    gateway,transport=lab(tmp_path,['head-192-0-2-7'],slots={'head-192-0-2-7':2})
    transport.loaded[endpoint('head-192-0-2-7')]=['m:1'];gateway.refresh_health()
    doc=snapshot(gateway.broker.path,limit=1000)
    # 0.4.1 adds gateway_load (listener/scheduler counters; null until the service publishes them).
    assert doc['contract']=='shared.inference.observation/1' and set(doc)==V030_TOP|{'suspensions','alerts','gateway_load'} and doc['gateway_load'] is None
    [resource]=doc['resources'];assert set(resource)==V030_RESOURCE|{'capacity'}
    capacity=resource['capacity']
    assert (capacity['slots'],capacity['slots_in_use'],capacity['memory_gb'],capacity['loaded_fresh'])==(2,0,31.4,True)
    assert capacity['loaded_models']==[{'name':'m:1','digest':'DIGEST-m:1','size_bytes':int(SIZES['m:1']*1.1)}]
    assert not {'endpoint','error','result','payload_hash','client_key','token_hash'}&set(json.dumps(doc).split('"'))


def _build_030_ledger(tmp_path):
    wheel=ROOT/'release'/'inference-gateway-0.3.0'/'wheel'/'inference_gateway-0.3.0-py3-none-any.whl'
    if not wheel.is_file():pytest.skip('0.3.0 release wheel not present')
    code=tmp_path/'gateway-0.3.0';zipfile.ZipFile(wheel).extractall(code)
    script=tmp_path/'make_030.py'
    script.write_text('''
import json,sys,time
sys.path.insert(0,sys.argv[1])
import inference_gateway
from inference_gateway.capacity_gateway import Gateway
assert inference_gateway.__version__=="0.3.0"
def head(url,operation,payload,timeout):
    if operation=="/api/version":return {"version":"0.40.13"}
    if operation=="/api/tags":return {"models":[{"name":n,"digest":"DIGEST-"+n,"size":1} for n in ("gemma3:270m","all-minilm:latest")]}
    if operation=="/api/show":return {"capabilities":["embedding"] if payload["model"]=="all-minilm:latest" else ["completion"]}
    if operation=="/api/embed":return {"model":payload["model"],"embeddings":[[.1,.2]]}
    return {"done":True,"model":payload["model"],"response":"READY"}
g=Gateway(sys.argv[2],transport=head);b=g.broker
b.register_client("capstone-research","t-capstone-research",models=["gemma3:270m","all-minilm:latest"],max_concurrency=1,queue_limit=32,priority=2)
heads=["head-192-0-2-%d"%n for n in (3,4,5,6,7)]
for n,rid in enumerate(heads):
    b.authorize_resource(rid,endpoint="http://127.0.0.1:%d"%(20000+int(rid.rsplit("-",1)[1])),resource_type="shared_inference",owner="infrastructure",
                         applications=["capstone-research"],permitted_uses=["inference"])
    assert g.qualify(rid,"gemma3:270m",operator="ops")["passed"] and g.qualify(rid,"all-minilm:latest",operator="ops",purpose="embedding")["passed"]
for rid in heads[:4]:
    r=b.submit("capstone-research","t-capstone-research","lost-"+rid,"gemma3:270m",[rid],time.time()+60)
    b.admit("capstone-research","t-capstone-research",r["request_id"]);b.executing("capstone-research","t-capstone-research",r["request_id"])
    with b.connect() as db:db.execute("UPDATE capacity_requests SET expires=? WHERE request_id=?",(time.time()-1,r["request_id"]))
    b.reconcile_expired()
with b.connect() as db:
    print(json.dumps({"ledger_id":db.execute("SELECT value FROM capacity_service_policy WHERE key='ledger_id'").fetchone()[0],
                      "health":{r[0]:r[1] for r in db.execute("SELECT resource_id,health FROM capacity_resources")}}))
''')
    return code,script


def test_upgrade_from_a_0_3_0_ledger_keeps_its_id_and_probes_the_quarantined_heads(tmp_path):
    code,script=_build_030_ledger(tmp_path);ledger=tmp_path/'capacity-ledger.db'
    made=subprocess.run([sys.executable,'-I',str(script),str(code),str(ledger)],capture_output=True,text=True,timeout=120)
    assert made.returncode==0,made.stderr
    before=json.loads(made.stdout)
    assert [h for h,v in sorted(before['health'].items()) if v=='quarantined']==['head-192-0-2-3','head-192-0-2-4','head-192-0-2-5','head-192-0-2-6']
    transport=Lab()
    transport.behaviour[(endpoint('head-192-0-2-3'),'gemma3:270m')]='500';transport.behaviour[(endpoint('head-192-0-2-5'),'gemma3:270m')]='500'
    began=time.time();gateway=Gateway(ledger,transport=transport) # 0.4.0 opens the 0.3.0 ledger
    with gateway.broker.connect() as db:
        report=json.loads(db.execute("SELECT value FROM capacity_service_policy WHERE key='upgrade_report:0.4.0'").fetchone()[0])
        assert db.execute("SELECT value FROM capacity_service_policy WHERE key='ledger_id'").fetchone()[0]==before['ledger_id']
        recovery.store_policy(db,'failure_policy',{'ps_settle_seconds':0})
    assert report['ledger_id']==before['ledger_id'] and [h['resource_id'] for h in report['held']]==['head-192-0-2-3','head-192-0-2-4','head-192-0-2-5','head-192-0-2-6']
    assert all(h['failed_model']=='gemma3:270m' and not h['active_reservations'] and h['first_probe_not_before']>=began+60 for h in report['held'])
    assert {(s['scope'],s['state'],s['source']) for s in suspensions(gateway)}=={('resource','held','upgrade')}
    assert gateway.run_recovery(now=time.time())==[] # first probe only after the first backoff
    outcomes={o['resource_id']:o for o in gateway.run_recovery(now=time.time()+61)}
    assert {h:o['outcome'] for h,o in outcomes.items()}=={h:'recovered' for h in ('head-192-0-2-3','head-192-0-2-4','head-192-0-2-5','head-192-0-2-6')}
    assert all([p[0] for p in o['probes']]==['all-minilm:latest','gemma3:270m'] for o in outcomes.values())
    assert 'model_suspension' in outcomes['head-192-0-2-3'] and 'model_suspension' in outcomes['head-192-0-2-5']
    assert {h:health(gateway,h) for h in ('head-192-0-2-3','head-192-0-2-4','head-192-0-2-5','head-192-0-2-6')}=={h:'healthy' for h in ('head-192-0-2-3','head-192-0-2-4','head-192-0-2-5','head-192-0-2-6')}
    assert ('model',None,'gemma3:270m') in {(s['scope'],s['resource_id'],s['model']) for s in suspensions(gateway)} # 3 and 5: cascade
    # Rollback: 0.3.0 starts on the upgraded ledger (it recreates its index) and admits work.
    check=tmp_path/'open_030.py'
    check.write_text('import sys,time\nsys.path.insert(0,sys.argv[1])\nfrom inference_gateway.capacity_gateway import Gateway\n'
                     'b=Gateway(sys.argv[2]).broker\nr=b.submit("capstone-research","t-capstone-research","after-rollback","all-minilm:latest",["head-192-0-2-4"],time.time()+60)\n'
                     'print(b.admit("capstone-research","t-capstone-research",r["request_id"])["status"])\n')
    rolled=subprocess.run([sys.executable,'-I',str(check),str(code),str(ledger)],capture_output=True,text=True,timeout=60)
    assert rolled.returncode==0 and rolled.stdout.strip()=='leased',rolled.stderr
