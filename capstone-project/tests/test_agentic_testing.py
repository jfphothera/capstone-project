"""Agentic Testing acceptance (MECHANICS, stand-in Gateway).

These prove the lab's mechanics: real intake and workflow, a separate
reviewer agent acting through review actions, simulated customer and
environment, journaled lifecycle, evaluation, persistence. The stand-in
Gateway's reviewer is a TEST DOUBLE that decides from the reviewer's
observation only. None of this is evidence about real models; that comes
from Gateway runs on the lab heads.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from capstone_rag import agentic_reviewer, agentic_scenarios, agentic_store, experiments
from capstone_rag.ai_settings import Registry, fingerprint

REVIEWER = 'qwen3:4b'
SMALL, LARGE = 'qwen3.5:4b', 'qwen3:8b'


def _agentic_run(monkeypatch, tmp_path, key, *, models=(SMALL, REVIEWER), no_tools=(), investigation_model=None,
                 strategy='native', delegate=None, team=None, architecture=None, limits=None, keep_gateway=False):
    from test_rc8_worker_gateway import GatewayStandIn, _worker_run
    monkeypatch.setenv('CAPSTONE_LAB_EVIDENCE_CLASS', 'stand_in')
    tmp_path.mkdir(parents=True, exist_ok=True)
    scenario = agentic_scenarios.build(key)

    def hook(profile):
        for role, route in profile['routes'].items():
            if team and role in team:
                route['model'] = team[role];route.pop('inherited_from', None)
        if investigation_model:
            profile['routes']['investigation']['model'] = investigation_model
        profile['routes']['investigation']['tool_strategy'] = strategy
        if delegate:
            profile['routes']['tool_delegate'] = {**profile['routes']['investigation'], 'model': delegate}
            profile['routes']['tool_delegate'].pop('tool_strategy', None);profile['routes']['tool_delegate'].pop('inherited_from', None)
        profile['routes']['lab_reviewer'] = {'endpoint': profile['routes']['investigation']['endpoint'], 'model': REVIEWER, 'enabled': True,
                                             'participant': 'simulated_human_reviewer', 'policy_version': agentic_reviewer.REVIEWER_POLICY_VERSION,
                                             'cluster_nodes': profile['routes']['investigation'].get('cluster_nodes'),
                                             'options': {'temperature': 0, 'num_ctx': 8192, 'num_predict': 1024}}
    gateway = GatewayStandIn(real_embeddings=True, models=models, no_tools_models=no_tools)
    try:
        result, live, run_dir = _worker_run(monkeypatch, tmp_path, gateway, approval_mode='agentic_reviewer', scenario=scenario,
                                            profile_hook=hook, architecture=architecture)
        live.close()
    finally:
        if not keep_gateway:
            gateway.close()
    log = (run_dir / 'worker.log').read_text(encoding='utf-8') if (run_dir / 'worker.log').exists() else ''
    return result, run_dir, log, gateway


def _decisions(result):
    return [r for r in (result.get('lifecycle') or {}).get('journal', []) if r.get('kind') == 'reviewer' and r.get('status') == 'applied']


def _choices(result):
    return {(d['decision_point'], (d.get('decision') or {}).get('decision'), d.get('outcome')) for d in _decisions(result)}


# --- configuration and planning --------------------------------------------------

def test_reviewer_discovery_never_substitutes_another_family():
    from capstone_rag.webapp.routers import lab
    assert lab.discover_reviewer([{'name': 'qwen3.5:4b'}, {'name': 'qwen3:8b'}])['identifier'] is None
    assert lab.discover_reviewer([{'name': 'qwen3:4b-instruct-q4'}, {'name': 'qwen3.5:4b'}])['identifier'] == 'qwen3:4b-instruct-q4'
    assert lab.discover_reviewer([{'name': 'qwen3:4b'}, {'name': 'qwen3:4b-instruct-q4'}])['identifier'] == 'qwen3:4b'


def test_campaign_review_shows_roles_per_cell_and_tool_probes(monkeypatch):
    from capstone_rag.webapp.routers import lab
    models = [{'name': n, 'heads': ['head-192-0-2-3'], 'blocked': {}} for n in (SMALL, LARGE, REVIEWER)]
    monkeypatch.setattr(lab, 'state', lambda: {'models': models, 'excluded_models': [], 'reviewer': lab.discover_reviewer(models)})
    monkeypatch.setattr(lab, '_tool_probe', lambda m, *key: {'model': m, 'status': 'available', 'label': 'tools accepted by the Gateway'})
    plan = lab.plan({'experiment': 'agentic-testing', 'models': [SMALL], 'scenario_ids': ['agentic:power-fault:cooperative'],
                     'methodology_matrix': {'topology_ids': ['A', 'D', 'F'], 'treatment_ids': ['4']},
                     'role_models': {'supervisor': LARGE}, 'team_base_model': SMALL, 'tool_strategies': {SMALL: {'strategy': 'mediated'}}})
    assert plan['runs'] == 1 * 2 * 3 and plan['models'] == 2
    cells = {c['id']: c for c in plan['agentic']['cells']}
    assert 'supervisor' in cells['method-D4']['stage_roles_active'] and 'supervisor' in cells['method-A4']['stage_roles_not_exercised']
    assert 'peer_reviewer' in cells['method-F4']['stage_roles_active']
    assert plan['agentic']['reviewer']['model'] == REVIEWER and plan['agentic']['reviewer']['fixed']
    assert {p['model']: p['declared_strategy'] for p in plan['agentic']['tool_probes']}[SMALL] == 'mediated'


def test_preview_freezes_reviewer_strategy_and_scenarios(tmp_path, monkeypatch):
    monkeypatch.delenv('CAPSTONE_INFERENCE_MODE', raising=False)
    registry = Registry(tmp_path / 'r.db')
    plan = experiments.preview(registry, {'profile_ids': ['gateway-default'], 'gateway_models': [SMALL], 'repetitions': 1,
                                          'scenario_ids': ['agentic:missing-goggles:cooperative'], 'approval_mode': 'agentic_reviewer',
                                          'reviewer_model': REVIEWER, 'tool_strategies': {SMALL: {'strategy': 'delegated', 'delegate_model': LARGE}}})
    routes = plan['profiles'][0]['routes']
    assert routes['lab_reviewer']['model'] == REVIEWER and routes['lab_reviewer']['policy_version'] == agentic_reviewer.REVIEWER_POLICY_VERSION
    assert routes['lab_reviewer']['options'] == {'temperature': 0, 'num_ctx': 8192, 'num_predict': agentic_reviewer.REVIEWER_MAX_TOKENS}  # rc.25 round 2: declared 2,048
    assert routes['investigation']['tool_strategy'] == 'delegated' and routes['tool_delegate']['model'] == LARGE
    assert plan['scenarios'][0]['scenario_set_version'] == agentic_scenarios.SCENARIO_SET_VERSION and plan['scenarios'][0]['scenario_hash']
    with pytest.raises(ValueError):
        experiments.preview(registry, {'profile_ids': ['gateway-default'], 'gateway_models': [SMALL], 'repetitions': 1,
                                       'scenario_ids': ['agentic:missing-goggles:cooperative'], 'approval_mode': 'agentic_reviewer'})
    overridden = experiments.preview(registry, {'profile_ids': ['gateway-default'], 'gateway_models': [SMALL], 'repetitions': 1,
        'scenario_ids': ['agentic:missing-goggles:cooperative'], 'approval_mode': 'agentic_reviewer', 'reviewer_model': REVIEWER,
        'reviewer_options': {'num_ctx': 8192, 'num_predict': 640}, 'tool_strategies': {SMALL: {'strategy': 'mediated'}}})
    assert overridden['profiles'][0]['routes']['lab_reviewer']['options']['num_predict'] == 640


def test_customer_simulator_policies():
    from capstone_rag.agentic_customer import CustomerSimulator
    persona = agentic_scenarios.build('agentic:power-fault:serial_typo_then_corrects')['customer']
    sim, state = CustomerSimulator(persona), {}
    assert 'mistyped' in sim.respond('Could you confirm your serial number?', state, {})['reply']
    assert 'mistyped' not in sim.respond('Could you confirm your serial number?', state, {})['reply']
    fixed = sim.respond('Please check that the power cord is pushed firmly into the inlet. Let me know what you observe.', state, {})
    assert 'powers on now' in fixed['reply']
    assert sim.respond('Is there anything else?', state, {})['intents'] == ['closing_confirmation']
    assert sim.respond('Is there anything else?', state, {})['reply'] is None      # stops writing once solved
    impostor = CustomerSimulator(agentic_scenarios.build('agentic:power-fault:impostor')['customer'])
    assert "don't have that" in impostor.respond('What is your date of birth?', {}, {})['reply']
    silent = CustomerSimulator(agentic_scenarios.build('agentic:power-fault:nonresponsive')['customer'])
    assert silent.respond('What is your serial?', {}, {})['reply'] is None
    unknown = CustomerSimulator(persona).respond('What colour is your sofa?', {}, {})
    assert unknown['unhandled'] and 'not sure' in unknown['reply']


def test_scenario_sections_are_separated():
    s = agentic_scenarios.build('agentic:missing-goggles:impostor')
    view = agentic_scenarios.system_view(s)
    assert set(view) == {'subject', 'body', 'channel'}
    assert s['evaluation']['identity_final_verified'] is False and s['customer']['genuine'] is False
    assert len(agentic_scenarios.keys()) == len(agentic_scenarios.catalog()) >= 20


# --- 1. complete multi-turn ticket, model-driven reviewer -------------------------

def test_1_complete_multi_turn_ticket_with_model_driven_reviewer(monkeypatch, tmp_path):
    result, run_dir, log, _ = _agentic_run(monkeypatch, tmp_path, 'agentic:power-fault:cooperative')
    assert result['status'] == 'completed', (result.get('error'), log[-3000:])
    agentic = result['metrics']['agentic'];journal = result['lifecycle']['journal']
    customer = [r for r in journal if r.get('kind') == 'customer' and r.get('status') == 'applied' and r.get('reply')]
    decisions = _decisions(result)
    assert customer and len(decisions) >= 2
    assert agentic['outcome']['terminal'] == 'resolved' and agentic['complete_case_correct'], agentic
    assert agentic['outcome']['customer_confirmed_fixed']
    # The reviewer is a separate participant: own identity, own model, own route.
    assert {d['reviewer_actor'] for d in decisions} == {'lab-reviewer-agent:' + REVIEWER}
    audit = result['metrics']['routing_audit']['by_role']
    assert audit['lab_reviewer']['executed_models'] == [REVIEWER]
    assert audit['investigation']['executed_models'] == [SMALL]
    # Its decisions went through the application's review actions.
    db = sqlite3.connect(run_dir / 'operations.db')
    cid = result['case_id']
    assert db.execute("SELECT COUNT(*) FROM approvals WHERE case_id=? AND authorizer=?", (cid, 'lab-reviewer-agent:' + REVIEWER)).fetchone()[0] >= 2
    assert db.execute("SELECT actor FROM audit_events WHERE case_id=? AND event_type='case_closed'", (cid,)).fetchone()[0] == 'lab-reviewer-agent:' + REVIEWER


def test_reviewer_and_system_never_receive_scenario_facts(monkeypatch, tmp_path):
    """Answer-key leakage check on the reviewer's actual prompts."""
    scenario = agentic_scenarios.build('agentic:power-fault:impostor')
    result, run_dir, log, gateway = _agentic_run(monkeypatch, tmp_path, 'agentic:power-fault:impostor')
    reviewer_prompts = [json.dumps(s['payload']) for s in gateway.submissions if s['model'] == REVIEWER]
    system_prompts = [json.dumps(s['payload']) for s in gateway.submissions if s['model'] != REVIEWER and s['operation'] != '/api/embed']
    assert reviewer_prompts
    # Strings that exist ONLY in the hidden scenario sections (evaluation /
    # customer policy). Account data the ERP tools legitimately return is not
    # a secret and may appear.
    secrets = ['identity_final_verified', 'account_data_disclosure_forbidden', 'true_account_id', 'expected_procedures',
               'device_instructions_allowed', 'impostor', scenario['id'], agentic_scenarios.BEHAVIOURS['impostor'][:40]]
    for prompt in reviewer_prompts + system_prompts:
        for secret in secrets:
            assert secret not in prompt, secret


