"""Whole request lifecycle under concurrency (0.4.4): N ticket workflows against one Gateway.

In-process Gateway over fake Ollama heads with different speeds and slots, two models (one in the
qwen35 serial family, one not), Arena-declared capacity on some heads, short leases so renewal
runs constantly, the recovery and health cycles running, a few client cancellations, and an
injector that holds the ledger write lock from another connection for 0.2-1.2 s every 0.5-1.5 s
(transient contention from outside the Gateway). Each workflow runs `calls` sequential calls:
submit, poll status until terminal, retry with the same client_key on LedgerBusy (the HTTP 503).

Checks: no exception escapes, no call dispatched twice, no finished reply without a completed
result, no lease expiry or head hold, per-model head limits respected. Reports queue time,
latency, throughput and the ledger's write-lock statistics.

    python scripts/lifecycle_load.py --workflows 12 16 20 --out verification/lifecycle-0.4.4.json
"""
import argparse,functools,json,random,sqlite3,statistics,sys,tempfile,threading,time
from datetime import datetime,timezone
from pathlib import Path

HEADS={'head-192-0-2-%d'%n:{'endpoint':'http://127.0.0.1:%d'%(22000+n),'slots':s,'latency':l}
       for n,s,l in ((2,4,.08),(3,2,.25),(4,1,.6),(5,4,.15),(6,1,1.0),(7,2,.35))}
MODELS={'qwen3.5:4b':'qwen35','gemma3:4b':'gemma3'}
RUNTIME='0.40.15'


