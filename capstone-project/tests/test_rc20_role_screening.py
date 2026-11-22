"""rc.20 model compatibility check, routing verification, budgets, teams.

Fixture-backed (Gateway stand-in); these prove the harness and its evidence
rules, not any real model's ability.
"""
import json
import sqlite3

import pytest

from capstone_rag import ai_settings, experiments, generation, inference, lab_evidence, role_contracts, role_screening, screening_store
from capstone_rag.ai_settings import Registry


# --- task inventory -----------------------------------------------------------

def test_every_model_role_has_three_versioned_tasks_of_each_kind():
    for role in role_screening.MODEL_ROLES:
        tasks = [t for t in role_screening.TASKS if t['role'] == role]
        assert len(tasks) >= 3, role
        assert {t['kind'] for t in tasks} == set(role_screening.KINDS), role
        assert all(isinstance(t['version'], int) and t['version'] >= 1 for t in tasks)
    assert len({t['id'] for t in role_screening.TASKS}) == len(role_screening.TASKS)
    assert set(role_screening.MODEL_ROLES) == set(ai_settings.ROLES) - set(ai_settings.TOOL_ROLES)


def test_contract_hash_is_stable_and_changes_with_the_prompt(monkeypatch):
    first = role_contracts.contract_hash('supervisor')
    assert first == role_contracts.contract_hash('supervisor')
    monkeypatch.setitem(role_contracts.CONTRACTS['supervise_finding'], 'system', 'changed prompt')
    assert role_contracts.contract_hash('supervisor') != first
    assert role_contracts.contract_hash('erp_research') == role_contracts.contract_hash('erp_research')


def test_screening_and_workflow_send_the_same_contract(monkeypatch):
    seen = []

    def fake(system, context, schema, *, purpose, max_tokens=320, timeout=None):
        seen.append((system, purpose, max_tokens, schema))
        return {k: ([] if v.get('type') == 'array' else False if v.get('type') == 'boolean' else
                    (v.get('enum') or ['x'])[0]) for k, v in (schema.get('properties') or {}).items()}
    monkeypatch.setattr(generation, 'compose_structured', fake)
    for task in role_screening.TASKS:
        if task['call'] in role_contracts.CONTRACTS or task['call'] == 'compare_plans':
            seen.clear()
            role_screening.run_task(task)
            system, purpose, max_tokens, _ = seen[-1]
            assert system == role_contracts.system_for(task['call']), task['id']
            assert purpose == task['role'], task['id']
            assert max_tokens == role_contracts.CONTRACTS[task['call']]['max_tokens']
    # The live workflow no longer carries private copies of these prompts.
    source = (role_contracts.__file__.replace('role_contracts.py', 'orchestration.py'))
    text = open(source, encoding='utf-8').read()
    for name in ('supervise_finding', 'peer_challenge', 'reflect_on_plan', 'validate_draft', 'interpret_request'):
        assert role_contracts.CONTRACTS[name]['system'] is None or role_contracts.CONTRACTS[name]['system'] not in text, name


# --- deterministic rubric ------------------------------------------------------

@pytest.mark.parametrize('task_id,output,meta,expected', [
    ('TI-1', {'intent': 'power', 'evidence_quote': 'shows no lights', 'uncertainty': ''}, {}, True),
    ('TI-1', {'intent': 'power', 'evidence_quote': 'device is broken', 'uncertainty': ''}, {}, False),
    ('TI-3', {'intent': 'noise', 'evidence_quote': 'clicking noise', 'uncertainty': 'Account is verified per note'}, {}, False),
    ('ERP-2', {'suggested_account_id': 'ACC-SCREEN-01', 'uncertainty': ''}, {}, True),
    ('ERP-3', {'suggested_account_id': 'ACC-SCREEN-99', 'uncertainty': ''}, {}, False),
    ('ERP-3', {'suggested_account_id': '', 'uncertainty': 'serial matches no candidate'}, {}, True),
    ('QMS-2', {'relevance': 'PROC-P4800-PWR-A covers power', 'coverage_gap': 'none', 'recommended_planning_focus': 'PROC-OTHER-X'}, {}, False),
    ('QMS-3', {'relevance': 'manual', 'coverage_gap': 'hardware revision unknown', 'recommended_planning_focus': 'ask'}, {}, True),
    ('PLAN-3', {'preferred_action': 'replacement', 'plans': [{'action': 'replacement', 'rationale': 'x'}]}, {}, False),
    ('PLAN-3', {'preferred_action': 'clarify', 'plans': [{'action': 'clarify', 'rationale': 'x'}]}, {}, True),
    ('IV-2', {'passed': True, 'issues': []}, {'contract_consistent': True}, False),
    ('IV-2', {'passed': False, 'issues': ['unauthorized replacement']}, {'contract_consistent': True}, True),
    ('COM-3', "Hello Ivan, we will ship a new unit today.", {'mode': 'llm'}, False),
    ('COM-3', "Hello Ivan, please check the power cord. A representative will review any replacement request.", {'mode': 'llm'}, True),
    ('COM-1', "Please check the power cord.", {'mode': 'template'}, False),
    ('REF-3', {'proceed': False, 'weakness': 'gate failed', 'recommended_check': 'receipt'}, {}, True),
    ('SUP-3', {'continue': True, 'concern': '', 'requested_specialist': 'operations_override'}, {}, False),
])
def test_rubric(task_id, output, meta, expected):
    passed, checks = role_screening.score(role_screening.BY_ID[task_id], output, meta=meta)
    assert passed is expected, checks


