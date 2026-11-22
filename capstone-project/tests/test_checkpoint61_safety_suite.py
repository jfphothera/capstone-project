"""Checkpoint 6.1 synthetic safety suite (isolated data, stand-in Gateway).

What this demonstrates: the deterministic safety gates and failure handling of
the real agent code, run end to end through the Testing Lab worker subprocess
on copied data. The stand-in Gateway returns schema-shaped placeholder model
output, so these runs say nothing about how well a real model writes; model
behaviour on these cases must be measured on the heads (Testing Lab category
"Safety evaluation (6.1)").

Set CAPSTONE_SAFETY_RESULTS=<path.json> to save every observed outcome.
"""
import json
import os
import time

import pytest

from capstone_rag import safety_cases

RESULTS = []


@pytest.fixture(scope='module', autouse=True)
def _save_results():
    yield
    path = os.environ.get('CAPSTONE_SAFETY_RESULTS')
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump({'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'harness': 'stand-in Gateway (placeholder model output), real agent code, isolated copied data',
                       'not_run': safety_cases.NOT_RUN, 'results': RESULTS}, stream, indent=2, default=str)


def _run(monkeypatch, tmp_path, key, *, approval_mode='automated', fault=None):
    from test_rc8_worker_gateway import GatewayStandIn, _worker_run
    case = safety_cases.build(key)
    # Real MiniLM query embeddings: without them retrieval is empty and the
    # document-release gates are never exercised.
    gateway = GatewayStandIn(fault=fault, real_embeddings=True)
    started = time.perf_counter()
    try:
        result, live, run_dir = _worker_run(monkeypatch, tmp_path, gateway, approval_mode=approval_mode, scenario=case)
        try:
            assessment = (safety_cases.assess((run_dir / 'operations.db').as_posix(), result['case_id'], case)
                          if result.get('case_id') else {'evaluated': False, 'reason': 'run stopped before a case was created'})
        finally:
            live.close()
    finally:
        gateway.close()
    lab_scored = ((result.get('metrics') or {}).get('safety_assessment') or {}).get('verdict')
    record = {'case': key, 'control': case['control'], 'policy': case['policy'], 'approval_mode': approval_mode, 'fault': fault,
              'testing_lab_scored_verdict': lab_scored,
              'run_status': result.get('status'), 'run_error': result.get('error'), 'wall_s': round(time.perf_counter() - started, 1),
              'model_calls': (result.get('metrics') or {}).get('model_calls'),
              'critical_violations': (result.get('metrics') or {}).get('critical_violations'), **assessment}
    RESULTS.append(record)
    return record


def test_testing_lab_offers_and_scores_safety_cases(monkeypatch):
    from capstone_rag.webapp.routers import lab
    available = {'name': 'qwen3.5:4b', 'heads': ['head-192-0-2-3'], 'blocked': {}}
    monkeypatch.setattr(lab, 'state', lambda: {'models': [available]})
    item = next(row for row in lab._catalog() if row['id'] == 'safety-6-1')
    assert item['scenario_ids'] == sorted(safety_cases.CASES)
    chosen = ['safety-burning-smell', 'safety-routine-accessory', 'safety-wrong-revision', 'safety-missing-identity']
    selected = lab._selection({'experiment': 'agentic-evaluation', 'models': ['qwen3.5:4b'], 'heads': [], 'case_count': 4,
                               'scenario_ids': chosen, 'methodology_matrix': {}})
    assert selected[6] == chosen and selected[5] == 4 * 32
    assert {row['id'] for row in lab._scenario_rows()} >= set(chosen)


@pytest.mark.parametrize('key', sorted(safety_cases.CASES))
def test_safety_case_deterministic_gates(monkeypatch, tmp_path, key):
    record = _run(monkeypatch, tmp_path, key)
    assert record['run_status'] in ('completed', 'paused', 'failed')
    assert record.get('evaluated'), record
    assert record['verdict'] != 'safety failure', json.dumps(record, indent=2, default=str)
    # The Testing Lab worker scored the same case itself (what campaigns on the heads record).
    assert record['testing_lab_scored_verdict'] == record['verdict']


def test_replacement_is_never_executed_without_a_human(monkeypatch, tmp_path):
    record = _run(monkeypatch, tmp_path, 'safety-replacement-demand', approval_mode='interactive')
    assert record.get('evaluated') and record['checks']['no_unapproved_transaction']
    assert not any(m.endswith(':executed') for m in record['observed']['movements'])


@pytest.mark.parametrize('fault', ['tools_400', 'truncate', 'hang', 'unavailable'])
def test_model_and_node_failures_fail_safe(monkeypatch, tmp_path, fault):
    record = _run(monkeypatch, tmp_path, 'safety-correct-revision', fault=fault)
    # Safe = nothing unauthorized released, no transaction, no unvalidated reply.
    if record.get('evaluated'):
        assert record['checks']['no_unauthorized_procedure'] and record['checks']['no_unapproved_transaction']
    assert not record.get('critical_violations')


# Gate-level checks for fabricated references, failed validation and unapproved
# movements are already covered by existing tests (see docs/checkpoint_6_1); the
# report cites them and the reproducible command runs them.
