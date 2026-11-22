import json
from pathlib import Path

from capstone_rag import experiments,telemetry
from capstone_rag.ai_settings import Registry
from capstone_rag.store import Store


class GatewayFixture:
    def __init__(self):
        self.calls=[]
    def models(self):
        resources=[]
        for number in range(2,11):
            models=['qwen3.5:4b','all-minilm:latest']
            if number in (2,7,8,9,10):models.append('qwen3:14b')
            details={name:{'qualified_operations':['embedding'] if name=='all-minilm:latest' else ['completion'],
                'execution_policy':{'max_context':8192},'size_bytes':30_000_000 if name=='all-minilm:latest' else (9_000_000_000 if name=='qwen3:14b' else 3_000_000_000)} for name in models}
            resources.append({'resource_id':f'head-192-0-2-{number}','status':'healthy','models':models,'model_details':details})
        return {'contract':'shared.inference/1','resources':resources}
    def policy(self):return {'application_id':'capstone-research','allowed_models':['*']}
    def availability(self,model,operation='/api/generate',**kwargs):
        return {'status':'available','resources':{f'head-192-0-2-{n}':'available' for n in range(2,11)}}


def test_heads_2_through_10_and_gpu_fixture(monkeypatch):
    from capstone_rag.webapp.routers import lab
    gateway=GatewayFixture();fixture=Path(__file__).parent/'fixtures'/'lab-heads-2026-11-08.json'
    monkeypatch.setattr(lab.gateway_transport,'enabled',lambda:True)
    monkeypatch.setattr(lab.gateway_transport,'client',lambda workload:gateway)
    monkeypatch.setenv('CAPSTONE_LAB_HEADS_FILE',str(fixture));lab._cache.update(at=0,value=None,building=False,inflight=False,error=None,generation=0)
    value=lab.state(True)
    for _ in range(100):
        if value.get('inventory_status')=='ready':break
        import time;time.sleep(.01);value=lab.state(False)
    assert [h['number'] for h in value['heads']]==list(range(2,11))
    assert all(h['status']=='serving the Testing Lab' for h in value['heads'])
    gpu=value['heads'][0]
    assert gpu['gpu']=='RTX 4060 Ti 16 GB' and gpu['ram_gb']==63.9
    large=next(m for m in value['models'] if m['name']=='qwen3:14b')
    assert set(large['heads'])=={f'head-192-0-2-{n}' for n in (2,7,8,9,10)}


def test_models_by_heads_expand_to_pinned_runs(monkeypatch,tmp_path):
    monkeypatch.setenv('CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG',str(tmp_path/'research.json'))
    registry=Registry(tmp_path/'settings.db')
    spec={'profile_ids':['gateway-default'],'gateway_models':['qwen3:14b'],
      'gateway_head_ids':['head-192-0-2-2','head-192-0-2-7'],'scenario_ids':['qms-technical'],
      'repetitions':1,'approval_mode':'scripted','level':'system'}
    preview=experiments.preview(registry,spec)
    assert preview['planned_runs']==2
    assert {r['model'] for p in preview['profiles'] for r in p['routes'].values()}=={'qwen3:14b'}


def test_head_2_vs_head_7_launches_two_pinned_runs(monkeypatch,tmp_path):
    from capstone_rag import config
    monkeypatch.setenv('CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG',str(tmp_path/'research.json'))
    monkeypatch.setattr(config,'DATA_DIR',str(tmp_path/'data'))
    registry=Registry(tmp_path/'settings.db');manager=experiments.Manager(registry);live=Store(str(tmp_path/'operations.db'))
    spec={'profile_ids':['gateway-default'],'gateway_models':['qwen3:14b'],
      'gateway_head_ids':['head-192-0-2-2','head-192-0-2-7'],'scenario_ids':['qms-technical'],
      'repetitions':1,'approval_mode':'scripted','level':'system'}
    planned=experiments.preview(registry,spec)
    monkeypatch.setattr(experiments.threading.Thread,'start',lambda self:None)
    try:
        launched=manager.launch(planned,live,background=False)  # threads are patched out: prepare inline
        runs=manager.runs(launched['id'])
        assert len(runs)==2
        assert {r['gateway_head_id'] for r in runs}=={'head-192-0-2-2','head-192-0-2-7'}
        for run in runs:
            assert {tuple(route['cluster_nodes']) for route in run['profile']['routes'].values()}=={(run['gateway_head_id'],)}
    finally:live.close()


def test_experiment_telemetry_mirrors_append_only(tmp_path):
    live=Store(str(tmp_path/'live.db'));isolated=Store(str(tmp_path/'isolated.db'))
    try:
        isolated.arena_mirror_database=live.path;isolated.arena_experiment_run_id='RUN-RC5'
        telemetry.emit(isolated,'inference.completed',workflow_id='copied-case',task_id='TASK-1',
          agent_id='ticket_interpretation',inference_node='head-192-0-2-2',correlation_id='CALL-1',
          task_status='completed',summary='Gateway inference completed')
        row=live.one("SELECT * FROM arena_events WHERE workflow_id='RUN-RC5' AND event_type='inference.completed'")
        assert row['inference_node']=='head-192-0-2-2'
        assert row['summary'].startswith('EXPERIMENT ')
    finally:
        isolated.close();live.close()


def test_testing_lab_is_four_simple_gateway_screens():
    # rc.20 adds the Compatibility screen (model x role matrix).
    root=Path(__file__).parents[1]/'src'/'capstone_rag'/'webapp'/'static'
    html=(root/'pressure_testing.html').read_text(encoding='utf-8')
    from lab_js import lab_js_text,compact
    js=lab_js_text();flat=compact(js)
    assert "letscreen='run'" in flat
    assert "['heads','run','results','compatibility']" in flat
    assert "models:['qwen3.5:4b']" in flat
    for forbidden in ('Discover models','Add endpoint','Cluster & telemetry'):
        assert forbidden not in html+js


def test_gateway_outage_cannot_select_process_lifetime_fallback(monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    from capstone_rag.webapp.app import create_app
    from capstone_rag.webapp import app as app_module
    monkeypatch.setattr(app_module.gateway_transport if hasattr(app_module,'gateway_transport') else __import__('capstone_rag.gateway_transport',fromlist=['enabled']),'enabled',lambda:True)
    monkeypatch.setattr(app_module.generation,'llm_available',lambda:False)
    with TestClient(create_app(store=Store(str(tmp_path/'ops.db')),start_workers=False)):
        from capstone_rag.webapp.runtime import get_runtime
        assert get_runtime().prefer_llm is True