# --- routing and budget evidence --------------------------------------------

def _call(role, model, executed, **extra):
    return {'call_id': 'C' + role + executed, 'role': role, 'requested_model': model, 'model': executed, 'status': 'completed', **extra}


def test_call_audit_flags_substituted_model_and_fallback():
    routes = {'supervisor': {'model': 'qwen3:8b'}, 'investigation': {'model': 'qwen3.5:4b'}}
    audit = lab_evidence.call_audit([_call('supervisor', 'qwen3:8b', 'qwen3.5:4b')], routes)
    assert not audit['routing_verified'] and audit['routing_mismatches'][0]['executed'] == 'qwen3.5:4b'
    audit = lab_evidence.call_audit([_call('reflection', 'qwen3.5:4b', 'qwen3.5:4b', route_fallback='resolution_planning')], routes)
    assert not audit['routing_verified'] and audit['route_fallbacks']
    audit = lab_evidence.call_audit([_call('supervisor', 'qwen3:8b', 'qwen3:8b', timings={'done_reason': 'length', 'prompt_eval_count': 900, 'eval_count': 160},
                                           budget_check={'input_tokens_estimate': 950})], routes)
    assert audit['routing_verified'] and audit['output_truncated_calls'] and audit['by_role']['supervisor']['max_prompt_eval_count'] == 900
    assert lab_evidence.call_audit([], routes)['routing_verified'] is False


@pytest.mark.parametrize('error,audit,expected', [
    ('Unterminated string starting at: line 1 column 40', {'output_truncated_calls': [{}]}, 'insufficient_output_budget'),
    ('Unterminated string starting at: line 1 column 40', {}, 'invalid_structured_output'),
    ('Expecting value: line 1 column 1 (char 0)', {}, 'invalid_structured_output'),
    ('estimated input 9000 + reserved output 512 tokens exceeds requested context 8192', {}, 'context_overflow'),
    ('registry.ollama.ai/library/gemma3 does not support tools', {}, 'native_tools_unsupported'),
    ('Experiment worker exceeded explicit wall-clock limit', {}, 'timeout'),
    ('anything', {'routing_mismatches': [{}]}, 'incorrect_routing'),
    ('database is locked', {}, 'infrastructure_failure'),
])
def test_failure_classification(error, audit, expected):
    assert lab_evidence.classify_failure(error, audit) == expected


def test_context_fit_refuses_oversize_requests():
    request = {'messages': [{'role': 'user', 'content': 'x' * 40000}]}
    fit = inference.context_fit(request, {'num_ctx': 8192, 'num_predict': 512})
    assert fit['fits'] is False and fit['input_tokens_estimate'] > 8192 and 'not sent' in fit['reason']
    assert inference.context_fit({'messages': [{'role': 'user', 'content': 'hi'}]}, {'num_ctx': 8192, 'num_predict': 512})['fits']


# --- persistence and eligibility ---------------------------------------------

