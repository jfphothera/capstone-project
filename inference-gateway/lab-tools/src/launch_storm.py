"""G1/G3 launch-storm measurement (Inference Gateway 0.4.6 lab tool). Run on the hub, HUB, with LAUNCH-STORM.cmd.

N simulated worker launches start at the same moment (default 16, 18 and 24, three rounds each) and do only
discovery, no inference:
  rc24      what Capstone 6.0.0-rc.24 does before a trial: GET /v1/models, GET /v1/policy, then GET /v1/availability
            for every advertised model, and one more availability check for the main model's request shape;
  scoped    (Gateway 0.4.6 and later) the new contract: GET /v1/policy?availability=0 and one GET /v1/fleet scoped to
            the trial's models (default qwen3.5:4b and qwen3:4b), operation /api/chat, num_ctx 8192.
Reported per sequence and level: API calls per launch, client-side p50/p95/max per route, the round's wall time,
and the Gateway's own counters over each round from the observation file (per-route counts and times on 0.4.6,
write-lock waits and holds per transaction label, slow requests, 503 answers).

Run it before and after the 0.4.6 update for a before/after on the real hub (the scoped sequence exists only after).
It refuses to start while the Gateway has running or queued work. It only reads: no request is submitted, nothing
is loaded, and no policy, capacity or qualification changes. Results go to results\\ beside this file.
"""
TOOL='launch-storm'


def rc24(gateway,timed,main):
    status,inventory=timed('GET /v1/models',lambda:gateway.call('GET','/v1/models'))
    timed('GET /v1/policy',lambda:gateway.call('GET','/v1/policy'))
    advertised={}
    for r in (inventory or {}).get('resources') or []:
        for m in r.get('models') or []:advertised.setdefault(m,(r.get('model_details') or {}).get(m) or {})
    for m,d in sorted(advertised.items()):
        ops=d.get('qualified_operations') or []
        op='/api/embed' if 'embedding' in ops and 'completion' not in ops else '/api/generate'
        caps=sorted(set(d.get('qualified_capabilities') or [])&{'chat','tools','structured_output','vision','context'})
        q={'model':m,'operation':op};q.update({'capabilities':','.join(caps)} if caps else {})
        timed('GET /v1/availability',lambda q=q:gateway.call('GET','/v1/availability?'+urllib.parse.urlencode(q)))
    timed('GET /v1/availability',lambda:gateway.call('GET','/v1/availability?'+urllib.parse.urlencode({'model':main,'operation':'/api/chat','num_ctx':'8192'})))


def scoped(gateway,timed,models):
    timed('GET /v1/policy?availability=0',lambda:gateway.call('GET','/v1/policy?availability=0'))
    timed('GET /v1/fleet',lambda:gateway.call('GET','/v1/fleet?'+urllib.parse.urlencode({'models':','.join(models),'operations':'/api/chat','num_ctx':'8192'})))


def gateway_load(observation):
    return read_observation(observation).get('gateway_load') or {}


def delta(before,after):
    out={'routes':{},'ledger':{},'counters':{}}
    for route,v in (after.get('routes') or {}).items():
        p=(before.get('routes') or {}).get(route,{})
        n=v.get('count',0)-p.get('count',0)
        if n:out['routes'][route]={'count':n,'ms_total':v.get('ms_total',0)-p.get('ms_total',0),'ms_mean':round((v.get('ms_total',0)-p.get('ms_total',0))/n,1)}
    b=((before.get('ledger') or {}).get('labels') or {})
    for label,v in ((after.get('ledger') or {}).get('labels') or {}).items():
        p=b.get(label,{});n=v.get('count',0)-p.get('count',0)
        if n or v.get('timeouts',0)-p.get('timeouts',0):
            out['ledger'][label]={'count':n,'timeouts':v.get('timeouts',0)-p.get('timeouts',0),'wait_ms_total':v.get('wait_ms_total',0)-p.get('wait_ms_total',0),
                                  'hold_ms_total':v.get('hold_ms_total',0)-p.get('hold_ms_total',0)}
    for k,v in (after.get('counters') or {}).items():
        d=v-(before.get('counters') or {}).get(k,0)
        if d:out['counters'][k]=d
    out['published']={'before':before.get('updated_at'),'after':after.get('updated_at')}
    return out


