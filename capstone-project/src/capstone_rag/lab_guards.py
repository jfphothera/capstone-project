"""Action-boundary preconditions for autonomous (simulated) reviewer commands (rc.25).

The simulated reviewer acts through the same application functions as a
person, but it is not a person with authority to attest facts. These checks
use only application-visible records (never scenario facts):

* identity attestation (C4): an autonomous reviewer cannot assert that
  verification happened. Its attestation is accepted only when the customer's
  OWN supplied details match the ERP record on two points (the exact check the
  application runs); the matched points are recorded. A named human's audited
  attestation is unaffected (``review._apply_consequence``);
* resolution (C5/C6): a case is resolved only when nothing is pending, no
  safety hold is active, and the latest customer message after the last sent
  reply visibly confirms the problem is solved without contradicting itself;
* safety (C5): a held safety report cannot be closed routinely; closing it
  unresolved requires the recorded QA determination and an escalation;
* approval binding (C3): a send is bound to the reviewed artifact hash and to
  the case state hash it was reviewed under.
"""
from __future__ import annotations

import json
import re

GUARD_VERSION = 'lab-guards-2026-11-19.1'
CONFIRM = re.compile(r"\b(works now|working now|powers? on now|it powers on|now powers on|arrived|received (?:them|it|the)|"
                     r"(?:problem|issue) is (?:resolved|solved|fixed)|that resolves it|all working|everything is complete)\b", re.I)
CONTRADICT = re.compile(r"\b(no lights|stays? (?:black|dark)|still (?:does not|doesn't|won't|will not|not)|not working|"
                        r"(?:is|seems) (?:completely )?dead|have not arrived|has not arrived|not yet|cannot hear|makes no sound|"
                        r"(?:have|has|had|did)(?: not|n't)(?: yet| still)? (?:receiv|arriv|got|come)\w*|(?:still )?(?:haven't|hasn't|didn't) (?:receiv|arriv|got)\w*|"
                        r"not (?:sure|certain)|(?:isn't|is not|wasn't|was not) (?:fixed|resolved|solved|working))", re.I)


def autonomous(actor):
    return str(actor or '').startswith(('lab-reviewer-agent:', 'synthetic-reviewer:', 'autonomous-sandbox-policy:'))


def customer_claim(store, cid):
    """Identity claim built only from the customer's own inbound messages;
    a later correction replaces the earlier value (same rule as the ERP step)."""
    from . import identity
    serials = [r['serial_number'] for r in store.query('SELECT serial_number FROM devices')]
    names = [r['customer_name'] for r in store.query('SELECT customer_name FROM customers')]
    claim = identity.Claim()
    for message in [m for m in store.messages(cid) if m['direction'] == 'inbound']:
        fresh = identity.Claim.from_text(message['body'] or '', known_serials=serials, known_names=names)
        signal = identity.correction_signal(message['body'] or '')
        for field in ('name', 'dob', 'serial', 'phone', 'email'):
            value = getattr(fresh, field)
            if value is not None or field in signal['fields']:
                setattr(claim, field, value)
        if fresh.serial or fresh.serial_issue or 'serial' in signal['fields']:
            claim.serial = fresh.serial;claim.serial_issue = fresh.serial_issue;claim.serial_input = fresh.serial_input
        if fresh.dob_issue:
            claim.dob = None;claim.dob_issue = fresh.dob_issue
    chat = store.one('SELECT state_json FROM conversations WHERE case_id=?', (cid,))
    if chat:
        import json as _json
        claim = identity.Claim(**_json.loads(chat['state_json']).get('claim', {}))
    return claim


def attestation(store, cid, actor):
    """(allowed, reason, verification) for an identity attestation command."""
    if not autonomous(actor):
        return True, 'named human attestation (audited)', None
    from . import identity
    case = store.case(cid) or {}
    result = identity.verify(store, customer_claim(store, cid), account_id=case.get('account_id'))
    if result.verified:
        return True, 'customer-supplied details match the ERP record: ' + ', '.join(result.points_matched), result
    return False, ('an autonomous reviewer cannot attest identity; the customer-supplied details do not match the ERP '
                   'record on two points (' + (result.reason or 'no match') + ')'), result


def confirmation_evidence(store, cid):
    """Visible customer confirmation: the customer's LATEST message, which must
    be a reply (not the opening ticket), confirms the fix and does not
    contradict or negate it."""
    rows = [m for m in store.messages(cid) if m['direction'] == 'inbound']
    if len(rows) < 2 and not (rows and rows[-1].get('parent_message_id')):
        return {'status': 'none', 'reason': 'the customer has not replied to any sent message'}
    body = rows[-1]['body'] or ''
    confirmed, contradicted = bool(CONFIRM.search(body)), bool(CONTRADICT.search(body))
    if confirmed and contradicted:
        return {'status': 'contradictory', 'message_id': rows[-1]['message_id'],
                'reason': 'the customer reply both confirms and contradicts the fix'}
    if confirmed:
        return {'status': 'confirmed', 'message_id': rows[-1]['message_id']}
    return {'status': 'not_confirmed', 'message_id': rows[-1]['message_id'], 'reason': 'latest customer message does not confirm resolution'}


def pending_work(store, cid):
    drafts = store.query("SELECT message_id FROM messages WHERE case_id=? AND direction='outbound' AND status='draft' AND channel!='qa_notification'", (cid,))
    moves = store.query("SELECT rma_id FROM rmas WHERE case_id=? AND status IN ('proposed','failed')", (cid,))
    qa = store.query("SELECT notification_id FROM qa_notifications WHERE case_id=? AND status='open'", (cid,))
    return [d['message_id'] for d in drafts] + [m['rma_id'] for m in moves] + [q['notification_id'] for q in qa]


def escalated_to_safety(store, cid):
    case = store.case(cid) or {}
    return case.get('assigned_to') in ('qa_team',) and bool(store.one(
        "SELECT 1 FROM audit_events WHERE case_id=? AND event_type='case_assigned' AND summary='assigned to qa_team'", (cid,)))


def closure(store, cid, actor, resolved):
    """(allowed, reason) for closing a case."""
    case = store.case(cid) or {}
    held = bool(case.get('safety_hold'))
    if resolved and held:
        return False, ('safety report under hold: a device hazard or adverse event is not released by routine resolution '
                       '(QMS-DOC-0303: lack of confirmed injury does not release a device hazard from review); '
                       'record the QA determination and escalate to qa_team')
    if not resolved and held and not (store.one("SELECT 1 FROM qa_notifications WHERE case_id=? AND status='determined'", (cid,))
                                      and escalated_to_safety(store, cid)):
        return False, 'a held safety report may be closed unresolved only after its QA determination and escalation to qa_team are recorded'
    if not autonomous(actor):
        return True, 'named human decision (audited)'
    if resolved:
        pending = pending_work(store, cid)
        if pending:
            return False, 'unfinished work blocks resolution: ' + ', '.join(pending[:6])
        evidence = confirmation_evidence(store, cid)
        if evidence['status'] != 'confirmed':
            return False, 'resolution needs a visible, consistent customer confirmation (' + evidence.get('reason', evidence['status']) + ')'
    return True, 'preconditions met'


def send_binding(store, cid, command):
    """(allowed, reason): the case state must match the state that was reviewed."""
    want = command.get('reviewed_case_state')
    if not want:
        return True, 'no case-state binding supplied (legacy command)'
    from .agentic_reviewer import case_state
    now = case_state(store, cid)
    return (True, 'case state unchanged') if now == want else (False, 'the case changed materially after the review; it must be reviewed again')