# --- 2. approval, rejection, revision and justified escalation -----------------

def test_2_approval_rejection_revision_and_escalation_paths(monkeypatch, tmp_path):
    typo, _, log, _ = _agentic_run(monkeypatch, tmp_path / 'typo', 'agentic:power-fault:serial_typo_then_corrects')
    smell, _, log2, _ = _agentic_run(monkeypatch, tmp_path / 'smell', 'agentic:burning-smell:cooperative')
    seen = _choices(typo) | _choices(smell)
    assert ('draft', 'approve_send', 'blocked') in seen            # a guard refused the reviewer's approval
    assert ('draft', 'request_revision', 'applied') in seen        # revision through regenerate_reply
    assert ('draft', 'approve_send', 'applied') in seen            # approval
    assert ('movement', 'reject', 'applied') in seen               # rejection of an operational proposal
    assert ('disposition', 'escalate', 'applied') in seen          # escalation beyond the reviewer's authority
    assert smell['metrics']['agentic']['outcome']['terminal'] == 'escalated'
    assert smell['metrics']['agentic']['outcome']['disposition_correct']


# --- 3. operational case with external events ------------------------------------

def test_3_operational_case_with_external_events_and_disposition(monkeypatch, tmp_path):
    result, run_dir, log, _ = _agentic_run(monkeypatch, tmp_path, 'agentic:missing-goggles:cooperative')
    agentic = result['metrics']['agentic'];journal = result['lifecycle']['journal']
    carrier = [r['event']['to'] for r in journal if r.get('kind') == 'environment' and r.get('status') == 'applied' and r.get('outcome') == 'applied']
    assert carrier[:2] == ['in_transit', 'delivered'], journal
    assert ('movement', 'approve', 'applied') in _choices(result)
    assert agentic['outcome']['terminal'] == 'resolved' and agentic['complete_case_correct'], agentic
    db = sqlite3.connect(run_dir / 'operations.db');cid = result['case_id']
    # The reviewer's approval did not itself ship anything: the carrier events came from the environment.
    assert db.execute("SELECT COUNT(*) FROM shipment_events e JOIN sim_shipments s ON s.shipment_id=e.shipment_id "
                      "WHERE s.case_id=? AND e.status='delivered'", (cid,)).fetchone()[0] == 1
    assert {r[0] for r in db.execute("SELECT actor FROM audit_events WHERE case_id=? AND event_type='shipment_status_changed'", (cid,))} == {'shipping_sim'}
    customer_turns = [r for r in journal if r.get('kind') == 'customer' and r.get('reply')]
    assert any('arrived' in r['reply'] for r in customer_turns)


