"""Concurrency load harness (release 0.4.1, item A). Loopback only; never contacts a lab head.

Three processes, as on the lab:
  heads    nine fake Ollama heads (real HTTP on 127.0.0.1), each running at most `slots`
           generations at once (OLLAMA_NUM_PARALLEL) with a random generation time;
  gateway  the Gateway service runtime (GatewayRuntime: listener, health refresh, recovery,
           in-service observation publisher) over a fresh ledger, bootstrapped like the hub;
  clients  N concurrent application flows, each several sequential calls through
           shared_inference_client, all started within a few milliseconds.

`--src` selects the code under test (a src/ folder holding inference_gateway and
shared_inference_client), so the same run can be repeated on 0.4.0 and 0.4.1.

  python scripts/concurrency_load.py run --src src --flows 32 --calls 4 --slots 10 --out report.json

The report counts client failures by kind, ledger events (lease.expired, resource.held,
overload), same-head overlap measured by the heads, and the listener's 503 answers.
"""
import argparse,json,os,random,sqlite3,subprocess,sys,threading,time,traceback
from pathlib import Path

HERE=Path(__file__).resolve()
MODEL='qwen3:4b'
DIGEST='sha256:'+'4b'*32
HEADS=['head-192-0-2-%d'%n for n in range(2,11)]


# ---------------------------------------------------------------- fake heads

def heads_main(a):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    lo,hi=(float(x) for x in a.latency.split(','))
    stats={};lock=threading.Lock()
    class Head(ThreadingHTTPServer):
        daemon_threads=True;request_queue_size=128
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def reply(self,value):
            data=json.dumps(value).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_GET(self):
            if self.path=='/api/version':return self.reply({'version':'0.40.13'})
            if self.path=='/api/tags':return self.reply({'models':[{'name':MODEL,'digest':DIGEST,'size':2_600_000_000}]})
            if self.path=='/api/ps':return self.reply({'models':[{'name':MODEL,'digest':DIGEST,'size':3_100_000_000,'expires_at':'2026-11-13T18:00:00Z'}]})
            if self.path=='/stats':
                with lock:return self.reply(stats)
            self.send_error(404)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
            if self.path=='/api/show':return self.reply({'capabilities':['completion'],'details':{'family':'qwen3'}})
            if self.path!='/api/generate':return self.send_error(404)
            head=self.server.head
            with head['gate']:
                with lock:
                    s=stats[head['id']];s['active']+=1;s['calls']+=1;s['max_active']=max(s['max_active'],s['active'])
                try:time.sleep(0.05 if body.get('prompt','').startswith('Return exactly') else random.uniform(lo,hi))
                finally:
                    with lock:s['active']-=1
            self.reply({'model':body['model'],'created_at':'2026-11-13T00:00:00Z','response':'READY','done':True,'done_reason':'stop',
                        'eval_count':24,'prompt_eval_count':12,'total_duration':1})
    servers=[];ports={}
    slots=[int(x) for x in a.slots.split(',')]
    for index,rid in enumerate(HEADS):
        server=Head(('127.0.0.1',0),Handler)
        # The head runs at most `slots` generations at once and queues the rest, like OLLAMA_NUM_PARALLEL.
        server.head={'id':rid,'gate':threading.BoundedSemaphore(slots[index%len(slots)])}
        stats[rid]={'active':0,'max_active':0,'calls':0}
        threading.Thread(target=server.serve_forever,daemon=True).start();servers.append(server);ports[rid]=server.server_port
    print(json.dumps({'ports':ports}),flush=True)
    sys.stdin.read() # until the orchestrator closes stdin


# ---------------------------------------------------------------- gateway

