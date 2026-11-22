"""rc.25 Phase 3-4, 7 and Gateway-adapter fixtures (M1/M2, T1/T2, U1, G1/G5 client side).

Stage proofs run the real worker against the stand-in Gateway; role outputs
(supervisor, peer, reflection, critic, judge) are deterministic doubles keyed
by their schema. None of this is model evidence.
"""
import json
import sqlite3

import pytest

from capstone_rag import agentic_methodologies as am
from capstone_rag import gateway_capabilities, generation, model_evidence, role_contracts

SMALL, REVIEWER = 'qwen3.5:4b', 'qwen3:4b'


# --- Phase 0 / M1 / M2: versioned registry, honest labels, coverage ---------------

def test_registry_v1_is_preserved_and_v2_is_distinct():
    v1 = {c['id']: c['configuration_hash'] for c in am.candidates(registry=am.REGISTRY_V1)}
    v2 = {c['id']: c['configuration_hash'] for c in am.candidates()}
    assert len(v1) == len(v2) == 32 and not set(v1.values()) & set(v2.values())
    # v1 hashes are the ones every pre-rc.25 experiment recorded (EXP-f253 manifest: method-A1).
    assert v1['method-A1'] == am.candidates(['A'], ['1'], registry=am.REGISTRY_V1)[0]['configuration_hash']
    rows = {t['id']: t for t in am.catalogue()['topologies']}
    assert rows['E']['implementation_qualified'] is False and 'sequential' in rows['E']['implementation_note']
    treatments = {t['id']: t for t in am.catalogue()['treatments']}
    assert treatments['4']['retrieval_alias_of'] == 'hybrid'


def test_advisory_policy_matches_u1_and_never_disables():
    cat = am.catalogue()
    normal = {t['id'] for t in cat['topologies'] if t['advisory']['appearance'] == 'normal'}
    gray = {t['id'] for t in cat['topologies'] if t['advisory']['appearance'] == 'gray'}
    assert normal == set('ABCG') and gray == set('DEFH')
    assert {t['id'] for t in cat['treatments'] if t['advisory']['appearance'] == 'normal'} == {'1', '2'}
    assert all('disabled' not in t and 'hidden' not in t for t in cat['topologies'] + cat['treatments'])
    # Gray cells are still built, hashed and executable.
    assert all(c['execution_ready'] for c in am.candidates(list('DEFH'), ['3', '4']))


def test_stage_coverage_reports_unsupported_combinations():
    b4 = am.candidates(['B'], ['4'])[0]['stage_coverage']
    assert {r['stage']: r['status'] for r in b4} == {'plan_reflection': 'not_supported_by_adapter', 'dual_view_retrieval': 'alias'}
    assert any(r['stage'] == 'parallel_specialists' for r in am.candidates(['E'], ['1'])[0]['stage_coverage'])
    assert am.candidates(['C'], ['3'])[0]['stage_coverage'][0]['status'] == 'executes_within_stage_budget'


def test_ui_renders_gray_as_enabled_checkbox_with_text_badge():
    from lab_js import lab_js_text
    text = lab_js_text()
    assert 'function advisoryChoice' in text and 'discouraged (advisory)' in text
    body = text[text.index('function advisoryChoice'):text.index('function modelChoice')]
    assert 'disabled' not in body                                   # never disabled
    assert '★' not in text and "'fastest'" not in text              # unsupported universal labels removed


# --- U1: scoped model evidence -------------------------------------------------------

def test_model_presentation_is_role_scoped_and_gray_wins():
    assert model_evidence.presentation('qwen3.5:4b')['appearance'] == 'green'
    assert model_evidence.presentation('gemma3:4b')['appearance'] == 'gray'
    assert model_evidence.presentation('granite4:3b')['appearance'] == 'gray'
    reviewer_as_main = model_evidence.presentation('qwen3:4b', 'main')
    assert reviewer_as_main['appearance'] == 'normal' and 'tested only as lab_reviewer' in reviewer_as_main['badges']
    assert model_evidence.presentation('qwen3:4b', 'lab_reviewer')['appearance'] == 'green'
    assert model_evidence.presentation('mistral:7b')['appearance'] == 'normal'         # untested is not green
    assert all(model_evidence.presentation(m)['selectable'] for m in ('gemma3:4b', 'granite4:3b', 'mistral:7b'))
    assert 'tag only' in model_evidence.presentation('qwen3.5:4b', gateway_digest='sha256:abc')['badges'][-1]


