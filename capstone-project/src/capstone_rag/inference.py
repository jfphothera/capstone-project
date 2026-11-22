"""One private-LAN router, pinned run configuration and cross-process endpoint slots."""
import contextvars,copy,functools,json,os,re,sqlite3,time,uuid,urllib.error
from contextlib import contextmanager,nullcontext
from .ai_settings import Registry,canonical,fingerprint,http
from . import task_context,runtime_identity
from . import gateway_transport
_SESSION=contextvars.ContextVar('inference_session',default=None)
_ROLE=contextvars.ContextVar('inference_specialist',default='investigation')
SCHEMA="""CREATE TABLE IF NOT EXISTS inference_snapshots(snapshot_id TEXT PRIMARY KEY,scope_key TEXT NOT NULL,profile_id TEXT NOT NULL,configuration_hash TEXT NOT NULL,payload TEXT NOT NULL,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS inference_calls(call_id TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL,case_id TEXT,conversation_id TEXT,investigation_id TEXT,role TEXT NOT NULL,endpoint_id TEXT NOT NULL,model TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS response_reviews(review_id TEXT PRIMARY KEY,message_id TEXT NOT NULL,body_hash TEXT NOT NULL,passed INTEGER NOT NULL,payload TEXT NOT NULL,at REAL NOT NULL);"""
def ensure(store):
    for statement in SCHEMA.split(';'):
        if statement.strip():store.execute(statement)
    for table in ('inference_snapshots','inference_calls','response_reviews'):
        for operation in ('UPDATE','DELETE'):
            store.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable inference evidence'); END")
def current():return _SESSION.get()
def raise_if_failed():
    """Fail an experimental workflow as soon as its pinned route has failed.

    Operational workflows may deliberately record a controlled finding and
    continue to a human review.  Testing Lab runs may not silently substitute
    that deterministic continuation for the methodology being measured.
    """
    state=current()
    if state and state.get('error') and getattr(state.get('store'),'arena_experiment_run_id',None):
        raise RuntimeError('Selected inference route failed; no alternate model was substituted: '+state['error'])
THINKING_MODEL_PREFIXES=('qwen','deepseek-r1','qwq','magistral','phi4-reasoning','gpt-oss')

class ContextBudgetError(ValueError):
    """Input + reserved output does not fit the requested context (checked before sending)."""