def gateway_main(a):
    sys.path.insert(0,str(Path(a.src).resolve()))
    from inference_gateway.capacity_gateway import Gateway
    from inference_gateway.service_runtime import GatewayRuntime
    import inference_gateway
    ports=json.loads(a.ports);slots=[int(x) for x in a.slots.split(',')];work=Path(a.work)
    (work/'logs').mkdir(parents=True,exist_ok=True)
    ledger=work/'capacity-ledger.db'
    bootstrap=Gateway(ledger);b=bootstrap.broker
    for app in ('capstone','capstone-research'):
        # Operator limits on the hub (Agent Arena bootstrap, 2026-11-13).
        b.register_client(app,'token-'+app+'-0123456789abcdef0123456789',models=[MODEL],max_concurrency=100,queue_limit=500,priority=5 if app=='capstone' else 4)
    for index,rid in enumerate(HEADS):
        b.authorize_resource(rid,endpoint='http://127.0.0.1:%d'%ports[rid],resource_type='shared_inference',owner='infrastructure',
                             applications=['capstone','capstone-research'],permitted_uses=['inference'],slots=slots[index%len(slots)],memory_gb=None)
        evidence=bootstrap.qualify(rid,MODEL,operator='load-harness')
        assert evidence['passed'],evidence
    bootstrap.refresh_health()
    settings={'service_id':'shared-inference','ledger':str(ledger),'listen':{'host':'127.0.0.1','port':0},'advertised_url':'http://127.0.0.1:1',
              'tls':{},'upstream_tls':{'ca':None,'certificate':None,'key':None},
              'limits':{'max_connections':a.max_connections,'max_pending':a.max_pending},'shutdown':{'drain_seconds':5},
              'logging':{'directory':str(work/'logs'),'max_bytes':10*1024*1024,'backup_count':2},'health_interval_seconds':15,
              'observation':{'output':None,'state':None,'interval_seconds':5,'overlap':100,'limit':1000}}
    runtime=GatewayRuntime(settings).start()
    print(json.dumps({'port':runtime.server.server_port,'release':inference_gateway.__version__}),flush=True)
    sys.stdin.read()
    runtime.stop(drain_seconds=2)


# ---------------------------------------------------------------- clients

def clients_main(a):
    sys.path.insert(0,str(Path(a.src).resolve()))
    import shared_inference_client
    from shared_inference_client import Client
    app='capstone-research';token='token-'+app+'-0123456789abcdef0123456789'
    barrier=threading.Barrier(a.flows);calls=[];lock=threading.Lock();starts=[]
    def flow(index):
        client=Client(a.url,app,token)
        barrier.wait();started=time.time()
        with lock:starts.append(started)
        for call in range(a.calls):
            seen={'request_id':None}
            def progress(current):seen['request_id']=current['request']['request_id']
            t0=time.time();outcome={'flow':index,'call':call}
            try:
                result=client.infer(MODEL,None,'/api/generate',{'prompt':'flow %d call %d'%(index,call),'options':{'num_predict':32}},
                                    timeout=a.timeout,progress=progress)
                seen['request_id']=result['request']['request_id']
                outcome.update(status=result['status'],resource_id=result['request'].get('resource_id'),error=result.get('error'))
                record=result.get('result') or {}
                outcome.update(started_at=record.get('started_at'),finished_at=record.get('finished_at'))
            except Exception as exc:
                outcome.update(status='exception',kind=type(exc).__name__,error=str(exc)[:200])
            outcome.update(request_id=seen['request_id'],seconds=round(time.time()-t0,3))
            with lock:calls.append(outcome)
    threads=[threading.Thread(target=flow,args=(i,)) for i in range(a.flows)]
    for t in threads:t.start()
    for t in threads:t.join()
    print(json.dumps({'client_version':getattr(shared_inference_client,'__version__','0.4.0 or earlier'),
                      'start_spread_ms':round((max(starts)-min(starts))*1000,1),'calls':calls}),flush=True)


# ---------------------------------------------------------------- orchestration

