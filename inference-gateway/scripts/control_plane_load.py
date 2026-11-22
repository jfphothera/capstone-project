"""Control-plane before/after measurement (release 0.4.6, items G1 and G3). No network: fake heads, in-process Gateway.

One fixed, seeded workload, run once per source tree (`--src`, e.g. the 0.4.5 snapshot and the 0.4.6 src), so the
two reports compare the same work:

  ledger   a lab-shaped ledger: 9 heads (head 6 small), 36 models authorized for the research identity, 6 installed
           and qualified (qwen3.5:4b declared 4 per head, the qwen3:4b reviewer, gemma3:4b, llama3.2:3b, qwen3.5:9b,
           all-minilm), capability evidence, unmeasured_model_limit 1, and --history old completed requests.
  launch   G1: --launch concurrent worker launches, each doing what Capstone rc.24 does before a trial
           (ai_settings.health: models, policy, availability for every advertised model). On a tree that has them,
           the 0.4.6 sequence is measured too (policy without availability, one scoped fleet snapshot).
           Reports calls per launch and p50/p95 per call, and the Gateway's internal availability calculations.
  admit    G3: --workers callers for --seconds, each alternating a main-model call and a reviewer call through
           execute(asynchronous) and status polling, with an availability check per call (Capstone's default), while
           a health cycle runs every --health-interval s. Fake generations take 0.3-1.2 s. Reports completions,
           admission and execution p50/p95, write-lock holds and waits per transaction label (p50/p95/max), placement
           passes with and without a write, and health-cycle duration and ledger wait.

    python scripts/control_plane_load.py --src <0.4.5 src> --out verification/control-plane-0.4.5.json
    python scripts/control_plane_load.py --src src --out verification/control-plane-0.4.6.json
"""
import argparse,collections,hashlib,json,os,random,sqlite3,statistics,sys,tempfile,threading,time,uuid
from pathlib import Path

HEADS={'head-192-0-2-%d'%n:{'url':'http://127.0.0.1:%d'%(22000+n),'slots':4 if n==6 else 8,'memory':7.4 if n==6 else 15.4 if n in (3,4,5) else 31.4}
       for n in range(2,11)}
MAIN,REVIEWER='qwen3.5:4b','qwen3:4b'
INSTALLED={MAIN:('qwen35',3.4),REVIEWER:('qwen3',2.6),'gemma3:4b':('gemma3',3.3),'llama3.2:3b':('llama',2.0),'qwen3.5:9b':('qwen35',6.6),
           'all-minilm:latest':('bert',0.05)}
SMALL_HEAD_MODELS={'all-minilm:latest','llama3.2:3b'} # head 6 (8 GB) never serves the 4B-class work
AUTHORIZED=sorted(INSTALLED)+['catalog-%02d:1'%i for i in range(30)] # 36, as in c922
GIB=1024**3


def pct(values,f):
    values=sorted(values)
    return round(values[min(len(values)-1,int(len(values)*f))],2) if values else None


def summary(values):
    return {'n':len(values),'p50':pct(values,.5),'p95':pct(values,.95),'max':round(max(values),2) if values else None}


class Heads:
    """Fake Ollama heads. Generations sleep 0.3-1.2 s (seeded by the request), so admission has real queueing."""
    def __init__(self):
        self.lock=threading.Lock();self.active=collections.Counter();self.peak=collections.Counter();self.calls=0
    def models(self,url):
        small=url.endswith(':22006')
        return [m for m in INSTALLED if not small or m in SMALL_HEAD_MODELS]
    def __call__(self,url,operation,payload,timeout):
        if operation=='/api/version':return {'version':'0.40.15'}
        if operation=='/api/tags':
            return {'models':[{'name':m,'digest':'sha256:'+hashlib.sha256(m.encode()).hexdigest(),'size':int(INSTALLED[m][1]*GIB),
                               'details':{'family':INSTALLED[m][0],'families':[INSTALLED[m][0]]}} for m in self.models(url)]}
        if operation=='/api/ps':return {'models':[]}
        if operation=='/api/show':
            m=payload['model'];caps=['embedding'] if m=='all-minilm:latest' else ['completion','tools']
            return {'capabilities':caps,'details':{'family':INSTALLED[m][0]},'model_info':{'general.architecture':INSTALLED[m][0]},'license':'x'*8000}
        m=payload['model']
        if operation=='/api/embed':return {'model':m,'embeddings':[[.1,.2]]}
        prompt=payload.get('prompt') or json.dumps(payload.get('messages'))
        if operation=='/api/chat' and payload.get('tools'):
            return {'done':True,'model':m,'message':{'role':'assistant','content':'','tool_calls':[{'function':{'name':'record_value','arguments':{'value':42}}}]}}
        if operation=='/api/generate' and payload.get('format'):return {'done':True,'model':m,'response':'{"status": "READY", "count": 3}','eval_count':5}
        if not prompt.startswith('Return exactly') and not prompt.startswith('Count from'):
            with self.lock:self.active[url]+=1;self.peak[url]=max(self.peak[url],self.active[url]);self.calls+=1
            try:time.sleep(random.Random(prompt).uniform(.3,1.2))
            finally:
                with self.lock:self.active[url]-=1
        if operation=='/api/chat':return {'done':True,'model':m,'message':{'role':'assistant','content':'READY'},'eval_count':3}
        return {'done':True,'model':m,'response':'READY','eval_count':3,'done_reason':'stop'}