def estimate_tokens(value):
    """Clearly labeled ESTIMATE (characters / 4). Real counts come from the backend
    response (prompt_eval_count, eval_count) and are recorded separately."""
    text=json.dumps(value,default=str) if not isinstance(value,str) else value
    return max(1,len(text)//4)

def context_fit(request,options):
    payload={k:request.get(k) for k in ('messages','prompt','system','format','tools') if request.get(k) is not None}
    estimate=estimate_tokens(payload);reserved=int(options.get('num_predict') or 0);limit=options.get('num_ctx')
    if not limit:
        return {'fits':True,'input_tokens_estimate':estimate,'reserved_output_tokens':reserved,'num_ctx':None,
                'note':'no num_ctx requested; the serving default applies and is unverified'}
    fits=estimate+reserved<=int(limit)
    return {'fits':fits,'input_tokens_estimate':estimate,'reserved_output_tokens':reserved,'num_ctx':int(limit),
            'headroom_tokens':int(limit)-estimate-reserved,
            'reason':None if fits else f'estimated input {estimate} + reserved output {reserved} tokens exceeds requested context {limit}; not sent (would be truncated or refused)'}

_HTTP_400=re.compile(r'HTTP (Error )?400\b')
def native_tools_rejected(exc):
    """A tool-carrying request refused with HTTP 400: the model/serving path has
    no native tool calling (direct urllib says 'HTTP Error 400', the Gateway
    client says 'Gateway HTTP 400'). Oversize prompts never get this far: the
    pre-inference context check refuses them first."""
    return (isinstance(exc,urllib.error.HTTPError) and exc.code==400) or bool(_HTTP_400.search(str(exc))) or 'does not support tools' in str(exc)
def model():
    from . import config
    s=current();return s['snapshot']['routes'].get(_ROLE.get(),s['snapshot']['routes']['investigation'])['model'] if s else config.OLLAMA_MODEL
def enabled(role):
    s=current();return not s or s['snapshot']['routes'].get(role,{}).get('enabled',True)
@contextmanager
def specialist(role):
    token=_ROLE.set(role if role in RegistryRoles() else 'investigation')
    try:yield
    finally:_ROLE.reset(token)
def RegistryRoles():
    from .ai_settings import ROLES,LAB_ROLES
    return ROLES+LAB_ROLES
def route_setting(key,default=None):
    """A setting on the active role's route (e.g. tool_strategy)."""
    s=current()
    if not s:return default
    return (s['snapshot']['routes'].get(_ROLE.get()) or {}).get(key,default)
def current_role():return _ROLE.get()
def physical_node(store,url):
    row=store.one('SELECT node_id FROM worker_registry WHERE endpoint=?',(url,))
    return row['node_id'] if row else url
def task(role):
    def decorate(fn):
        @functools.wraps(fn)
        def call(*args,**kwargs):
            chosen=role
            if role=='structured':
                purpose=kwargs.get('purpose','investigation')
                chosen={'tree_of_thought':'resolution_planning','tot_planning':'resolution_planning','operational_tree_of_thought':'resolution_planning','customer_composition':'customer_communication'}.get(purpose,purpose)
            with specialist(chosen):return fn(*args,**kwargs)
        return call
    return decorate
@contextmanager
def session(store,investigation_id,case_id=None,conversation_id=None,snapshot=None):
    parent=current()
    if parent:
        yield parent
        return
    if not getattr(store,'routing_enabled',False) and snapshot is None:
        yield None
        return
    ensure(store);registry=getattr(store,'inference_registry',None) or Registry()
    scope='case:'+case_id if case_id else 'conversation:'+str(conversation_id or investigation_id)
    prior=store.one('SELECT * FROM inference_snapshots WHERE scope_key=? ORDER BY at DESC LIMIT 1',(scope,))
    if snapshot is None and prior:
        effective=json.loads(prior['payload']);sid=prior['snapshot_id']
    else:
        effective=copy.deepcopy(snapshot or getattr(store,'inference_profile_snapshot',None) or registry.snapshot())
        sid='CFG-'+uuid.uuid4().hex
        store.insert('inference_snapshots',dict(snapshot_id=sid,scope_key=scope,profile_id=effective['id'],configuration_hash=effective['configuration_hash'],payload=canonical(effective),at=time.time()))
    state={'store':store,'registry':registry,'snapshot':effective,'snapshot_id':sid,'investigation_id':investigation_id,'case_id':case_id,'conversation_id':conversation_id,'error':None,'deadline':time.monotonic()+effective.get('case_budget_seconds',300)}
    token=_SESSION.set(state)
    from . import activity
    activity.emit('configuration','Effective inference configuration pinned',evidence={'snapshot_id':sid,'profile_id':effective['id'],'configuration_hash':effective['configuration_hash'],'retrieval_profile':effective.get('retrieval_profile','minilm'),'routes':effective['routes']})
    try:
        yield state
        if state['error']:raise RuntimeError('Selected inference route failed; no alternate model was substituted: '+state['error'])
    finally:_SESSION.reset(token)
def link_case(case_id):
    s=current()
    if s:
        s['case_id']=case_id;scope='case:'+case_id
        if not s['store'].one('SELECT 1 FROM inference_snapshots WHERE scope_key=?',(scope,)):
            value=s['snapshot'];s['store'].insert('inference_snapshots',dict(snapshot_id='CFG-'+uuid.uuid4().hex,scope_key=scope,profile_id=value['id'],configuration_hash=value['configuration_hash'],payload=canonical(value),at=time.time()))
@contextmanager
def _local_endpoint_slot(registry,url,deadline):
    owner=uuid.uuid4().hex;start=time.perf_counter();previous=None
    while True:
        if time.monotonic()>=deadline:raise TimeoutError('Inference endpoint slot wait exceeded the run budget')
        with registry.connect() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT owner,expires,last_model FROM inference_slots WHERE endpoint=?',(url,)).fetchone()
            if not row or not row[0] or row[1]<time.time():
                previous=row[2] if row else None
                db.execute('INSERT OR REPLACE INTO inference_slots VALUES (?,?,?,?)',(url,owner,time.time()+max(1,deadline-time.monotonic())+30,previous));break
        time.sleep(.1)
    try:yield (time.perf_counter()-start)*1000,previous
    finally:
        with registry.connect() as db:db.execute('UPDATE inference_slots SET owner=NULL,expires=0 WHERE endpoint=? AND owner=?',(url,owner))
@contextmanager
def endpoint_slot(registry,url,deadline,model_id=None):
    # Direct development mode retains only its process-local collision guard.
    # Shared admission is exclusively owned by the standalone gateway and is
    # reached through gateway_transport when gateway mode is enabled.
    with _local_endpoint_slot(registry,url,deadline) as slot:yield slot
def infer(path,payload,timeout):
    s=current()
    if not s:raise RuntimeError('Routed inference needs a pinned session')
    role=_ROLE.get()
    from .ai_settings import LAB_ROLES
    if s['error'] and role not in LAB_ROLES:raise RuntimeError('Earlier inference transport failure stopped this session: '+s['error'])
    route=s['snapshot']['routes'].get(role)
    route_fallback=None
    if route is None:
        # Never silently borrow another role's model: record the fallback so a
        # run cannot count as evidence for a configuration it did not use.
        if role in LAB_ROLES:raise RuntimeError(f'No route configured for lab participant {role}; it never borrows a system-under-test model')
        fallback='resolution_planning' if role in ('supervisor','peer_reviewer','reflection') else 'investigation'
        route=s['snapshot']['routes'][fallback];route_fallback=fallback
    endpoint=s['snapshot']['endpoints'][route['endpoint']]
    gateway_mode=s['snapshot'].get('execution_mode')=='distributed'
    gateway_workload=s['snapshot'].get('gateway_workload','operational')
    if endpoint.get('transport')=='gateway' and not gateway_mode and os.environ.get('CAPSTONE_TEST_ALLOW_LOCAL_EMBEDDING')!='1':
        raise PermissionError('A gateway route cannot fall back to direct transport')
    if not route.get('enabled',True):raise RuntimeError('Inference is disabled for '+role)
    transport_timeout=float(timeout) if gateway_mode else min(float(timeout),endpoint['timeout'])
    deadline=min(s['deadline'],time.monotonic()+transport_timeout);begin=time.perf_counter();call_id='CALL-'+uuid.uuid4().hex
    record={'call_id':call_id,'snapshot_id':s['snapshot_id'],'profile_id':s['snapshot']['id'],'configuration_hash':s['snapshot']['configuration_hash'],'role':role,'endpoint_id':endpoint['id'],'endpoint_url':endpoint['url'],'model':route['model'],'request':copy.deepcopy(payload),'slot_wait_ms':0,'model_switch_ms':0,'status':'failed',
            'requested_model':route['model'],'route_fallback':route_fallback,'route_inherited_from':route.get('inherited_from'),
            'requested_num_ctx':(route.get('options') or {}).get('num_ctx'),'requested_num_predict':None}
    context=task_context.current() or {}
    logical_agent=context.get('agent_id')
    task_id=context.get('task_id')
    workflow_id=context.get('workflow_id') or s['investigation_id']
    correlation={'route_role':role,'logical_agent_status':'known' if logical_agent else 'unavailable',
                 'deadline_at':context.get('deadline_at'),'inference_deadline_seconds':max(0,deadline-time.monotonic()),
                 'request_id':call_id,'task_id':task_id,'workflow_id':workflow_id}
    record.update(correlation,logical_agent_id=logical_agent,execution_node=runtime_identity.coordinator_id(),session_scope_id=s['investigation_id'])
    from . import activity, telemetry
    telemetry.emit(s['store'],'inference.queued',case_id=s['case_id'],
        workflow_id=workflow_id,task_id=task_id,agent_id=logical_agent,correlation_id=call_id,
        inference_node=None if gateway_mode or route.get('cluster_nodes') else (physical_node(s['store'],endpoint['url']) if os.environ.get('CAPSTONE_TEST_ALLOW_LOCAL_EMBEDDING')=='1' else runtime_identity.coordinator_id()),model_id=route['model'],endpoint_id=endpoint['id'],
        task_status='queued',summary=f'{role.replace("_", " ")} inference queued',detail=correlation)
    try:
        cluster_worker=None
        if route.get('cluster_nodes') and not gateway_mode:
            cluster=getattr(s['store'],'cluster_service',None)
            if cluster is None:raise RuntimeError('Qualified cluster service unavailable')
            cluster_worker=cluster.route(route['model'],route['cluster_nodes'],call_id,workflow_id)
            endpoint={'id':cluster_worker['worker_id'],'url':cluster_worker['endpoint'],'timeout':endpoint['timeout']}
            record.update(endpoint_id=endpoint['id'],endpoint_url=endpoint['url'],
                          inference_node=cluster_worker['node_id'],execution_node=runtime_identity.coordinator_id(),mcp_node=runtime_identity.coordinator_id())
        slot=nullcontext((0,None)) if gateway_mode else endpoint_slot(s['registry'],endpoint['url'],deadline,route['model'])
        with slot as (wait,previous):
            record['slot_wait_ms']=round(wait,3)
            if not gateway_mode:telemetry.emit(s['store'],'inference.started',case_id=s['case_id'],
                workflow_id=workflow_id,task_id=task_id,agent_id=logical_agent,correlation_id=call_id,
                inference_node=physical_node(s['store'],endpoint['url']) if os.environ.get('CAPSTONE_TEST_ALLOW_LOCAL_EMBEDDING')=='1' else runtime_identity.coordinator_id(),model_id=route['model'],endpoint_id=endpoint['id'],
                task_status='running',summary=f'{role.replace("_", " ")} inference started',
                detail={**correlation,'slot_wait_ms':record['slot_wait_ms'],'lease_seconds':min(120,max(1,deadline-time.monotonic()))})
            if previous and previous!=route['model']:
                start=time.perf_counter();http(endpoint['url'],'/api/generate',{'model':previous,'keep_alive':0},timeout=max(1,min(60,deadline-time.monotonic())));record['model_switch_ms']=round((time.perf_counter()-start)*1000,3)
            request=copy.deepcopy(payload);request['model']=route['model'];request['stream']=False;request['keep_alive']='2m'
            options={**payload.get('options',{}),**route.get('options',{})}
            options['num_predict']=min(int(payload.get('options',{}).get('num_predict',600)),int(options.get('num_predict',600)))
            request['options']=options
            record['requested_num_predict']=options['num_predict'];record['requested_num_ctx']=options.get('num_ctx')
            fit=context_fit(request,options)
            record['budget_check']=fit
            if not fit['fits']:
                raise ContextBudgetError(fit['reason'])
            budget_id=s['snapshot'].get('campaign_budget_id')
            if budget_id:
                # Reserve the maximum output allocation before sending. Failed
                # requests remain charged conservatively; unknown generation is
                # never refunded. One durable budget is shared by all workers.
                reservation=int(options['num_predict'])
                with s['registry'].connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    changed=db.execute('UPDATE campaign_budgets SET remaining_tokens=remaining_tokens-?,remaining_calls=remaining_calls-1 WHERE experiment_id=? AND remaining_tokens>=? AND remaining_calls>0',
                                       (reservation,budget_id,reservation)).rowcount
                    if not changed:raise ValueError('Campaign model-call/output-token budget exhausted before transport')
            # Gemma models do not advertise Qwen's thinking toggle.
            # Send think:false only to models with a thinking toggle; others reject it.
            if not route['model'].lower().startswith(THINKING_MODEL_PREFIXES):request.pop('think',None)
            record['request']=request
            activity.emit('inference',f"{role}: {route['model']} requested",'awaiting_model_response',{k:v for k,v in record.items() if k!='request'})
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Inference budget expired before request')
            if gateway_mode:
                def progress(update):
                    assigned=update['request']
                    if update.get('execution_started_at') and assigned.get('resource_id') and not record.get('gateway_started'):
                        record.update(gateway_started=True,gateway_request_id=assigned['request_id'],
                            endpoint_id=assigned['resource_id'],inference_node=assigned['resource_id'],
                            gateway_execution_started_at=update['execution_started_at'])
                        telemetry.emit(s['store'],'inference.started',case_id=s['case_id'],workflow_id=workflow_id,task_id=task_id,
                            agent_id=logical_agent,correlation_id=call_id,inference_node=assigned['resource_id'],
                            endpoint_id=assigned['resource_id'],model_id=route['model'],task_status='running',
                            summary='Gateway dispatched model execution',detail={**correlation,'gateway_request_id':assigned['request_id'],'lease_id':assigned['lease_id'],'dispatch_at':update['execution_started_at']})
                    elif record.get('gateway_started'):
                        telemetry.emit(s['store'],'inference.heartbeat',case_id=s['case_id'],workflow_id=workflow_id,task_id=task_id,
                            agent_id=logical_agent,correlation_id=call_id,inference_node=record.get('inference_node'),
                            endpoint_id=record.get('endpoint_id'),model_id=record.get('model',route['model']),task_status='running',
                            summary='Gateway model execution heartbeat',detail={**correlation,'gateway_request_id':record.get('gateway_request_id') or assigned.get('request_id'),'lease_seconds':60})
                envelope=gateway_transport.invoke(s['store'],endpoint,route['model'],path,request,remaining,
                    nodes=route.get('cluster_nodes'),request_id=call_id,progress=progress,
                    expected_digest=route.get('expected_digest'),
                    required_capabilities=route.get('required_capabilities'),workload=gateway_workload,
                    check_availability=not bool(s['snapshot'].get('capacity_preflight_verified')))
                provenance=envelope.get('provenance') or {}
                executed_model=envelope.get('model') or provenance.get('executed_model') or route['model']
                record.update(gateway=envelope,endpoint_url=None,model_digest=envelope.get('model_digest') or provenance.get('executed_digest'),
                    endpoint_id=envelope['resource_id'],inference_node=envelope['resource_id'],
                    model=executed_model,gateway_request_id=envelope.get('request_id') or provenance.get('request_id'),gateway_provenance=provenance)
                if envelope.get('upstream_errors'):
                    # Gateway 0.4.6 (G4): protected-summary of what a head answered
                    # on an earlier attempt; diagnostic evidence only.
                    record['gateway_upstream_errors']=envelope['upstream_errors']
                if not record.get('gateway_started'):
                    record['gateway_started']=True
                    telemetry.emit(s['store'],'inference.started',case_id=s['case_id'],workflow_id=workflow_id,task_id=task_id,
                        agent_id=logical_agent,correlation_id=call_id,inference_node=envelope['resource_id'],
                        endpoint_id=envelope['resource_id'],model_id=executed_model,task_status='running',
                        summary='Gateway dispatched model execution',detail={**correlation,
                            'gateway_request_id':record['gateway_request_id'],
                            'executed_digest':record['model_digest'],
                            'dispatch_at':envelope.get('execution_started_at') or provenance.get('execution_started_at')})
                s['last_inference_node']=envelope['resource_id']
                result=envelope['response']
            else:result=http(endpoint['url'],path,request,timeout=remaining)
            if budget_id and isinstance(result.get('eval_count'),int):
                unused=max(0,reservation-result['eval_count'])
                with s['registry'].connect() as db:db.execute('UPDATE campaign_budgets SET remaining_tokens=remaining_tokens+? WHERE experiment_id=?',(unused,budget_id))
            with s['registry'].connect() as db:db.execute('UPDATE inference_slots SET last_model=? WHERE endpoint=?',(route['model'],endpoint['url']))
            # Private reasoning is never kept or treated as the answer; only its size is recorded.
            record['thinking_chars']=len(str((result.get('message') or {}).get('thinking') or result.get('thinking') or ''))
            if isinstance(result.get('message'),dict):result['message'].pop('thinking',None)
            result.pop('thinking',None)
            record['response']=result
            record['timings']={key:result.get(key) for key in ('total_duration','load_duration','prompt_eval_duration','eval_duration','prompt_eval_count','eval_count','done_reason')}
            # Persist the effective request budget beside the backend's finish
            # evidence.  The complete raw response remains in ``response`` so
            # malformed or partial JSON is diagnosable and is never promoted
            # to a successful decision.
            record['effective_settings']={'num_ctx':options.get('num_ctx'),'num_predict':options.get('num_predict')}
            record['finish_reason']=result.get('done_reason')
            record['available_output_tokens']=(max(0,int(options['num_predict'])-int(result['eval_count']))
                                               if isinstance(result.get('eval_count'),int) else None)
            if result.get('model') and result['model']!=route['model']:raise RuntimeError('Ollama returned a different model than requested')
            try:record['resources']={'status':'gateway_owned'} if gateway_mode else http(endpoint['url'],'/api/ps',timeout=min(3,max(.1,deadline-time.monotonic())))
            except Exception as exc:record['resource_probe_error']=str(exc)
            record['status']='completed'
            return result
    except Exception as exc:
        record['error']=str(exc)
        if isinstance(exc,gateway_transport.GatewayExecutionError):
            record['gateway_envelope']=exc.envelope
            request=exc.envelope.get('request') or {}
            record['gateway_request_id']=request.get('request_id')
            if exc.envelope.get('upstream_errors'):record['gateway_upstream_errors']=exc.envelope['upstream_errors']
            record['gateway_execution_uncertain']=isinstance(exc,gateway_transport.GatewayUnknownExecution)
        # Tool-capability rejection can be corrected with structured output using
        # the SAME selected model. Availability/budget failures cannot disappear.
        # Native tool calling rejected by the model/Gateway (HTTP 400) is recoverable:
        # the role falls back to application-mediated tool selection on the SAME model.
        recoverable=bool(payload.get('tools')) and native_tools_rejected(exc)
        if recoverable:record['native_tools_rejected']=True
        if isinstance(exc,ContextBudgetError):record['budget_failure']=True
        # A lab participant's failure (simulated reviewer) is recorded against
        # that participant; it must not stop the system under test.
        from .ai_settings import LAB_ROLES
        if not recoverable and role not in LAB_ROLES:s['error']=str(exc)
        raise
    finally:
        if locals().get('cluster_worker'):
            s['store'].cluster_service.enrollment.workers.release(cluster_worker['worker_id'])
        record['duration_ms']=round((time.perf_counter()-begin)*1000,3)
        s['store'].insert('inference_calls',dict(call_id=call_id,snapshot_id=s['snapshot_id'],case_id=s['case_id'],conversation_id=s['conversation_id'],investigation_id=workflow_id,role=role,endpoint_id=record['endpoint_id'],model=route['model'],status=record['status'],payload=canonical(record),at=time.time()))
        activity.emit('inference',f"{role}: {route['model']} {record['status']}",record['status'],{k:v for k,v in record.items() if k not in ('request','response')},record['duration_ms'])
        telemetry.emit(s['store'],'inference.completed' if record['status']=='completed' else 'inference.failed',
            case_id=s['case_id'],workflow_id=workflow_id,task_id=task_id,agent_id=logical_agent,
            correlation_id=call_id,inference_node=record.get('inference_node') if gateway_mode else (physical_node(s['store'],endpoint['url']) if os.environ.get('CAPSTONE_TEST_ALLOW_LOCAL_EMBEDDING')=='1' else runtime_identity.coordinator_id()),model_id=record.get('model',route['model']),
            endpoint_id=record['endpoint_id'],task_status=record['status'],
            summary=f'{role.replace("_", " ")} inference {record["status"]}',
            detail={**correlation,'duration_ms':record['duration_ms'],'slot_wait_ms':record['slot_wait_ms'],
                    'gateway_request_id':record.get('gateway_request_id'),'executed_digest':record.get('model_digest'),
                    'queue_ms':(record.get('gateway') or {}).get('queue_ms'),
                    'model_switch_ms':record['model_switch_ms'],
                    'load_duration_ns':(record.get('timings') or {}).get('load_duration'),
                    'inference_duration_ns':(record.get('timings') or {}).get('eval_duration'),
                    'total_model_duration_ns':(record.get('timings') or {}).get('total_duration')})

def embed(registry,endpoint,model,texts,expected_digest=None,timeout=120):
    """Embedding requests share the same private transport and endpoint slot."""
    from . import activity
    start=time.perf_counter();deadline=time.monotonic()+timeout
    state=current();context=task_context.current() or {};call_id='EMBED-'+uuid.uuid4().hex
    gateway_mode=True;node=None;completed=False;gateway_detail={}
    def event(kind,status,detail=None):
        if not state:return
        from . import telemetry
        telemetry.emit(state['store'],kind,case_id=state['case_id'],workflow_id=context.get('workflow_id') or state['investigation_id'],
            task_id=context.get('task_id'),agent_id=context.get('agent_id'),correlation_id=call_id,
            inference_node=node,model_id=model,endpoint_id=endpoint['id'],task_status=status,
            summary='Embedding '+status,detail={'operation':'embedding','route_role':'retrieval_embedding',
                'logical_agent_status':'known' if context.get('agent_id') else 'unavailable',**(detail or {})})
    event('inference.queued','queued')
    try:
        if gateway_mode:
            dispatched=False
            def progress(update):
                nonlocal node,dispatched
                if update.get('execution_started_at') and update['request'].get('resource_id') and not dispatched:
                    dispatched=True;node=update['request']['resource_id']
                    event('inference.started','running',{'gateway_request_id':update['request']['request_id'],'dispatch_at':update['execution_started_at']})
                elif dispatched:
                    event('inference.heartbeat','running',{'gateway_request_id':update['request']['request_id'],'lease_seconds':60})
            workload=(state or {}).get('snapshot',{}).get('gateway_workload','operational')
            routes=(state or {}).get('snapshot',{}).get('routes',{})
            pinned_nodes=next((route.get('cluster_nodes') for route in routes.values() if route.get('cluster_nodes')),None)
            envelope=gateway_transport.invoke(state['store'] if state else None,endpoint,model,'/api/embed',
                {'input':texts,'truncate':False},timeout,expected_digest=expected_digest,
                required_capabilities=(),progress=progress,request_id=call_id,workload=workload,nodes=pinned_nodes)
            response=envelope['response']
            node=envelope['resource_id']
            provenance=envelope.get('provenance') or {}
            gateway_detail={'gateway_request_id':envelope.get('request_id') or provenance.get('request_id'),
                'executed_digest':envelope.get('model_digest') or provenance.get('executed_digest'),
                'queue_ms':envelope.get('queue_ms')}
            if not dispatched:
                event('inference.started','running',{**gateway_detail,
                    'dispatch_at':envelope.get('execution_started_at') or provenance.get('execution_started_at')})
            completed=True
            return response['embeddings'],{'duration_ms':(time.perf_counter()-start)*1000,
                'model_digest':envelope.get('model_digest'),'gateway_resource_id':envelope['resource_id'],
                'slot_wait_ms':envelope['queue_ms'],'metrics':{k:response.get(k) for k in ('total_duration','load_duration','prompt_eval_count')}}
        tags=http(endpoint['url'],'/api/tags',timeout=min(30,float(endpoint.get('timeout',120)),max(1,deadline-time.monotonic()))).get('models',[])
        installed=next((m for m in tags if m['name']==model),None)
        if not installed:raise RuntimeError('Embedding model is not installed: '+model)
        if expected_digest and installed['digest']!=expected_digest:raise RuntimeError('Embedding weights changed; rebuild the separate experimental index')
        with endpoint_slot(registry,endpoint['url'],deadline,model) as (wait,previous):
            if previous and previous!=model:http(endpoint['url'],'/api/generate',{'model':previous,'keep_alive':0},timeout=min(60,max(1,deadline-time.monotonic())))
            node=physical_node(state['store'],endpoint['url']) if state else None
            event('inference.started','running')
            result=http(endpoint['url'],'/api/embed',{'model':model,'input':texts,'truncate':False,'keep_alive':0},timeout=max(1,deadline-time.monotonic()))
            with registry.connect() as db:db.execute('UPDATE inference_slots SET last_model=NULL WHERE endpoint=?',(endpoint['url'],))
        activity.emit('embedding','Experimental embedding request completed',evidence={'model':model,'endpoint':endpoint['url'],'slot_wait_ms':wait,'prompt_eval_count':result.get('prompt_eval_count'),'total_duration':result.get('total_duration'),'load_duration':result.get('load_duration')},duration_ms=(time.perf_counter()-start)*1000)
        completed=True
        return result['embeddings'],{'duration_ms':(time.perf_counter()-start)*1000,'slot_wait_ms':wait,'model_digest':installed['digest'],'metrics':{k:result.get(k) for k in ('total_duration','load_duration','prompt_eval_count')}}
    except Exception as exc:
        if current():current()['error']=str(exc)
        activity.emit('embedding','Selected embedding profile failed','failed',{'error':str(exc),'model':model,'endpoint':endpoint['url']})
        raise
    finally:event('inference.completed' if completed else 'inference.failed','completed' if completed else 'failed',{'duration_ms':(time.perf_counter()-start)*1000,**gateway_detail})
