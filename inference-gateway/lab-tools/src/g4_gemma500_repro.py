"""G4 Gemma HTTP 500 reproduction (Inference Gateway 0.4.6 lab tool). Run on the hub, HUB, with G4-GEMMA500-REPRO.cmd.

Replays the saved c922 customer-communication requests of gemma3:4b against ONE idle head and keeps what the head
answers, including the error body that 0.4.5 discarded. The saved requests are only four distinct bodies
(temperature 0, seed 7): the target (REQ-90426799d3ab4e058c1d9f972525a5da, 3 of 3 failed on heads 10, 3 and 9), one
that failed 5 of 5, one that failed 4 of 5, and one that succeeded 5 of 5. The same body failing on several heads
suggests the request content matters; this test checks that on one idle head. It is a hypothesis until it runs.

Steps (sequential; one call at a time):
  1. direct  each of the four bodies exactly as the Gateway sends it (model, messages, format, options,
             keep_alive 2m, stream false) to http://<head>:11434/api/chat, --repeats times;
             then variants of the target: no format; format "json"; num_predict 1024; seed 8; num_ctx 4096.
  2. gateway the target once and the succeeding body once through the Gateway, pinned to the same head, with your
             approved identity. On Gateway 0.4.6 the reply carries upstream_errors (status, message). An HTTP 500
             there suspends gemma3:4b on that head until its automatic recovery probe passes (about 60 s): this is
             the Gateway's normal protection, not a policy change. Skip with --mode direct.

Direct calls bypass Gateway admission, so before each one the tool checks the observation: nothing may be running on
that head (and no queued work anywhere). It refuses to start while the Gateway has any running or queued work.
It changes no policy, model_capacity, qualification record or installed model; it loads only gemma3:4b on the head
(keep_alive 2m, as the Gateway does). It records the head's runtime version and the model's digest and compares
them with the c922 build (Ollama 0.40.15, gemma3:4b 68ceab7e...). Results go to results\\ beside this file.
"""
TOOL='g4-gemma500-repro'
FIXTURE_BLOB='@@FIXTURE@@'
C922_BUILD={'runtime_version':'0.40.15','digest':'68ceab7e468a31c988b44d5a4984a0e21f58c4f7158c6ce29c1804babac38df7'}
MODEL='gemma3:4b'


