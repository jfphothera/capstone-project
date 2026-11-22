"""Ledger write-lock measurements for 0.4.4 (no network, no heads; a synthetic ledger in a temp folder).

1. hold: how long the write transactions on the request path hold the lock, against a ledger
   with a pilot-sized history (the 2026-11-16 hub ledger had at least 19,092 requests and
   59,320 events since 2026-11-06). Run it with --src pointing at 0.4.3's src and at 0.4.4's.
2. scheduler_starvation: the admission scheduler running place() back to back (as it does while
   events keep arriving) next to lease renewals; 0.4.3 renewals wait in SQLite's busy handler.
3. fairness: N threads doing short write transactions at a high offered load, waiting either
   in SQLite's busy handler (0.4.3: one connection each, timeout, BEGIN IMMEDIATE) or in the
   0.4.4 FIFO gate (CapacityBroker.write). Reports wait percentiles and refusals.

    python scripts/ledger_contention.py --src src --history 20000 --result-kb 8 --out verification/ledger-0.4.4.json
"""
import argparse,json,os,random,sqlite3,statistics,sys,tempfile,threading,time,uuid
from pathlib import Path

ENDPOINTS={'head-192-0-2-%d'%n:'http://127.0.0.1:%d'%(21000+n) for n in range(2,11)}
MODELS=['qwen3.5:4b','gemma3:4b','all-minilm:latest']


class Heads:
    def __call__(self,url,operation,payload,timeout):
        if operation=='/api/version':return {'version':'0.40.15'}
        if operation=='/api/tags':return {'models':[{'name':m,'digest':'D-'+m,'size':2*1024**3,'details':{'family':'qwen35' if 'qwen' in m else 'gemma3'}} for m in MODELS]}
        if operation=='/api/show':return {'capabilities':['embedding'] if 'minilm' in payload['model'] else ['completion'],'license':'x'*12000,
                                          'model_info':{'general.architecture':'qwen35'},'template':'t'*4000}
        if operation=='/api/ps':return {'models':[]}
        if operation=='/api/embed':return {'model':payload['model'],'embeddings':[[.1,.2]]}
        return {'done':True,'model':payload['model'],'response':'READY','eval_count':3}


def build(src,folder,history,result_kb):
    sys.path.insert(0,str(Path(src).resolve()))
    from inference_gateway.capacity_gateway import Gateway
    path=Path(folder)/'ledger.db';gateway=Gateway(path,transport=Heads());b=gateway.broker
    b.register_client('capstone','t-capstone',models=MODELS,max_concurrency=64,queue_limit=256,priority=6)
    for rid,url in ENDPOINTS.items():
        b.authorize_resource(rid,endpoint=url,resource_type='shared_inference',owner='infrastructure',applications=['capstone'],permitted_uses=['inference'],slots=4,memory_gb=31.4)
        for m in MODELS:gateway.qualify(rid,m,operator='ops',purpose='embedding' if 'minilm' in m else 'completion')
    gateway.refresh_health()
    blob=json.dumps({'response':{'model':MODELS[0],'response':'w '*(result_kb*512),'done':True},'provenance':{'x':'y'*200}})
    now=time.time();rng=random.Random(7);rows=[];inv=[];ev=[];times=[]
    for i in range(history):
        rid='REQ-'+uuid.UUID(int=rng.getrandbits(128)).hex;at=now-86400*10+i*40
        head=rng.choice(list(ENDPOINTS))
        rows.append((rid,'capstone','k%d'%i,MODELS[0],'[]','completed',at,at+300,'LEASE-'+rid[4:],head,at+60,0,'authorized_dynamic'))
        inv.append((rid,'capstone','h','owner','completed',blob,None,at+30,at+5))
        times.append((rid,at+1,at+30))
        for e in ('queued','admitted','completed'):ev.append(('capstone',rid,e,'{}',at))
    with sqlite3.connect(path) as db:
        db.executemany('INSERT INTO capacity_requests(request_id,app_id,client_key,model,nodes,status,created,deadline,lease_id,resource_id,expires,executing,node_policy) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',rows)
        db.executemany('INSERT INTO capacity_invocations(request_id,app_id,payload_hash,execution_owner,state,result,error,updated,started_at) VALUES(?,?,?,?,?,?,?,?,?)',inv)
        db.executemany('INSERT INTO capacity_request_times VALUES(?,?,?)',times)
        db.executemany('INSERT INTO capacity_events(app_id,request_id,event_type,payload,at) VALUES(?,?,?,?,?)',ev)
    return gateway,path


