import json
import io
import zipfile
from pathlib import Path

from capstone_rag import experiments
from capstone_rag.agentic_methodologies import candidates,catalogue
from capstone_rag.ai_settings import Registry
from capstone_rag.candidate_execution import execution_plan
from capstone_rag.store import Store


def _spec():
    return {'profile_ids':['gateway-default'],'gateway_models':['qwen3.5:4b'],
        'scenario_ids':['qms-technical'],'repetitions':1,'approval_mode':'automated',
        'level':'system','methodology_matrix':{}}


def test_agentic_matrix_has_eight_by_four_real_execution_plans():
    contract=catalogue();matrix=candidates()
    assert len(contract['topologies'])==8
    assert len(contract['treatments'])==4
    assert len(matrix)==32 and len({row['configuration_hash'] for row in matrix})==32
    assert {row['id'] for row in matrix}=={f'method-{letter}{number}' for letter in 'ABCDEFGH' for number in '1234'}
    plans=[execution_plan(row) for row in matrix]
    assert {row['topology_id'] for row in plans}==set('ABCDEFGH')
    assert {row['treatment_id'] for row in plans}==set('1234')
    assert any(row['peer_exchange'] for row in plans)
    assert any(row['hierarchical_delegation'] for row in plans)
    assert any(row['debate'] for row in plans)
    assert any(row['force_tree'] for row in plans)


def test_one_case_expands_to_32_paired_methodology_runs(monkeypatch,tmp_path):
    from capstone_rag import config
    monkeypatch.setenv('CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG',str(tmp_path/'research.json'))
    monkeypatch.setattr(config,'DATA_DIR',str(tmp_path/'data'))
    registry=Registry(tmp_path/'settings.db');planned=experiments.preview(registry,_spec())
    assert planned['planned_runs']==32
    assert len(planned['architectures'])==32
    manager=experiments.Manager(registry);live=Store(str(tmp_path/'operations.db'))
    monkeypatch.setattr(experiments.threading.Thread,'start',lambda self:None)
    try:
        launched=manager.launch(planned,live,background=False);runs=manager.runs(launched['id'])
        assert len(runs)==32
        assert {run['architecture']['id'] for run in runs}=={f'method-{letter}{number}' for letter in 'ABCDEFGH' for number in '1234'}
        assert {run['seed'] for run in runs}=={7}
        assert {run['scenario']['id'] for run in runs}=={'qms-technical'}
        assert {run['approval_mode'] for run in runs}=={'automated'}
    finally:live.close()


def test_case_model_head_and_repetition_multiply_the_full_matrix(monkeypatch,tmp_path):
    monkeypatch.setenv('CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG',str(tmp_path/'research.json'))
    registry=Registry(tmp_path/'settings.db');spec=_spec();spec.update(
        gateway_models=['qwen3.5:4b','gemma3:4b'],
        gateway_head_ids=['head-192-0-2-2','head-192-0-2-7'],
        scenario_ids=['qms-technical','ambiguous-accessory'],repetitions=2)
    planned=experiments.preview(registry,spec)
    assert planned['planned_runs']==2*2*2*2*32


def test_review_modes_and_human_facing_controls_are_exposed(monkeypatch,tmp_path):
    monkeypatch.setenv('CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG',str(tmp_path/'research.json'))
    registry=Registry(tmp_path/'settings.db')
    for mode in ('interactive','automated','autonomous_sandbox'):
        spec=_spec();spec['approval_mode']=mode
        assert experiments.preview(registry,spec)['spec']['approval_mode']==mode
    from lab_js import lab_js_text
    text=lab_js_text()
    for phrase in ('Agentic Evaluation matrix','Number of cases','Automated synthetic reviewer','Autonomous sandbox decisions','Complete ZIP','Action required'):
        assert phrase in text


def test_lab_agentic_selection_is_reproducible_and_reports_32_per_case(monkeypatch):
    from capstone_rag.webapp.routers import lab
    available={'name':'qwen3.5:4b','heads':['head-192-0-2-3'],'blocked':{}}
    monkeypatch.setattr(lab,'state',lambda:{'models':[available]})
    body={'experiment':'agentic-evaluation','models':['qwen3.5:4b'],'heads':[],
        'case_count':2,'repetitions':1,'seed':19,'methodology_matrix':{}}
    first=lab._selection(body);second=lab._selection(body)
    assert first[5]==64 and first[7]==32
    assert len(first[6])==2 and first[6]==second[6]


def test_complete_bundle_exports_every_run_artifact_but_no_credentials(monkeypatch,tmp_path):
    from capstone_rag.webapp.routers import experiments as router
    root=tmp_path/'EXP-1';run_dir=root/'RUN-1';run_dir.mkdir(parents=True)
    (root/'manifest.json').write_text('{"campaign":true}',encoding='utf-8')
    for name in ('manifest.json','result.json','trace.json','worker.log','operations.db'):
        (run_dir/name).write_bytes(b'fixture')
    (run_dir/'capstone-research.token').write_text('must-not-export',encoding='utf-8')
    run={'id':'RUN-1','directory':str(run_dir),'experiment_id':'EXP-1'}
    class Registry:
        def get(self,table,key):return {'id':'EXP-1','directory':str(root)} if table=='experiments' else None
    class Manager:
        registry=Registry()
        def runs(self,eid):return [run]
    monkeypatch.setattr(router,'manager',lambda:Manager())
    monkeypatch.setattr(router,'detail',lambda *args,**kwargs:{'experiment':{'id':'EXP-1'},'runs':[run]})
    response=router.export_bundle('EXP-1')
    with zipfile.ZipFile(response.path) as archive:
        names=set(archive.namelist())
        assert {'summary.json','export-manifest.json','manifest.json','runs/RUN-1/result.json','runs/RUN-1/trace.json','runs/RUN-1/worker.log','runs/RUN-1/operations.db'}<=names
        assert not any(name.endswith('.token') for name in names)
    Path(response.path).unlink()


