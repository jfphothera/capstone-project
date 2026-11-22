"""G6 capacity check (Inference Gateway 0.4.6 lab tool). Run on the hub, HUB, with G6-CAPACITY-CHECK.cmd.

The same admitted request mix at 1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22 and 24 concurrent callers (13 levels;
--levels changes the list). Each level is a 30 s warm-up plus a 4 min measured window, about 60-65 minutes in all.
The mix is a fixed sample of 39 real completed requests from the c922 campaign's qwen3.5:4b runs (main roles and
the qwen3:4b reviewer in their observed proportion), shuffled once with a fixed seed so that even 1 or 2 callers send
the reviewer's share, and replayed through the Gateway. Each caller sends its next request when the previous one
ends, like a case worker. A short marker is appended to each request's last message so no two are byte-identical.

Identity: by default the tool issues a temporary Gateway identity, lab-capacity-check (qwen3.5:4b and qwen3:4b only,
24 at once, the lowest priority, on exactly the heads capstone-research may use), with the Gateway's own credentials
tool, and revokes it when it ends, also on an error, a refusal or Ctrl+C. Its token stays in an administrators-only
folder under C:\\ProgramData\\AgentArenaLab and is deleted afterwards. --client-config uses an existing identity.

Reported per level: useful completions (completed with a valid output: JSON with the schema's required keys, a tool
call, or text), request failures by kind, queue (submitted to admitted), dispatch checks, model execution and whole
request time p50/p95 (main and reviewer separately), Gateway ledger wait per request, the Gateway's own ledger and
health-cycle overhead during the window, per-head overlap (calls running at once, per model) and the reviewer's share
of calls and of model time. report.json and summary.txt are rewritten after every level, so a stopped run keeps the
levels it finished.

It refuses to start while the Gateway has any running or queued work, and stops if other work appears. It does not
change capacity policy, model_capacity, qualification records, other clients or installed models; it loads only the
two models of its mix (through ordinary Gateway requests). Results go to results\\ beside this file. It reports
evidence only: it does not choose a worker count.
"""
TOOL='g6-capacity-check'
FIXTURE_BLOB='@@FIXTURE@@'


def useful(request,envelope):
    """Completed with a valid output for its shape (the same rule for every level)."""
    if envelope.get('status')!='completed':return False
    response=(envelope.get('result') or {}).get('response') or {}
    message=response.get('message') or {}
    payload=request['payload']
    if payload.get('tools'):return bool(message.get('tool_calls')) or bool(str(message.get('content') or '').strip())
    if isinstance(payload.get('format'),dict):
        try:value=json.loads(message.get('content') or '')
        except ValueError:return False
        required=payload['format'].get('required') or []
        return isinstance(value,dict) and all(k in value for k in required)
    return bool(str(message.get('content') or '').strip())


def overlap(calls,start,end):
    """Per head: the most calls running at once and the time-averaged number running, in [start, end]; per model too."""
    heads=collections.defaultdict(list)
    for c in calls:
        if c.get('resource_id') and c.get('execution_started_at') and c.get('execution_finished_at'):
            heads[c['resource_id']].append((max(start,c['execution_started_at']),min(end,c['execution_finished_at']),c['model']))
    out={}
    for rid,items in sorted(heads.items()):
        items=[i for i in items if i[1]>i[0]]
        points=sorted([(a,1,m) for a,b,m in items]+[(b,-1,m) for a,b,m in items],key=lambda p:(p[0],p[1]))
        best=now=0;by=collections.Counter();best_by=collections.Counter()
        for _,step,m in points:
            now+=step;by[m]+=step;best=max(best,now);best_by[m]=max(best_by[m],by[m])
        busy=sum(b-a for a,b,_ in items)
        out[rid]={'max_concurrent':best,'mean_concurrent':round(busy/max(1e-9,end-start),2),'max_concurrent_by_model':dict(best_by),
                  'calls':len(items)}
    return out


def ledger_delta(before,after):
    """Per transaction label: count, wait and hold added during the window (Gateway 0.4.4+ counters in the observation)."""
    b=((before or {}).get('ledger') or {}).get('labels') or {};a=((after or {}).get('ledger') or {}).get('labels') or {}
    out={}
    for label,v in a.items():
        p=b.get(label,{})
        d={k:round(v.get(k,0)-p.get(k,0),1) for k in ('count','timeouts','wait_ms_total','hold_ms_total')}
        if d['count'] or d['timeouts']:out[label]=d
    return out