def timed(fn,runs=15,setup=None):
    out=[]
    for _ in range(runs):
        if setup:setup()
        t=time.perf_counter();fn();out.append((time.perf_counter()-t)*1000)
    return {'median_ms':round(statistics.median(out),2),'max_ms':round(max(out),2)}


def holds(gateway,path):
    b=gateway.broker;deadline=time.time()+600;keys=iter(range(10**6))
    queued=[]
    for i in range(20):queued.append(b.submit('capstone','t-capstone','q%d'%i,MODELS[0],['@authorized'],deadline)['request_id'])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE capacity_requests SET nodes=? WHERE request_id IN (%s)"%','.join('?'*len(queued)),[json.dumps(list(ENDPOINTS))]+queued)
    def requeue():
        with sqlite3.connect(path) as db:
            db.execute("UPDATE capacity_requests SET status='queued',lease_id=NULL,resource_id=NULL,expires=NULL WHERE request_id IN (%s)"%','.join('?'*len(queued)),queued)
    def count_active():
        with sqlite3.connect(path,timeout=30) as db:
            db.execute('BEGIN IMMEDIATE');db.execute("SELECT count(*) FROM capacity_invocations WHERE state='active'").fetchone()
    def expire_pass():
        with sqlite3.connect(path,timeout=30) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("SELECT * FROM capacity_requests WHERE status IN ('leased','cancel_requested') AND expires<?",(time.time(),)).fetchall()
            db.execute("SELECT request_id,app_id FROM capacity_requests WHERE status='queued' AND deadline<?",(time.time(),)).fetchall()
    out={'place_20_queued_9_heads':timed(b.place,setup=requeue),
         'submit':timed(lambda:b.submit('capstone','t-capstone','s%d'%next(keys),MODELS[0],['@authorized'],deadline)),
         'invocation_count_in_execute':timed(count_active),'expiry_pass':timed(expire_pass),
         'status_has_expired_read':timed(b.has_expired),
         'models_read':timed(lambda:gateway.models('capstone','t-capstone'),runs=5)}
    requeue()
    return out


def fairness(src,path,threads,hold_ms,interval_ms,seconds,timeout):
    sys.path.insert(0,str(Path(src).resolve()))
    from inference_gateway.capacity_broker import CapacityBroker
    broker=CapacityBroker(path);results={}
    for mode in ('sqlite_busy_handler','fifo_gate'):
        waits=[];refused=[0];stop=time.monotonic()+seconds;lock=threading.Lock()
        def body(db):
            end=time.perf_counter()+hold_ms/1000
            db.execute("UPDATE capacity_service_policy SET value=value WHERE key='admission_open'")
            while time.perf_counter()<end:pass # the measured cost of a transaction, held like one
        def worker(seed):
            rng=random.Random(seed)
            while time.monotonic()<stop:
                time.sleep(rng.uniform(0,2*interval_ms/1000))
                t=time.perf_counter()
                try:
                    if mode=='sqlite_busy_handler':
                        db=sqlite3.connect(path,timeout=timeout)
                        try:
                            db.execute('BEGIN IMMEDIATE');w=time.perf_counter()-t;body(db);db.commit()
                        finally:db.close()
                    else:
                        with broker.write('bench',wait=timeout) as db:w=time.perf_counter()-t;body(db)
                    with lock:waits.append(w*1000)
                except Exception as exc:
                    with lock:refused[0]+=1
        pool=[threading.Thread(target=worker,args=(i,)) for i in range(threads)]
        for p in pool:p.start()
        for p in pool:p.join()
        waits.sort()
        q=lambda f:round(waits[min(len(waits)-1,int(len(waits)*f))],1) if waits else None
        results[mode]={'transactions':len(waits),'refused_after_timeout':refused[0],'wait_ms_p50':q(.5),'wait_ms_p95':q(.95),'wait_ms_p99':q(.99),'wait_ms_max':q(1.0)}
    return {'threads':threads,'hold_ms':hold_ms,'mean_interval_ms':interval_ms,'seconds':seconds,'timeout_s':timeout,
            'offered_utilization':round(threads*hold_ms/(interval_ms+hold_ms),2),**results}