def read_ledger(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        events={r['event_type']:r['n'] for r in db.execute('SELECT event_type,count(*) n FROM capacity_events GROUP BY event_type')}
        suspensions=[dict(r) for r in db.execute("SELECT scope,resource_id,source,state FROM capacity_suspensions")]
        invocations={r['state']:r['n'] for r in db.execute('SELECT state,count(*) n FROM capacity_invocations GROUP BY state')}
        listener=db.execute("SELECT value FROM capacity_service_policy WHERE key='gateway_counters'").fetchone()
        return {'events':events,'suspensions':suspensions,'invocations':invocations,'gateway_load':json.loads(listener[0]) if listener else None}
    finally:db.close()


def run_main(a):
    work=Path(a.work or (Path(os.environ.get('TEMP','.'))/('gateway-load-%d'%int(time.time()))));work.mkdir(parents=True,exist_ok=True)
    py=[sys.executable,'-I',str(HERE)]
    heads=subprocess.Popen(py+['heads','--latency',a.latency,'--slots',a.slots],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    ports=json.loads(heads.stdout.readline())['ports']
    gateway=subprocess.Popen(py+['gateway','--src',a.src,'--work',str(work),'--ports',json.dumps(ports),'--slots',a.slots,
                                 '--max-connections',str(a.max_connections),'--max-pending',str(a.max_pending)],
                             stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    line=gateway.stdout.readline()
    if not line:raise SystemExit('gateway did not start')
    started=json.loads(line)
    url='http://127.0.0.1:%d'%started['port'];t0=time.time()
    clients=subprocess.run(py+['clients','--src',a.client_src or a.src,'--url',url,'--flows',str(a.flows),'--calls',str(a.calls),'--timeout',str(a.timeout)],
                           capture_output=True,text=True,timeout=3600)
    elapsed=time.time()-t0
    import urllib.request
    head_stats={rid:json.loads(urllib.request.urlopen('http://127.0.0.1:%d/stats'%port,timeout=5).read())[rid] for rid,port in ports.items()}
    gateway.stdin.close();gateway.wait(60);heads.stdin.close();heads.wait(10)
    if clients.returncode:raise SystemExit(clients.stderr[-2000:])
    out=json.loads(clients.stdout.strip().splitlines()[-1]);calls=out['calls']
    failures={}
    for c in calls:
        if c['status']!='completed':
            key='%s:%s'%(c['status'],c.get('kind') or (c.get('error') or '')[:60])
            failures[key]=failures.get(key,0)+1
    ledger=read_ledger(work/'capacity-ledger.db')
    overlap={rid:s['max_active'] for rid,s in head_stats.items()}
    report={'gateway_release':started['release'],'client_version':out['client_version'],'flows':a.flows,'calls_per_flow':a.calls,
            'slots':a.slots,'latency_seconds':a.latency,'max_connections':a.max_connections,'max_pending':a.max_pending,
            'flow_start_spread_ms':out['start_spread_ms'],'elapsed_seconds':round(elapsed,1),
            'calls_total':len(calls),'calls_completed':sum(c['status']=='completed' for c in calls),
            'failures':failures,'failures_before_request_id':sum(1 for c in calls if c['status']!='completed' and not c.get('request_id')),
            'max_call_seconds':max(c['seconds'] for c in calls),
            'heads_max_concurrent_generations':overlap,'head_calls':{rid:s['calls'] for rid,s in head_stats.items()},
            'ledger_events':{k:v for k,v in ledger['events'].items() if k in ('lease.expired','resource.held','resource.quarantined','overload.rejected',
                             'request.expired','invocation.failed','completed','admitted','model.suspended','lease.extended','request.resubmitted')},
            'suspensions':ledger['suspensions'],'invocations':ledger['invocations'],'gateway_load':ledger['gateway_load'],
            'failed_request_ids':[c['request_id'] for c in calls if c['status']!='completed'][:20],'work':str(work)}
    text=json.dumps(report,indent=2)
    if a.out:Path(a.out).write_text(text+'\n',encoding='utf-8')
    print(text)
    return report


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter);sub=p.add_subparsers(dest='verb',required=True)
    h=sub.add_parser('heads');h.add_argument('--latency',default='1,3');h.add_argument('--slots',default='1')
    g=sub.add_parser('gateway');g.add_argument('--src',required=True);g.add_argument('--work',required=True);g.add_argument('--ports',required=True)
    g.add_argument('--slots',default='1');g.add_argument('--max-connections',type=int,default=64);g.add_argument('--max-pending',type=int,default=128)
    c=sub.add_parser('clients');c.add_argument('--src',required=True);c.add_argument('--url',required=True);c.add_argument('--flows',type=int,default=32)
    c.add_argument('--calls',type=int,default=4);c.add_argument('--timeout',type=float,default=300)
    r=sub.add_parser('run');r.add_argument('--src',default=str(HERE.parents[1]/'src'));r.add_argument('--flows',type=int,default=32)
    r.add_argument('--calls',type=int,default=4);r.add_argument('--slots',default='10',help='per head, cycled over the nine heads, e.g. 1 or 10 or 4,10')
    r.add_argument('--latency',default='1,3',help='generation seconds min,max');r.add_argument('--timeout',type=float,default=300)
    r.add_argument('--max-connections',type=int,default=64);r.add_argument('--max-pending',type=int,default=128)
    r.add_argument('--work');r.add_argument('--out');r.add_argument('--client-src',help='src folder for shared_inference_client (default --src)')
    a=p.parse_args(argv)
    {'heads':heads_main,'gateway':gateway_main,'clients':clients_main,'run':run_main}[a.verb](a)


if __name__=='__main__':main()
