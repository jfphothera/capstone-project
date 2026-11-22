import pytest

from capstone_rag.ai_settings import Registry
from capstone_rag.research_loop import ResearchLedger


def evidence(safe=True,physical=False,paired=0):
    return {'format':'capstone-phase6-comparison-v1','baseline_hash':'base',
        'candidates':[{'candidate_hash':'base','safety_eligible':safe,
            'execution_classifications':['physical_gateway_inference_observed' if physical else 'offline_deterministic_execution']}],
        'paired_comparisons':[{'paired_samples':paired}],
        'performance_ranking_eligible_only':['base'] if safe else []}


def test_planner_prioritizes_safety_and_requires_separate_approval(tmp_path):
    ledger=ResearchLedger(Registry(tmp_path/'settings.db'))
    item=ledger.propose(evidence(safe=False),{'max_iterations':3,'max_trials_per_iteration':8},'research.operator')
    assert item['priority']=='safety_blocker' and item['status']=='proposed'
    assert not item['automatic_execution_permitted'] and not item['operational_activation_permitted']
    approved=ledger.approve(item['id'],'research.approver','Reproduce the safety failure in isolation.')
    assert approved['status']=='approved_for_testing_lab'
    assert approved['execution_scope']=='isolated_testing_lab_campaign_only'
    assert not approved['operational_activation_permitted']


def test_planner_preserves_exploration_reason_and_blocks_duplicate_loops(tmp_path):
    ledger=ResearchLedger(Registry(tmp_path/'settings.db'));policy={'max_iterations':4,
        'max_trials_per_iteration':10,'exploration_reason':'close physical evidence gap'}
    first=ledger.propose(evidence(),policy,'operator')
    assert first['priority']=='integration_evidence'
    assert first['exploration_reason']=='close physical evidence gap'
    with pytest.raises(ValueError,match='identical'):
        ledger.propose(evidence(),policy,'operator')
    repeated=ledger.propose(evidence(),{**policy,'repeat_reason':'Prior run was interrupted.'},'operator')
    assert repeated['repeat_of']==first['id']


def test_planner_uses_paired_measurement_then_one_variable_exploration(tmp_path):
    ledger=ResearchLedger(Registry(tmp_path/'settings.db'))
    paired=ledger.propose(evidence(physical=True,paired=1),{'max_trials_per_iteration':4},'operator')
    assert paired['priority']=='measurement_quality'
    varied=ledger.propose(evidence(physical=True,paired=2),{'max_trials_per_iteration':4},'operator')
    assert varied['priority']=='controlled_exploration'


def test_planner_rejects_unbounded_policy_and_approval_without_identity(tmp_path):
    ledger=ResearchLedger(Registry(tmp_path/'settings.db'))
    with pytest.raises(ValueError,match='bounded'):
        ledger.propose(evidence(),{'max_iterations':999},'operator')
    item=ledger.propose(evidence(),{'max_trials_per_iteration':4},'operator')
    with pytest.raises(ValueError,match='Named approver'):
        ledger.approve(item['id'],'','')


def test_approved_envelope_dispatches_paired_holdout_campaign(tmp_path,monkeypatch):
    monkeypatch.setattr('capstone_rag.telemetry.emit',lambda *args,**kwargs:None)
    ledger=ResearchLedger(Registry(tmp_path/'settings.db'))
    item=ledger.propose(evidence(),{'max_iterations':3,'max_trials_per_iteration':12,
        'max_concurrency':2,'max_model_calls':40,'max_tokens':4000,
        'allowed_architecture_ids':['candidate-a','candidate-b-direct'],
        'allowed_profile_ids':['profile-a'],'allowed_scenario_ids':['holdout-a']},'operator')
    approved=ledger.approve(item['id'],'approver','Authorize a paired synthetic holdout envelope.')
    planned={'preview_hash':'preview-1','planned_runs':4,
        'spec':{'research_iteration_id':item['id'],'approval_mode':'scripted',
            'architecture_ids':['candidate-a','candidate-b-direct'],'profile_ids':['profile-a'],
            'scenario_ids':['holdout-a'],'concurrency':2,'model_call_budget':20,
            'total_token_budget':2000},'scenarios':[{'id':'holdout-a','evaluation_split':'holdout'}]}
    class Manager:
        def launch(self,preview,source_store):
            assert preview is planned and source_store=='source-store'
            return {'id':'EXP-DISPATCHED','status':'running'}
    dispatched=ledger.dispatch(item['id'],planned,Manager(),'source-store','dispatcher','Run approved paired evidence.')
    assert dispatched['experiment']['id']=='EXP-DISPATCHED'
    assert dispatched['iteration']['status']=='dispatched'
    assert dispatched['iteration']['dispatches'][0]['architecture_ids']==['candidate-a','candidate-b-direct']


def test_dispatch_rejects_unpaired_or_unbounded_campaign(tmp_path):
    ledger=ResearchLedger(Registry(tmp_path/'settings.db'))
    item=ledger.propose(evidence(),{'allowed_architecture_ids':['candidate-a','candidate-b-direct'],
        'max_trials_per_iteration':4},'operator')
    ledger.approve(item['id'],'approver','Bounded research only.')
    base={'preview_hash':'x','planned_runs':2,'scenarios':[{'evaluation_split':'holdout'}],
        'spec':{'research_iteration_id':item['id'],'approval_mode':'scripted',
            'architecture_ids':['candidate-a'],'profile_ids':['p'],'scenario_ids':['s'],
            'concurrency':1,'model_call_budget':2,'total_token_budget':100}}
    with pytest.raises(ValueError,match='Candidate A and at least one candidate'):
        ledger.dispatch(item['id'],base,None,None,'dispatcher','reason')
    base['spec']['architecture_ids']=['candidate-a','candidate-b-direct'];base['spec']['model_call_budget']=None
    with pytest.raises(ValueError,match='explicit model-call'):
        ledger.dispatch(item['id'],base,None,None,'dispatcher','reason')


def test_followup_inherits_approval_without_expanding_authority(tmp_path):
    ledger=ResearchLedger(Registry(tmp_path/'settings.db'))
    item=ledger.propose(evidence(),{'max_iterations':3,'max_trials_per_iteration':6,
        'allowed_architecture_ids':['candidate-a','candidate-c-tot']},'operator')
    approved=ledger.approve(item['id'],'approver','Allow bounded result-driven follow-up.')
    new_evidence=evidence(safe=True,physical=True,paired=1)
    followup=ledger.propose_followup(approved['id'],new_evidence,{'exploration_reason':'First paired result remains uncertain.'})
    assert followup['status']=='approved_for_testing_lab'
    assert followup['parent_iteration_id']==approved['id']
    assert followup['authorization_envelope']==approved['authorization_envelope']
    assert not followup['operational_activation_permitted']
