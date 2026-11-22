import json
from pathlib import Path

import pytest

from capstone_rag import experiments
from capstone_rag.ai_settings import Registry
from capstone_rag.experiment_catalogue import category_contract
from capstone_rag.experiment_modes import contract
from capstone_rag.experiment_reports import compare
from capstone_rag.store import Store


def test_advertised_modes_and_installed_engineering_targets_are_truthful():
    modes=contract()['modes']
    assert {row['implementation_status'] for row in modes}=={'executable','partial','not_implemented'}
    assert all(row['selectable']==(row['implementation_status']=='executable') for row in modes)
    categories=category_contract(Path(__file__).resolve().parents[1])
    engineering=[row for row in categories if row['level']=='engineering']
    assert engineering and all(row['selectable'] for row in engineering)
    assert all((Path(__file__).resolve().parents[1]/row['target'].split('::',1)[0]).is_file()
               for row in engineering)


def test_campaign_recovery_is_explicit_and_never_replays_terminal_runs(tmp_path,monkeypatch):
    registry=Registry(tmp_path/'settings.db');manager=experiments.Manager(registry)
    revision=experiments.source_revision()
    manager.save({'id':'EXP-R','status':'incomplete','source_revision':revision,
                  'stop_requested':True,'concurrency':1,'failure_limit':3,
                  'directory':str(tmp_path)})
    for status in ('completed','failed','queued'):
        directory=tmp_path/status;directory.mkdir()
        manager._store_run({'id':'RUN-'+status,'experiment_id':'EXP-R','status':status,
                            'directory':str(directory)})
    monkeypatch.setattr(manager,'_campaign',lambda _eid: None)
    value=manager.resume_campaign('EXP-R','Release Operator','Reviewed preserved evidence')
    manager.threads['EXP-R'].join()
    assert value['last_recovery_authorization']['scope']=='continue_queued_only'
    assert value['last_recovery_authorization']['interrupted_trials_replayed'] is False
    states={row['id']:row['status'] for row in manager.runs('EXP-R')}
    assert states=={'RUN-completed':'completed','RUN-failed':'failed','RUN-queued':'queued'}
    cancelled=manager.cancel('EXP-R')
    assert cancelled['status']=='cancelled'
    assert {row['id']:row['status'] for row in manager.runs('EXP-R')}=={
        'RUN-completed':'completed','RUN-failed':'failed','RUN-queued':'cancelled'}


def test_disk_safety_floor_is_bounded_in_preview(tmp_path):
    registry=Registry(tmp_path/'settings.db')
    spec={'profile_ids':['gateway-default'],'scenario_ids':['phone-log'],'token_limit':64}
    assert experiments.preview(registry,spec)['spec']['minimum_free_disk_bytes']==268435456
    with pytest.raises(ValueError,match='free disk'):
        experiments.preview(registry,{**spec,'minimum_free_disk_bytes':2**41})


def test_comparison_preserves_profiles_parameters_strategies_and_missing_resources(tmp_path):
    store=Store(str(tmp_path/'operations.db'))
    runs=[]
    for profile_id,strategy,seed in [('A','sequential',7),('B','parallel_bounded',8)]:
        runs.append({'id':'RUN-'+profile_id,'status':'completed','specialist':'system',
            'execution_strategy':strategy,'campaign_concurrency':1 if strategy=='sequential' else 2,
            'seed':seed,'source_revision':'rev','source_snapshot_sha256':'snap',
            'profile':{'id':profile_id,'configuration_hash':'hash-'+profile_id,
                'routes':{'investigation':{'options':{'temperature':0,'seed':seed}}},'endpoints':{}},
            'metrics':{'all_scored_checks_passed':True,'critical_violations':[],
                       'total_ms':100+seed,'model_usage':[]}})
    result=compare(runs,store)
    assert {row['profile_id'] for row in result['trial_groups']}=={'A','B'}
    assert {row['execution_strategy'] for row in result['trial_groups']}=={'sequential','parallel_bounded'}
    assert all(row['resource_metrics'] is None for row in result['trial_groups'])
    assert all(row['snapshot_hashes']==['snap'] for row in result['trial_groups'])