def build(folder,history,result_kb):
    from inference_gateway.capacity_gateway import Gateway
    from inference_gateway import recovery
    heads=Heads();path=Path(folder)/'ledger.db';gateway=Gateway(path,transport=heads);b=gateway.broker
    b.register_client('capstone-research','t-research',models=AUTHORIZED,max_concurrency=64,queue_limit=512,priority=6)
    b.register_client('lab-traffic','t-traffic',models=[MAIN],max_concurrency=4,queue_limit=8,priority=1)
    for rid,h in HEADS.items():
        digest='sha256:'+hashlib.sha256(MAIN.encode()).hexdigest()
        b.authorize_resource(rid,endpoint=h['url'],resource_type='shared_inference',owner='infrastructure',applications=['capstone-research','lab-traffic'],
                             permitted_uses=['inference'],slots=h['slots'],memory_gb=h['memory'],
                             model_capacity=None if rid.endswith('-6') else {MAIN:{'parallel':4,'digest':digest,'runtime_version':'0.40.15','num_ctx':8192,'evidence':'matrix'}})
        for m in heads.models(h['url']):
            if m=='all-minilm:latest':gateway.qualify(rid,m,operator='ops',purpose='embedding')
            else:
                gateway.qualify(rid,m,operator='ops',max_context=8192)
                for purpose in ('tools','structured_output'):gateway.qualify(rid,m,operator='ops',purpose=purpose)
    with b.write('setup') as db:
        recovery.store_policy(db,'failure_policy',{'ps_settle_seconds':0})
        recovery.store_policy(db,'placement_policy',{'unmeasured_model_limit':1})
    gateway.refresh_health()
    blob=json.dumps({'response':{'model':MAIN,'response':'w '*(result_kb*512),'done':True},'provenance':{'x':'y'*200}})
    now=time.time();rng=random.Random(7);rows=[];inv=[];ev=[];times=[]
    for i in range(history):
        rid='REQ-'+uuid.UUID(int=rng.getrandbits(128)).hex;at=now-86400*10+i*40;head=rng.choice(list(HEADS))
        rows.append((rid,'capstone-research','h%d'%i,MAIN,'[]','completed',at,at+300,'LEASE-'+rid[4:],head,at+60,0,'authorized_dynamic'))
        inv.append((rid,'capstone-research','h','owner','completed',blob,None,at+30,at+5));times.append((rid,at+1,at+30))
        for e in ('queued','admitted','completed'):ev.append(('capstone-research',rid,e,'{}',at))
    with sqlite3.connect(path) as db:
        db.executemany('INSERT INTO capacity_requests(request_id,app_id,client_key,model,nodes,status,created,deadline,lease_id,resource_id,expires,executing,node_policy) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',rows)
        db.executemany('INSERT INTO capacity_invocations(request_id,app_id,payload_hash,execution_owner,state,result,error,updated,started_at) VALUES(?,?,?,?,?,?,?,?,?)',inv)
        db.executemany('INSERT INTO capacity_request_times VALUES(?,?,?)',times)
        db.executemany('INSERT INTO capacity_events(app_id,request_id,event_type,payload,at) VALUES(?,?,?,?,?)',ev)
    return gateway,heads,path


