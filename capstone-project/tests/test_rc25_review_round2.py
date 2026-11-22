"""rc.25 independent review, round 2: one test group per review item.

1. Unverified-disclosure normalization (surname / title, first name in a
   sign-off, written dates, reformatted phone numbers, email local part)
   without blocking public manual text.
2. A revision QUESTION or a list of alternatives is not a disclosure; an
   assertion of the account's recorded revision is.
3. Reconciliation: blocked / escalated are final; an ``inference.*`` row that
   carries a task correlation id belongs to that task (probe rows).
4. The run lock never spans the live-database import; the import is bounded
   and leaves the outbox pending; coordinator recovery never waits on it.
5. Coordinator restart imports the remaining mirror first, then closes, with
   a unique attempt id per restart.
6. Reviewer output budget vs the objection schema (declared profile change).
7. A revision replacement needs its own validation record: a package that
   froze another draft fails closed, and the send gate never falls back to the
   predecessor chain for an explicit revision.
"""
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from test_operations import IVAN, REP, SERIAL_A, flow, make_case, ops, shared_agent, source_db, st  # noqa: F401
from test_rc25_revision_and_review import GRANITE_DRAFT, QWEN_DRAFT, REVIEWER, S1, S2, _draft, _goggles, _writer

from capstone_rag import agentic_reviewer, experiments, generation, independent_review
from capstone_rag import lab_reconciliation as lr
from capstone_rag import objection_grounding, response_policy, review, revision
from capstone_rag.store import Store

MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']


def _impostor(flow, st):
    """RUN-5ae4a77d shape: sender Noah Thomas, real owner Ivan Nowak, unverified."""
    cid = make_case(flow, 'Protective goggles missing', 'My name is Noah Thomas. Serial 98148469. The protective goggles were missing from my box.')
    st.update('cases', 'case_id', cid, {'identity_verified': 0})
    owner = st.one('SELECT customer_name,date_of_birth,phone,mobile_phone,email FROM customers WHERE account_id=?', (IVAN,))
    return cid, owner


def _disclosures(st, cid, text):
    return [f['fact'] for f in response_policy.validate_case(st, cid, text)['failures'] if f.get('claim_kind') == 'unverified_disclosure']


# --- 1. disclosure normalization ---------------------------------------------------

def test_surname_with_title_and_first_name_signoff_are_disclosures(flow, st):
    cid, owner = _impostor(flow, st)
    first, last = owner['customer_name'].split()[0], owner['customer_name'].split()[-1]
    assert _disclosures(st, cid, f'Dear Mr {last},\n\nThank you for your message.')
    assert _disclosures(st, cid, f'Dear Mr. {last},\n\nThank you for your message.')
    assert _disclosures(st, cid, f'Thank you for your message, {first}. Please check the compartment.')
    # Whole words only: a longer word containing the name is not the name.
    assert not _disclosures(st, cid, f'Hello Noah, the {first}hoe-style box is fine. Please check the compartment.')


def test_reformatted_date_of_birth_is_a_disclosure(flow, st):
    cid, owner = _impostor(flow, st)
    dob = owner['date_of_birth']
    assert dob, 'fixture owner needs a date of birth'
    y, m, d = dob.split('-')
    for written in (dob, f'{int(d)} {MONTHS[int(m) - 1]} {y}', f'{MONTHS[int(m) - 1]} {int(d)}, {y}', f'{d}/{m}/{y}', f'{int(d)} {MONTHS[int(m) - 1][:3]} {y}'):
        assert _disclosures(st, cid, f'Hello there, the account holder was born on {written}.'), written


def test_reformatted_phone_and_email_local_part_are_disclosures(flow, st):
    cid, owner = _impostor(flow, st)
    phone = owner['phone'] or owner['mobile_phone']
    assert phone and owner['email'], 'fixture owner needs a phone and an email'
    digits = re.sub(r'\D', '', phone)
    spaced = ' '.join(digits[i:i + 3] for i in range(0, len(digits), 3))
    dashed = '-'.join(digits[i:i + 2] for i in range(0, len(digits), 2))
    assert _disclosures(st, cid, f'Hello there, we will call you on {spaced} tomorrow.')
    assert _disclosures(st, cid, f'Hello there, we will call you on ({dashed}) tomorrow.')
    local = owner['email'].split('@')[0]
    assert len(local) >= 4
    assert _disclosures(st, cid, f'Hello there, we will write to {local} at your usual provider.')


def test_public_manual_text_is_not_over_blocked(flow, st):
    cid, _ = _impostor(flow, st)
    public = ('Hello Noah,\n\nThank you for your message. The P4800 manual covers revision A and revision B units; revision B units '
              'have the accessory compartment under the moulded insert. Please check there and tell us which items you received. '
              'Our support line is open Monday to Friday, 8:00-17:00, and serial numbers are printed on the carton label. '
              'To look at an account we need two details that match our records.')
    assert not _disclosures(st, cid, public)


