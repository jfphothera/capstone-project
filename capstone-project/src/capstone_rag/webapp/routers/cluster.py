"""Developer cluster controls and scoped authenticated host telemetry."""
import hmac
import json
import os
from fastapi import APIRouter, Depends, Header, HTTPException
from ..developer_auth import require_developer
from ..runtime import get_runtime

router=APIRouter(prefix='/api/cluster',tags=['physical cluster'])

@router.get('',dependencies=[Depends(require_developer)])
def status():
    rt=get_runtime()
    return {'settings':rt.cluster.settings(),'nodes':rt.store.query('SELECT * FROM physical_nodes'),
            'workers':rt.cluster.enrollment.workers.all(),
            'metrics':rt.store.query('SELECT * FROM node_metrics ORDER BY at DESC LIMIT 100')}

@router.post('/economics/{node_id}',dependencies=[Depends(require_developer)])
def economics(node_id:str,body:dict):
    import math
    allowed={'acquisition_price','electricity_per_kwh','estimated_watts','maintenance_per_year','lifetime_years'}
    if set(body)-allowed:raise ValueError('Unknown economic assumption')
    if any(v is not None and (isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0) for v in body.values()):raise ValueError('Enter finite non-negative user assumptions')
    st=get_runtime().store
    if not st.one('SELECT 1 FROM physical_nodes WHERE node_id=?',(node_id,)):raise ValueError('Unknown node')
    st.upsert('node_economics',{'node_id':node_id,'payload':json.dumps(body)},'node_id')
    return {'node_id':node_id,'assumptions':body,'measured':False}

@router.post('/configure',dependencies=[Depends(require_developer)])
def configure(body:dict):return get_runtime().cluster.configure(body)

@router.post('/qualify',dependencies=[Depends(require_developer)])
def qualify():return {'workers':get_runtime().cluster.refresh()}

@router.post('/profile',dependencies=[Depends(require_developer)])
def profile(body:dict):
    import copy
    from ... import gateway_transport
    from ...ai_settings import ROLES
    rt=get_runtime();nodes=body.get('nodes',[]);model=body.get('model')
    workers=[w for w in rt.cluster.enrollment.workers.all() if w['node_id'] in nodes and model in w['available_models'] and w['health_state']=='healthy']
    if not nodes or {w['node_id'] for w in workers}!=set(nodes):raise ValueError('Every selected node must have the exact model qualified and healthy')
    endpoint=rt.inference_registry.put('endpoints',{'id':workers[0]['worker_id'],'name':workers[0]['node_id'],'url':workers[0]['endpoint'],
        'resource_id':workers[0]['node_id'],'transport':'gateway' if gateway_transport.enabled() else 'direct','timeout':120})
    value=copy.deepcopy(rt.inference_registry.active())
    value.update(id=body.get('id'),name=body.get('name') or 'Qualified cluster experiment',default_endpoint=endpoint['id'])
    for role in ROLES:
        value['routes'][role].update(endpoint=endpoint['id'],model=model,cluster_nodes=nodes)
    return rt.inference_registry.put('profiles',value)

@router.post('/nodes/{node_id}/{action}',dependencies=[Depends(require_developer)])
def state(node_id:str,action:str):
    service=get_runtime().cluster
    if action=='reenroll':service.enrollment.reenable(node_id)
    elif action in {'disabled','quarantined','retired'}:service.enrollment.set_state(node_id,action)
    else:raise ValueError('Unknown lifecycle action')
    return {'node_id':node_id,'action':action}

@router.post('/metrics/{node_id}')
def metrics(node_id:str,body:dict,x_capstone_metrics_token:str|None=Header(default=None)):
    # Unique per-node tokens, configured outside source, cannot operate the Lab.
    tokens=json.loads(os.environ.get('CAPSTONE_METRICS_TOKENS','{}'))
    expected=tokens.get(node_id)
    if not expected or not x_capstone_metrics_token or not hmac.compare_digest(expected,x_capstone_metrics_token):
        raise HTTPException(403,'Node-scoped metrics authorization required')
    return get_runtime().cluster.ingest_metrics(node_id,body)
