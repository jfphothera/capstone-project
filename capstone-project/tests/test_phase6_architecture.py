import copy

import pytest

from capstone_rag.ai_settings import Registry
from capstone_rag.experimental_architecture import (
    ArchitectureStore, COMPONENTS, CATEGORIES, assemble, registry, template, vary,
)
from capstone_rag.experiments import preview


def test_component_registry_is_versioned_hashed_and_complete():
    value=registry()
    assert value['registry_sha256'] and value['registry_version']=='6.4.0'
    assert set(CATEGORIES)=={row['category'] for row in value['components']}
    required={'id','version','category','inputs','outputs','parameters','required_tools',
              'required_model_capabilities','compatible_components','resource_requirements',
              'safety_authority_constraints','supported_execution_evaluation_methods',
              'provenance','configuration_hash','implementation_status'}
    assert all(required<=set(row) for row in value['components'])
    assert len({row['configuration_hash'] for row in value['components']})==len(value['components'])


def test_candidate_a_is_immutable_ready_baseline_and_variants_are_truthful():
    baseline=template('candidate-a')
    assert baseline['valid'] and baseline['execution_ready'] and baseline['baseline']
    assert baseline['operational_activation_permitted'] is False
    direct=template('candidate-b-direct')
    assert direct['valid'] and direct['execution_ready']
    assert template('candidate-c-tot')['execution_ready']
    fixed=vary('candidate-a','chunking','chunk.fixed_window')
    assert fixed['valid'] and not fixed['execution_ready']
    assert not fixed['warnings']
    assert fixed['registry_version']=='6.4.0'
    assert template('candidate-a')['registry_version']=='6.1.0'


def test_incompatible_and_authority_reducing_architectures_fail_before_execution():
    baseline=template('candidate-a')
    missing={**baseline,'components':[x for x in baseline['components']
                                     if x!='validation.mandatory_multistage']}
    result=assemble(missing)
    assert not result['valid'] and any('validation' in error.lower() for error in result['errors'])
    wrong={**baseline,'components':['orchestration.deterministic' if x=='orchestration.langgraph' else x
                                    for x in baseline['components']]}
    result=assemble(wrong)
    assert not result['valid'] and any('single guarded agent' in error for error in result['errors'])
    ranged={**baseline,'settings':{'reasoning.react_reflexion':{'max_unproductive':99}}}
    assert not assemble(ranged)['valid']


def test_architecture_versions_are_persisted_immutably(tmp_path):
    store=ArchitectureStore(Registry(tmp_path/'settings.db'))
    spec={**template('candidate-a'),'id':'candidate-a-frozen'}
    saved=store.save(spec,'research.operator')
    assert saved['immutable'] and store.get('candidate-a-frozen')['configuration_hash']==saved['configuration_hash']
    with pytest.raises(ValueError,match='immutable'):
        store.save(spec,'research.operator')


def test_experiment_preview_pins_verified_execution_architectures(tmp_path):
    registry_settings=Registry(tmp_path/'settings.db')
    spec={'profile_ids':['gateway-default'],'scenario_ids':['phone-log'],'token_limit':64,
          'architecture_id':'candidate-a'}
    planned=preview(registry_settings,spec)
    assert planned['architecture']['configuration_hash']==template('candidate-a')['configuration_hash']
    with pytest.raises(ValueError,match='retrieval artifact'):
        preview(registry_settings,{**spec,'architecture_id':'candidate-b-direct'})
    with pytest.raises(ValueError,match='retrieval artifact'):
        preview(registry_settings,{**spec,'architecture_id':'candidate-c-tot'})


def test_preview_builds_paired_architecture_trials_on_same_dataset(tmp_path,monkeypatch):
    registry_settings=Registry(tmp_path/'settings.db')
    monkeypatch.setattr('capstone_rag.document_curator.validate_selection',
        lambda selection,architecture:{'index_hash':selection['index_hash'],
            'chunk_artifact_hash':selection['chunk_artifact_hash'],'kind':'sparse',
            'strategy':'fixed','selection_hash':'selected'})
    planned=preview(registry_settings,{'profile_ids':['gateway-default'],
        'scenario_ids':['phone-log'],'repetitions':2,'architecture_ids':['candidate-a','candidate-b-direct'],
        'curator_retrievals':{'candidate-b-direct':{'index_hash':'index-b','chunk_artifact_hash':'chunks-b'}}})
    assert planned['planned_runs']==4
    assert [row['id'] for row in planned['architectures']]==['candidate-a','candidate-b-direct']
    assert planned['curator_retrievals']['candidate-b-direct']['selection_hash']=='selected'
