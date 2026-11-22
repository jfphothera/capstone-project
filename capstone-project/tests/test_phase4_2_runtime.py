import copy
import json
import time

import pytest
from fastapi.testclient import TestClient

from capstone_rag.ai_settings import Registry,canonical
from capstone_rag.cluster_runtime import ClusterService
from capstone_rag.experiments import Manager,preview
from capstone_rag.scheduler import NoEligibleWorker
from capstone_rag.store import Store
from capstone_rag.webapp.app import create_app
from datetime import datetime,timezone


def fixture():
    return {'address':'192.0.2.11','endpoint':'http://192.0.2.11:11434','actual_execution':True,
        'observed_at':datetime.now(timezone.utc).isoformat(),'inventory':[{'name':'tiny:1','digest':'abc'}],
        'model':'tiny:1','digest':'abc','metadata':{'tiny:1':{'capabilities':['completion']}},
        'generation_completed':True,'correct':True,'response':{'done':True,'model':'tiny:1','response':'READY'}}


def test_real_route_capacity_and_expiry(tmp_path):
    st=Store(str(tmp_path/'ops.db'));service=ClusterService(st)
    service.configure({'addresses':['192.0.2.11']})
    worker=service.enrollment.ingest(fixture())
    selected=service.route('tiny:1',[worker['node_id']],'task1','wf')
    assert selected['endpoint']=='http://192.0.2.11:11434'
    with pytest.raises(NoEligibleWorker):service.route('tiny:1',[worker['node_id']],'task2','wf')
    service.enrollment.workers.release(worker['worker_id'])
    service.ingest_metrics(worker['node_id'],{'sampled_at':time.time(),'cpu_percent':95,'ram_available_bytes':1024})
    with pytest.raises(NoEligibleWorker):service.route('tiny:1',[worker['node_id']],'task3','wf')
    for value in (float('nan'),float('inf'),-1,101):
        with pytest.raises(ValueError):service.ingest_metrics(worker['node_id'],{'sampled_at':time.time(),'cpu_percent':value})


def test_scoped_viewer_survives_server_session_reset_and_cannot_control_lab(tmp_path,monkeypatch):
    monkeypatch.setenv('CAPSTONE_VIEWER_TOKEN','test-view-only')
    monkeypatch.setenv('CAPSTONE_AI_SETTINGS',str(tmp_path/'settings.db'))
    app=create_app(prefer_llm=False,store=Store(str(tmp_path/'ops.db')),start_workers=False,developer_token='test-admin')
    with TestClient(app) as client:
        response=client.post('/api/arena/session',json={'token':'test-view-only'})
        assert response.status_code==200
        csrf=response.json()['csrf_token']
        app.state.developer_sessions.clear()
        assert client.get('/api/arena/status').status_code==200
        # A read-only viewer is refused every developer-only surface.
        assert client.get('/api/cluster').status_code==403
        assert client.get('/api/trace/subjects').status_code==403
        assert client.get('/trace').status_code==403
        # Policy (app.py, since the baseline commit): the Testing Lab is a
        # synthetic, isolated research tool on the dedicated single-operator
        # laptop and intentionally has no browser credential gate.
        assert client.get('/api/experiments').status_code==200
        assert client.post('/api/arena/logout',headers={'X-CSRF-Token':csrf}).status_code==200
        app.state.developer_sessions.clear()
        assert client.get('/api/arena/status').status_code==403
        assert client.post('/api/developer/session',json={'token':'test-view-only'}).status_code==403
        assert client.post('/api/developer/session',json={'token':'test-admin'}).status_code==200
        assert client.get('/pressure-testing').status_code==200


def test_metric_token_is_node_scoped(tmp_path,monkeypatch):
    monkeypatch.setenv('CAPSTONE_AI_SETTINGS',str(tmp_path/'settings.db'))
    monkeypatch.setenv('CAPSTONE_METRICS_TOKENS',json.dumps({'head-192-0-2-11':'test-metric'}))
    st=Store(str(tmp_path/'ops.db'));service=ClusterService(st);service.configure({'addresses':['192.0.2.11']});service.enrollment.ingest(fixture())
    with TestClient(create_app(prefer_llm=False,store=st,start_workers=False,developer_token='test-admin')) as client:
        body={'sampled_at':time.time(),'cpu_percent':42,'ram_available_bytes':None}
        assert client.post('/api/cluster/metrics/head-192-0-2-11',json=body).status_code==403
        headers={'X-Capstone-Metrics-Token':'test-metric'}
        assert client.post('/api/cluster/metrics/head-192-0-2-11',json=body,headers=headers).status_code==200
        assert client.post('/api/cluster/metrics/head-192-0-2-12',json=body,headers=headers).status_code==403
        assert client.get('/api/cluster',headers=headers).status_code==403


def test_lab_duration_validation_and_restart_preserves_completed(tmp_path):
    registry=Registry(tmp_path/'settings.db');manager=Manager(registry)
    spec={'profile_ids':['gateway-default'],'scenario_ids':['phone-log'],'duration_seconds':3600,'token_limit':64}
    planned=preview(registry,spec)
    assert planned['profiles'][0]['routes']['investigation']['options']['num_predict']==64
    with pytest.raises(ValueError):preview(registry,{**spec,'duration_seconds':-1})
    with pytest.raises(ValueError,match='trial budget'):preview(registry,{**spec,'repetitions':2,'max_trials':1})
    manager.save({'id':'EXP-recover','status':'running'})
    for status in ('completed','running','queued'):
        manager._store_run({'id':'RUN-'+status,'experiment_id':'EXP-recover','status':status,'directory':str(tmp_path/status)})
    manager.recover()
    results={r['id']:r for r in manager.runs('EXP-recover')}
    assert results['RUN-completed']['status']=='completed'
    assert results['RUN-running']['status']=='failed'
    assert results['RUN-queued']['status']=='queued'
    assert manager.registry.get('experiments','EXP-recover')['status']=='incomplete'


def test_campaign_token_and_call_budgets_block_before_transport(tmp_path,monkeypatch):
    from capstone_rag import inference
    st=Store(str(tmp_path/'ops.db'));registry=Registry(tmp_path/'settings.db');st.inference_registry=registry
    snapshot=registry.snapshot();snapshot['campaign_budget_id']='test-budget'
    with registry.connect() as db:db.execute('INSERT INTO campaign_budgets VALUES(?,?,?)',('test-budget',10,1))
    requests=[]
    def fake(url,path,payload=None,timeout=5):
        if path=='/api/ps':return {'models':[]}
        requests.append(payload)
        return {'model':'qwen3.5:4b','response':'READY','done':True,'eval_count':4}
    monkeypatch.setattr(inference,'http',fake)
    with inference.session(st,'budget-1',snapshot=snapshot):
        inference.infer('/api/generate',{'prompt':'test','options':{'num_predict':8}},10)
    with pytest.raises(ValueError,match='budget exhausted'):
        with inference.session(st,'budget-2',snapshot=snapshot):
            inference.infer('/api/generate',{'prompt':'test','options':{'num_predict':8}},10)
    assert len(requests)==1
    with registry.connect() as db:assert db.execute('SELECT remaining_tokens,remaining_calls FROM campaign_budgets').fetchone()==(6,0)