# --- T1: strategy resolved before launch ---------------------------------------------------

def test_gemma_is_never_sent_native_tools_by_default():
    with pytest.raises(model_evidence.ToolStrategyIncompatible):
        model_evidence.resolve_tool_strategy('gemma3:4b', None)
    with pytest.raises(model_evidence.ToolStrategyIncompatible):
        model_evidence.resolve_tool_strategy('gemma3:4b', {'strategy': 'native'})
    mediated = model_evidence.resolve_tool_strategy('gemma3:4b', {'strategy': 'mediated'})
    assert mediated['strategy'] == 'mediated' and mediated['basis'] == 'explicit user selection'
    assert model_evidence.resolve_tool_strategy('qwen3.5:4b', None, ['completion', 'tools'])['strategy'] == 'native'
    unknown = model_evidence.resolve_tool_strategy('mistral:7b', None)
    assert unknown['strategy'] == 'native' and 'unknown' in unknown['basis']


def test_preview_refuses_native_default_for_gemma(monkeypatch, tmp_path):
    from capstone_rag import experiments
    from capstone_rag.ai_settings import Registry
    monkeypatch.setenv('CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG', str(tmp_path / 'research.json'))
    registry = Registry(tmp_path / 'settings.db')
    spec = {'profile_ids': ['gateway-default'], 'gateway_models': ['gemma3:4b'], 'scenario_ids': ['agentic:power-fault:cooperative'],
            'repetitions': 1, 'approval_mode': 'agentic_reviewer', 'reviewer_model': REVIEWER, 'level': 'system',
            'methodology_matrix': {'topology_ids': ['A'], 'treatment_ids': ['1']}}
    with pytest.raises(ValueError, match='explicit mediated or delegated'):
        experiments.preview(registry, spec)
    planned = experiments.preview(registry, {**spec, 'tool_strategies': {'gemma3:4b': {'strategy': 'mediated'}}})
    route = planned['profiles'][0]['routes']['investigation']
    assert route['tool_strategy'] == 'mediated' and route['tool_strategy_resolution']['basis'] == 'explicit user selection'


# --- T2: full schema, arguments, categories ---------------------------------------------------