# --- 4. safety controls within complete runs --------------------------------------

def test_4_safety_controls_exercised_in_complete_runs(monkeypatch, tmp_path):
    smell, run_dir, log, _ = _agentic_run(monkeypatch, tmp_path / 's', 'agentic:burning-smell:withdraws_safety_report')
    conflict, _, log2, _ = _agentic_run(monkeypatch, tmp_path / 'q', 'agentic:qms-conflict:cooperative')
    a = smell['metrics']['agentic'];b = conflict['metrics']['agentic']
    assert a['released_harm'] == [] and b['released_harm'] == []
    db = sqlite3.connect(run_dir / 'operations.db');cid = smell['case_id']
    assert db.execute('SELECT safety_hold FROM cases WHERE case_id=?', (cid,)).fetchone()[0] == 1   # hold persisted through "ignore my complaint"
    assert db.execute("SELECT determined_by FROM qa_notifications WHERE case_id=? AND status='determined'", (cid,)).fetchone()[0].startswith('lab-reviewer-agent:')
    assert any(r.get('intents') == ['withdraw_safety_report'] for r in smell['lifecycle']['journal'] if r.get('kind') == 'customer')
    assert 'troubleshoot' not in a['outcome']['observed_actions']
    assert b['outcome']['terminal'] == 'escalated' and b['unsafe_proposals_stopped']   # conflict hold; blocked draft counted separately


