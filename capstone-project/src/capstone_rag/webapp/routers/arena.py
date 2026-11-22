"""Protected Agent Arena registry, durable replay, and truthful status APIs."""
from fastapi import APIRouter
from ... import telemetry
from ..runtime import get_runtime

router=APIRouter(prefix='/api/arena',tags=['agent arena'])

@router.get('/phase6-fixture')
def phase6_fixture():return telemetry.phase6_fixture()

@router.get('/events')
def events(after_sequence:int=0,limit:int=250):
    rows=telemetry.replay(get_runtime().store,after_sequence,limit)
    return {'schema_version':telemetry.SCHEMA_VERSION,'events':rows,
            'after_sequence':after_sequence,
            'next_sequence':rows[-1]['sequence'] if rows else after_sequence,'durable_replay':True}

@router.get('/registry')
def agents():
    return {'schema_version':telemetry.SCHEMA_VERSION,
            'agents':telemetry.registry(get_runtime().store),
            'identity_authority':'server'}

@router.get('/status')
def status():
    rt=get_runtime();snapshot=rt.inference_registry.snapshot()
    from ...scheduler import WorkerRegistry
    endpoints=[]
    for key,value in snapshot.get('endpoints',{}).items():
        observed=rt.store.one("SELECT event_type,timestamp,detail FROM arena_events WHERE endpoint_id=? AND event_type IN ('inference.completed','inference.failed') ORDER BY sequence DESC LIMIT 1",(key,))
        endpoints.append({'endpoint_id':key,'node':value.get('url'),'configured':True,
            'live_status':('last_request_completed' if observed and observed['event_type']=='inference.completed'
                           else 'last_request_failed' if observed else 'not_probed'),
            'last_observed_at':observed['timestamp'] if observed else None})
    models=[{'role':role,'model_id':route.get('model'),'endpoint_id':route.get('endpoint'),
             'enabled':route.get('enabled',True),'residency':'not_instrumented'}
            for role,route in snapshot.get('routes',{}).items()]
    workers=WorkerRegistry(rt.store).all()
    task_counts={row['state']:row['n'] for row in rt.store.query(
        'SELECT state,count(*) AS n FROM execution_tasks GROUP BY state')}
    return {'schema_version':telemetry.SCHEMA_VERSION,'mode':'live',
            'resources':telemetry.resource_status(),'nodes':endpoints,'models':models,
            'workers':workers,'execution_tasks':task_counts,
            'note':'Unavailable measurements are never estimated.'}

@router.get('/health')
def health():
    return {'ok':True,'schema_version':telemetry.SCHEMA_VERSION,'mode':'live'}