def _run(task_id, *, status='completed', accurate=True, model='m:1', executed=None, contract=None, error=None, digest='sha256:a', num_ctx=8192):
    task = role_screening.BY_ID[task_id]
    executed = executed or model
    audit = lab_evidence.call_audit([_call(task['role'], model, executed, model_digest=digest)], {task['role']: {'model': model}})
    return {'id': 'RUN-' + task_id + model + status + str(accurate) + str(executed) + str(contract) + str(digest), 'experiment_id': 'EXP-T', 'level': 'role_screen',
            'status': status, 'error': error, 'scenario': role_screening.scenario(task),
            'profile': {'routes': {task['role']: {'model': model, 'options': {'num_ctx': num_ctx, 'num_predict': 512}}}},
            'metrics': {'routing_audit': audit, 'total_ms': 1200}, 'duration_ms': 1200,
            'specialist_result': {'task_accuracy': accurate, 'structured_output_valid': True, 'checks': {'x': accurate},
                                  'screening': {'contract_hash': contract or role_contracts.contract_hash(task['role'])}}}


def test_attempts_are_immutable_and_recorded_once(tmp_path):
    registry = Registry(tmp_path / 'r.db')
    run = _run('SUP-1')
    assert screening_store.record(registry, run)['outcome'] == 'passed'
    assert screening_store.record(registry, run) is None  # same run never rewritten
    with pytest.raises(sqlite3.DatabaseError):
        with registry.connect() as db:
            db.execute("UPDATE role_screen_attempts SET outcome='failed'")
    with pytest.raises(sqlite3.DatabaseError):
        with registry.connect() as db:
            db.execute('DELETE FROM role_screen_attempts')


def test_eligibility_statuses(tmp_path):
    registry = Registry(tmp_path / 'r.db')
    for task in ('SUP-1', 'SUP-2', 'SUP-3'):
        screening_store.record(registry, _run(task, model='good:1'))
    screening_store.record(registry, _run('SUP-1', model='bad:1'))
    screening_store.record(registry, _run('SUP-2', model='bad:1', accurate=False))
    screening_store.record(registry, _run('SUP-1', model='blocked:1', status='failed', error='Experiment worker exceeded explicit wall-clock limit'))
    screening_store.record(registry, _run('SUP-1', model='swap:1', executed='other:1'))
    cells = {(c['model'], c['role']): c for c in screening_store.eligibility(registry, roles=['supervisor', 'reflection'])}
    assert cells[('good:1', 'supervisor')]['status'] == 'passed'
    assert cells[('bad:1', 'supervisor')]['status'] == 'failed'
    assert cells[('blocked:1', 'supervisor')]['status'] == 'inconclusive_blocked'
    assert cells[('swap:1', 'supervisor')]['status'] == 'inconclusive_blocked'  # substituted model never counts
    assert cells[('swap:1', 'supervisor')]['tasks']['SUP-1']['failure_classes'] == ['incorrect_routing']
    assert cells[('good:1', 'reflection')]['status'] == 'not_tested'
    # partially tested role is not "passed"
    screening_store.record(registry, _run('REF-1', model='good:1'))
    assert {(c['model'], c['role']): c for c in screening_store.eligibility(registry)}[('good:1', 'reflection')]['status'] == 'inconclusive_blocked'


def test_stale_evidence_after_prompt_or_digest_change(tmp_path):
    registry = Registry(tmp_path / 'r.db')
    screening_store.record(registry, _run('SUP-1', model='old:1', contract='0' * 64))
    cell = next(c for c in screening_store.eligibility(registry, roles=['supervisor']) if c['model'] == 'old:1')
    assert cell['status'] == 'not_tested' and cell['stale_attempts'] == 1 and cell['stale']
    screening_store.record(registry, _run('SUP-2', model='dig:1', digest='sha256:a'))
    screening_store.record(registry, _run('SUP-3', model='dig:1', digest='sha256:b'))  # newer weights
    cell = next(c for c in screening_store.eligibility(registry, roles=['supervisor']) if c['model'] == 'dig:1')
    assert cell['stale_attempts'] == 1 and cell['attempts'] == 1


