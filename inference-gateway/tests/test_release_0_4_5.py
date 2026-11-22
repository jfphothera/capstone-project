"""Release 0.4.5: placement_policy.unmeasured_model_limit. A model without current per-head evidence (no current
declared or measured value) gets at most this many leases on a head instead of every slot; 0 (the default) keeps 0.4.4.

Lab, 2026-11-18 (evidence-20261118-102700): the reviewer qwen3:4b had no declaration on the 16 GB heads 3-5, got their
8 slots, ran 7-8 calls at once on heads 3 and 5, 11 timed out, and its memory kept the main model off those heads.
The lab applies unmeasured_model_limit 1 with APPLY-MODEL-CAPACITY.
"""
import importlib.util
import time
from pathlib import Path

import pytest

from inference_gateway import capacity_bootstrap, model_capacity, recovery
from inference_gateway.capacity_gateway import Gateway

_spec = importlib.util.spec_from_file_location('fp_helpers', Path(__file__).with_name('test_failures_and_placement.py'))
fp = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(fp)
_spec4 = importlib.util.spec_from_file_location('r044', Path(__file__).with_name('test_release_0_4_4.py'))
r044 = importlib.util.module_from_spec(_spec4); _spec4.loader.exec_module(r044)


def test_default_keeps_0_4_4_and_the_key_is_validated():
    p = recovery.validate_policy('placement_policy', None)
    assert p['unmeasured_model_limit'] == 0
    assert recovery.validate_policy('placement_policy', {'unmeasured_model_limit': 1})['unmeasured_model_limit'] == 1
    for bad in (65, -1, True, '1', 1.5):
        with pytest.raises(ValueError):
            recovery.validate_policy('placement_policy', {'unmeasured_model_limit': bad})


def eff(policy, families=('qwen3',), declared=None, measured=None, slots=8, version='0.40.15'):
    row = lambda n: {'parallel': n, 'digest': 'D', 'runtime_version': '0.40.15', 'num_ctx': 8192, 'evidence': 'e', 'recorded_at': 1.0}
    return model_capacity.effective('qwen3:4b', slots=slots, families=families, runtime_version=version, digest='D',
                                    declared=row(declared) if declared else None, measured=row(measured) if measured else None, policy=policy)


def test_unmeasured_models_get_the_limit_and_any_current_evidence_wins():
    p = {**recovery.validate_policy('placement_policy', None), 'unmeasured_model_limit': 1}
    r = eff(p)
    assert (r['limit'], r['source'], r['reason']) == (1, 'unmeasured_default', 'no_current_evidence'), "the 16 GB heads' reviewer"
    assert (eff(p, declared=4)['limit'], eff(p, declared=4)['source']) == (4, 'declared'), 'a measured head keeps its measurement'
    assert (eff(p, measured=2)['limit'], eff(p, measured=2)['source']) == (2, 'measured')
    r = eff(p, declared=4, version='0.40.16')
    assert (r['limit'], r['source'], r['declared']['stale_because']) == (1, 'unmeasured_default', 'runtime_changed'), 'stale evidence counts as none'
    assert eff(p, families=('qwen35',))['source'] == 'serial_family', 'qwen3.5 keeps its own rule and reason'
    assert eff({**p, 'unmeasured_model_limit': 0})['limit'] == 8 and eff({**p, 'unmeasured_model_limit': 3})['limit'] == 3
    assert eff({**p, 'unmeasured_model_limit': 3}, slots=2)['limit'] == 2, "never above the head's slots"
    assert eff({**p, 'model_parallel': {'qwen3:4b': 2}})['source'] == 'policy'


def lab(tmp_path, model_capacity_value=None, unmeasured=1):
    rid = 'head-192-0-2-3'
    transport = r044.UpgradedLab(); gateway = Gateway(tmp_path / 'ledger.db', transport=transport); b = gateway.broker
    b.register_client('capstone', 't-capstone', models=['m:1'], max_concurrency=16, queue_limit=32, priority=6)
    b.authorize_resource(rid, endpoint=fp.endpoint(rid), resource_type='shared_inference', owner='infrastructure', applications=['capstone'],
                         permitted_uses=['inference'], slots=8, memory_gb=31.4, model_capacity=model_capacity_value)
    assert gateway.qualify(rid, 'm:1', operator='ops')['passed']
    with b.write('test') as db:
        recovery.store_policy(db, 'failure_policy', {'ps_settle_seconds': 0})
        recovery.store_policy(db, 'placement_policy', {'unmeasured_model_limit': unmeasured})
    transport.calls.clear(); gateway.refresh_health()
    return gateway, transport, rid


@pytest.mark.parametrize('declared,unmeasured,expected,source', [(None, 1, 1, 'unmeasured_default'), (3, 1, 3, 'declared'), (None, 0, 5, 'slots')])
def test_admission_runs_only_the_limit_at_once_and_queues_the_rest(tmp_path, declared, unmeasured, expected, source):
    entry = {'m:1': {'parallel': declared, 'digest': 'DIGEST-m:1', 'runtime_version': '0.40.15', 'num_ctx': 4096, 'evidence': 'lab'}} if declared else None
    gateway, transport, rid = lab(tmp_path, entry, unmeasured)
    transport.hold = True
    threads = fp.background(gateway, ['a', 'b', 'c', 'd', 'e'], model='m:1', prompt='hold')
    assert transport.wait_inflight(expected)
    time.sleep(.5)
    assert transport.inflight == expected, 'only the per-model limit runs at once on the 8-slot head; the rest wait in the Gateway queue'
    transport.release.set()
    for t in threads:
        t.join(10)
    with gateway.broker.connect() as db:
        [limit] = model_capacity.head_limits(db, rid, ['m:1'], recovery.load_policy(db)['placement'])
    assert (limit['limit'], limit['source']) == ((8 if source == 'slots' else expected), source)


def test_a_bootstrap_sets_the_limit_and_a_later_bootstrap_without_the_block_keeps_it(tmp_path):
    gateway, _transport, rid = lab(tmp_path, None, 0)
    db_path = tmp_path / 'ledger.db'
    base = {'configuration_version': 1, 'contract': 'shared.inference/1',
            'resources': [{'resource_id': rid, 'endpoint': fp.endpoint(rid), 'resource_type': 'shared_inference', 'owner': 'infrastructure',
                           'applications': ['capstone'], 'permitted_uses': ['inference'], 'slots': 8, 'memory_gb': 8.5}]}
    import json
    for name, policy, expected in (('with', {**base, 'placement_policy': {'unmeasured_model_limit': 1}}, 1), ('without', base, 1)):
        f = tmp_path / ('policy-%s.json' % name); f.write_text(json.dumps(policy), encoding='utf-8')
        out = capacity_bootstrap.apply(f, db_path)
        assert out['placement_policy']['unmeasured_model_limit'] == expected, name + ': a later hub bootstrap without the block keeps it'
    bad = tmp_path / 'policy-bad.json'; bad.write_text(json.dumps({**base, 'placement_policy': {'unmeasured_model_limit': 99}}), encoding='utf-8')
    with pytest.raises(ValueError):
        capacity_bootstrap.apply(bad, db_path)