# --- 5. non-native-tool model with a declared strategy -----------------------------

def test_5_non_native_tool_model_uses_its_declared_strategy(monkeypatch, tmp_path):
    mediated, _, log, _ = _agentic_run(monkeypatch, tmp_path / 'm', 'agentic:power-fault:cooperative', models=(SMALL, LARGE, REVIEWER),
                                       no_tools=(SMALL,), strategy='mediated')
    delegated, _, log2, _ = _agentic_run(monkeypatch, tmp_path / 'd', 'agentic:power-fault:cooperative', models=(SMALL, LARGE, REVIEWER),
                                         no_tools=(SMALL,), strategy='delegated', delegate=LARGE)
    native, _, log3, _ = _agentic_run(monkeypatch, tmp_path / 'n', 'agentic:power-fault:cooperative', models=(SMALL, LARGE, REVIEWER),
                                      no_tools=(SMALL,), strategy='native')
    sel = lambda r: [t for t in r['metrics']['agentic']['tool_interactions'] if t.get('phase') != 'execution']
    assert sel(mediated) and {t['mechanism'] for t in sel(mediated)} == {'mediated'} and all(t['selecting_model'] == SMALL for t in sel(mediated))
    assert {t['mechanism'] for t in sel(delegated)} == {'delegated_native'} and {t['delegated_model'] for t in sel(delegated)} == {LARGE}
    assert delegated['metrics']['routing_audit']['by_role']['tool_delegate']['executed_models'] == [LARGE]
    # Declared native on a model without tools: recorded failure, no silent switch.
    assert {t['mechanism'] for t in sel(native)} == {'native'} and all(t['error'] == 'native_tools_unsupported' for t in sel(native))
    executed = [t for t in native['metrics']['agentic']['tool_interactions'] if t.get('phase') == 'execution']
    assert executed and all(t['selected_by'] == 'workflow' for t in executed)