class Recorder:
    """Every write transaction's wait and hold per label (wraps the 0.4.4+ gate's record())."""
    def __init__(self,gateway):
        self.samples=collections.defaultdict(lambda:{'wait':[],'hold':[],'timeouts':0});self.lock=threading.Lock()
        gate=gateway.broker.gate;real=gate.record
        def record(label,wait,hold,*,timeout=False,external=0):
            with self.lock:
                s=self.samples[label]
                if timeout:s['timeouts']+=1
                else:s['wait'].append(wait*1000);s['hold'].append(hold*1000)
            return real(label,wait,hold,timeout=timeout,external=external)
        gate.record=record
    def report(self):
        with self.lock:
            return {label:{'transactions':len(s['hold']),'timeouts':s['timeouts'],'hold_ms':summary(s['hold']),'wait_ms':summary(s['wait']),
                           'hold_ms_total':round(sum(s['hold']))} for label,s in sorted(self.samples.items())}


class Counting:
    """Counts the Gateway's internal availability calculations (per head and model) during a phase."""
    def __init__(self,gateway):
        self.n=0;self.lock=threading.Lock();cls=type(gateway)
        name='_reason_in' if hasattr(cls,'_reason_in') else '_reason'
        real=getattr(cls,name)
        def wrapped(*a,**k):
            with self.lock:self.n+=1
            return real(*a,**k)
        setattr(gateway,name,lambda *a,**k:wrapped(gateway,*a,**k))


def launch_storm(gateway,launches):
    """G1: concurrent worker launches, each the rc.24 preflight sequence (and, where served, the 0.4.6 one)."""
    app,token='capstone-research','t-research';out={}
    sequences={'rc24_preflight':None}
    if hasattr(gateway,'fleet'):sequences['gateway_0_4_6_scoped']=None
    for name in sequences:
        timings=collections.defaultdict(list);lock=threading.Lock();calls=[0];counter=Counting(gateway);start=threading.Barrier(launches)
        def timed(kind,fn):
            t=time.perf_counter();value=fn()
            with lock:timings[kind].append((time.perf_counter()-t)*1000);calls[0]+=1
            return value
        def launch():
            start.wait()
            if name=='rc24_preflight':
                # ai_settings.health: models, policy, then availability for each advertised model (completion models with
                # their qualified capabilities); then gateway_transport.invoke's availability check before the first call.
                inventory=timed('/v1/models',lambda:gateway.models(app,token));timed('/v1/policy',lambda:gateway.policy(app,token))
                advertised={}
                for r in inventory['resources']:
                    for m in r['models']:advertised.setdefault(m,r['model_details'][m])
                for m,d in sorted(advertised.items()):
                    op='/api/embed' if 'embedding' in d['qualified_operations'] and 'completion' not in d['qualified_operations'] else '/api/generate'
                    timed('/v1/availability',lambda:gateway.availability(app,token,m,op))
                timed('/v1/availability',lambda:gateway.availability(app,token,MAIN,'/api/chat',num_ctx=8192))
            else:
                # 0.4.6 contract: the operator policy alone, and one fleet snapshot scoped to the trial's two models.
                timed('/v1/policy?availability=0',lambda:gateway.policy(app,token,include_availability=False))
                timed('/v1/fleet',lambda:gateway.fleet(app,token,models=[MAIN,REVIEWER],operations=['/api/chat'],num_ctx=8192))
        began=time.perf_counter()
        pool=[threading.Thread(target=launch) for _ in range(launches)]
        for p in pool:p.start()
        for p in pool:p.join()
        wall=(time.perf_counter()-began)*1000
        out[name]={'launches':launches,'api_calls':calls[0],'api_calls_per_launch':round(calls[0]/launches,1),
                   'internal_availability_calculations':counter.n,'per_launch_internal_calculations':round(counter.n/launches,1),
                   'wall_ms':round(wall),'per_call_ms':{k:summary(v) for k,v in sorted(timings.items())},
                   'all_calls_ms':summary([x for v in timings.values() for x in v])}
    return out


