"""rc.27 operational ticketing build: the C1 role profile, its output caps, the pinned policy on every pass,
the progress read model, and "record reply only". Model calls are stubbed; the real-model rehearsal runs on the
lab laptop (see docs/RELEASE_NOTES_6.0.0-rc.27.md)."""
import json
import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from capstone_rag import ai_settings, config, inference, operational_profile, progress, role_contracts, store as store_mod
from capstone_rag import agent as agent_mod
from capstone_rag.webapp import create_app

REP = "rep.test"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv('CAPSTONE_AI_SETTINGS', str(tmp_path / 'ai.db'))
    return ai_settings.Registry(str(tmp_path / 'ai.db')) if 'path' in ai_settings.Registry.__init__.__code__.co_varnames else ai_settings.Registry()


def test_profile_routes_caps_and_policy(registry):
    snap = operational_profile.snapshot(registry)
    routes = snap['routes']
    for role in ai_settings.BASE_ROLES:
        assert routes[role]['model'] == operational_profile.ROLE_MODELS[role]
        assert routes[role]['expected_digest'] == operational_profile.EXPECTED_DIGESTS[routes[role]['model']]
        assert routes[role]['options']['num_ctx'] == 8192
    assert 'lab_reviewer' not in routes
    # rc.28: the digest exactly as the Gateway reports it (bare hex); 'sha256:' matched no head in the field.
    assert all(len(d) == 64 and ':' not in d for d in operational_profile.EXPECTED_DIGESTS.values())
    assert routes['ticket_interpretation']['model'] == 'qwen3.5:2b' and routes['customer_communication']['model'] == 'qwen3:14b'
    assert snap['output_caps']['erp_identity_advisory'] == 360 and snap['output_caps']['interpret_request'] == 320
    policy = snap['operational_policy']
    assert policy['method_id'] == 'method-C1' and policy['topology_id'] == 'C' and policy['treatment_id'] == '1'
    plan = policy['execution_policy']
    assert plan['parallel_initial_evidence'] is False and plan['reflection'] is False and plan['debate'] is False
    assert plan['force_tree'] is False and plan['supervisor_checkpoints'] is False and plan['peer_exchange'] is False
    assert len(policy['method_definition_hash']) == 64 and len(snap['configuration_hash']) == 64


def test_caps_apply_only_inside_a_profile_session(registry, monkeypatch):
    assert operational_profile.cap('erp_identity_advisory', 120) == 120          # no session: code default
    seen = {}
    from capstone_rag import generation
    monkeypatch.setattr(generation, 'compose_structured', lambda *a, **k: seen.setdefault('max', k['max_tokens']) and {})
    token = inference._SESSION.set({'snapshot': operational_profile.snapshot(registry)})
    try:
        assert operational_profile.cap('grounded_writer', 600) == 1200
        role_contracts.call('erp_identity_advisory', 'x')
        assert seen['max'] == 360
    finally:
        inference._SESSION.reset(token)


@pytest.fixture
def st(tmp_path):
    if not os.path.exists(config.DB_PATH):
        pytest.skip("run scripts/import_synthetic.py first")
    path = str(tmp_path / "ops.db")
    shutil.copy(config.DB_PATH, path)
    from db_reset import clear_copy
    clear_copy(path, ("shipment_events", "sim_shipments", "rmas", "erp_calls", "approvals", "recommendations",
                      "reasoning_attempts", "retrieved_sources", "tool_calls", "checklist_items", "qa_notifications",
                      "conversations", "messages", "cases", "improvement_proposals", "audit_events"))
    s = store_mod.Store(path)
    yield s
    s.close()


def _case(st, cid='CASE-T27', status='New'):
    st.insert('cases', {'case_id': cid, 'subject': 'Power', 'status': status, 'priority': 'Medium', 'created_at': store_mod.now(),
                        'updated_at': store_mod.now(), 'ai_state': 'queued'})
    return cid


def test_pinned_policy_used_on_initial_and_resume_and_closed_case_skipped(st, registry, monkeypatch):
    from capstone_rag import caseflow, orchestration
    cid = _case(st)
    snap = operational_profile.snapshot(registry)
    st.insert('inference_snapshots', dict(snapshot_id='CFG-t', scope_key='case:' + cid, profile_id=snap['id'],
              configuration_hash=snap['configuration_hash'], payload=ai_settings.canonical(snap), at=time.time()))
    used = []

    class Fake:
        def __init__(self, flow, policy=None): used.append(policy)
        def run(self, case_id, resume=False): used.append(('resume', resume)); return {'ok': True}
    monkeypatch.setattr(orchestration, 'CaseOrchestrator', Fake)
    flow = caseflow.CaseFlow(st, agent_mod.Agent(prefer_llm=False))
    flow.pre_investigate(cid)
    flow.pre_investigate(cid, resume=True)
    assert used[0]['methodology_id'] == 'method-C1' and used[0]['_trigger'] == 'initial'
    assert used[2]['_trigger'] == 'human_resume' and used[3] == ('resume', True)
    # a case created before the profile keeps the default orchestrator
    old = _case(st, 'CASE-OLD')
    used.clear(); flow.pre_investigate(old)
    assert used[0] is None or used[0] == {} or 'methodology_id' not in (used[0] or {})
    st.update('cases', 'case_id', cid, {'status': 'Closed'})
    used.clear()
    assert flow.pre_investigate(cid) == {'skipped': 'case_closed'} and used == []


def test_progress_read_model(st):
    cid = _case(st)
    rows = [('read_message', 'completed', 'Message read', {}), ('screen_adverse_event', 'completed', 'No concern found', {}),
            ('specialist', 'started', 'ERP research started', {'role': 'erp_research'}),
            ('planning', 'completed', 'Tree-of-thought entry: single path', {'entry': {'enter': False}})]
    for cat, status, summary, ev in rows:
        st.insert('activity_events', {'at': time.time(), 'investigation_id': 'INV-1', 'case_id': cid, 'category': cat,
                                      'status': status, 'summary': summary, 'evidence': json.dumps(ev)})
    p = progress.case_progress(st, cid)
    by = {s['key']: s for s in p['stages']}
    assert by['intake']['status'] == 'completed' and by['erp']['status'] == 'running'
    assert by['planning']['outcome'].startswith('Single path') and by['qms']['status'] == 'not_started'
    assert by['waiting']['status'] == 'queued' and p['cursor'] > 0
    assert progress.case_progress(st, cid, after=p['cursor'])['new_events'] == []


def test_record_reply_only_does_not_queue(st):
    from capstone_rag import operations_workflow
    cid = _case(st, status='AI Reviewed')
    st.insert('messages', {'message_id': cid + '-OUT-1', 'case_id': cid, 'created_at': store_mod.now(), 'direction': 'outbound',
                           'channel': 'email', 'author': 'rep', 'subject': 's', 'body': 'b', 'status': 'sent'})
    out = operations_workflow.add_customer_reply(st, cid, 'It works now, thanks.', REP, cid + '-OUT-1', continue_investigation=False)
    case = st.case(cid)
    assert out['continue_investigation'] is False and case['ai_state'] == 'completed' and case['status'] == 'Human Review Needed'
