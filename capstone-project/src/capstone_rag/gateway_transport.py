"""Capstone adapter for the shared.inference/1 client SDK."""
import os,json,time,urllib.error
from shared_inference_client import Client

CLIENT_CONFIG_ENV='CAPSTONE_GATEWAY_CLIENT_CONFIG'
RESEARCH_CLIENT_CONFIG_ENV='CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG'

class GatewayExecutionError(OSError):
    def __init__(self,envelope):
        self.envelope=envelope
        status=envelope.get('status','unavailable')
        super().__init__('Gateway execution '+status+': '+str(envelope.get('error','result unavailable')))

class GatewayUnknownExecution(GatewayExecutionError):
    """The invocation may have executed and must only be reconciled by request ID."""

class GatewayAdmissionDeadline(TimeoutError):
    """No request was created before the configured admission deadline."""

def enabled():
    # Deployment startup requires this approved configuration. False is a
    # fail-closed diagnostic state, never permission to select another transport.
    return bool(os.environ.get(CLIENT_CONFIG_ENV))

def client(workload='operational'):
    if workload not in {'operational','research'}:raise ValueError('Unknown Gateway workload identity')
    config_env=RESEARCH_CLIENT_CONFIG_ENV if workload=='research' else CLIENT_CONFIG_ENV
    config_path=os.environ.get(config_env)
    if config_path:
        return Client.from_config(config_path)
    raise PermissionError(f'{workload.title()} Gateway identity requires its approved Client.from_config file')

def qualified_model(operation,workload='operational',num_ctx=None):
    """Return an authenticated, measured model eligible for this request shape."""
    gateway=client(workload);inventory=gateway.models();gateway.policy()
    qualified='embedding' if operation=='/api/embed' else 'completion'
    candidates=[]
    for resource in inventory.get('resources',[]):
        for model in resource.get('models',[]):
            detail=(resource.get('model_details') or {}).get(model,{})
            if qualified in (detail.get('qualified_operations') or []):candidates.append(model)
    for model in sorted(set(candidates)):
        check=gateway.availability(model,operation,num_ctx=num_ctx)
        if check.get('status') in {'available','busy'}:return model,check
    if qualified=='embedding':
        raise PermissionError(f'no embedding model is allowed for {"capstone-research" if workload=="research" else "capstone"} in the Gateway; the administrator allows one in the admin hub')
    raise PermissionError(f'no generation model is currently eligible for {workload} Gateway use')

def invoke(store,endpoint,model,operation,payload,timeout,*,nodes=None,request_id=None,
           progress=None,expected_digest=None,required_capabilities=None,workload='operational',check_availability=True):
    nodes=nodes or ([endpoint['resource_id']] if endpoint.get('resource_id') else [])
    if not nodes and store:
        row=store.one('SELECT node_id FROM worker_registry WHERE endpoint=?',(endpoint['url'],))
        if row:nodes=[row['node_id']]
    if not nodes:nodes=None # The Gateway chooses any authorized, qualified head.
    # The gateway alone owns model/residency/stream controls and upstream HTTP.
    clean={k:v for k,v in payload.items() if k not in {'model','stream','keep_alive'}}
    gateway=client(workload)
    if check_availability:
        availability=gateway.availability(model,operation,num_ctx=(clean.get('options') or {}).get('num_ctx'))
        if availability.get('status') not in {'available','busy'}:
            raise GatewayExecutionError({'status':'refused','error':availability.get('reason') or availability.get('status'),'availability':availability})
    deadline=time.monotonic()+float(timeout);backpressure_retries=0;backpressure_wait=0.0;delay=.25
    while True:
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise GatewayAdmissionDeadline('waited too long (admission deadline): Application queue capacity exhausted')
        try:
            result=gateway.infer(model,nodes,operation,clean,client_key=request_id,timeout=remaining,
                progress=progress,expected_digest=expected_digest,required_capabilities=required_capabilities)
            break
        except urllib.error.HTTPError as exc:
            raw=exc.read().decode('utf-8','replace')
            try:message=(json.loads(raw) or {}).get('error') or raw
            except Exception:message=raw
            if exc.code!=400 or 'application queue capacity exhausted' not in str(message).lower():
                raise OSError(f'Gateway HTTP {exc.code}: {message or exc.reason}') from exc
            # Gateway 0.4.1 documents this 400 as pre-admission: no request was
            # created, so it is safe to resubmit with the same client identity.
            retry_after=exc.headers.get('Retry-After') if exc.headers else None
            try:wait=max(delay,float(retry_after)) if retry_after else delay
            except (TypeError,ValueError):wait=delay
            if deadline-time.monotonic()<=wait:
                raise GatewayAdmissionDeadline('waited too long (admission deadline): '+str(message)) from exc
            time.sleep(wait);backpressure_wait+=wait;backpressure_retries+=1;delay=min(2.0,delay*1.5)
    if result['status']=='unknown':raise GatewayUnknownExecution(result)
    if result['status']!='completed':raise GatewayExecutionError(result)
    value=result['result']
    if isinstance(value,dict) and result.get('upstream_errors'):
        # Gateway 0.4.6 (G4): head HTTP errors of earlier attempts, kept as evidence.
        value.setdefault('upstream_errors',result['upstream_errors'])
    if isinstance(value,dict):
        value.setdefault('client_diagnostics',{}).update(
            backpressure_retries=backpressure_retries,backpressure_wait_ms=round(backpressure_wait*1000,3))
    return value