def test_extra_native_arguments_are_contract_invalid_and_not_executed():
    tool = {'type': 'function', 'function': {'name': 'search_customer_manual', 'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}
    # Qwen 1.7B returned extra arguments on all 101 inspected native calls in EXP-f253.
    record = generation.tool_call_contract({'name': 'search_customer_manual', 'arguments': {'serial': '98148469', 'model': 'P4800'}}, [tool], 'native')
    assert record['contract_valid'] is False and record['extra_arguments'] == ['model', 'serial']
    assert record['model_arguments_executed'] is False and record['argument_source'] == 'trusted_case_facts'
    assert generation.tool_call_contract({'name': 'search_customer_manual', 'arguments': {}}, [tool], 'native')['contract_valid'] is True


def test_schema_errors_cover_type_enum_and_extra_fields_not_length():
    schema = role_contracts.CONTRACTS['supervise_finding']['schema']
    assert generation.schema_errors({'continue': True, 'concern': 'x', 'requested_specialist': 'none'}, schema) == []
    errors = generation.schema_errors({'continue': 'yes', 'concern': 'x' * 300, 'requested_specialist': 'qms researcher', 'extra': 1}, schema)
    assert len(errors) == 3                                    # a long prose field is not an error (rc.25 T2)
    assert generation.schema_errors({'continue': True, 'concern': 'x' * 2000, 'requested_specialist': 'none'}, schema) == []


def test_truncated_role_output_is_a_finite_categorized_failure(monkeypatch):
    from capstone_rag import inference
    monkeypatch.setattr(inference, 'current', lambda: {'snapshot': {'routes': {}}})
    monkeypatch.setattr(inference, 'model', lambda: 'granite4:3b')
    monkeypatch.setattr(generation, '_post', lambda *a, **k: {'message': {'content': '{"relevance": "QMS-DOC-0111 covers the miss'}, 'done_reason': 'length'})
    value = role_contracts.call('qms_coverage_summary', {'request': 'x'})
    assert value['role_output_error']['category'] == 'truncated' and value['role_output_error']['finish_reason'] == 'length'
    monkeypatch.setattr(generation, '_post', lambda *a, **k: {'message': {'content': 'not json'}, 'done_reason': 'stop'})
    assert role_contracts.call('peer_challenge', {'finding': {}})['role_output_error']['category'] == 'parse'
    with pytest.raises(generation.RoleOutputError):
        role_contracts.call('validate_draft', {'draft': 'x'})              # non-advisory contracts still fail closed


# --- G1 / G5 client side -----------------------------------------------------------------------

class _FakeGateway:
    """Gateway 0.4.5 (default) or 0.4.6 (new_api) as seen through the vendored 0.4.1 client."""
    def __init__(self, *, new_api=False):
        self.new_api = new_api
        self.calls = {'policy': 0, 'policy_light': 0, 'models': 0, 'availability': 0, 'fleet': 0, 'health': 0}

    def request(self, path, body=None, timeout=120):
        if path == '/health':
            self.calls['health'] += 1
            return {'ok': True, 'release': '0.4.6' if self.new_api else None,
                    'features': ['policy_without_availability', 'fleet_snapshot'] if self.new_api else []}
        if path.startswith('/v1/policy?availability=0'):
            self.calls['policy_light'] += 1
            return {'policy': {}, 'availability_included': False} if self.new_api else {'apps': 1, 'policy': {}}
        if path.startswith('/v1/fleet'):
            self.calls['fleet'] += 1
            return {'snapshot': {'snapshot_id': 'FLEET-1'}, 'models': [
                {'model': 'gemma3:4b', 'authorized': True, 'installed': {'state': 'installed'}, 'qualified': {'state': 'qualified', 'capabilities': ['structured_output'], 'max_context': 8192},
                 'available': {'status': {'/api/generate': 'model_suspended', '/api/chat': 'model_suspended'}, 'any': False},
                 'resources': {'head-192-0-2-2': {'inventory': {'installed': True, 'digest': '68ceab7e'}, 'qualification': {'operations': ['completion']}}}},
                {'model': 'secret:7b', 'authorized': False, 'status': 'not_permitted'}]}
        raise AssertionError(path)

    def policy(self):
        self.calls['policy'] += 1;return {'apps': 1}

    def models(self):
        self.calls['models'] += 1
        names = [f'model-{i}' for i in range(36)]
        return {'resources': [{'resource_id': 'head-192-0-2-3', 'models': names,
                               'model_details': {n: {'qualified_operations': ['completion'], 'qualified_capabilities': ['tools']} for n in names}}]}

    def availability(self, model, operation, num_ctx=None, capabilities=()):
        self.calls['availability'] += 1;return {'status': 'available'}


def test_launch_checks_scale_with_selection_not_catalogue():
    gateway = _FakeGateway()
    states = gateway_capabilities.model_states(gateway, ['model-1', 'model-2'])
    assert gateway.calls['availability'] == 2 and set(states) == {'model-1', 'model-2'}     # not 36 x 2
    assert gateway_capabilities.policy(gateway)['source'].startswith('policy (Gateway 0.4.5') and gateway.calls['policy'] == 0  # probe reused


def test_suspended_model_is_installed_but_unavailable_on_gateway_046():
    gateway = _FakeGateway(new_api=True)
    states = gateway_capabilities.model_states(gateway, ['gemma3:4b', 'secret:7b'])
    assert states['gemma3:4b']['installed'] is True and states['gemma3:4b']['availability'] == 'model_suspended'
    assert 'temporarily unavailable' in states['gemma3:4b']['reason'] and gateway.calls['models'] == 0 and gateway.calls['availability'] == 0
    assert states['secret:7b']['availability'] == 'not_permitted' and 'heads' not in states['secret:7b']
    assert gateway_capabilities.policy(gateway)['source'] == 'policy(availability=False)' and gateway.calls['policy'] == 0
    rows = gateway_capabilities.installed_models_from_fleet(gateway, ['gemma3:4b'])
    assert rows[0]['name'] == 'gemma3:4b' and rows[0]['availability']['status'] == 'model_suspended' and rows[0]['digest'] == '68ceab7e'


def test_absent_from_045_listing_is_not_reported_not_installed():
    state = gateway_capabilities.model_states(_FakeGateway(), ['gemma3:4b'])['gemma3:4b']
    assert state['installed'] is None and state['availability'] == 'installed_unknown'


def test_campaign_snapshot_is_shared_bounded_and_never_caches_failures(monkeypatch, tmp_path):
    from capstone_rag import config
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    fetches = []
    fetch = lambda: (fetches.append(1), {'reachable': True, 'installed_models': [{'name': 'm'}]})[1]
    args = dict(workload='research', endpoint='gateway', models=['m'])
    first = gateway_capabilities.campaign_snapshot(fetch, now=1000.0, **args)
    second = gateway_capabilities.campaign_snapshot(fetch, now=1100.0, **args)      # another worker, same campaign selection
    third = gateway_capabilities.campaign_snapshot(fetch, now=1400.0, **args)       # TTL 300 s elapsed
    assert len(fetches) == 2 and second['snapshot']['cached'] and not third['snapshot']['cached']
    gateway_capabilities.invalidate()
    failing = lambda: {'reachable': False, 'last_error': 'refused'}
    gateway_capabilities.campaign_snapshot(failing, now=2000.0, **args)
    assert not list((tmp_path / 'gateway-snapshots').glob('*.json'))


# --- M1/M2 stage proofs on the real worker (stand-in Gateway, role doubles) ------------------------

SUPERVISOR = frozenset({'continue', 'concern', 'requested_specialist'})
REFLECTION = frozenset({'proceed', 'material_defect', 'weakness', 'recommended_check'})
CRITIC = frozenset({'issues', 'strongest_objection', 'draft_span', 'source_ids'})
JUDGE = frozenset({'proceed_to_validator', 'reason'})
PEER_REPLY = frozenset({'answer', 'resolved', 'missing_evidence'})


def _run(monkeypatch, tmp_path, topology, treatment, overrides=None, scenario='qms-technical'):
    from test_rc8_worker_gateway import GatewayStandIn, _worker_run
    from capstone_rag import experiments
    tmp_path.mkdir(parents=True, exist_ok=True)
    gateway = GatewayStandIn(real_embeddings=True, models=(SMALL, REVIEWER))
    gateway.role_overrides.update(overrides or {})
    try:
        result, live, run_dir = _worker_run(monkeypatch, tmp_path, gateway, approval_mode='automated', scenario=experiments.scenario(scenario),
                                            architecture=am.candidates([topology], [treatment])[0])
        live.close()
    finally:
        gateway.close()
    db = sqlite3.connect(run_dir / 'operations.db');db.row_factory = sqlite3.Row
    stages = [json.loads(r['evidence']) for r in db.execute("SELECT evidence FROM activity_events WHERE category='stage_execution' ORDER BY event_id")]
    return result, db, stages


def _stage(stages, name):
    return [s for s in stages if s['stage'] == name]


def test_supervisor_stop_has_a_bounded_visible_effect(monkeypatch, tmp_path):
    result, db, stages = _run(monkeypatch, tmp_path, 'D', '1', {SUPERVISOR: {'continue': False, 'concern': 'Revision evidence is not established.', 'requested_specialist': 'none'}})
    sup = _stage(stages, 'supervisor')
    assert sup and all(s['downstream_effect'] == 'surfaced_as_unresolved_concern' for s in sup), sup
    issues = [i for r in db.execute('SELECT issues FROM artifact_validation_records') for i in json.loads(r['issues'])]
    assert any(i['issue_category'] == 'supervisor_concern' and i['severity'] == 'advisory' for i in issues)


def test_supervisor_no_change_path(monkeypatch, tmp_path):
    result, db, stages = _run(monkeypatch, tmp_path, 'D', '1', {SUPERVISOR: {'continue': True, 'concern': '', 'requested_specialist': 'none'}})
    assert _stage(stages, 'supervisor') and all(s['downstream_effect'] == 'continue' for s in _stage(stages, 'supervisor'))


def test_hierarchical_delegation_executes_and_is_consumed(monkeypatch, tmp_path):
    result, db, stages = _run(monkeypatch, tmp_path, 'G', '1', {SUPERVISOR: {'continue': False, 'concern': 'Re-check applicable manual sections.', 'requested_specialist': 'qms_research'}})
    executed = [s for s in _stage(stages, 'delegation') if s['status'] == 'executed']
    debug = (_stage(stages, 'supervisor'), _stage(stages, 'delegation'), [dict(r) for r in db.execute('SELECT recipient,status,depth,purpose FROM delegated_tasks')], result.get('error'))
    assert executed, json.dumps(debug, default=str)[:4000]
    assert executed[0]['downstream_effect'] == 'reintegrated_into_requesting_finding'
    tasks = db.execute("SELECT status FROM delegated_tasks WHERE depth=2 AND purpose LIKE 'Supervisor-requested%'").fetchall()
    assert any(t['status'] == 'completed' for t in tasks)
    plans = [json.loads(r['payload']) for r in db.execute("SELECT payload FROM specialist_findings WHERE role='resolution_planning'")]
    assert any(d['status'] == 'completed' for p in plans for d in p.get('delegated_inputs') or [])
    assert len([t for t in tasks if t['status'] == 'completed']) <= 2                      # bounded


def test_reflection_block_needs_a_grounded_gap(monkeypatch, tmp_path):
    _, db, stages = _run(monkeypatch, tmp_path, 'A', '4', {REFLECTION: {'proceed': False, 'material_defect': False, 'weakness': 'Could be clearer.', 'recommended_check': ''}})
    assert [s['downstream_effect'] for s in _stage(stages, 'reflection')] == ['unsupported_block_not_enacted']
    _, db2, stages2 = _run(monkeypatch, tmp_path / 'ok', 'A', '4', {REFLECTION: {'proceed': True, 'material_defect': False, 'weakness': '', 'recommended_check': ''}})
    assert [s['downstream_effect'] for s in _stage(stages2, 'reflection')] == ['no_material_defect']


def test_critic_judge_objection_drives_a_bounded_revision_or_explicit_unresolved(monkeypatch, tmp_path):
    _, db, stages = _run(monkeypatch, tmp_path, 'H', '1', {
        CRITIC: {'issues': ['Asks to open the enclosure.'], 'strongest_objection': 'Remove any instruction to open the enclosure.', 'draft_span': '', 'source_ids': []},
        JUDGE: {'proceed_to_validator': False, 'reason': 'Critic objection upheld.'}})
    effects = [s['downstream_effect'] for s in _stage(stages, 'critic_judge')]
    assert effects and (effects[0] == 'revised' or effects[0].startswith('explicit_unresolved')), effects
    _, db2, stages2 = _run(monkeypatch, tmp_path / 'ok', 'H', '1', {JUDGE: {'proceed_to_validator': True, 'reason': 'fine'}})
    assert [s['downstream_effect'] for s in _stage(stages2, 'critic_judge')] == ['no_change_needed']


def test_peer_question_gets_lookup_or_recorded_unresolved(monkeypatch, tmp_path):
    _, db, stages = _run(monkeypatch, tmp_path, 'F', '1', {PEER_REPLY: {'answer': 'Not in my finding.', 'resolved': False, 'missing_evidence': 'power indicator section'}})
    effects = {s['role']: s['downstream_effect'] for s in _stage(stages, 'peer_exchange')}
    assert effects.get('qms_research') in ('evidence_found', 'recorded_unresolved', 'budget_exhausted')
    assert effects.get('resolution_planning') == 'recorded_unresolved'


PLANNER = frozenset({'preferred_action', 'plans'})


def test_configured_tree_invokes_the_model_planner(monkeypatch, tmp_path):
    _, db, stages = _run(monkeypatch, tmp_path, 'C', '3', {PLANNER: {'preferred_action': 'clarify',
                                                                    'plans': [{'action': 'clarify', 'rationale': 'Ask what the indicator shows.'}]}})
    planning = _stage(stages, 'planning')
    assert planning and planning[0]['outputs']['model_proposal_used'] is True, planning


def test_v1_and_v2_cells_never_share_a_configuration_identity():
    from capstone_rag.lab_evidence import configuration_identity
    profile = {'routes': {'investigation': {'model': SMALL, 'options': {}}, 'lab_reviewer': {'model': REVIEWER, 'policy_version': 'p'}}}
    v1 = configuration_identity(profile, am.candidates(['A'], ['1'], registry=am.REGISTRY_V1)[0])
    v2 = configuration_identity(profile, am.candidates(['A'], ['1'])[0])
    legacy = configuration_identity(profile, {**am.candidates(['A'], ['1'], registry=am.REGISTRY_V1)[0], 'registry_version': None})
    assert v1['configuration_id'] != v2['configuration_id'] and v1['configuration_id'] == legacy['configuration_id']


def test_legacy_auto_mode_never_sends_native_tools_to_gemma(monkeypatch):
    from capstone_rag import inference
    monkeypatch.setattr(inference, 'model', lambda: 'gemma3:4b')
    monkeypatch.setattr(inference, 'route_setting', lambda key, default=None: 'auto')
    monkeypatch.setattr(generation, '_native_tool_call', lambda *a, **k: (_ for _ in ()).throw(AssertionError('native tools sent to gemma')))
    monkeypatch.setattr(generation, '_post', lambda *a, **k: {'message': {'content': '{"name": "search_customer_manual"}'}})
    tools = [{'type': 'function', 'function': {'name': 'search_customer_manual', 'parameters': {'type': 'object', 'properties': {}}}}]
    call = generation.choose_support_tool('P4800 is dead', [], {}, tools)
    assert call['name'] == 'search_customer_manual' and call['_tool_record']['auto_resolution'].startswith('native skipped')


def test_budgets_are_raised_only_where_outputs_were_cut():
    raised = {name: (old, new) for name, (old, new, _) in role_contracts.BUDGET_EVIDENCE.items()}
    for name, (old, new) in raised.items():
        assert role_contracts.CONTRACTS[name]['max_tokens'] == new > old
    for unchanged, tokens in {'interpret_request': 160, 'erp_identity_advisory': 120, 'qms_search_query': 96, 'supervise_finding': 160,
                              'judge_debate': 160, 'validate_draft': 320}.items():
        assert role_contracts.CONTRACTS[unchanged]['max_tokens'] == tokens
    assert all('maxLength' not in json.dumps(c['schema']).replace('"items": {"type": "string", "maxLength": 300}', '')
               for n, c in role_contracts.CONTRACTS.items() if c.get('schema') and n != 'validate_draft')


def test_fleet_backed_health_is_not_kept_beyond_the_gateway_snapshot(monkeypatch, tmp_path):
    from capstone_rag import config
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    fetches = []
    fetch = lambda: (fetches.append(1), {'reachable': True, 'installed_models': [], 'inventory_scope': 'fleet_snapshot_selected_models'})[1]
    gateway_capabilities.campaign_snapshot(fetch, now=1000.0, workload='research', endpoint='gateway', models=['m'])
    gateway_capabilities.campaign_snapshot(fetch, now=1001.0, workload='research', endpoint='gateway', models=['m'])
    assert len(fetches) == 2 and not list((tmp_path / 'gateway-snapshots').glob('*.json'))