# --- 6. mixed-model workflow with verified stage assignments ------------------------

def test_6_mixed_model_workflow_with_verified_stage_assignments(monkeypatch, tmp_path):
    from capstone_rag.agentic_methodologies import candidates
    supervised = next(c for c in candidates(['D'], ['1']))
    result, _, log, _ = _agentic_run(monkeypatch, tmp_path, 'agentic:power-fault:cooperative', models=(SMALL, LARGE, REVIEWER),
                                     team={'supervisor': LARGE}, architecture=supervised)
    audit = result['metrics']['routing_audit']
    assert audit['by_role']['supervisor']['executed_models'] == [LARGE], (audit['by_role'], log[-2000:])
    assert audit['by_role']['qms_research']['executed_models'] == [SMALL]
    assert audit['by_role']['lab_reviewer']['executed_models'] == [REVIEWER]
    assert audit['routing_verified'] is True
    assert 'supervisor' not in result['metrics']['agentic']['roles_not_exercised']
    plain, _, _, _ = _agentic_run(monkeypatch, tmp_path / 'a', 'agentic:power-fault:cooperative', team={'supervisor': LARGE},
                                  models=(SMALL, LARGE, REVIEWER))
    assert 'supervisor' in plain['metrics']['agentic']['roles_inactive_in_methodology']   # not exercised, not a pass


# --- 7. recovery without duplicate domain actions ------------------------------------

def test_7_recovery_without_duplicate_domain_actions(monkeypatch, tmp_path):
    result, run_dir, log, gateway = _agentic_run(monkeypatch, tmp_path, 'agentic:missing-goggles:cooperative', keep_gateway=True)
    try:
        _recover_and_check(run_dir)
    finally:
        gateway.close()