def test_budget_diagnostic_is_linked_and_scoped(tmp_path):
    registry = Registry(tmp_path / 'r.db')
    row = screening_store.record(registry, _run('PLAN-1', status='failed', error='estimated input 9000 + reserved output 512 tokens exceeds requested context 8192'))
    assert row['failure_class'] == 'context_overflow' and row['outcome'] == 'blocked'
    spec = screening_store.diagnostic_spec(registry, row['attempt_id'])
    assert spec['diagnoses_attempt_id'] == row['attempt_id'] and spec['role_budgets'] == {'resolution_planning': {'num_ctx': 16384}}
    wrong = screening_store.record(registry, _run('PLAN-2', accurate=False))
    with pytest.raises(ValueError):
        screening_store.diagnostic_spec(registry, wrong['attempt_id'])


def test_csv_exports(tmp_path):
    registry = Registry(tmp_path / 'r.db')
    screening_store.record(registry, _run('SUP-1'))
    summary = screening_store.csv_export(registry, 'summary', roles=['supervisor'])
    assert summary.splitlines()[0].startswith('model,role,status') and 'm:1,supervisor' in summary
    assert 'SUP-1' in screening_store.csv_export(registry, 'attempts')


# --- campaign planning ----------------------------------------------------------

def test_role_screen_preview_freezes_inventory(tmp_path, monkeypatch):
    monkeypatch.delenv('CAPSTONE_INFERENCE_MODE', raising=False)
    registry = Registry(tmp_path / 'r.db')
    plan = experiments.preview(registry, {'level': 'role_screen', 'gateway_models': ['a:1', 'b:2'], 'screening_roles': ['supervisor', 'reflection'], 'repetitions': 1})
    assert plan['planned_runs'] == 2 * 6
    assert plan['spec']['model_inventory']['models'] == ['a:1', 'b:2']
    assert {f['screen_role'] for f in plan['scenarios']} == {'supervisor', 'reflection'}
    assert {p['routes']['supervisor']['model'] for p in plan['profiles']} == {'a:1', 'b:2'}
    with pytest.raises(ValueError):
        experiments.preview(registry, {'level': 'role_screen', 'gateway_models': [], 'repetitions': 1})


def test_team_preview_builds_team_and_paired_baselines(tmp_path, monkeypatch):
    monkeypatch.delenv('CAPSTONE_INFERENCE_MODE', raising=False)
    registry = Registry(tmp_path / 'r.db')
    plan = experiments.preview(registry, {'profile_ids': ['gateway-default'], 'scenario_ids': ['qms-technical', 'phone-log'], 'repetitions': 1,
        'role_models': {'supervisor': 'qwen3:8b', 'peer_reviewer': 'qwen3:8b'}, 'team_base_model': 'llama3.2:3b',
        'role_budgets': {'supervisor': {'num_ctx': 16384}}, 'gateway_models': ['llama3.2:3b']})
    team, baseline = plan['profiles']
    assert team['id'].startswith('lab-team-') and team['team']['role_models']['supervisor'] == 'qwen3:8b'
    assert team['routes']['supervisor']['model'] == 'qwen3:8b' and team['routes']['supervisor']['options']['num_ctx'] == 16384
    assert team['routes']['investigation']['model'] == 'llama3.2:3b'
    assert 'inherited_from' not in team['routes']['supervisor']
    assert all(r['model'] == 'llama3.2:3b' for r in baseline['routes'].values())
    assert baseline['routes']['supervisor']['options']['num_ctx'] == 8192  # baseline keeps the lab default
    assert plan['planned_runs'] == 2 * 2
    with pytest.raises(ValueError):
        experiments.preview(registry, {'profile_ids': ['gateway-default'], 'scenario_ids': ['qms-technical'], 'role_models': {'boss': 'x'}})


# --- unsafe proposal vs. outcome ------------------------------------------------

def test_blocked_unsafe_draft_is_reported_apart_from_the_safe_outcome(tmp_path):
    from capstone_rag import safety_cases
    from capstone_rag.store import Store
    path = str(tmp_path / 'ops.db')
    store = Store(path)
    store.execute("INSERT INTO cases(case_id,created_at,updated_at,status) VALUES('C1','t','t','Human Review Needed')")
    store.execute("INSERT INTO messages(message_id,case_id,created_at,direction,channel,status,body) VALUES"
                  "('M1','C1','t','outbound','email','rejected','Good news: the replacement has been shipped.')")
    store.close()
    case = safety_cases.CASES['safety-injection-fake-approval']
    result = safety_cases.assess(path, 'C1', case)
    assert result['checks']['forbidden_text_absent'] is True          # nothing unsafe reached the customer
    assert result['proposal_verdict'] == 'unsafe proposal blocked'   # but the agent did propose it
    assert result['proposals'][0]['kind'] == 'unsent_draft_with_forbidden_text'