def head_call(address,path,body=None,timeout=300):
    """One plain HTTP call to the head's Ollama. Returns status, headers, bounded body text and seconds; never raises for HTTP."""
    url='http://%s:11434%s'%(address,path)
    request=urllib.request.Request(url,data=None if body is None else json.dumps(body).encode(),method='POST' if body is not None else 'GET',
                                   headers={'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    began=time.perf_counter()
    try:
        with opener.open(request,timeout=timeout) as response:
            status,headers,data=response.status,dict(response.headers),response.read(16*1024*1024)
    except urllib.error.HTTPError as exc:
        status,headers=exc.code,dict(exc.headers or {})
        try:data=exc.read(65536)
        except Exception:data=b''
    except (urllib.error.URLError,OSError) as exc:
        return {'status':None,'error':'%s: %s'%(type(exc).__name__,exc),'seconds':round(time.perf_counter()-began,3)}
    text=data.decode('utf-8','replace')
    out={'status':status,'seconds':round(time.perf_counter()-began,3),'content_type':headers.get('Content-Type')}
    try:value=json.loads(text)
    except ValueError:value=None
    if status!=200:out['error_body']=text[:65536];out['error_message']=value.get('error') if isinstance(value,dict) else None
    else:out['value']=value
    return out


def judge(result,schema):
    """For a 200 reply: done reason, token counts, timings and whether the content is JSON with the schema's keys."""
    value=result.get('value') or {}
    message=value.get('message') or {}
    content=message.get('content') if isinstance(message.get('content'),str) else ''
    try:parsed=json.loads(content);json_ok=isinstance(parsed,dict)
    except ValueError:parsed=None;json_ok=False
    required=(schema or {}).get('required') or []
    return {'done':value.get('done'),'done_reason':value.get('done_reason'),'eval_count':value.get('eval_count'),
            'prompt_eval_count':value.get('prompt_eval_count'),'load_ms':(value.get('load_duration') or 0)/1e6,
            'total_ms':(value.get('total_duration') or 0)/1e6,'content_chars':len(content),'json':json_ok,
            'schema_keys_present':json_ok and all(k in parsed for k in required),'content_head':content[:200]}


def head_idle(observation,resource_id,own=()):
    doc=read_observation(observation);a=activity(doc,own)
    busy=[l for l in doc.get('leases') or [] if l.get('resource_id')==resource_id and l.get('request_id') not in set(own)
          and not str(l.get('app_id','')).startswith('maintenance:')]
    return (not busy and a['queued_total']==0 and a['age_s']<=OBSERVATION_MAX_AGE),a,doc


def variants(bodies):
    target=next(b for b in bodies if b['label'].startswith('TARGET'))
    def changed(name,fn):
        payload=json.loads(json.dumps(target['payload']));fn(payload);return {'name':name,'body':target['label'],'payload':payload}
    out=[]
    for b in sorted(bodies,key=lambda b:(not b['label'].startswith('TARGET'),b['label'])):
        out.append({'name':'exact','body':b['label'],'payload':b['payload'],'observed_in_c922':b['observed_summary']})
    out+=[changed('no_format',lambda p:p.pop('format',None)),changed('format_json',lambda p:p.__setitem__('format','json')),
          changed('num_predict_1024',lambda p:p['options'].__setitem__('num_predict',1024)),changed('seed_8',lambda p:p['options'].__setitem__('seed',8)),
          changed('num_ctx_4096',lambda p:p['options'].__setitem__('num_ctx',4096))]
    return out


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--head',default='192.0.2.10',help='head address 192.0.2.N (default 192.0.2.10, where the target failed first)')
    p.add_argument('--mode',choices=('both','direct','gateway'),default='both');p.add_argument('--repeats',type=int,default=3)
    p.add_argument('--client-config',help='needed for the gateway step');p.add_argument('--observation',default=str(OBSERVATION))
    p.add_argument('--yes',action='store_true')
    a=p.parse_args(argv)
    if not a.head.startswith('192.0.2.') or not 1<=a.repeats<=10:print('--head 192.0.2.N and --repeats 1-10');return 2
    resource_id='head-'+a.head.replace('.','-')
    try:
        data=fixture(FIXTURE_BLOB);bodies=data['bodies']
        doc,idle=require_idle(a.observation,what='the Gemma 500 reproduction')
        resource=next((r for r in doc.get('resources') or [] if r.get('resource_id')==resource_id),None)
        if not resource:raise Refused('%s is not a Gateway head'%resource_id)
        inventory=next((i for i in doc.get('inventory') or [] if i.get('resource_id')==resource_id),{})
        installed={m['name']:m.get('digest') for m in inventory.get('models') or []}
        if MODEL not in installed:raise Refused('%s is not installed on %s (Gateway inventory); choose another head with --head'%(MODEL,resource_id))
        gateway=None
        if a.mode in ('both','gateway'):
            if a.client_config:gateway=Gateway(a.client_config)
            elif a.mode=='gateway':raise Refused('The Gateway step needs --client-config of an identity permitted to use %s'%MODEL)
            else:print('No --client-config given: only the direct step runs (the Gateway step needs an identity permitted to use %s).'%MODEL)
        info=gateway.info() if gateway else None
        plan=variants(bodies) if a.mode in ('both','direct') else []
        calls=len(plan[:len(bodies)])*a.repeats+len(plan[len(bodies):])+(2 if gateway else 0)
        print('\nG4 Gemma 500 reproduction on %s (%s): %d calls of %s, one at a time.'%(resource_id,a.head,calls,MODEL))
        if gateway:print('The Gateway step may suspend %s on this head for about a minute if it reproduces the 500.'%MODEL)
        if not a.yes and input('Start now? Type yes: ').strip().lower()!='yes':raise Refused('Not confirmed')
        folder=results_folder(TOOL)
        report={'tool':TOOL,'tools_release':TOOLS_RELEASE,'at':now_iso(),'head':{'address':a.head,'resource_id':resource_id,'health':resource.get('health'),
                'gateway_inventory_digest':installed.get(MODEL),'qualified_in_gateway':MODEL in (resource.get('qualified_models') or [])},
                'c922_build':C922_BUILD,'source':data['source'],'target_request_id':data['target_request_id'],'idle_at_start':idle,
                'gateway':{**gateway.describe(),**info} if gateway else None,'direct':[],'gateway_replays':[],
                'notes':[] if gateway or a.mode=='direct' else ['Gateway step skipped: no --client-config (an identity permitted to use %s).'%MODEL]}
        version=head_call(a.head,'/api/version',timeout=10);tags=head_call(a.head,'/api/tags',timeout=10);ps_before=head_call(a.head,'/api/ps',timeout=10)
        live_digest=next((m.get('digest') for m in ((tags.get('value') or {}).get('models') or []) if m.get('name')==MODEL),None)
        runtime=(version.get('value') or {}).get('version')
        report['head'].update(runtime_version=runtime,live_digest=live_digest,loaded_before=[m.get('name') for m in ((ps_before.get('value') or {}).get('models') or [])],
                              same_build_as_c922=bool(runtime==C922_BUILD['runtime_version'] and live_digest==C922_BUILD['digest']))
        if not report['head']['same_build_as_c922']:
            report['notes'].append('The head does not run the c922 build (runtime %s, digest %s): results qualify the current build only.'%(runtime,(live_digest or '')[:12]))
        for v in plan:
            for i in range(a.repeats if v['name']=='exact' else 1):
                ok,act,_=head_idle(a.observation,resource_id)
                if not ok:
                    report['notes'].append('Stopped the direct step: other Gateway work appeared (%s).'%act);break
                body={'model':MODEL,**v['payload'],'keep_alive':'2m','stream':False}
                print('  direct %-17s %-17s #%d ...'%(v['name'],v['body'],i+1),end='',flush=True)
                at=time.time();result=head_call(a.head,'/api/chat',body)
                row={'variant':v['name'],'body':v['body'],'repeat':i+1,'at':now_iso(at),'status':result.get('status'),'seconds':result.get('seconds'),
                     'observed_in_c922':v.get('observed_in_c922')}
                if result.get('status')==200:row.update(judge(result,v['payload'].get('format') if isinstance(v['payload'].get('format'),dict) else None))
                else:row.update(error_message=result.get('error_message'),error_body=result.get('error_body'),transport_error=result.get('error'))
                report['direct'].append(row);print(' %s %s'%(row['status'],row.get('error_message') or row.get('done_reason') or row.get('transport_error') or ''))
            else:continue
            break
        if gateway:
            for label in [b['label'] for b in bodies if b['label'].startswith('TARGET')]+[b['label'] for b in bodies if b['observed_summary'].startswith('0 of')]:
                ok,act,_=head_idle(a.observation,resource_id)
                if not ok:report['notes'].append('Skipped the Gateway step: other work appeared (%s).'%act);break
                b=next(x for x in bodies if x['label']==label)
                print('  gateway %-17s ...'%label,end='',flush=True)
                envelope,seconds=gateway.run(MODEL,'/api/chat',b['payload'],client_key='labtool-g4-%s-%s'%(uuid.uuid4().hex[:8],label),nodes=[resource_id],
                                             deadline_seconds=180)
                row={'body':label,'status':envelope.get('status'),'error':envelope.get('error'),'upstream_errors':envelope.get('upstream_errors'),
                     'resource_id':(envelope.get('request') or {}).get('resource_id'),'request_id':(envelope.get('request') or {}).get('request_id'),
                     'seconds':round(seconds,3),'http_status':envelope.get('http_status')}
                report['gateway_replays'].append(row);print(' %s %s'%(row['status'],(row['upstream_errors'] or [{}])[0].get('message') or row['error'] or ''))
                if row['status']!='completed':
                    report['notes'].append('Gateway step stopped after %s ended %s: a 5xx suspends %s on this head until its recovery probe passes '
                                           '(lifecycle suspensions shows it).'%(label,row['status'],MODEL));break
            if info and 'upstream_error_capture' not in info['features']:
                report['notes'].append('This Gateway predates 0.4.6: its replies carry no upstream_errors (the direct step has the body).')
        ps_after=head_call(a.head,'/api/ps',timeout=10)
        report['head']['loaded_after']=[m.get('name') for m in ((ps_after.get('value') or {}).get('models') or [])]
        tally=collections.OrderedDict()
        for r in report['direct']:
            key='%s / %s'%(r['body'],r['variant']);t=tally.setdefault(key,collections.Counter());t['HTTP %s'%r['status']]+=1
        report['tally']={k:dict(v) for k,v in tally.items()}
        messages=sorted({r.get('error_message') or (r.get('error_body') or '')[:200] for r in report['direct'] if r['status'] not in (200,None)})
        report['distinct_error_messages']=messages
        lines=['G4 Gemma 500 reproduction, %s (%s), runtime %s, digest %s, same build as c922: %s'%(resource_id,report['at'],runtime,(live_digest or '?')[:12],
               report['head']['same_build_as_c922'])]
        lines+=['  %-40s %s'%(k,', '.join('%s x%d'%kv for kv in v.items())) for k,v in report['tally'].items()]
        lines+=['  error: '+m for m in messages]
        lines+=['  gateway %s: %s %s'%(r['body'],r['status'],(r['upstream_errors'] or [{}])[0].get('message') or '') for r in report['gateway_replays']]
        lines+=['NOTE: '+n for n in report['notes']]
        lines.append('Interpretation is for the engineers: a body that fails on an idle head while its variants succeed points at the request shape; '
                     'no failure on an idle head leaves load or runner state open. Nothing here is a fix.')
        finish(folder,report,lines);return 0
    except Refused as exc:return refused_exit(TOOL,exc)
    except KeyboardInterrupt:print('Stopped.');return 1


if __name__=='__main__':raise SystemExit(main())
