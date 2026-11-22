"""Access/use/disclosure and operational effects of the controlled QMS."""
from dataclasses import replace
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_defects import st, source_db, shared_agent, bot, flow
from capstone_rag import activity, config, generation, qms, qms_policy, support_workflow
from capstone_rag.index import VectorIndex
from capstone_rag.webapp.app import create_app
from fastapi.testclient import TestClient


def doc(identifier):
    return next(c for c in qms.load_chunks() if c.document_id == identifier)


def test_internal_readability_does_not_grant_customer_disclosure():
    document = doc('QMS-DOC-0303')
    internal = qms_policy.authorize(document, operation='internal_workflow', recipient='internal')
    assert internal.allowed
    assert not internal.disclosure_allowed
    customer = qms_policy.authorize(document, operation='customer_answer', recipient='customer')
    assert not customer.allowed
    assert any('disclosure' in reason for reason in customer.denial_reasons)


def test_general_manual_authority_is_distinct_from_revision_procedure():
    context = {'model': 'P4800', 'revision': None, 'revision_confirmed': False,
               'compatibility': qms.load_compatibility()}
    assert qms_policy.authorize(doc('QMS-DOC-0203'), context=context).allowed
    assert not qms_policy.authorize(doc('QMS-DOC-0101'), context=context).allowed
    assert not qms_policy.authorize(doc('QMS-DOC-0203'), operation='revision_troubleshooting', context=context).allowed


@pytest.mark.parametrize('context', [
    {'model': 'P2400', 'revision': 'A', 'revision_confirmed': True},
    {'model': 'P4800', 'revision': 'D', 'revision_confirmed': True},
    {'model': 'P4800', 'revision': 'A', 'revision_confirmed': True, 'safety_hold': True},
])
def test_wrong_model_revision_or_safety_denies_public_manual(context):
    assert not qms_policy.authorize(doc('QMS-DOC-0203'), context=context).allowed


def test_service_and_audit_only_cannot_authorize_agent_customer_steps():
    for identifier in ('QMS-DOC-0116', 'QMS-DOC-0103'):
        result = qms_policy.authorize(doc(identifier), context={
            'model': 'Series-M', 'revision': 'A', 'revision_confirmed': True,
            'compatibility': qms.load_compatibility()})
        assert not result.allowed
        assert not result.disclosure_allowed


def test_internal_rma_readability_does_not_approve_execution():
    document = doc('QMS-DOC-0304')
    assert qms_policy.authorize(document, operation='internal_workflow', recipient='internal').allowed
    assert not qms_policy.authorize(document, operation='execute_movement', recipient='internal',
                                     context={'identity_verified': True}).allowed
    conflict = replace(document, required_approvals=[])
    result = qms_policy.authorize(conflict, operation='internal_workflow', recipient='internal')
    assert not result.allowed
    assert any('conflicts' in reason for reason in result.denial_reasons)


def test_human_approval_does_not_expand_document_workflow_scope():
    decision = qms_policy.authorize(doc('QMS-DOC-0303'), operation='execute_movement',
        recipient='internal', context={'identity_verified': True, 'human_approvals': ['inventory_movement']})
    assert not decision.allowed
    assert any('outside this document' in reason for reason in decision.denial_reasons)


def test_public_model_manual_without_private_account_verification(bot, st):
    session = bot.new_session()
    reply = bot.respond(session, 'My P2400 turns on but the lamp stays dark. Could I have the user manual guidance?')
    assert not session.verified
    assert not session.device
    assert not session.handed_off
    assert reply.sources
    assert {s['document_id'] for s in reply.sources} == {'QMS-DOC-0201'}
    assert 'customer_name' not in reply.text
    assert 'on your account' not in reply.text
    assert 'PROC-' not in reply.text
    assert 'scope and prerequisites' not in reply.text.lower()
    assert len(reply.text.split()) < 100


def test_serial_device_manual_link_without_account_disclosure(bot, st):
    session = bot.new_session()
    reply = bot.respond(session, 'Serial 72377DL1. The lamp stays dark while the unit powers on.')
    assert session.device['serial_number'] == '72377-DL1'
    assert not session.verified
    assert reply.sources
    assert 'Wei Murphy' not in reply.text
    rows = st.query('SELECT * FROM activity_events WHERE conversation_id=? AND category=?',
                    (session.conversation_id, 'manual_retrieval'))
    evidence = json.loads(next(row for row in rows if row['status'] == 'completed')['evidence'])
    assert evidence['model'] == 'P2400'
    assert evidence['selected_procedure'] == 'PROC-MANUAL-P2400'