# --- 2. revision questions vs assertions ---------------------------------------------

def test_revision_question_and_alternatives_are_not_disclosures(flow, st):
    cid, _ = _impostor(flow, st)
    assert st.case(cid)['revision'].upper() == 'A'
    for allowed in ('Hello there, could you tell us whether your unit is revision A or revision B?',
                    'Hello there, is your unit revision A? The label on the base shows it.',
                    'Hello there, please tell us which hardware revision your unit is: revision A, revision B or revision C.',
                    'Hello there, the manual for revision A and revision B units describes the compartment.'):
        assert not _disclosures(st, cid, allowed), allowed


def test_asserting_the_recorded_revision_is_a_disclosure(flow, st):
    cid, _ = _impostor(flow, st)
    for leak in ('Hello there, I can see your P4800 revision A on the account.',
                 'Hello there. Your unit is revision A, so please check the compartment.',
                 'Hello there. Our records show it as rev. A.'):
        facts = _disclosures(st, cid, leak)
        assert any('revision' in f for f in facts), leak


# --- 3. reconciliation terminal states and task keys (probe rows) ---------------------

def _probe_row(t, **k):
    return {'event_id': t + str(len(k)) + str(k.get('task_id')), 'timestamp': k.pop('ts'), 'event_type': t, 'workflow_id': 'RUN-1', **k}


def test_blocked_escalated_and_task_scoped_inference_rows_are_not_outstanding():
    rows = [
        _probe_row('task.started', ts='1', agent_id='qms_research', task_id='T1', correlation_id='CORR-T1'),
        _probe_row('task.blocked', ts='2', agent_id='qms_research', task_id='T1', correlation_id='CORR-T1'),
        _probe_row('task.started', ts='3', agent_id='planner', task_id='T2', correlation_id='CORR-T2'),
        _probe_row('task.escalated', ts='4', agent_id='planner', task_id='T2', correlation_id='CORR-T2'),
        # execution engine: waiting_for_inference -> 'inference.queued' under the TASK correlation id
        _probe_row('task.started', ts='5', agent_id='erp_research', task_id='T3', correlation_id='CORR-T3'),
        _probe_row('inference.queued', ts='6', agent_id='erp_research', task_id='T3', correlation_id='CORR-T3'),
        _probe_row('task.completed', ts='7', agent_id='erp_research', task_id='T3', correlation_id='CORR-T3'),
    ]
    assert lr.outstanding(rows, 'RUN-1') == []


def test_a_real_model_call_is_still_keyed_by_its_call_id():
    rows = [_probe_row('task.started', ts='1', agent_id='erp_research', task_id='T4', correlation_id='CORR-T4'),
            _probe_row('inference.started', ts='2', agent_id='erp_research', task_id='T4', correlation_id='CALL-abc'),
            _probe_row('task.completed', ts='3', agent_id='erp_research', task_id='T4', correlation_id='CORR-T4')]
    items = lr.outstanding(rows, 'RUN-1')
    assert [i['last']['event_type'] for i in items] == ['inference.started']
    assert lr._key(rows[1]) == ('inference', 'CALL-abc')
    assert lr._key(rows[0]) == ('task', 'erp_research', 'T4')
    # A task-less inference row keeps its own correlation key.
    assert lr._key({'event_type': 'inference.started', 'correlation_id': 'REQ-1'}) == ('inference', 'REQ-1')


# --- 4. lock scope, bounded import, non-blocking recovery -----------------------------