def admission(gateway,heads,workers,seconds,health_interval):
    """G3: a fixed mix through execute/status with a health cycle running; seeded, so both trees get the same work."""
    app,token='capstone-research','t-research';stop=time.monotonic()+seconds;lock=threading.Lock()
    done=[];failed=collections.Counter();health=[];recorder=Recorder(gateway);broker=gateway.broker
    stats_before=dict(getattr(broker,'place_stats',{}))
    def worker(i):
        n=0
        while time.monotonic()<stop:
            model=MAIN if n%2==0 else REVIEWER;n+=1
            key='w%02d-%04d'%(i,n);body={'contract':'shared.inference/1','client_key':key,'model':model,'nodes':['@authorized'],
                                        'operation':'/api/chat','deadline':time.time()+120,
                                        'payload':{'messages':[{'role':'user','content':'work %s'%key}],'options':{'num_ctx':8192,'num_predict':64}}}
            try:
                gateway.availability(app,token,model,'/api/chat',num_ctx=8192) # Capstone's check_availability=True
                result=gateway.execute(app,token,body,asynchronous=True);rid=result['request']['request_id'];delay=.25
                while result['status'] in ('active','queued','leased'):
                    time.sleep(delay);delay=min(2,delay*1.5);result=gateway.result(app,token,rid)
                with lock:
                    if result['status']=='completed':
                        t=result;done.append({'model':model,'admission_s':t['admitted_at']-t['submitted_at'],
                                              'execution_s':t['execution_finished_at']-t['execution_started_at'],
                                              'total_s':t['completed_at']-t['submitted_at']})
                    else:failed[result['status']+': '+str(result.get('error'))[:60]]+=1
            except Exception as exc:
                with lock:failed[type(exc).__name__+': '+str(exc)[:60]]+=1
    def monitor():
        while time.monotonic()<stop:
            t=time.perf_counter()
            try:gateway.refresh_health()
            except Exception as exc:failed['health: '+type(exc).__name__]+=1
            health.append((time.perf_counter()-t)*1000)
            try:gateway.publish_counters()
            except Exception:pass
            time.sleep(health_interval)
    pool=[threading.Thread(target=worker,args=(i,),daemon=True) for i in range(workers)]+[threading.Thread(target=monitor,daemon=True)]
    for p in pool:p.start()
    for p in pool:p.join(seconds+180)
    by=lambda m:[d for d in done if d['model']==m]
    stats_after=dict(getattr(broker,'place_stats',{}))
    ledger=recorder.report()
    return {'workers':workers,'seconds':seconds,'health_interval_s':health_interval,'completed':len(done),'completed_per_minute':round(len(done)*60/seconds,1),
            'failed':dict(failed),'per_model':{m:{'completed':len(by(m)),'admission_s':summary([d['admission_s'] for d in by(m)]),
                                                  'execution_s':summary([d['execution_s'] for d in by(m)])} for m in (MAIN,REVIEWER)},
            'admission_s':summary([d['admission_s'] for d in done]),'execution_s':summary([d['execution_s'] for d in done]),
            'total_s':summary([d['total_s'] for d in done]),
            'health_cycle_ms':summary(health),'head_peak_concurrency':dict(heads.peak),
            'ledger':ledger,'ledger_hold_ms_total':round(sum(v['hold_ms_total'] for v in ledger.values())),
            'place_transactions':ledger.get('place',{}).get('transactions',0),
            'placement_passes':{k:stats_after.get(k,0)-stats_before.get(k,0) for k in stats_after} or None,
            'scheduler_passes':gateway.scheduler.passes}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--src',required=True);p.add_argument('--history',type=int,default=20000);p.add_argument('--result-kb',type=int,default=4)
    p.add_argument('--launch',type=int,default=24);p.add_argument('--workers',type=int,default=24);p.add_argument('--seconds',type=float,default=45)
    p.add_argument('--health-interval',type=float,default=2.0);p.add_argument('--out');a=p.parse_args(argv)
    sys.path.insert(0,str(Path(a.src).resolve()))
    import inference_gateway
    random.seed(46)
    folder=tempfile.mkdtemp(prefix='control-plane-')
    t=time.perf_counter();gateway,heads,path=build(folder,a.history,a.result_kb);built=time.perf_counter()-t
    report={'src':str(Path(a.src).resolve()),'release':inference_gateway.__version__,'python':sys.version.split()[0],
            'workload':{'heads':len(HEADS),'authorized_models':len(AUTHORIZED),'installed_models':len(INSTALLED),'history_requests':a.history,
                        'launches':a.launch,'workers':a.workers,'seconds':a.seconds,'health_interval_s':a.health_interval,'seed':46},
            'ledger_bytes':os.path.getsize(path),'build_s':round(built,1)}
    report['launch_storm']=launch_storm(gateway,a.launch)
    report['admission']=admission(gateway,heads,a.workers,a.seconds,a.health_interval)
    text=json.dumps(report,indent=2);print(text)
    if a.out:Path(a.out).write_text(text,encoding='utf-8')


if __name__=='__main__':main()
