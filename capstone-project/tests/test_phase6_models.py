import pytest

from capstone_rag.ai_settings import Registry
from capstone_rag.model_research import (
    SuitabilityStore, discover, exploration_plan, normalize_inventory, suitability,
)


PAYLOAD={
    'contract':'shared.inference/1',
    'resources':[
        {'resource_id':'gpu-a','status':'ready','available':True,
         'models':['baseline:1','candidate:1','embed:1'],
         'model_details':{
             'baseline:1':{'qualified_operations':['completion'],
                 'qualified_capabilities':['structured_output','tool_calling'],
                 'capabilities':['structured_output','tool_calling'],
                 'digest':'sha256:base','quantization':'q4',
                 'execution_policy':{'max_context':8192}},
             'candidate:1':{'qualified_operations':['completion'],
                 'qualified_capabilities':['structured_output'],'capabilities':['structured_output']},
             'embed:1':{'qualified_operations':['embedding'],
                 'qualified_capabilities':['embedding'],'capabilities':['embedding']},
         }},
        {'resource_id':'gpu-b','models':['candidate:1'],
         'model_details':{'candidate:1':{'qualified_operations':['completion'],
             'qualified_capabilities':['structured_output'],'capabilities':['structured_output']}}},
    ],
}


def test_inventory_uses_only_gateway_facts_and_reports_missing_fields():
    inventory=normalize_inventory(PAYLOAD)
    assert inventory['source']=='authenticated_gateway'
    assert [row['model_id'] for row in inventory['models']]==['baseline:1','candidate:1','embed:1']
    baseline=inventory['models'][0]
    candidate=inventory['models'][1]
    assert baseline['currently_available'] is True
    assert candidate['currently_available'] is True
    assert candidate['qualified_resources']==['gpu-a','gpu-b']
    assert {'family','version','quantization','context_window'}<=set(candidate['missing_gateway_fields'])
    assert 'current_availability' not in candidate['missing_gateway_fields']
    assert 'family' not in candidate and 'parameter_count' not in candidate


def test_suitability_never_uses_model_name_as_evidence():
    report=suitability(normalize_inventory(PAYLOAD))
    rows={(row['model_id'],row['role']):row for row in report['suitability']}
    assert rows['baseline:1','tool_use']['classification']=='compatible'
    assert rows['candidate:1','tool_use']['classification']=='unknown'
    assert rows['embed:1','customer_response']['classification']=='incompatible'
    assert rows['candidate:1','customer_response']['classification']=='compatible'
    assert 'unverified' in rows['candidate:1','customer_response']['basis']


def test_named_reviewed_evidence_is_immutable_and_never_activates(tmp_path):
    store=SuitabilityStore(Registry(tmp_path/'settings.db'))
    saved=store.record({'model_id':'candidate:1','role':'customer_response',
        'classification':'verified','evidence_refs':['EXP-1/RUN-2'],
        'rationale':'Passed the reviewed paired fixture set.'},'research.reviewer')
    assert not saved['operational_activation_permitted']
    row=next(row for row in suitability(normalize_inventory(PAYLOAD),store.list())['suitability']
             if row['model_id']=='candidate:1' and row['role']=='customer_response')
    assert row['classification']=='verified'
    assert row['evidence_refs']==['EXP-1/RUN-2']
    with pytest.raises(ValueError,match='Named reviewer'):
        store.record({'model_id':'candidate:1'},'')


def test_exploration_is_deterministic_baseline_first_bounded_and_fair():
    inventory=normalize_inventory(PAYLOAD)
    report=suitability(inventory)
    policy={'allowlist':['baseline:1','candidate:1','embed:1'],
        'roles':['customer_response'],'baseline_model':'baseline:1',
        'exploration_budget':4,'minimum_useful_comparisons':2,
        'repetitions':2,'seeds':[41,42]}
    first=exploration_plan(inventory,report,policy)
    second=exploration_plan(inventory,report,policy)
    assert first==second
    assert [(row['model_id'],row['seed']) for row in first['assignments']]==[
        ('baseline:1',41),('candidate:1',41),('baseline:1',42),('candidate:1',42)]
    assert any(row['model_id']=='embed:1' and 'operation' in row['reason'] for row in first['not_tested'])
    assert first['gateway_allocation_authority']=='external'
    assert not first['automatic_permission_changes']


def test_exploration_rejects_unbounded_or_statistically_useless_plans():
    inventory=normalize_inventory(PAYLOAD);report=suitability(inventory)
    with pytest.raises(ValueError,match='Bounded'):
        exploration_plan(inventory,report,{'exploration_budget':0})
    with pytest.raises(ValueError,match='minimum useful'):
        exploration_plan(inventory,report,{'allowlist':['baseline:1'],'roles':['customer_response'],
            'exploration_budget':2,'minimum_useful_comparisons':2,'repetitions':1,'seeds':[1]})


def test_gateway_03_discovery_authenticates_policy_and_preflights_availability():
    class Client:
        def models(self):return PAYLOAD
        def policy(self):return {'contract':'shared.inference/1','application_id':'capstone-research','policy':{'enabled':True}}
        def availability(self,model,operation,num_ctx=None):
            return {'contract':'shared.inference/1','status':'available','resources':['gpu-a']}
    inventory=discover(Client())
    assert inventory['gateway_status']=='authenticated'
    assert inventory['policy_verified'] and inventory['availability_verified']
    assert all(model['currently_available'] is True for model in inventory['models'])


def test_gateway_offline_never_manufactures_available_models():
    class Offline:
        def models(self):raise ConnectionError('offline')
    inventory=discover(Offline())
    assert inventory['gateway_status']=='offline_unverified'
    assert inventory['models']==[]
    assert not inventory['availability_verified']