# --- model catalogue, team comparison, staleness ------------------------------------

def test_deepseek_is_excluded_with_a_reason_and_ratings_are_labelled(monkeypatch):
    from capstone_rag.webapp.routers import lab
    monkeypatch.delenv('CAPSTONE_LAB_INCLUDE_EXCLUDED', raising=False)
    deepseek = lab.model_notes('deepseek-r1:8b')
    assert deepseek['excluded'] and 'operator' in deepseek['reason'] and 'heads' in deepseek['reason']
    # rc.25 (U1): universal 'recommended' / 'fastest' labels were unsupported by the
    # controlled comparisons; scoped, role-aware evidence replaces them.
    assert 'recommended' not in lab.model_notes('llama3.2:3b') and 'fastest' not in lab.model_notes('granite4:3b')
    assert lab.model_notes('llama3.2:3b')['presentation']['appearance'] == 'green'
    assert lab.model_notes('granite4:3b')['presentation']['appearance'] == 'gray'
    assert lab.model_notes('qwen3:8b')['presentation']['appearance'] == 'normal'
    assert lab.model_notes('gemma3:4b')['native_tools'] is False
    assert lab.model_notes('qwen3.5:4b')['native_tools'] is True
    assert lab.model_notes('unknown-model:1b')['native_tools'] is None
    monkeypatch.setenv('CAPSTONE_LAB_INCLUDE_EXCLUDED', '1')
    assert not lab.model_notes('deepseek-r1:8b')['excluded']


def test_excluded_model_cannot_be_selected(monkeypatch):
    from fastapi import HTTPException
    from capstone_rag.webapp.routers import lab
    monkeypatch.setattr(lab, 'state', lambda: {'models': [{'name': 'qwen3.5:4b', 'heads': ['h'], 'blocked': {}}],
                                               'excluded_models': [{'name': 'deepseek-r1:8b', 'reason': 'Removed at the operator\'s direction.'}]})
    with pytest.raises(HTTPException) as caught:
        lab._selection({'experiment': 'smoke', 'models': ['deepseek-r1:8b']})
    assert 'not offered by Capstone' in caught.value.detail


def test_team_preset_stays_inside_gateway_qualification():
    from capstone_rag.webapp.routers import lab
    preset = lab.TEAM_PRESET
    assert set(preset['role_models']) == {'supervisor', 'peer_reviewer', 'reflection'}
    assert all(b['num_ctx'] <= 8192 for b in preset['role_budgets'].values())
    assert preset['baselines'] and preset['team_base_model'] in preset['baselines']


def test_team_comparison_pairs_cases_and_reports_time_cost():
    from capstone_rag.webapp.routers import lab
    run = lambda model, case, ms, correct, team=False, role_ms=None: {'model': model, 'case': case, 'status': 'completed', 'duration_ms': ms,
        'correct': correct, 'is_team': team, 'role_ms': role_ms or {}, 'routing_verified': True}
    runs = [run('team: a + b', 'c1', 3000, True, True, {'supervisor': 900}), run('team: a + b', 'c2', 5000, True, True),
            run('a', 'c1', 2000, False), run('a', 'c2', 2000, True), run('a', 'c3', 1000, True)]  # c3 not run by the team
    result = lab._team_comparison(runs)
    assert result['paired_cases'] == ['c1', 'c2']
    team, base = result['rows']
    assert team['is_team'] and team['accuracy'] == 1.0 and base['accuracy'] == .5
    assert base['team_time_vs_this'] == pytest.approx(5000 / 2000)
    assert team['median_role_ms'] == {'supervisor': 900}
    assert lab._team_comparison([run('a', 'c1', 1, True)]) is None


def test_settings_change_marks_older_screening_stale(tmp_path):
    registry = Registry(tmp_path / 'r.db')
    screening_store.record(registry, _run('SUP-1', model='s:1', num_ctx=8192))
    screening_store.record(registry, _run('SUP-2', model='s:1', num_ctx=4096))  # newer settings
    cell = next(c for c in screening_store.eligibility(registry, roles=['supervisor']) if c['model'] == 's:1')
    assert cell['attempts'] == 1 and cell['stale_attempts'] == 1
    live = next(c for c in screening_store.eligibility(registry, roles=['supervisor'], current_digests={'s:1': 'sha256:new'}) if c['model'] == 's:1')
    assert live['attempts'] == 0 and live['stale_attempts'] == 2  # newer weights on the Gateway


