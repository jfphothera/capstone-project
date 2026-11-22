"""Phase 2 coordinator delegation, plan enforcement, and UI-boundary tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_defects import flow, shared_agent, source_db, st  # noqa: E402,F401

from capstone_rag.collaboration import Coordinator
from capstone_rag.orchestration import CaseOrchestrator
from capstone_rag.webapp.app import create_app


def test_delegated_task_contract_is_idempotent_and_traceable(flow, st):
    cid = flow.intake('Power question', 'My P4800 will not power on.')
    coordinator = Coordinator(st, 'WF-TEST')
    arguments = dict(case_id=cid, issuer='erp_research', recipient='qms_research',
        purpose='Find authorized guidance at the established identity tier',
        based_on_version=st.case(cid)['case_version'], required_inputs=['request'],
        evidence_ids=['MESSAGE-1'], permitted_tools=['public_manual_search'])
    first = coordinator.delegate(**arguments)
    second = coordinator.delegate(**arguments)
    assert first['task_id'] == second['task_id']
    coordinator.start(first['task_id'], 'qms_research')
    coordinator.complete(first['task_id'], 'qms_research',
                         {'sources': ['QMS-DOC-0203#S1-scope-and-prerequisites']})
    trace = coordinator.trace(cid)
    assert len(trace['tasks']) == 1
    assert [event['kind'] for event in trace['events']] == [
        'assigned', 'started', 'completed']


def test_identity_to_qms_handoff_preserves_missing_evidence_and_general_guidance(flow, st):
    cid = flow.intake('P4800 power',
                      'My P4800 will not power on. The serial label is partly unreadable: 9814.')
    result = flow.pre_investigate(cid)
    assert result['paused_for_review']
    tasks = st.query('SELECT * FROM delegated_tasks WHERE case_id=? ORDER BY created_at', (cid,))
    handoff = next(task for task in tasks if task['issuer'] == 'erp_research'
                   and task['recipient'] == 'qms_research')
    qms_finding = json.loads(st.one(
        "SELECT payload FROM specialist_findings WHERE case_id=? AND role='qms_research'",
        (cid,))['payload'])
    assert handoff['status'] == 'completed'
    assert qms_finding['sources']
    assert not qms_finding['exact_procedures']
    assert qms_finding['evidence_contract']['canonical_device_context'][
        'revision_authority']['usable_for_troubleshooting'] is False


def test_qms_requests_revision_evidence_without_fabricating_it(flow, st):
    device = st.one("SELECT * FROM devices WHERE revision_status='inferred' AND model='P4800' LIMIT 1")
    customer = st.customer(device['account_id'])
    cid = flow.intake('P4800 no power',
        f"My name is {customer['customer_name']}. Serial {device['serial_number']}. My P4800 will not power on.")
    flow.pre_investigate(cid)
    request = st.one(
        "SELECT * FROM delegated_tasks WHERE case_id=? AND issuer='qms_research' "
        "AND recipient='erp_research' ORDER BY created_at DESC LIMIT 1", (cid,))
    qms_finding = json.loads(st.one(
        "SELECT payload FROM specialist_findings WHERE case_id=? AND role='qms_research'",
        (cid,))['payload'])
    assert request and request['status'] == 'awaiting_evidence'
    assert request['recommended_next_action'] == 'customer_clarification'
    assert not qms_finding['exact_procedures']
    assert 'trusted hardware revision' in ' '.join(qms_finding['missing_evidence'])


def test_multi_turn_identity_evidence_triggers_targeted_qms_update(flow, st):
    cid = flow.intake('P4800 power follow-up',
                      'My P4800 will not power on. The serial label is partly unreadable.')
    first = flow.pre_investigate(cid)
    first_qms = json.loads(st.one(
        "SELECT payload FROM specialist_findings WHERE case_id=? AND role='qms_research' "
        "AND command_id LIKE ?", (cid, first['workflow_id'] + ':%'))['payload'])
    assert not first_qms['exact_procedures']
    st.insert('messages', {'message_id': 'PHASE2-FOLLOWUP', 'case_id': cid,
        'created_at': '2099-01-01', 'direction': 'inbound', 'channel': 'email',
        'body': 'Correction: my name is Ivan Nowak. Serial 98148469. The P4800 still will not power on.',
        'status': 'received', 'parent_message_id': st.messages(cid)[0]['message_id']})
    second = flow.pre_investigate(cid)
    second_qms = json.loads(st.one(
        "SELECT payload FROM specialist_findings WHERE case_id=? AND role='qms_research' "
        "AND command_id LIKE ?", (cid, second['workflow_id'] + ':%'))['payload'])
    assert second_qms['exact_procedures'] == ['PROC-P4800-PWR-A']
    assert set(first_qms['sources']) != set(second_qms['sources'])
    assert st.one(
        "SELECT 1 FROM delegated_tasks WHERE workflow_id=? AND issuer='erp_research' "
        "AND recipient='qms_research'", (second['workflow_id'],))


def test_conflicting_specialist_context_is_recorded_and_escalated(flow, st):
    cid = flow.intake('Conflict fixture', 'My P4800 will not power on.')
    orchestrator = CaseOrchestrator(flow)
    state = {'cid': cid, 'workflow_id': 'WF-CONFLICT',
             'body': 'My P4800 will not power on.',
             'based_on_version': st.case(cid)['case_version'],
             'erp': {'device': {'model': 'P4800', 'revision': 'A'}},
             'qms': {'sources': ['SOURCE-1'], 'evidence_contract': {
                 'canonical_device_context': {'model': 'P3410', 'revision': 'A'}}}}
    result = orchestrator._plan(state)
    assert result['selected_action'] == 'human_review'
    assert result['authorized_transition'] == 'escalate_conflict'
    assert st.one('SELECT * FROM specialist_conflicts WHERE case_id=?', (cid,))


def test_planner_selected_part_transition_controls_operations(flow, st):
    cid = flow.intake('Missing goggles',
        'My name is Ivan Nowak. Serial 98148469. Protective goggles were missing from the box.')
    result = flow.pre_investigate(cid)
    transition = st.one('SELECT * FROM plan_transitions WHERE case_id=?', (cid,))
    assert result['proposal'] and transition
    assert transition['selected_action'] == 'part_shipment'
    assert transition['permitted_command'] == 'propose_part_movement'
    assert transition['execution_status'] == 'deferred_for_human_approval'
    assert not st.query('SELECT * FROM sim_shipments WHERE case_id=?', (cid,))


def test_specialist_failure_becomes_controlled_visible_finding(flow, st):
    cid = flow.intake('Failure fixture', 'My device needs help.')
    result = CaseOrchestrator(flow)._finding(
        {'cid': cid, 'workflow_id': 'WF-FAIL', 'message_id': 'MSG-FAIL'},
        'qms_research', lambda state: (_ for _ in ()).throw(TimeoutError('model timeout')))
    task = st.one("SELECT * FROM delegated_tasks WHERE case_id=? AND recipient='qms_research'", (cid,))
    assert result['controlled_failure'] and result['recommended_next_action'] == 'human_review'
    assert task['status'] == 'retryable'


def test_phone_information_record_skips_qms_and_customer_composition(flow, st):
    cid = flow.intake('Missed call callback request',
                      'Automated call-back request logged from inbound IVR session. No conversation transcript is available.',
                      channel='phone_ai')
    flow.pre_investigate(cid)
    assert not st.one(
        "SELECT 1 FROM delegated_tasks WHERE case_id=? AND recipient='qms_research'", (cid,))
    assert not st.one(
        "SELECT 1 FROM messages WHERE case_id=? AND direction='outbound'", (cid,))
    assert st.one('SELECT 1 FROM contact_tasks WHERE case_id=?', (cid,))


def test_testing_lab_is_open_but_operational_configuration_remains_retired(st, shared_agent):
    app = create_app(store=st, agent=shared_agent, prefer_llm=False,
                     start_workers=False, developer_token='phase2-secret')
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        assert client.get('/chat').status_code == 200
        lab=client.get('/pressure-testing')
        assert lab.status_code == 200 and 'Phothera Testing Lab' in lab.text
        assert client.get('/api/ai/configuration').status_code == 409
        headers = {'X-Capstone-Developer-Token': 'phase2-secret'}
        assert '🧪🥐' in lab.text
        response=client.get('/api/ai/configuration', headers=headers)
        assert response.status_code == 409
        assert response.json()=={'detail':'not available: Capstone runs through the Inference Gateway only'}
        assert client.get('/api/experiments/scenarios').status_code == 200
        representative = client.get('/').text
        customer = client.get('/chat').text
        for text in (representative, customer):
            assert 'Test Tube Pastries' not in text
            assert 'AI Configuration' not in text
            assert 'Experiments / Pressure Test' not in text