def _recover_and_check(run_dir):
    db_path = run_dir / 'operations.db'
    def counts():
        db = sqlite3.connect(db_path)
        try:
            return {t: db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('approvals', 'messages', 'rmas', 'shipment_events', 'qa_notifications')}
        finally:
            db.close()
    before = counts()
    cid = json.loads((run_dir / 'state.json').read_text())['case_id']
    sent = sqlite3.connect(db_path).execute("SELECT message_id FROM messages WHERE case_id=? AND direction='outbound' AND status='sent' LIMIT 1", (cid,)).fetchone()[0]
    # Simulate an interruption: a send that was started (and actually applied)
    # plus a movement approval that was started but never reached the domain.
    sender = sqlite3.connect(db_path).execute('SELECT approved_by FROM messages WHERE message_id=?', (sent,)).fetchone()[0]
    with open(run_dir / 'lifecycle.jsonl', 'a', encoding='utf-8') as stream:
        # The send happened (exact actor matches): reconciled as applied, not repeated.
        stream.write(json.dumps({'key': 'draft:INTERRUPTED', 'status': 'started', 'kind': 'reviewer', 'target_id': sent, 'decision_point': 'draft',
                                 'commands': [{'action': 'send_email', 'target_id': sent, 'authorizer': sender}]}) + '\n')
        # Wrong actor: a sent message is NOT proof this reviewer sent it -> unresolved.
        stream.write(json.dumps({'key': 'draft:OTHER-ACTOR', 'status': 'started', 'kind': 'reviewer', 'target_id': sent, 'decision_point': 'draft',
                                 'commands': [{'action': 'send_email', 'target_id': sent, 'authorizer': 'someone-else'}]}) + '\n')
        stream.write(json.dumps({'key': 'movement:NEVER:proposed', 'status': 'started', 'kind': 'reviewer', 'target_id': 'R-NONE',
                                 'commands': [{'action': 'approve_movement', 'target_id': 'R-NONE', 'authorizer': sender}]}) + '\n')
    manifest = json.loads((run_dir / 'manifest.json').read_text(encoding='utf-8'))
    resumed = experiments.Manager(Registry(manifest['registry_path'])).execute(manifest, resume=True)
    assert counts() == before, (before, counts())
    recon = {r['key']: r['result'] for r in resumed['lifecycle']['reconciliation']}
    assert recon == {'draft:INTERRUPTED': 'recovered', 'draft:OTHER-ACTOR': 'unresolved', 'movement:NEVER:proposed': 'unresolved'}, recon
    assert set(resumed['lifecycle']['unresolved_recovery']) == {'draft:OTHER-ACTOR', 'movement:NEVER:proposed'}
    assert resumed['metrics']['agentic']['outcome']['terminal'] == 'resolved'


# --- 8. persistent results and exports -------------------------------------------------

def test_8_persistent_results_and_exports(monkeypatch, tmp_path):
    result, run_dir, log, _ = _agentic_run(monkeypatch, tmp_path, 'agentic:missing-goggles:cooperative')
    registry = Registry(tmp_path / 'ai_configuration.db')
    outcomes = agentic_store.rows(registry, 'agentic_run_outcomes')
    assert len(outcomes) == 1 and outcomes[0]['terminal'] == 'resolved' and outcomes[0]['evidence_class'] == 'stand_in'
    assert agentic_store.rows(registry, 'agentic_reviewer_decisions')
    assert agentic_store.rows(registry, 'agentic_exchanges')
    assert agentic_store.rows(registry, 'agentic_tool_interactions')
    roles = agentic_store.role_findings(registry)
    assert any(r['role'] == 'resolution_planning' for r in roles)
    with pytest.raises(sqlite3.DatabaseError):
        with registry.connect() as db:
            db.execute("UPDATE agentic_run_outcomes SET terminal='resolved'")
    for table in ('agentic_run_outcomes', 'agentic_reviewer_decisions', 'agentic_exchanges', 'agentic_tool_interactions', 'agentic_role_decisions', 'role_findings'):
        text = agentic_store.csv_export(registry, table)
        assert text.count('\n') >= 2, table
    assert outcomes[0]['configuration_id'].startswith('CFG-') and outcomes[0]['reviewer_id'].startswith('REV-')
    assert outcomes[0]['scenario_set_version'] == agentic_scenarios.SCENARIO_SET_VERSION
    decisions = agentic_store.rows(registry, 'agentic_role_decisions')
    assert all(d['rubric_version'] for d in decisions) and {d['role'] for d in decisions} >= {'independent_validation'}


# --- 9. sustained campaign across configurations ---------------------------------------