def _trial(tmp_path, name, rows):
    rd = tmp_path / 'EXP-R2' / name
    rd.mkdir(parents=True)
    (rd / 'arena-mirror.imported-1.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    return {'id': name, 'experiment_id': 'EXP-R2', 'directory': str(rd)}, rd


def _open_call(trial, call='CALL-open', n=0):
    return {'event_id': f'ARENA-{trial}-{call}-{n}', 'schema_version': '1.0', 'timestamp': f'2026-11-19T10:0{n}:00+00:00',
            'event_type': 'inference.started', 'case_id': 'CASE-1', 'workflow_id': trial, 'task_id': 'draft:X', 'agent_id': 'lab_reviewer',
            'sender_agent_id': None, 'receiver_agent_id': None, 'correlation_id': call, 'logical_home_node': 'coordinator',
            'execution_node': 'coordinator', 'tool_node': None, 'inference_node': 'n1', 'model_id': 'qwen3:4b', 'endpoint_id': 'n1',
            'task_status': 'running', 'summary': 'EXPERIMENT x', 'evidence_refs': '[]',
            'detail': json.dumps({'request_id': call, 'route_role': 'lab_reviewer'})}


EXIT = {'kind': 'timeout_kill', 'reason': 'worker stopped', 'confirmed_at': 1795000287.0}


def test_busy_live_database_is_bounded_and_never_holds_the_run_lock(tmp_path, monkeypatch):
    live = tmp_path / 'live.db'
    Store(str(live)).close()
    run, rd = _trial(tmp_path, 'RUN-A', [_open_call('RUN-A')])
    monkeypatch.setattr(lr, 'IMPORT_BUSY_SECONDS', 1)
    holder = sqlite3.connect(live, timeout=1, isolation_level=None)
    holder.execute('BEGIN EXCLUSIVE')                       # the coordinator's writer holds the live database
    outcome = {}

    def finish():
        started = time.monotonic()
        try:
            lr.reconcile(run, 'ATTEMPT-1', EXIT, str(live))
            outcome['error'] = None
        except sqlite3.OperationalError as exc:
            outcome['error'] = str(exc)
        outcome['seconds'] = time.monotonic() - started
    worker = threading.Thread(target=finish)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not (rd / lr.OUTBOX).exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert (rd / lr.OUTBOX).exists()
        # While the import waits on the database, this run's lock is free ...
        lock = lr._lock_for(rd)
        assert lock.acquire(timeout=.5)
        lock.release()
        # ... and another trial's finish is not blocked by it.
        other, other_rd = _trial(tmp_path, 'RUN-B', [_open_call('RUN-B', 'CALL-b')])
        assert lr._lock_for(other_rd).acquire(timeout=.5)
        lr._lock_for(other_rd).release()
    finally:
        worker.join(30)
        holder.execute('ROLLBACK');holder.close()
    assert not worker.is_alive()
    assert outcome['error'] and 'locked' in outcome['error'].lower()
    assert outcome['seconds'] < 15                          # bounded, not telemetry's ~5-minute retry
    assert (rd / lr.OUTBOX).exists() and not (rd / lr.DONE).exists()   # still pending
    assert lr.flush(rd, str(live)) == 1                    # imported once the database is free
    assert lr.flush(rd, str(live)) == 0


def test_coordinator_recovery_does_not_wait_for_the_outbox_flush(tmp_path, monkeypatch):
    from capstone_rag.ai_settings import Registry
    release, entered = threading.Event(), threading.Event()

    def slow_flush(root, live_for):
        entered.set();release.wait(10)
        return 0
    monkeypatch.setattr(lr, 'flush_pending', slow_flush)
    manager = experiments.Manager(Registry(tmp_path / 'settings.db'))
    started = time.monotonic()
    try:
        manager.recover()
        assert time.monotonic() - started < 3
        assert entered.wait(3)                               # the flush did run, in the background
    finally:
        release.set()


# --- 5. restart: mirror first, then closures, unique attempt ---------------------------

def test_restart_imports_the_remaining_mirror_before_closing(tmp_path, monkeypatch):
    live = tmp_path / 'live.db'
    Store(str(live)).close()
    run, rd = _trial(tmp_path, 'RUN-C', [])
    (rd / 'arena-mirror.imported-1.jsonl').unlink()
    # Rows the worker wrote but nobody imported yet (it was killed first).
    (rd / 'arena-mirror.jsonl').write_text(json.dumps(_open_call('RUN-C', 'CALL-c', 0)) + '\n', encoding='utf-8')
    run['source_database'] = str(live)
    attempts = []
    real = lr.reconcile
    monkeypatch.setattr(lr, 'reconcile', lambda r, attempt, *a, **k: attempts.append(attempt) or real(r, attempt, *a, **k))
    experiments.Manager._reconcile_restart(None, run)
    with sqlite3.connect(live) as db:
        order = [r[0] for r in db.execute('SELECT event_type FROM arena_events WHERE workflow_id=? ORDER BY sequence', ('RUN-C',))]
    assert order == ['inference.started', 'inference.interrupted']
    experiments.Manager._reconcile_restart(None, run)
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    assert all(a.startswith('restart-') for a in attempts)
    with sqlite3.connect(live) as db:                        # the second restart closes nothing twice
        assert db.execute("SELECT COUNT(*) FROM arena_events WHERE event_type='inference.interrupted'").fetchone()[0] == 1


# --- 6. reviewer budget vs objections ---------------------------------------------------

def test_objection_schema_is_bounded():
    schema = objection_grounding.OBJECTION_SCHEMA
    assert schema['maxItems'] == 3
    props = schema['items']['properties']
    assert (props['draft_span']['maxLength'], props['rule']['maxLength'], props['correction']['maxLength']) == (160, 120, 200)
    assert props['source_ids']['maxItems'] == 2 and props['source_ids']['items']['maxLength'] == 64


def test_worst_case_reviewer_reply_fits_the_declared_budget_with_margin():
    budget = agentic_reviewer.REVIEWER_MAX_TOKENS
    assert budget == 2048 and agentic_reviewer.REVIEWER_POLICY_VERSION == 'reviewer-policy-2026-11-19.2'
    # The pessimistic ratio stays below the measured minimum (3.43 chars/token, 390 c922 replies).
    assert agentic_reviewer.REVIEWER_WORST_CHARS_PER_TOKEN < 3.43
    ids = ['source:QMS-DOC-0111#S2-symptom-confirmation-xxxxxxxx'] * 8
    for kind in agentic_reviewer.OPTIONS:
        assert agentic_reviewer.worst_case_tokens(kind, ids) <= .9 * budget, kind
    # Strict single-call worst case of the draft schema would not fit 1,024 even capped.
    assert agentic_reviewer.worst_case_tokens('draft', ids) > 1024
    # Review-session final call: measured rc.24 maximum plus everything rc.25 adds.
    assert agentic_reviewer.session_envelope_tokens() <= .9 * budget


def test_reviewer_calls_request_the_declared_budget(monkeypatch):
    sent = []
    monkeypatch.setattr(generation, 'compose_structured', lambda system, ctx, schema, **k: sent.append(k) or {'decision': 'approve'})
    from capstone_rag import inference
    monkeypatch.setattr(inference, 'model', lambda: 'qwen3:4b')
    view = {'decision_point': 'draft', 'options': ['approve'], 'evidence': [], 'withheld': []}
    agentic_reviewer.decide(view)
    assert sent[-1]['max_tokens'] == agentic_reviewer.REVIEWER_MAX_TOKENS


# --- 7. revision replacement fails closed ---------------------------------------------

def _revise_with_mismatch(flow, st, monkeypatch):
    cid = _goggles(flow, st);mid = _draft(st, cid, GRANITE_DRAFT)
    objections = [{'kind': 'add', 'draft_span': '', 'rule': 'QMS-DOC-0111 symptom confirmation', 'source_ids': [S2],
                   'correction': 'Ask the customer which items they have received and the part name printed on the carton label.'}]
    monkeypatch.setattr(revision, '_write', _writer(QWEN_DRAFT))
    real_freeze = independent_review.freeze

    def freeze_other(store, case_id, workflow_id, state, cycle):
        row, package = real_freeze(store, case_id, workflow_id, state, cycle)
        return row, {**package, 'draft': {**(package.get('draft') or {}), 'message_id': mid}}   # froze the predecessor
    monkeypatch.setattr(independent_review, 'freeze', freeze_other)
    out = revision.revise(st, cid, mid, objections=objections, authorizer=REVIEWER, idempotency_key='r2-7', presented_source_ids=[S1, S2])
    return cid, mid, out


def test_validation_of_a_different_draft_fails_closed(flow, st, monkeypatch):
    cid, mid, out = _revise_with_mismatch(flow, st, monkeypatch)
    assert not out['ok'] and out['outcome'] == 'revised_unvalidated'
    assert out['validation'] == 'target_mismatch' and mid in out['reason']
    meta = json.loads(st.one('SELECT metadata FROM messages WHERE message_id=?', (out['message_id'],))['metadata'])
    assert meta['grounded'] is False and meta['verification']['disposition'] == 'target_mismatch'
    assert st.one('SELECT * FROM artifact_validation_records WHERE artifact_id=?', (out['message_id'],)) is None


def test_send_gate_requires_the_replacements_own_validation(flow, st, monkeypatch):
    cid, mid, out = _revise_with_mismatch(flow, st, monkeypatch)
    new = out['message_id']
    # Even with a passing record on the predecessor, the revision may not borrow it.
    prior = st.one('SELECT * FROM artifact_validation_records WHERE artifact_id=? ORDER BY at DESC LIMIT 1', (mid,))
    if prior is None or prior['disposition'] != 'pass':
        template = st.one('SELECT * FROM artifact_validation_records LIMIT 1')
        if template is not None:
            st.insert('artifact_validation_records', {**template, 'record_id': 'AVR-r2-prior', 'artifact_id': mid, 'disposition': 'pass'})
    body = st.one('SELECT body FROM messages WHERE message_id=?', (new,))['body']
    result = review._approve_and_send(st, cid, new, body, REP)
    assert not result['ok'] and 'validation of its own' in result['reason']
    assert st.one('SELECT status FROM messages WHERE message_id=?', (new,))['status'] == 'draft'
    assert st.one("SELECT * FROM audit_events WHERE case_id=? AND event_type='send_blocked' ORDER BY id DESC LIMIT 1", (cid,)) is not None