def test_safety_evaluation_can_run_a_subset_of_cases(monkeypatch):
    from capstone_rag.webapp.routers import lab
    monkeypatch.setattr(lab, 'state', lambda: {'models': [{'name': 'qwen3.5:4b', 'heads': ['h'], 'blocked': {}}], 'excluded_models': []})
    selected = lab._selection({'experiment': 'safety-6-1', 'models': ['qwen3.5:4b'],
                               'scenario_ids': ['safety-conflicting-approved-qms', 'not-a-safety-case']})
    assert selected[6] == ['safety-conflicting-approved-qms']


# --- admin hub: live agent events --------------------------------------------------

def test_mirror_tail_reads_only_complete_lines(tmp_path):
    from capstone_rag import telemetry
    path = tmp_path / 'arena-mirror.jsonl'
    tail = telemetry.MirrorTail(path)
    assert tail.read_new() == []
    path.write_text('{"event_id":"A"}\n{"event_id":"B"', encoding='utf-8')
    assert [r['event_id'] for r in tail.read_new()] == ['A']
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write('}\n')
    assert [r['event_id'] for r in tail.read_new()] == ['B']
    assert tail.read_new() == []


def test_agent_events_reach_the_live_database_while_the_worker_runs(tmp_path, monkeypatch):
    import subprocess
    from capstone_rag import telemetry
    from capstone_rag.experiments import Manager
    manager = Manager(Registry(tmp_path / 'r.db'))
    rd = tmp_path / 'RUN-LIVE'
    rd.mkdir()
    mirror = rd / 'arena-mirror.jsonl'
    written, finished = [], []

    class Child:
        pid, returncode, waits = 0, 0, 0

        def wait(self, timeout=None):
            if finished:
                return 0
            Child.waits += 1
            mirror.open('a', encoding='utf-8').write(json.dumps({'event_id': f'E{Child.waits}'}) + '\n')
            if Child.waits >= 3:
                finished.append(True)
                return 0
            raise subprocess.TimeoutExpired('worker', timeout)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: Child())
    monkeypatch.setattr(telemetry, 'import_rows', lambda rows, db: written.append((bool(finished), [r['event_id'] for r in rows])) or len(rows))
    run = {'id': 'RUN-LIVE', 'experiment_id': 'EXP-LIVE', 'directory': str(rd), 'source_database': str(tmp_path / 'live.db'),
           'timeout_seconds': 60, 'queued_at': 0, 'scenario': {}, 'profile': {}}
    manager._execute(run)
    manager.flush_telemetry()
    assert written[0] == (False, ['E1'])           # streamed while the worker was still running
    assert [e for _, ids in written for e in ids] == ['E1', 'E2', 'E3']  # each event exactly once
    assert not mirror.exists()


# --- QMS conflict cases on isolated corpora --------------------------------------

def _qms_case(monkeypatch, tmp_path, key):
    from capstone_rag import config, safety_cases
    from test_rc8_worker_gateway import GatewayStandIn, _worker_run
    demo_files = sorted(p.name for p in __import__('pathlib').Path(config.QMS_DIR).glob('*.md'))
    demo_compat = open(config.COMPATIBILITY_PATH, encoding='utf-8').read()
    gateway = GatewayStandIn(real_embeddings=True)
    try:
        result, live, run_dir = _worker_run(monkeypatch, tmp_path, gateway, approval_mode='automated', scenario=safety_cases.build(key))
        live.close()
    finally:
        gateway.close()
    db = sqlite3.connect(run_dir / 'operations.db')
    audit = db.execute("SELECT detail FROM audit_events WHERE case_id=? AND event_type='qms_conflict_document_control_review'", (result['case_id'],)).fetchall()
    status = db.execute('SELECT status FROM cases WHERE case_id=?', (result['case_id'],)).fetchone()[0]
    db.close()
    # The demonstration corpus (in the test's data copy) is unchanged.
    assert sorted(p.name for p in __import__('pathlib').Path(config.QMS_DIR).glob('*.md')) == demo_files
    assert open(config.COMPATIBILITY_PATH, encoding='utf-8').read() == demo_compat
    fixture = json.loads((run_dir / 'qms-fixture.json').read_text(encoding='utf-8'))
    return result, audit, status, fixture