def storm(gateway,launches,sequence,args):
    timings=collections.defaultdict(list);errors=collections.Counter();lock=threading.Lock();barrier=threading.Barrier(launches);per=[0]*launches
    def launch(i):
        def timed(route,fn):
            began=time.perf_counter()
            try:status,value,_=fn()
            except Exception as exc:status,value=None,None;errors['%s: %s'%(route,type(exc).__name__)]+=1
            with lock:
                timings[route].append((time.perf_counter()-began)*1000);per[i]+=1
                if status not in (200,None):errors['%s: HTTP %s'%(route,status)]+=1
            return status,value
        barrier.wait()
        if sequence=='rc24':rc24(gateway,timed,args.main)
        else:scoped(gateway,timed,args.models.split(','))
    began=time.perf_counter()
    pool=[threading.Thread(target=launch,args=(i,),daemon=True) for i in range(launches)]
    for t in pool:t.start()
    for t in pool:t.join(600)
    wall=(time.perf_counter()-began)*1000
    return {'sequence':sequence,'launches':launches,'wall_ms':round(wall),'api_calls':sum(per),'api_calls_per_launch':round(sum(per)/launches,1),
            'errors':dict(errors),'per_route_ms':{k:summary(v) for k,v in sorted(timings.items())},
            'all_calls_ms':summary([x for v in timings.values() for x in v])}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    identity_args(p);p.add_argument('--levels',default='16,18,24');p.add_argument('--rounds',type=int,default=3)
    p.add_argument('--main',default='qwen3.5:4b');p.add_argument('--models',default='qwen3.5:4b,qwen3:4b')
    p.add_argument('--observation',default=str(OBSERVATION));p.add_argument('--yes',action='store_true')
    a=p.parse_args(argv)
    try:levels=[int(x) for x in a.levels.split(',') if x.strip()]
    except ValueError:print('--levels must be numbers, for example 16,18,24');return 2
    if not levels or any(not 1<=x<=64 for x in levels) or not 1<=a.rounds<=10:print('Levels 1-64 and rounds 1-10');return 2
    if a.cleanup:return run_cleanup(a)
    identity=None
    try:
        if not a.client_config:
            identity=temporary_identity(a)
            if identity.leftover():print('An earlier run left %s behind; it is revoked now.'%IDENTITY_APP);identity.close(final=False)
        doc,idle=require_idle(a.observation,what='the launch-storm measurement')
        ledger=identity.ledger if identity else Path(a.gateway_ledger or GATEWAY_DATA/'state'/'capacity-ledger.db')
        own_app=IDENTITY_APP if identity else None
        found=outside_work(a.observation,ledger,own_app=own_app,own_ids=())
        if found['any']:
            raise Refused('The Gateway has active work: %d running and %d queued requests (applications: %s). An experiment may be running, '
                          'so the launch-storm measurement does not run. Wait until it has finished, then start this again.'
                          %(found['foreign_running'],found['foreign_queued'],', '.join(found['foreign_apps']) or '-'))
        print('\nLaunch storm: levels %s simultaneous launches, %d rounds each. Discovery calls only: nothing is run on a head.'%(','.join(map(str,levels)),a.rounds))
        if identity:print('It issues a temporary Gateway identity, %s, and revokes it at the end.'%IDENTITY_APP)
        if not a.yes and input('Start now? Type yes: ').strip().lower()!='yes':raise Refused('Not confirmed')
        # The identity expires by itself 40 minutes after issue (the storm takes a few), even if this window is killed.
        gateway=Gateway(identity.open(expires_in=2400) if identity else a.client_config);info=gateway.info()
        hint=identity.cleanup_hint('LAUNCH-STORM.cmd') if identity else None
        if hint:print(hint)
        sequences=['rc24']+(['scoped'] if 'fleet_snapshot' in info['features'] else [])
        print('Gateway %s at %s as %s; sequences %s.'%(info.get('release') or 'before 0.4.6',gateway.url,gateway.app,sequences))
        folder=results_folder(TOOL);rounds=[];notes=[]
        if hint:(folder/'IF-INTERRUPTED.txt').write_text(hint+'\n',encoding='utf-8')
        stopped=None
        for level in levels:
            for sequence in sequences:
                for r in range(a.rounds):
                    found=outside_work(a.observation,ledger,own_app=own_app,own_ids=())
                    if found['any']:stopped=found;break
                    # Watch for outside work during the round too (every second): the round's numbers would be mixed.
                    seen=[];done=threading.Event()
                    def watch():
                        while not done.wait(1):
                            f=outside_work(a.observation,ledger,own_app=own_app,own_ids=())
                            if f['any']:seen.append(f);return
                    watcher=threading.Thread(target=watch,daemon=True);watcher.start()
                    before=gateway_load(a.observation)
                    result=storm(gateway,level,sequence,a)
                    done.set();watcher.join(5)
                    if seen:stopped=seen[0];break
                    time.sleep(6) # the in-service publisher writes the counters every 5 s
                    result['gateway']=delta(before,gateway_load(a.observation));result['round']=r+1
                    rounds.append(result)
                    print('  %-6s %2d launches round %d: %5.1f calls/launch, wall %6d ms, p50 %s ms, p95 %s ms%s'%(sequence,level,r+1,result['api_calls_per_launch'],
                          result['wall_ms'],result['all_calls_ms']['p50'],result['all_calls_ms']['p95'],', errors %s'%result['errors'] if result['errors'] else ''),flush=True)
                    time.sleep(2)
                if stopped:break
            if stopped:break
        if stopped:
            print('  OUTSIDE WORK: %d running and %d queued requests of %s: the measurement stops now.'
                  %(stopped['foreign_running'],stopped['foreign_queued'],', '.join(stopped['foreign_apps']) or '-'),flush=True)
            notes.append('Stopped: outside work appeared (%d running, %d queued, %s); the round in progress was discarded.'
                         %(stopped['foreign_running'],stopped['foreign_queued'],', '.join(stopped['foreign_apps']) or '-'))
        if identity:identity.close();notes.append('Temporary identity %s revoked.'%IDENTITY_APP)
        report={'tool':TOOL,'tools_release':TOOLS_RELEASE,'at':now_iso(),'gateway':{**gateway.describe(),**info},'idle_at_start':idle,'rounds':rounds,'notes':notes,
                'identity':identity.report() if identity else {'client_config':'given by --client-config'},'stopped_by_outside_work':stopped,
                'scope':'Client-side times on the hub include TLS and the Gateway handler; the Gateway counters cover all callers in each round.'}
        lines=['Launch storm %s, Gateway %s, identity %s'%(report['at'],info.get('release') or 'before 0.4.6',gateway.app),
               '%-7s %7s %6s %12s %10s %10s %10s'%('seq','launch','round','calls/launch','wall ms','p50 ms','p95 ms')]
        for r in rounds:lines.append('%-7s %7d %6d %12.1f %10d %10s %10s'%(r['sequence'],r['launches'],r['round'],r['api_calls_per_launch'],r['wall_ms'],r['all_calls_ms']['p50'],r['all_calls_ms']['p95']))
        lines+=['NOTE: '+n for n in notes]
        finish(folder,report,lines);return 1 if stopped else 0
    except Refused as exc:return refused_exit(TOOL,exc)
    except KeyboardInterrupt:print('Stopped.');return 1
    finally:
        if identity:identity.close() # repeating it is harmless


if __name__=='__main__':raise SystemExit(main())