def starvation(gateway,path,victims,seconds,timeout):
    """The admission scheduler under constant events: place() back to back (as 0.4.1-0.4.3's Scheduler runs while
    a new event arrives during each pass), 20 requests waiting for full heads, and `victims` threads renewing a
    lease every 0.5 s. 0.4.3 renewals wait in SQLite's busy handler (its timeout forced to `timeout`); 0.4.4's
    in the FIFO gate with the same budget. Reports the renewals' waits and refusals."""
    import importlib
    b=gateway.broker;module=importlib.import_module(type(b).__module__);deadline=time.time()+900
    with sqlite3.connect(path) as db:
        db.execute('UPDATE capacity_resource_capacity SET slots=1')
        for i,rid in enumerate(ENDPOINTS): # every head full: the pass looks at all 20 waiting requests and places none
            db.execute("INSERT INTO capacity_requests(request_id,app_id,client_key,model,nodes,status,created,deadline,lease_id,resource_id,expires,executing,node_policy) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ('REQ-full%d'%i,'capstone','full%d'%i,MODELS[0],json.dumps(list(ENDPOINTS)),'leased',time.time(),deadline,'L%d'%i,rid,deadline,1,'authorized_dynamic'))
    real=module.sqlite3.connect
    class Shim:
        def __getattr__(self,name):return getattr(sqlite3,name)
        @staticmethod
        def connect(*args,**kwargs):kwargs['timeout']=min(kwargs.get('timeout',5),timeout);return real(*args,**kwargs)
    module.sqlite3=Shim()
    stop=time.monotonic()+seconds;waits=[];refused=[0];passes=[0];lock=threading.Lock()
    def scheduler():
        while time.monotonic()<stop:
            try:b.place();passes[0]+=1
            except Exception:pass
    def victim(i):
        rid='REQ-full%d'%i
        while time.monotonic()<stop:
            time.sleep(.5);t=time.perf_counter()
            try:
                if hasattr(b,'write'):
                    with b.write('renew',wait=timeout) as db:db.execute('UPDATE capacity_requests SET expires=? WHERE request_id=?',(deadline,rid))
                else:
                    with b.connect() as db:db.execute('UPDATE capacity_requests SET expires=? WHERE request_id=?',(deadline,rid))
                with lock:waits.append((time.perf_counter()-t)*1000)
            except Exception:
                with lock:refused[0]+=1
    try:
        pool=[threading.Thread(target=scheduler)]+[threading.Thread(target=victim,args=(i,)) for i in range(victims)]
        for p in pool:p.start()
        for p in pool:p.join()
    finally:module.sqlite3=sqlite3
    waits.sort();q=lambda f:round(waits[min(len(waits)-1,int(len(waits)*f))],1) if waits else None
    return {'scheduler_passes':passes[0],'renewals':len(waits),'renewals_refused':refused[0],'timeout_s':timeout,
            'wait_ms_p50':q(.5),'wait_ms_p95':q(.95),'wait_ms_max':q(1.0)}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--src',required=True);p.add_argument('--history',type=int,default=20000);p.add_argument('--result-kb',type=int,default=8)
    p.add_argument('--fairness-threads',type=int,default=24);p.add_argument('--hold-ms',type=float,default=30);p.add_argument('--interval-ms',type=float,default=750)
    p.add_argument('--seconds',type=float,default=20);p.add_argument('--timeout',type=float,default=2.0);p.add_argument('--skip-fairness',action='store_true')
    p.add_argument('--out');a=p.parse_args(argv)
    folder=tempfile.mkdtemp(prefix='ledger-contention-')
    gateway,path=build(a.src,folder,a.history,a.result_kb)
    import inference_gateway
    report={'src':str(Path(a.src).resolve()),'release':inference_gateway.__version__,'history_requests':a.history,'result_kb':a.result_kb,
            'ledger_bytes':os.path.getsize(path),'hold':holds(gateway,path)}
    report['scheduler_starvation']=starvation(gateway,path,8,a.seconds,3.0)
    if not a.skip_fairness and hasattr(gateway.broker,'write'):
        report['fairness']=fairness(a.src,path,a.fairness_threads,a.hold_ms,a.interval_ms,a.seconds,a.timeout)
    text=json.dumps(report,indent=2);print(text)
    if a.out:Path(a.out).write_text(text,encoding='utf-8')


if __name__=='__main__':main()