class Heads:
    """Fake Ollama 0.40.15 heads: per-head latency, Ollama-shaped replies, dispatch and overlap accounting."""
    def __init__(self):
        self.lock=threading.Lock();self.dispatched={};self.replied=set();self.inflight={};self.peak={}
        self.by_url={h['endpoint']:(rid,h) for rid,h in HEADS.items()}
    def __call__(self,url,operation,payload,timeout):
        rid,head=self.by_url[url]
        if operation=='/api/version':return {'version':RUNTIME}
        if operation=='/api/tags':return {'models':[{'name':m,'digest':'D-'+m,'size':3*1024**3,'details':{'family':f,'families':[f]}} for m,f in MODELS.items()]}
        if operation=='/api/show':return {'capabilities':['completion']}
        if operation=='/api/ps':return {'models':[]}
        model=payload['model'];prompt=payload.get('prompt')
        if prompt=='Return exactly the word READY.':return {'done':True,'model':model,'response':'READY','eval_count':3}
        key=(rid,model)
        with self.lock:
            self.dispatched[prompt]=self.dispatched.get(prompt,0)+1
            self.inflight[key]=self.inflight.get(key,0)+1;self.peak[key]=max(self.peak.get(key,0),self.inflight[key])
        try:
            started=time.time();time.sleep(min(timeout,head['latency']*random.uniform(.7,1.3)))
            if time.time()-started>=timeout:raise TimeoutError('timed out')
            took=int((time.time()-started)*1e9)
            reply={'done':True,'model':model,'response':'ok','eval_count':20,'prompt_eval_count':50,'created_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
                   'total_duration':took,'load_duration':took//10,'prompt_eval_duration':took//5,'eval_duration':took-took//10-took//5}
            with self.lock:self.replied.add(prompt)
            return reply
        finally:
            with self.lock:self.inflight[key]-=1


def pct(values,f):
    values=sorted(values)
    return round(values[min(len(values)-1,int(len(values)*f))],1) if values else None


def run(workflows,*,calls=4,cancel_rate=.05,inject=True,work=None,seed=1):
    from inference_gateway import capacity_broker,capacity_gateway
    from inference_gateway.capacity_gateway import Gateway
    from inference_gateway.capacity_broker import LedgerBusy
    random.seed(seed)
    folder=Path(work or tempfile.mkdtemp(prefix='lifecycle-'));folder.mkdir(parents=True,exist_ok=True)
    old_lease,old_renew=capacity_broker.LEASE_SECONDS,capacity_gateway.renew_until_stopped
    capacity_broker.LEASE_SECONDS=3 # renewals every 0.5 s against a 3 s lease: renewal is always in play
    capacity_gateway.renew_until_stopped=functools.partial(old_renew,interval=.5)
    logs=[];heads=Heads()
    try:
        gateway=Gateway(folder/'ledger.db',transport=heads);b=gateway.broker
        gateway.emit=lambda event,**fields:logs.append((event,fields))
        b.register_client('capstone','t-capstone',models=list(MODELS),max_concurrency=64,queue_limit=256,priority=6)
        for rid,h in HEADS.items():
            declared={'qwen3.5:4b':{'parallel':2,'digest':'D-qwen3.5:4b','runtime_version':RUNTIME,'num_ctx':1024,'evidence':'arena-matrix-test'}} if h['slots']>=2 else None
            b.authorize_resource(rid,endpoint=h['endpoint'],resource_type='shared_inference',owner='infrastructure',applications=['capstone'],
                                 permitted_uses=['inference'],slots=h['slots'],memory_gb=31.4,model_capacity=declared)
            for m in MODELS:assert gateway.qualify(rid,m,operator='ops')['passed']
        gateway.refresh_health()
        limits={}
        with b.connect() as db:
            from inference_gateway import model_capacity,recovery
            policy=recovery.load_policy(db)['placement']
            for rid in HEADS:
                for l in model_capacity.head_limits(db,rid,list(MODELS),policy):limits[(rid,l['model'])]=(l['limit'],l['source'])
        stop=threading.Event();injected=[0];busy_retries=[0];records=[];errors=[]
        def injector():
            rng=random.Random(seed+1)
            while not stop.wait(rng.uniform(.5,1.5)):
                db=sqlite3.connect(folder/'ledger.db',timeout=10)
                try:db.execute('BEGIN IMMEDIATE');time.sleep(rng.uniform(.2,1.2));db.rollback();injected[0]+=1
                finally:db.close()
        def cycles():
            while not stop.wait(1.0):
                for job in (gateway.run_recovery,gateway.refresh_health,gateway.publish_counters):
                    try:job()
                    except Exception as exc:errors.append(('cycle',type(exc).__name__,str(exc)[:120]))
        def workflow(w):
            rng=random.Random(seed*100+w)
            for c in range(calls):
                model='qwen3.5:4b' if (w+c)%2 else 'gemma3:4b';prompt='wf%d-c%d'%(w,c)
                body={'contract':'shared.inference/1','client_key':prompt,'model':model,'nodes':['@authorized'],'deadline':time.time()+90,
                      'operation':'/api/generate','payload':{'prompt':prompt,'options':{'num_predict':20}}}
                t0=time.time();cancel=rng.random()<cancel_rate
                try:
                    while True:
                        try:current=gateway.execute('capstone','t-capstone',body,asynchronous=True);break
                        except LedgerBusy:busy_retries[0]+=1;time.sleep(.5) # the HTTP 503: same client_key again
                    rid=current['request']['request_id'];polls=0
                    while current['status'] in ('active','queued','leased','cancel_requested'):
                        time.sleep(rng.uniform(.05,.25));polls+=1
                        if cancel and polls==2:
                            try:current=gateway.cancel('capstone','t-capstone',rid);continue
                            except LedgerBusy:busy_retries[0]+=1
                        current=gateway.result('capstone','t-capstone',rid)
                        if time.time()-t0>120:raise TimeoutError('workflow call did not end in 120 s')
                    records.append({'prompt':prompt,'model':model,'status':current['status'],'cancel':cancel,'latency':time.time()-t0,
                                    'queue':(current['admitted_at']-current['submitted_at']) if current.get('admitted_at') else None,
                                    'resource':(current.get('result') or {}).get('resource_id'),'request_id':rid,
                                    'error':current.get('error') if current['status'] not in ('completed','cancelled') else None})
                except Exception as exc:errors.append(('workflow',prompt,type(exc).__name__+': '+str(exc)[:160]))
        aux=[threading.Thread(target=cycles,daemon=True)]+([threading.Thread(target=injector,daemon=True)] if inject else [])
        for t in aux:t.start()
        began=time.time();pool=[threading.Thread(target=workflow,args=(w,)) for w in range(workflows)]
        for t in pool:t.start()
        for t in pool:t.join(300)
        wall=time.time()-began;stop.set()
        for t in aux:t.join(15)
        time.sleep(.5)
        with b.connect() as db:
            events=[r[0] for r in db.execute('SELECT event_type FROM capacity_events')]
        statuses={}
        for r in records:statuses[r['status']]=statuses.get(r['status'],0)+1
        duplicates=sorted(p for p,n in heads.dispatched.items() if n>1)
        by_prompt={r['prompt']:r for r in records}
        lost=sorted(p for p in heads.replied if p in by_prompt and by_prompt[p]['status']!='completed' and not by_prompt[p]['cancel'])
        over_limit={'%s %s'%k:{'peak':v,'limit':limits[k][0]} for k,v in heads.peak.items() if v>limits[k][0]}
        done=[r for r in records if r['status']=='completed']
        with b.connect() as db:
            failed=[{**r,'events':[(e['event_type'],json.loads(e['payload'])) for e in db.execute('SELECT event_type,payload FROM capacity_events WHERE request_id=? ORDER BY event_id',(r['request_id'],))]}
                    for r in records if r['status'] not in ('completed','cancelled')]
        stats=b.ledger_stats()
        report={'workflows':workflows,'calls_per_workflow':calls,'calls_total':workflows*calls,'wall_seconds':round(wall,1),
                'throughput_calls_per_second':round(len(done)/wall,2) if wall else None,'statuses':statuses,
                'latency_ms':{'p50':pct([r['latency']*1000 for r in done],.5),'p95':pct([r['latency']*1000 for r in done],.95),'max':pct([r['latency']*1000 for r in done],1)},
                'queue_ms':{'p50':pct([r['queue']*1000 for r in done if r['queue'] is not None],.5),'p95':pct([r['queue']*1000 for r in done if r['queue'] is not None],.95),
                            'max':pct([r['queue']*1000 for r in done if r['queue'] is not None],1)},
                'errors':errors,'failed_requests':failed,'duplicate_dispatch':duplicates,'lost_results':lost,'over_model_limit':over_limit,
                'lease_expired':events.count('lease.expired'),'resource_held':events.count('resource.held'),'lease_extended_late':events.count('lease.extended'),
                'replies_from_heads':len(heads.replied),'cancelled_by_client':sum(1 for r in records if r['cancel']),
                'injected_external_locks':injected[0],'ledger_busy_retries':busy_retries[0],
                'database_locked_logged':sum(1 for e,f in logs if 'database is locked' in json.dumps(f)),
                'renewal_failures':sum(1 for e,_ in logs if e=='lease.renewal_failed'),
                'ledger_wait_timeouts':sum(1 for e,_ in logs if e=='ledger.wait_timeout'),
                'ledger':{'labels':{k:{x:v[x] for x in ('count','timeouts','external_busy','wait_ms_max','hold_ms_max')} for k,v in sorted(stats['labels'].items())}},
                'model_limits':{'%s %s'%k:{'limit':v[0],'source':v[1]} for k,v in sorted(limits.items())},
                'peak_concurrency':{'%s %s'%k:v for k,v in sorted(heads.peak.items())}}
        return report
    finally:
        capacity_broker.LEASE_SECONDS=old_lease;capacity_gateway.renew_until_stopped=old_renew


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--src',default=str(Path(__file__).resolve().parents[1]/'src'));p.add_argument('--workflows',type=int,nargs='+',default=[12,16,20])
    p.add_argument('--calls',type=int,default=4);p.add_argument('--no-inject',action='store_true');p.add_argument('--out');a=p.parse_args(argv)
    sys.path.insert(0,a.src)
    import inference_gateway
    reports=[run(n,calls=a.calls,inject=not a.no_inject) for n in a.workflows]
    out={'gateway_release':inference_gateway.__version__,'runs':reports}
    text=json.dumps(out,indent=2);print(text)
    if a.out:Path(a.out).write_text(text,encoding='utf-8')


if __name__=='__main__':main()
