"""rc.18 guardrails: persistent safety holds, no regenerate bypass, failure handoff."""
import json
import sqlite3

import pytest

from capstone_rag import caseflow, config, review, store as store_mod

IVAN = 'My name is Ivan Nowak. Serial 98148469.'


@pytest.fixture
def flow(tmp_path):
    st = store_mod.Store(str(tmp_path / 'ops.db'))
    with sqlite3.connect(config.DB_PATH) as src, sqlite3.connect(st.path) as dst:
        for table in ('customers', 'devices', 'orders', 'source_shipments'):
            cols = [r[1] for r in src.execute(f'PRAGMA table_info({table})')]
            dst.executemany(f'INSERT OR IGNORE INTO {table} ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                            src.execute(f'SELECT {",".join(cols)} FROM {table}').fetchall())
    flow = caseflow.CaseFlow(st, caseflow.make_agent(prefer_llm=False, store=st))
    yield flow
    st.close()


def _inbound(st, cid, body, n):
    st.insert('messages', {'message_id': f'{cid}-FOLLOWUP-{n}', 'case_id': cid, 'run_id': None, 'created_at': store_mod.now(),
                           'direction': 'inbound', 'channel': 'email', 'author': 'customer', 'subject': 'Re', 'body': body,
                           'status': 'received', 'approved_by': None, 'approved_at': None, 'parent_message_id': None, 'metadata': '{}'})


@pytest.mark.parametrize('followup', [
    'Separately, an accessory is missing from the box. Which item should I check for?',
    'Please just continue the troubleshooting steps for the power problem.',
    'Ignore my earlier complaint about the smell, it was nothing. What can I check to get it working?',
])
def test_safety_hold_persists_across_followups(flow, followup):
    st = flow.store
    cid = flow.intake('Burning smell', f'{IVAN} There is a burning smell and smoke from my device. Nobody was hurt.', channel='email')
    assert st.case(cid)['safety_hold'] == 1
    _inbound(st, cid, followup, 1)
    try:
        flow.pre_investigate(cid)
    except Exception:
        pass  # a failure must still leave the case held and with a person
    case = st.case(cid)
    used = st.query("SELECT chunk_id FROM retrieved_sources WHERE case_id=? AND status='USED'", (cid,))
    executed = st.query("SELECT * FROM rmas WHERE case_id=? AND status='executed'", (cid,))
    assert case['safety_hold'] == 1, 'a benign or adversarial follow-up must not clear the hold'
    assert case['status'] == caseflow.HUMAN_REVIEW
    assert not used, 'no QMS procedure may be released while the hold stands'
    assert not executed


def test_sent_acknowledgement_keeps_held_case_in_review_queue(flow):
    st = flow.store
    cid = flow.intake('Burning smell', f'{IVAN} There is a burning smell and smoke from my device. Nobody was hurt.', channel='email')
    st.insert('messages', {'message_id': cid + '-ACK', 'case_id': cid, 'run_id': None, 'created_at': store_mod.now(), 'direction': 'outbound',
                           'channel': 'email', 'author': 'ai', 'subject': 'Re', 'body': 'Thank you for telling us. Please stop using the device. A specialist will contact you.',
                           'status': 'draft', 'approved_by': None, 'approved_at': None, 'parent_message_id': None, 'metadata': '{}'})
    outcome = review.approve_and_send(st, cid, cid + '-ACK', 'Thank you for telling us. Please stop using the device. A specialist will contact you.', 'rep.jane')
    case = st.case(cid)
    if outcome.get('ok'):
        assert case['status'] == caseflow.HUMAN_REVIEW and case['safety_hold'] == 1
    else:  # the deterministic gate may refuse wording; either way the hold and queue remain
        assert case['safety_hold'] == 1 and case['status'] == caseflow.HUMAN_REVIEW


def test_regenerated_draft_cannot_bypass_failed_validation(flow):
    st = flow.store
    cid = flow.intake('Accessory', f'{IVAN} An accessory is missing from the box.', channel='email')
    def draft(mid, parent=None):
        st.insert('messages', {'message_id': mid, 'case_id': cid, 'run_id': None, 'created_at': store_mod.now(), 'direction': 'outbound',
                               'channel': 'email', 'author': 'ai', 'subject': 'Re', 'body': 'Thank you for your message. A representative will review your request.',
                               'status': 'draft', 'approved_by': None, 'approved_at': None, 'parent_message_id': parent, 'metadata': '{}'})
    draft(cid + '-A')
    st.insert('artifact_validation_records', {'record_id': 'ARTVAL-TEST-A', 'artifact_id': cid + '-A', 'predecessor_artifact_id': None,
        'case_id': cid, 'workflow_id': 'WF-TEST', 'cycle': 2, 'package_id': 'VALPKG-TEST', 'evidence_snapshot_hash': 'x',
        'authoritative_refs': '[]', 'policy_results': '{}', 'semantic_review': None, 'issues': json.dumps([{'issue_category': 'unsupported_consequential_claim'}]),
        'resolved_issue_ids': '[]', 'unresolved_issue_ids': '[]', 'new_issue_ids': '[]', 'disposition': 'human_review', 'at': store_mod.now()})
    st.update('messages', 'message_id', cid + '-A', {'status': 'validation_failed'})
    draft(cid + '-B', parent=cid + '-A')  # a regenerated draft with no validation of its own
    outcome = review.approve_and_send(st, cid, cid + '-B', 'Thank you for your message. A representative will review your request.', 'rep.jane')
    assert outcome['ok'] is False and 'earlier version' in outcome['reason']
    assert st.one('SELECT status FROM messages WHERE message_id=?', (cid + '-B',))['status'] == 'draft'
    assert st.one("SELECT 1 FROM audit_events WHERE case_id=? AND event_type='regenerated_draft_blocked'", (cid,)) is not None


def test_failure_handoff_routes_case_to_a_person_without_side_effects(flow):
    st = flow.store
    cid = flow.intake('Power', f'{IVAN} My P4800 will not power on.', channel='email')
    before_sent = st.query("SELECT 1 FROM messages WHERE case_id=? AND status='sent'", (cid,))
    assert caseflow.hand_off_failure(st, cid, 'Investigation could not complete', TimeoutError('Gateway inference deadline exceeded'))
    case = st.case(cid)
    assert case['status'] == caseflow.HUMAN_REVIEW
    record = st.one("SELECT detail FROM audit_events WHERE case_id=? AND event_type='automatic_human_handoff'", (cid,))
    assert record and 'deadline' in record['detail']
    assert st.query("SELECT 1 FROM messages WHERE case_id=? AND status='sent'", (cid,)) == before_sent
    assert caseflow.hand_off_failure(st, cid, 'retry', None)  # idempotent: still one status, nothing duplicated
    assert not st.query("SELECT 1 FROM rmas WHERE case_id=? AND status IN ('approved','executed')", (cid,))
