"""Correlation acceptance using simulated inference; no physical-head claim."""
import json
import os
from pathlib import Path
import pytest
from capstone_rag import inference,telemetry,runtime_identity
from capstone_rag.ai_settings import Registry
from capstone_rag.execution import ConcurrentExecutionEngine,TaskSpec,stable_task_key
from capstone_rag.scheduler import WorkerRegistry,WorkerSpec
from capstone_rag.store import Store,now


def setup(tmp_path):
    st=Store(str(tmp_path/'operations.db'))
    st.insert('cases',{'case_id':'CASE-CONT','created_at':now(),'updated_at':now(),'channel':'email',
        'status':'New','identity_verified':0,'rma_needed':0,'ai_state':'queued','subject':'Synthetic correlation','case_version':0})
    st.inference_registry=Registry(tmp_path/'settings.db')
    WorkerRegistry(st).register(WorkerSpec('simulated-model-worker','head-192-0-2-3','simulated.invalid','ollama','test',
        ('model.inference',),('*',),endpoint='https://192.0.2.254:8443',available_models=('qwen3.5:4b',)))
    engine=ConcurrentExecutionEngine(st,max_concurrency=2,queue_capacity=8)
    task=engine.repo.create(TaskSpec('GRAPH-CONT','CASE-CONT','communications-instance-2','test.infer','model.inference',
        'GRAPH-CONT',stable_task_key('CONT','one'),required_model='qwen3.5:4b',model_eligibility=('head-192-0-2-3',)))
    return st,engine,task


def run(st,engine,task,monkeypatch,fail=False):
    def transport(url,path,payload=None,timeout=5):
        if fail:raise TimeoutError('Simulated transport timeout')
        return {'models':[]} if path=='/api/ps' else {'model':'qwen3.5:4b','done':True,'response':'TEST_OK'}
    monkeypatch.setattr(inference,'http',transport)
    def handler(active):
        assert engine.heartbeat(active['task_id'])
        with inference.session(st,active['task_id'],case_id='CASE-CONT',snapshot=st.inference_registry.snapshot()),inference.specialist('customer_communication'):
            return inference.infer('/api/generate',{'prompt':'Synthetic fixture','options':{'num_predict':8}},5)
    engine.run([task['task_id']],{'test.infer':handler})
    return telemetry.replay(st,0,1000)


def test_task_agent_workflow_and_actual_execution_location(tmp_path,monkeypatch):
    st,engine,task=setup(tmp_path);events=run(st,engine,task,monkeypatch)
    model=[e for e in events if e['event_type'].startswith('inference.')]
    assert {e['event_type'] for e in model}=={'inference.queued','inference.started','inference.completed'}
    for event in model:
        assert event['task_id']==task['task_id']
        assert event['workflow_id']=='GRAPH-CONT'
        assert event['agent_id']=='communications-instance-2'
        assert event['detail']['route_role']=='customer_communication'
        assert event['detail']['application_id']=='capstone'
        assert event['detail']['orchestrator_id']=='main'
        assert event['execution_node']==runtime_identity.coordinator_id()
    started=next(e for e in events if e['event_type']=='task.started')
    assert started['execution_node']!='simulated-model-worker'
    assert started['inference_node']=='head-192-0-2-3'
    assert started['detail']['deadline_at']>0
    assert engine.heartbeat(task['task_id']) is False
    assert len([e for e in telemetry.replay(st,0,1000) if e['event_type']=='task.heartbeat'])==1


def test_failure_keeps_same_correlation_and_no_false_completion(tmp_path,monkeypatch):
    st,engine,task=setup(tmp_path);events=run(st,engine,task,monkeypatch,fail=True)
    failed=[e for e in events if e['event_type']=='inference.failed']
    assert len(failed)==1 and failed[0]['task_id']==task['task_id']
    assert failed[0]['workflow_id']=='GRAPH-CONT'
    assert not any(e['event_type']=='inference.completed' for e in events)


def test_identity_configuration_does_not_rewrite_history(tmp_path,monkeypatch):
    st=Store(str(tmp_path/'identity.db'))
    first=telemetry.emit(st,'test.event',agent_id='coordinator')
    monkeypatch.setenv('CAPSTONE_ORCHESTRATOR_ID','relocated')
    second=telemetry.emit(st,'test.event',agent_id='coordinator')
    assert first['execution_node']=='orch:capstone/main'
    assert second['execution_node']=='orch:capstone/relocated'
    assert st.one('SELECT execution_node FROM arena_events WHERE event_id=?',(first['event_id'],))['execution_node']==first['execution_node']


def test_current_read_only_arena_adapter_still_translates_events(tmp_path,monkeypatch):
    # The inspected pure translator, loaded without a production dependency or
    # bytecode written into the independent project (Arena 2.4.0 made it a
    # module of its standard-library-only package; see the rc.24 loader).
    from test_rc24_lab_participants_telemetry import _translator
    Translator=_translator(monkeypatch)
    if Translator is None:pytest.skip('Independent Arena translator not available; contract integration pending')
    st,engine,task=setup(tmp_path);events=run(st,engine,task,monkeypatch)
    translator=Translator({r['agent_id']:r for r in telemetry.registry(st)})
    mapped=[out for event in events for out in translator.translate(event)]
    assert any(e['type']=='assignment.created' and e.get('task_id')==task['task_id'] for e in mapped)
    assert any(e.get('to_location')=='head:head-192-0-2-3' for e in mapped)
    assert any(e['type']=='assignment.state' and e.get('state')=='completed' for e in mapped)
    assert all('E1:task_from_workflow_id' not in e.get('source_ref','') for e in mapped)


def test_public_health_preserves_adapter_probe_without_privileged_snapshot(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from capstone_rag.webapp.app import create_app
    from capstone_rag import generation
    st=Store(str(tmp_path/'health.db'))
    monkeypatch.setenv('CAPSTONE_AI_SETTINGS',str(tmp_path/'settings.db'))
    monkeypatch.setattr(generation,'model_status',lambda:{'ollama_available':True,'installed':True,'model':'test',
        'endpoint':'http://private.example:9999','error':'private diagnostic'})
    with TestClient(create_app(prefer_llm=False,store=st,start_workers=False,developer_token='test-health-admin')) as client:
        public=client.get('/api/health')
        assert public.status_code==200 and public.json()['ok'] is True
        assert 'store' not in public.json() and 'pending' not in public.json()['queue']
        assert 'private.example' not in public.text and 'private diagnostic' not in public.text
        assert client.get('/api/health/details').status_code==403
        assert client.get('/api/health/details',headers={'X-Capstone-Developer-Token':'test-health-admin'}).status_code==200
        assert client.get('/agent-arena').status_code==404
