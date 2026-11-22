from types import SimpleNamespace

import pytest

from capstone_rag import candidate_execution
from capstone_rag import experiments
from capstone_rag.experimental_architecture import template


def test_execution_preflight_maps_three_distinct_verified_paths():
    baseline=candidate_execution.execution_plan(template('candidate-a'))
    direct=candidate_execution.execution_plan(template('candidate-b-direct'))
    tree=candidate_execution.execution_plan(template('candidate-c-tot'))
    assert baseline['adapter']=='candidate_a_baseline'
    assert direct['adapter']=='candidate_b_direct_v1' and direct['arrangement']=='single_agent'
    assert tree['adapter']=='candidate_c_tot_v1' and tree['force_tree']
    assert len({baseline['adapter'],direct['adapter'],tree['adapter']})==3


def test_execution_preflight_rejects_metadata_only_component_mismatch():
    value=template('candidate-b-direct')
    value['components']=['reasoning.react_reflexion' if row=='reasoning.direct_guarded' else row
                         for row in value['components']]
    with pytest.raises(ValueError,match='do not match'):
        candidate_execution.execution_plan(value)


def test_candidate_dispatch_uses_baseline_direct_and_forced_tree_adapters(monkeypatch):
    calls=[]
    class Orchestrator:
        def __init__(self,flow,policy=None):calls.append(('init',policy))
        def run(self,cid):calls.append(('run',cid));return {'cid':cid}
    monkeypatch.setattr('capstone_rag.orchestration.CaseOrchestrator',Orchestrator)
    monkeypatch.setattr(candidate_execution,'_event',lambda *args,**kwargs:None)
    flow=SimpleNamespace(store=SimpleNamespace())
    candidate_execution.execute(flow,'CASE-A',{'id':'RUN-A','architecture':template('candidate-a')})
    # rc.21: the baseline still has no methodology stages, but every pass
    # carries an explicit, trigger-labelled policy (recorded per investigation).
    assert calls[0]==('init',{'adapter':'candidate_a_baseline','_trigger':'initial'})
    calls.clear()
    candidate_execution.execute(flow,'CASE-C',{'id':'RUN-C','architecture':template('candidate-c-tot')})
    assert calls[0][1]['force_tree'] is True
    calls.clear()
    monkeypatch.setattr(candidate_execution,'_execute_direct',
        lambda flow,cid,run,architecture,plan:{'adapter':plan['adapter'],'cid':cid})
    result=candidate_execution.execute(flow,'CASE-B',{'id':'RUN-B','architecture':template('candidate-b-direct')})
    assert result=={'adapter':'candidate_b_direct_v1','cid':'CASE-B'}


def test_candidate_a_configuration_hash_remains_frozen():
    assert template('candidate-a')['configuration_hash']==(
        '87fb1a1b7da3a7841aa4d8225bf34834d5a2e4ef2987ee44ad3c13359e1b394a')


def test_synthetic_phase6_scenarios_cover_multiturn_review_and_recovery():
    expected={'corrected-identity-multiturn','customer-nonresponse','reviewer-rejection',
        'reviewer-correction','qa-determination','transient-tool-recovery'}
    assert expected<=set(experiments.SMOKE)
    assert all(experiments.SMOKE[key]['evaluation_split']=='holdout' for key in expected)
    actions={step['action'] for key in expected for step in experiments.SMOKE[key]['review_script']}
    assert {'customer_reply','simulate_non_response','reject_component','revise_agent_draft',
        'determine_safety_qa'}<=actions
    fault=experiments.SMOKE['transient-tool-recovery']['fault_policy']
    assert fault['preflight_failures']<=fault['max_retries']


def test_experiment_local_fault_recovers_without_replaying_adapter(tmp_path,monkeypatch):
    emitted=[]
    monkeypatch.setattr('capstone_rag.telemetry.emit',lambda *args,**kwargs:emitted.append((args,kwargs)))
    calls=[]
    monkeypatch.setattr(candidate_execution,'_execute_direct',
        lambda flow,cid,run,architecture,plan:calls.append(cid) or {'ok':True})
    run={'id':'RUN-FAULT','directory':str(tmp_path),'architecture':template('candidate-b-direct'),
        'scenario':{'fault_policy':{'preflight_failures':1,'max_retries':1,'delay_ms':0}}}
    assert candidate_execution.execute(SimpleNamespace(store=SimpleNamespace()),'CASE-1',run)=={'ok':True}
    assert calls==['CASE-1']
    state=__import__('json').loads((tmp_path/'synthetic-fault-state.json').read_text())
    assert state=={'attempts':1,'failures':1}
    assert [args[1] for args,_ in emitted]==['tool.failed','tool.completed']