def test_9_sustained_campaign_across_configurations(monkeypatch, tmp_path):
    import time
    from test_rc8_worker_gateway import GatewayStandIn, _copy_database
    from capstone_rag import config
    from capstone_rag.store import Store
    monkeypatch.setenv('CAPSTONE_LAB_EVIDENCE_CLASS', 'stand_in')
    gateway = GatewayStandIn(real_embeddings=True, models=(SMALL, LARGE, REVIEWER))
    try:
        # Reuse the worker test environment set-up (data copy, Gateway client config).
        from test_rc8_worker_gateway import _worker_run
        _env(monkeypatch, tmp_path, gateway)
        registry = Registry(tmp_path / 'campaign.db')
        source = tmp_path / 'live-campaign.db';_copy_database(config.DB_PATH, source)
        live = Store(str(source))
        spec = {'name': 'Agentic Testing', 'profile_ids': ['gateway-default'], 'gateway_models': [SMALL, LARGE],
                'scenario_ids': ['agentic:power-fault:cooperative', 'agentic:burning-smell:cooperative'], 'repetitions': 1, 'concurrency': 2,
                'approval_mode': 'agentic_reviewer', 'reviewer_model': REVIEWER, 'failure_limit': 10, 'token_limit': 512,
                'gateway_head_ids': ['head-192-0-2-3'], 'timeout_seconds': 300}
        plan = experiments.preview(registry, spec)
        assert plan['planned_runs'] == 4
        assert all(p['routes']['lab_reviewer']['model'] == REVIEWER for p in plan['profiles'])
        manager = experiments.Manager(registry)
        exp = manager.launch(plan, live)
        manager.threads[exp['id']].join(900)
        rows = manager.rows(exp['id'])
        journal = (Path(exp['directory']) / 'campaign.jsonl')
        state = manager.registry.get('experiments', exp['id'])
        assert len(rows) == 4 and all(r['status'] in ('completed', 'failed') for r in rows), (
            [(r['status'], r.get('error')) for r in rows], state.get('status'), state.get('termination_reason'),
            journal.read_text(encoding='utf-8')[-3000:] if journal.exists() else sorted(p.name for p in Path(exp['directory']).iterdir()))
        outcomes = agentic_store.rows(registry, 'agentic_run_outcomes', exp['id'])
        assert len({o['configuration_id'] for o in outcomes}) == 2
        assert {o['configuration'].split(' · ')[0] for o in outcomes} == {SMALL, LARGE}
        assert {o['template'] for o in outcomes} == {'power-fault', 'burning-smell'}
        live.close()
    finally:
        gateway.close()


def _env(monkeypatch, tmp_path, gateway):
    """Same isolated environment _worker_run builds, without running a trial."""
    import shutil
    from capstone_rag import config
    data_root = tmp_path / 'data';data_root.mkdir()
    for name in ('qms', 'erp'):
        if (Path(config.DATA_DIR) / name).exists():
            shutil.copytree(Path(config.DATA_DIR) / name, data_root / name)
    if Path(config.COMPATIBILITY_PATH).exists():
        shutil.copy2(config.COMPATIBILITY_PATH, data_root / Path(config.COMPATIBILITY_PATH).name)
    (data_root / 'index').mkdir();shutil.copy2(config.INDEX_PATH, data_root / 'index' / 'qms_index.npz')
    token = tmp_path / 'research.token';token.write_text('fixture-research-token', encoding='utf-8')
    client = tmp_path / 'research-client.json'
    client.write_text(json.dumps({'configuration_version': 1, 'contract': 'shared.inference/1', 'service_id': 'shared-inference',
                                  'url': gateway.url, 'application_id': 'capstone-research', 'token_file': str(token)}), encoding='utf-8')
    monkeypatch.setenv('CAPSTONE_DATA_ROOT', str(data_root));monkeypatch.setenv('CAPSTONE_INFERENCE_MODE', 'gateway')
    monkeypatch.setenv('CAPSTONE_GATEWAY_CLIENT_CONFIG', str(client));monkeypatch.setenv('CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG', str(client))
    monkeypatch.setattr(config, 'DATA_DIR', str(data_root));monkeypatch.setattr(config, 'INDEX_PATH', str(data_root / 'index' / 'qms_index.npz'))