def test_internal_complaint_and_qa_procedures_support_actual_notification(flow, st):
    cid = flow.intake('Possible harm', 'My name is Ivan Nowak. Serial 98148469. I have painful blistering after use.')
    flow.pre_investigate(cid)
    assert st.one('SELECT * FROM qa_notifications WHERE case_id=?', (cid,))
    events = st.query('SELECT * FROM activity_events WHERE case_id=? AND category=?', (cid, 'operational_qms'))
    evidence = json.loads(events[0]['evidence'])
    ids = {d['document_id'] for d in evidence['documents'] if d['authorization']['allowed']}
    assert {'QMS-DOC-0302', 'QMS-DOC-0303'} <= ids
    assert 'propose_qa_notification' in evidence['available_actions']
    assert all(not d['authorization']['disclosure_allowed'] for d in evidence['documents'])


def test_missing_controlled_movement_support_blocks_actual_proposal(flow, st, monkeypatch):
    index = flow.agent.toolbox.index
    keep = [i for i,c in enumerate(index.chunks) if c.document_id != 'QMS-DOC-0304']
    altered = VectorIndex([index.chunks[i] for i in keep], index.matrix[keep])
    monkeypatch.setattr(flow.agent.toolbox, 'index', altered)
    cid = flow.intake('Missing goggles', 'My name is Ivan Nowak. Serial 98148469. Goggles were missing from the box.')
    flow.pre_investigate(cid)
    assert not st.query('SELECT * FROM rmas WHERE case_id=?', (cid,))
    assert any(e['event_type'] == 'qms_rule_conflict' for e in st.audit_trail(cid))


def test_rma_procedure_supports_proposal_but_approval_remains_required(flow, st):
    cid = flow.intake('Missing goggles', 'My name is Ivan Nowak. Serial 98148469. Goggles were missing from the box.')
    flow.pre_investigate(cid)
    rma = st.one('SELECT * FROM rmas WHERE case_id=?', (cid,))
    assert rma and rma['status'] == 'proposed'
    assert not st.query('SELECT * FROM sim_shipments WHERE case_id=?', (cid,))
    events = st.query('SELECT * FROM activity_events WHERE case_id=? AND category=?', (cid, 'operational_qms'))
    assert any('QMS-DOC-0304' in event['evidence'] for event in events)


def test_restricted_citation_copy_and_fallback_disclosure_denied():
    internal = doc('QMS-DOC-0304')
    result = qms_policy.disclosure_check(internal.text + '\n' + internal.procedure_id, [])
    assert not result['passed']
    assert any(f['document_id'] == internal.document_id for f in result['failures'])


def test_native_qwen_tool_selection_drives_real_manual_tool(bot, st, monkeypatch):
    monkeypatch.setattr(bot.agent, 'prefer_llm', True)
    monkeypatch.setattr(generation, 'llm_available', lambda: True)
    def local_response(path, payload, timeout):
        if payload.get('tools'):
            return {'message': {'tool_calls': [{'function': {'name': 'search_customer_manual', 'arguments': {}}}]}}
        return {'message': {'content': 'We are given a task; output only the reply.'}}
    monkeypatch.setattr(generation, '_post', local_response)
    session = bot.new_session()
    reply = bot.respond(session, 'My P2400 lamp stays dark while the display is on.')
    assert reply.sources
    calls = st.query('SELECT * FROM activity_events WHERE conversation_id=? AND category=?', (session.conversation_id, 'workflow_decision'))
    evidence = json.loads(calls[0]['evidence'])
    assert evidence['mode'] == 'qwen3_tool_selection'
    assert evidence['action'] == 'search_customer_manual'
    assert st.one('SELECT * FROM activity_events WHERE conversation_id=? AND category=?', (session.conversation_id, 'manual_retrieval'))


def test_inventory_exposes_independent_metadata_axes(st, shared_agent):
    with TestClient(create_app(store=st, agent=shared_agent, start_workers=False, prefer_llm=False)) as client:
        inventory = client.get('/api/trace/qms/inventory').json()
    assert inventory['document_count'] == 31
    assert inventory['chunk_count'] > 92
    internal = next(d for d in inventory['documents'] if d['document_id'] == 'QMS-DOC-0303')
    assert internal['document_type'] == 'complaint_qa_procedure'
    assert internal['internal_workflow_authorization']['allowed']
    assert not internal['customer_disclosure_authorization']['allowed']