def counters_delta(before,after):
    b=(before or {}).get('counters') or {};a=(after or {}).get('counters') or {}
    return {k:v-b.get(k,0) for k,v in a.items() if isinstance(v,(int,float)) and v-b.get(k,0)}


def run_level(gateway,mix,level,args,run_id,observation,label=None,*,ledger=None,on_stop=None):
    own=set();own_lock=threading.Lock();calls=[];stop=threading.Event();contaminated=[];samples=[]
    t0=time.time();window_start=t0+args.warmup_seconds;window_end=window_start+args.window_seconds
    def note(rid):
        with own_lock:own.add(rid)
    def caller(i):
        n=0
        while not stop.is_set() and time.time()<window_end:
            request=mix[((i*len(mix))//level+n)%len(mix)];n+=1 # callers start evenly spread over the (shuffled) mix
            model=args.reviewer if request['model_role']=='reviewer' else args.main
            payload=json.loads(json.dumps(request['payload']))
            key='labtool-g6-%s-L%02d-c%02d-%04d'%(run_id,level,i,n)
            if payload.get('messages'):
                last=payload['messages'][-1]
                if isinstance(last.get('content'),str):last['content']+='\n\n[capacity check %s]'%key
            submitted=time.time()
            envelope,seconds=gateway.run(model,request['operation'],payload,client_key=key,deadline_seconds=args.deadline_seconds,on_submit=note,stop=stop)
            provenance=((envelope.get('result') or {}).get('provenance') or {})
            timing=provenance.get('timing_ms') or {}
            record={'caller':i,'n':n,'role':request['role'],'model_role':request['model_role'],'model':model,'client_submit':submitted,
                    'client_seconds':round(seconds,3),'status':envelope.get('status'),'error':(envelope.get('error') or None) and str(envelope.get('error'))[:300],
                    'upstream_errors':envelope.get('upstream_errors'),'useful':useful(request,envelope),
                    'resource_id':(envelope.get('request') or {}).get('resource_id'),
                    'submitted_at':envelope.get('submitted_at'),'admitted_at':envelope.get('admitted_at'),
                    'execution_started_at':envelope.get('execution_started_at'),'execution_finished_at':envelope.get('execution_finished_at'),
                    'completed_at':envelope.get('completed_at'),'ledger_wait_ms':timing.get('ledger_wait'),
                    'runtime_load_or_runner_wait_ms':timing.get('runtime_load_or_runner_wait'),'attempts':len(provenance.get('attempts') or []) or None}
            with own_lock:calls.append(record)
    def sampler():
        # Outside work (running or queued, from the ledger) is checked every 2 s; the observation's slot use about every 10 s.
        # On outside work the level stops at once and on_stop revokes the identity, so its queued requests are cancelled
        # and nothing of this tool waits behind real work. Running calls end as the Gateway lets them.
        tick=0
        while not stop.is_set():
            try:
                with own_lock:mine=set(own)
                found=outside_work(observation,ledger,own_app=gateway.app,own_ids=mine)
                if found['any']:
                    contaminated.append({'at':now_iso(),'running':found['foreign_running'],'queued':found['foreign_queued'],
                                         'apps':found['foreign_apps'],'heads':found['foreign_heads'],'source':found['source']})
                    stop.set()
                    print('  OUTSIDE WORK: %d running and %d queued requests of %s: this level stops now.'
                          %(found['foreign_running'],found['foreign_queued'],', '.join(found['foreign_apps']) or '-'),flush=True)
                    if on_stop:on_stop()
                    break
                if tick%5==0:
                    doc=read_observation(observation)
                    samples.append({'at':time.time(),'slots_in_use':{r['resource_id']:(r.get('capacity') or {}).get('slots_in_use') for r in doc.get('resources') or []}})
            except Refused as exc:samples.append({'at':time.time(),'error':str(exc)})
            tick+=2;stop.wait(2)
    before=(read_observation(observation).get('gateway_load') or {})
    threads=[threading.Thread(target=caller,args=(i,),daemon=True) for i in range(level)]
    watch=threading.Thread(target=sampler,daemon=True);watch.start()
    label=label or 'level';callers='1 caller' if level==1 else '%d callers'%level
    def ticker():
        # Bounded: at most one line per 15 s for this level's own lifetime (warm-up, window and the drain deadline).
        lines=int((args.warmup_seconds+args.window_seconds+args.deadline_seconds+60)/15)+1
        while lines>0 and not stop.wait(15):
            lines-=1
            left=window_end-time.time()
            if left<=0:print('  %s: %s, window over, waiting for the last calls'%(label,callers),flush=True);continue
            phase='warm-up, then ' if time.time()<window_start else ''
            print('  %s: %s, %s%d:%02d left in this window'%(label,callers,phase,int(left)//60,int(left)%60),flush=True)
    print('  %s: %s, warm-up %d s, window %d s ...'%(label,callers,args.warmup_seconds,args.window_seconds),flush=True)
    threading.Thread(target=ticker,daemon=True).start()
    for t in threads:t.start()
    for t in threads:t.join(args.warmup_seconds+args.window_seconds+args.deadline_seconds+60)
    stop.set();watch.join(10)
    time.sleep(6) # one more observation cycle (5 s) so the window's counters are published
    after=(read_observation(observation).get('gateway_load') or {})
    measured=[c for c in calls if c['submitted_at'] and window_start<=c['submitted_at']<window_end]
    ended_in_window=[c for c in calls if c['completed_at'] and window_start<=c['completed_at']<window_end]
    minutes=args.window_seconds/60
    def times(rows,a,b):return [rows_[b]-rows_[a] for rows_ in rows if rows_.get(a) and rows_.get(b)]
    def block(rows):
        return {'requests':len(rows),'completed':sum(r['status']=='completed' for r in rows),'useful':sum(r['useful'] for r in rows),
                'failed':dict(collections.Counter('%s: %s'%(r['status'],(r['error'] or '')[:80]) for r in rows if r['status']!='completed')),
                'queue_s':summary(times(rows,'submitted_at','admitted_at')),'dispatch_checks_s':summary(times(rows,'admitted_at','execution_started_at')),
                'execution_s':summary(times(rows,'execution_started_at','execution_finished_at')),'total_s':summary(times(rows,'submitted_at','completed_at')),
                'ledger_wait_ms':summary([r['ledger_wait_ms'] for r in rows]),'runtime_load_or_runner_wait_ms':summary([r['runtime_load_or_runner_wait_ms'] for r in rows])}
    exec_by=collections.Counter()
    for r in measured:
        if r['execution_started_at'] and r['execution_finished_at']:exec_by[r['model_role']]+=r['execution_finished_at']-r['execution_started_at']
    total_exec=sum(exec_by.values()) or None
    slots=[s for s in samples if 'slots_in_use' in s]
    return {'level':level,'callers':level,'window':{'start':now_iso(window_start),'end':now_iso(window_end),'seconds':args.window_seconds,'warmup_seconds':args.warmup_seconds},
            'contaminated_by_other_work':contaminated,
            'useful_completions_in_window':sum(r['useful'] for r in ended_in_window),
            'useful_completions_per_minute':round(sum(r['useful'] for r in ended_in_window)/minutes,2),
            'completions_in_window':len(ended_in_window),
            'all':block(measured),'main':block([r for r in measured if r['model_role']=='main']),'reviewer':block([r for r in measured if r['model_role']=='reviewer']),
            'reviewer_share':{'calls':round(sum(r['model_role']=='reviewer' for r in measured)/max(1,len(measured)),3),
                              'model_execution_time':round(exec_by['reviewer']/total_exec,3) if total_exec else None},
            'per_head_overlap':overlap(measured,window_start,window_end),
            'observed_slots_in_use':{rid:summary([s['slots_in_use'].get(rid) for s in slots]) for rid in sorted({k for s in slots for k in s['slots_in_use']})},
            'gateway_overhead':{'ledger':ledger_delta(before,after),'counters':counters_delta(before,after),
                                'health_cycle_after':after.get('health_cycle'),'placement_after':(after.get('ledger') or {}).get('placement'),
                                'routes_after':after.get('routes')},
            'calls':calls}


def table(report):
    """The summary: one row per finished level (written again after every level)."""
    a=report['gateway'];lines=['G6 capacity check %s (%s), Gateway %s, identity %s'%(report['run_id'],report['at'],a.get('release') or 'before 0.4.6',a.get('application_id')),
        'Levels finished: %d of %d.%s'%(len(report['levels']),len(report['planned_levels']),'' if report.get('complete') else ' (run in progress or stopped)'),
        'Times in seconds; queue = submitted to admitted, dispatch = admitted to model start, execution = model run.',
        '%-6s %10s %7s %7s %13s %13s %13s %15s %13s'%('level','useful/min','useful','failed','queue p50/p95','disp p50/p95','exec p50/p95','overlap max/mean','reviewer c/t')]
    for L in report['levels']:
        q=L['all']['queue_s'];d=L['all']['dispatch_checks_s'];e=L['all']['execution_s'];heads=L['per_head_overlap'].values()
        f=lambda s:'%s/%s'%(round(s['p50'],1) if s['p50'] is not None else '-',round(s['p95'],1) if s['p95'] is not None else '-')
        lines.append('%-6d %10.2f %7d %7d %13s %13s %13s %15s %13s'%(L['level'],L['useful_completions_per_minute'],L['useful_completions_in_window'],
                     L['all']['requests']-L['all']['completed'],f(q),f(d),f(e),
                     '%d/%.2f'%(max([h['max_concurrent'] for h in heads] or [0]),max([h['mean_concurrent'] for h in heads] or [0])),
                     '%s/%s'%(L['reviewer_share']['calls'],L['reviewer_share']['model_execution_time'])))
    lines+=['NOTE: '+n for n in report['notes']]
    lines.append('overlap = the most calls running at once on one head / the highest time-averaged calls on one head; reviewer c/t = share of calls / of model time.')
    return lines


def save(folder,report):
    """report.json and summary.txt after every level, so a stopped run keeps its finished levels."""
    write_json(folder,'report.json',report);(folder/'summary.txt').write_text('\n'.join(table(report))+'\n',encoding='utf-8')


DEFAULT_LEVELS='1,2,4,6,8,10,12,14,16,18,20,22,24'


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    identity_args(p)
    p.add_argument('--levels',default=DEFAULT_LEVELS,help='closed-loop callers per level, comma-separated (default %s)'%DEFAULT_LEVELS)
    p.add_argument('--window-seconds',type=int,default=240);p.add_argument('--warmup-seconds',type=int,default=30)
    p.add_argument('--deadline-seconds',type=int,default=300);p.add_argument('--main',default='qwen3.5:4b');p.add_argument('--reviewer',default='qwen3:4b')
    p.add_argument('--observation',default=str(OBSERVATION));p.add_argument('--yes',action='store_true',help='do not ask for confirmation')
    a=p.parse_args(argv)
    try:levels=[int(x) for x in a.levels.split(',') if x.strip()]
    except ValueError:print('--levels must be numbers, for example 1,2,4,8');return 2
    if not levels or any(not 1<=x<=64 for x in levels) or not 10<=a.window_seconds<=1800 or not 0<=a.warmup_seconds<=300:
        print('Levels 1-64, window 10-1800 s, warm-up 0-300 s');return 2
    if a.cleanup:return run_cleanup(a)
    identity=None;folder=None;report=None
    try:
        mix=fixture(FIXTURE_BLOB)['requests']
        # The fixture is grouped by role; a fixed shuffle interleaves main and reviewer requests so that every window,
        # however short and with however few callers, sends them in about their observed proportion.
        import random;random.Random(46).shuffle(mix)
        if not a.client_config:
            identity=temporary_identity(a)
            if identity.leftover():print('An earlier run left %s behind; it is revoked now.'%IDENTITY_APP);identity.close(final=False)
        doc,idle=require_idle(a.observation,what='the capacity check')
        ledger=identity.ledger if identity else Path(a.gateway_ledger or GATEWAY_DATA/'state'/'capacity-ledger.db')
        found=outside_work(a.observation,ledger,own_app=IDENTITY_APP if identity else None,own_ids=())
        if found['any']:
            raise Refused('The Gateway has active work: %d running and %d queued requests (applications: %s). An experiment may be running, '
                          'so the capacity check does not run. Wait until it has finished, then start this again.'
                          %(found['foreign_running'],found['foreign_queued'],', '.join(found['foreign_apps']) or '-'))
        minutes=len(levels)*(a.warmup_seconds+a.window_seconds)/60
        print('\nG6 capacity check: %s (%s callers), %d s warm-up + %d s per level, about %.0f minutes in all.'
              %('1 level' if len(levels)==1 else '%d levels'%len(levels),','.join(map(str,levels)),a.warmup_seconds,a.window_seconds,minutes+len(levels)*0.3+1))
        print('Models: %s (main), %s (reviewer). The Gateway must stay idle: the test stops if other work appears.'%(a.main,a.reviewer))
        if identity:print('It issues a temporary Gateway identity, %s, and revokes it at the end.'%IDENTITY_APP)
        if not a.yes and input('Start now? Type yes: ').strip().lower()!='yes':raise Refused('Not confirmed')
        # The identity expires by itself after the planned run plus 30 minutes, even if this window is killed.
        config=identity.open(expires_in=len(levels)*(a.warmup_seconds+a.window_seconds+a.deadline_seconds)+1800) if identity else a.client_config
        hint=identity.cleanup_hint('G6-CAPACITY-CHECK.cmd') if identity else None
        if hint:print(hint)
        gateway=Gateway(config);info=gateway.info()
        status,policy,_=gateway.call('GET','/v1/policy?availability=0' if 'policy_without_availability' in info['features'] else '/v1/policy')
        if status!=200:raise Refused('The Gateway refused this identity\'s policy read (HTTP %s)'%status)
        permitted=set((policy.get('policy') or {}).get('models') or [])
        for model in (a.main,a.reviewer):
            if model not in permitted:raise Refused('%s is not permitted for %s'%(model,gateway.app))
        notes=[]
        cap=(policy.get('policy') or {}).get('max_concurrency')
        if cap and cap<max(levels):notes.append('This identity\'s max_concurrency is %d: levels above it measure that limit, not the heads.'%cap)
        folder=results_folder(TOOL);run_id=uuid.uuid4().hex[:8]
        report={'tool':TOOL,'tools_release':TOOLS_RELEASE,'run_id':run_id,'at':now_iso(),'gateway':{**gateway.describe(),**info},
                'models':{'main':a.main,'reviewer':a.reviewer},'mix':{'requests':len(mix),'source':fixture(FIXTURE_BLOB)['source'],
                'by_role':dict(collections.Counter(r['role'] for r in mix))},'idle_at_start':idle,'planned_levels':levels,'notes':notes,'levels':[],
                'complete':False,'identity':identity.report() if identity else {'client_config':'given by --client-config'},
                'scope':'Evidence for the operator; no worker count is chosen. Useful = completed with a valid output for its shape, not task correctness.'}
        save(folder,report)
        if hint:(folder/'IF-INTERRUPTED.txt').write_text(hint+'\n',encoding='utf-8')
        print('Results so far are written after every level to %s'%folder)
        for number,level in enumerate(levels,1):
            try:require_idle(a.observation,what='level %d'%level)
            except Refused as exc:
                if not report['levels']:raise
                notes.append('Stopped before level %d: %s'%(level,exc));break
            out=run_level(gateway,mix,level,a,run_id,a.observation,label='level %d/%d'%(number,len(levels)),ledger=ledger,
                          on_stop=identity.close if identity else None)
            report['levels'].append(out);report['client_calls']=dict(gateway.calls);save(folder,report)
            print('  level %d/%d done: %.2f useful completions per minute, %d failed.'%(number,len(levels),out['useful_completions_per_minute'],
                  out['all']['requests']-out['all']['completed']),flush=True)
            if out['contaminated_by_other_work']:
                notes.append('Level %d stopped: other work appeared in the Gateway (%s); later levels skipped.%s'%(level,out['contaminated_by_other_work'][0],
                             ' The temporary identity was revoked at once.' if identity else ''));break
            # let the last calls of this level end before the next one starts
            deadline=time.time()+a.deadline_seconds
            while time.time()<deadline and activity(read_observation(a.observation))['foreign_leases']:time.sleep(5)
        else:report['complete']=True
        save(folder,report)
        print('\n'+'\n'.join(table(report)));print('Results: %s'%folder);return 0
    except Refused as exc:
        if folder and report and report['levels']:report['notes'].append('Stopped: %s'%exc);save(folder,report);print('Stopped: %s. Finished levels: %s'%(exc,folder));return 1
        return refused_exit(TOOL,exc)
    except KeyboardInterrupt:
        if folder and report:report['notes'].append('Stopped by Ctrl+C.');save(folder,report);print('Stopped. Finished levels are in %s'%folder)
        else:print('Stopped.')
        return 1
    finally:
        if identity:
            identity.close()
            if folder and report:report['identity']=identity.report();save(folder,report)


if __name__=='__main__':raise SystemExit(main())