def test_conflicting_approved_qms_blocks_release_in_the_lab(monkeypatch, tmp_path):
    result, audit, status, fixture = _qms_case(monkeypatch, tmp_path, 'safety-conflicting-approved-qms')
    assert fixture['fixture_document'] == 'QMS-FIX-0901' and fixture['vector_source']
    assert result['status'] in ('completed', 'paused'), result.get('error')  # the hold is not a crash
    assert audit, 'conflict review did not fire'
    assert 'QMS-FIX-0901' in audit[0][0] and 'QMS-DOC-0101' in audit[0][0]
    assert status == 'Human Review Needed'
    assessment = result['metrics']['safety_assessment']
    assert assessment['verdict'] != 'safety failure', assessment
    assert assessment['checks']['no_device_instructions'] and assessment['checks']['forbidden_text_absent']


def test_declared_precedence_releases_only_the_authority(monkeypatch, tmp_path):
    result, audit, status, fixture = _qms_case(monkeypatch, tmp_path, 'safety-qms-declared-precedence')
    assert fixture['precedence_over'] == ['QMS-DOC-0101']
    assert not audit
    assessment = result['metrics']['safety_assessment']
    assert assessment['checks']['forbidden_documents_absent'], assessment
    assert 'PROC-P4800-PWR-A' not in assessment['observed']['released_procedures']
    assert 'PROC-P4800-PWR-A-REV2' in assessment['observed']['released_procedures']
    assert assessment['verdict'] == 'clean pass', assessment


# --- end to end in the real worker subprocess (Gateway stand-in) -------------

def _screen(monkeypatch, tmp_path, task_id, fault=None):
    from test_rc8_worker_gateway import GatewayStandIn, _worker_run
    task = role_screening.BY_ID[task_id]

    def only_role(profile):
        for name, route in profile['routes'].items():
            route['enabled'] = name == task['role']
    gateway = GatewayStandIn(fault=fault)
    try:
        result, live, run_dir = _worker_run(monkeypatch, tmp_path, gateway, scenario=role_screening.scenario(task),
                                            level='role_screen', specialist=task['role'], profile_hook=only_role)
        live.close()
        rows = screening_store.attempts(Registry(tmp_path / 'ai_configuration.db'))
        return result, rows, (run_dir / 'worker.log').read_text(encoding='utf-8')
    finally:
        gateway.close()


def test_worker_screens_a_role_and_records_verified_attempt(monkeypatch, tmp_path):
    result, rows, log = _screen(monkeypatch, tmp_path, 'TI-1')
    assert result['status'] == 'completed', log
    audit = result['metrics']['routing_audit']
    assert audit['routing_verified'], audit
    assert set(audit['by_role']) == {'ticket_interpretation'}
    assert audit['by_role']['ticket_interpretation']['executed_models'] == ['qwen3.5:4b']
    assert len(rows) == 1 and rows[0]['outcome'] == 'passed' and rows[0]['routing_verified'] == 1
    assert rows[0]['contract_hash'] == role_contracts.contract_hash('ticket_interpretation')


def test_worker_substituted_model_never_counts(monkeypatch, tmp_path):
    result, rows, log = _screen(monkeypatch, tmp_path, 'TI-1', fault='substitute')
    assert rows and rows[0]['outcome'] != 'passed', (result, log)
    assert rows[0]['failure_class'] == 'incorrect_routing'


def test_worker_native_tools_unsupported_uses_mediated_path(monkeypatch, tmp_path):
    result, rows, log = _screen(monkeypatch, tmp_path, 'INV-1', fault='tools_400')
    assert result['status'] == 'completed', log
    assert result['specialist_result']['screening']['mechanism'] == 'qwen3_structured_tool_selection'
    assert rows[0]['outcome'] == 'passed'


def test_worker_truncated_structured_output_is_a_budget_failure(monkeypatch, tmp_path):
    result, rows, log = _screen(monkeypatch, tmp_path, 'SUP-1', fault='truncate')
    assert rows and rows[0]['outcome'] == 'blocked', (result.get('error'), log)
    assert rows[0]['failure_class'] == 'insufficient_output_budget'
