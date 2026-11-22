"""Simulated human reviewer for Agentic Testing: a separate, model-driven agent.

* Outside the ticket-system agent team: its own route (``lab_reviewer``), its
  own identity (``lab-reviewer-agent:<model>``) and its own prompt.
* Observes only what a representative legitimately sees in the workspace:
  case record, conversation, drafts, validation findings, proposals, QA
  notifications, checklist items awaiting a person, shipments, notes and the
  ERP record of the device on the case. It never receives scenario facts,
  expected outcomes or customer private facts.
* Acts only through the existing review actions (experiment_worker.apply_command
  -> review / erp_sim / caseflow functions). Guards stay authoritative; a
  blocked action is recorded as such.

Human controls audited (webapp routers cases, erp, quality, workflows,
coordination, analytics) and their coverage here:

=====================================  ==========================================
Workspace control                       Reviewer decision
=====================================  ==========================================
send / reject / regenerate draft        approve_send / reject / request_revision
approve / reject / retry movement       approve / reject / retry
QA adverse-event determination          determine (structured determination)
checklist verify_identity attestation   attest_identity / decline_identity
revision confirmation (RV-1)            confirm_revision / reject_revision
re-run investigation                    request_investigation
assign case                             escalate (to a named queue)
priority override                       escalate (Critical) when escalating safety
case note                               recorded with every escalation / request
close resolved / unresolved             resolve / close_unresolved
(wait for customer / external)          defer
NOT reviewer actions (external world):  carrier scans, service-centre receiving,
                                        customer replies -> environment/customer
NOT used: improvement-proposal and memory review, tags, contact-observation
review, exchange proposal, return-method selection (no scenario requires them
yet; listed so their absence is explicit).
=====================================  ==========================================
"""
from __future__ import annotations

import contextvars
import hashlib
import json
from contextlib import contextmanager

REVIEWER_POLICY_VERSION = 'reviewer-policy-2026-11-19.2'  # rc.25: grounded objections, no autonomous attestation, safety terminal
# Declared reviewer profile change (rc.25 review round 2): 1,024 -> 2,048
# output tokens, objections capped as well (objection_grounding). Capping alone
# could not fit 1,024: measured qwen3:4b reviewer output is 3.43-4.16
# characters per token (390 c922 replies); the single-call schema's strict
# worst case is ~1,410 tokens at a pessimistic 3.0, and the review session's
# final call (measured max 758 tokens under the rc.24 caps) grows by the
# raised rc.25 caps plus capped objections to an envelope of ~1,780 tokens.
# Ordinary replies (p95 604-634 tokens) are unaffected; a reply that still
# overruns is a ``truncated`` role-output error and the review fails closed.
REVIEWER_MAX_TOKENS = 2048
REVIEWER_OBSERVED_MAX_TOKENS = 758      # f253 review-session final calls, rc.24 schema, 0/3,262 truncated
RC25_PROSE_GROWTH_CHARS = 200 + 200 + 300   # rationale 400->600, instructions 400->600, synthesis 500->800
REVIEWER_WORST_CHARS_PER_TOKEN = 3.0
REVIEWER_SYSTEM = (
    'You are a customer-service representative at a medical-device company reviewing work prepared by AI agents. '
    'You are accountable for what is sent to customers and for every operational action. Decide using only the case '
    'information shown. Company rules: send a draft only if it is accurate, answers the customer, is grounded in the '
    'evidence shown and gives no device instructions unless identity is verified and the procedure applies; ask for a '
    'revision when a draft is fixable, reject it when it should not exist. Approve a movement only when the customer '
    'identity is verified and the movement is clearly warranted by the conversation and records; never approve a '
    'device replacement before the original unit has been received. Attest identity only when the customer has given '
    'two details that match the ERP record. Safety or injury reports need a QA determination and stay with safety '
    'review; conflicting controlled documents go to document control. Resolve a case only when the customer confirms '
    'the problem is solved; close it unresolved when it cannot be progressed; escalate when the decision is beyond '
    'your authority; defer when you are waiting for the customer or an external event. Be concise. '
    'When you request a revision, list each objection with the exact draft_span it concerns, the rule or the source id '
    '(from the sources shown) it relies on, and the intended correction. Do not ask to remove an instruction that an '
    'authorized source shown to you supports. If the draft has no material defect, say so (kind no_defect) instead of '
    'inventing criticism. You cannot verify identity yourself: identity counts as verified only when the application '
    'records two customer-supplied details matching the ERP record. A safety or injury report is never resolved by '
    'you: after the QA determination, escalate it to qa_team.')
QA_DETERMINATIONS = ['confirmed adverse event', 'suspected adverse event', 'device hazard without confirmed injury',
                     'not an adverse event', 'insufficient information']
ESCALATION_TARGETS = ['qa_team', 'document_control', 'senior_representative', 'service_centre']
COMMON = ['escalate', 'defer']
OPTIONS = {
    'draft': ['approve_send', 'request_revision', 'reject'] + COMMON,
    'movement': ['approve', 'reject'] + COMMON,
    'movement_failed': ['retry', 'reject'] + COMMON,
    'qa': ['determine'] + COMMON,
    'identity': ['attest_identity', 'decline_identity'] + COMMON,
    'revision': ['confirm_revision', 'reject_revision'] + COMMON,
    'disposition': ['resolve', 'close_unresolved', 'request_investigation'] + COMMON,
}


def actor(model):
    return 'lab-reviewer-agent:' + str(model or 'unconfigured')


# Telemetry identity of the reviewer (Agent Arena). A lab participant, never one
# of the ticket workflow's agents (telemetry.AGENTS): its own agent id, registry
# role and capabilities. Only the telemetry context around the reviewer's model
# calls uses it; the request, prompt, options and decisions are unaffected.
REVIEWER_AGENT_ID = 'lab_reviewer'
REVIEWER_AGENT_ROLE = 'lab-reviewer'
REVIEWER_DISPLAY_NAME = 'Testing Lab Reviewer'
REVIEWER_PARTICIPANT = 'simulated_human_reviewer'
_STEP = contextvars.ContextVar('lab_reviewer_step', default=None)


@contextmanager
def review_step(key):
    """The lifecycle journal key of the decision point being reviewed; reviewer
    model calls inside it report it as their task id."""
    token = _STEP.set(key)
    try:
        yield
    finally:
        _STEP.reset(token)


def _register_identity(store):
    if store is None or getattr(store, '_lab_reviewer_registered', False):
        return
    from . import telemetry
    telemetry.register(store, REVIEWER_AGENT_ID, REVIEWER_DISPLAY_NAME, REVIEWER_AGENT_ROLE,
                       capabilities=('simulated_human_review',),
                       execution_constraints={'participant': REVIEWER_PARTICIPANT, 'system_under_test': False})
    try:
        store._lab_reviewer_registered = True
    except AttributeError:
        pass


@contextmanager
def telemetry_identity(view=None):
    """Bind the reviewer's logical agent identity for the model calls inside.

    inference.infer reads task_context for agent_id/task_id/workflow_id, so the
    queued/started/heartbeat/completed/failed events of every reviewer call
    carry this identity (logical_agent_status 'known') instead of none. The
    binding replaces any enclosing context, so a reviewer call is never
    attributed to a system-under-test agent. Telemetry problems never stop a
    review."""
    from . import inference, task_context
    session = inference.current() or {}
    try:
        _register_identity(session.get('store'))
    except Exception:
        pass
    view = view or {}
    task_id = _STEP.get() or (f"{view['decision_point']}:{view['target_id']}" if view.get('decision_point') and view.get('target_id') else None)
    with task_context.bind(task_id=task_id, workflow_id=session.get('investigation_id'), agent_id=REVIEWER_AGENT_ID):
        yield


def _clip(text, n=900):
    text = str(text or '')
    return text if len(text) <= n else text[:n] + ' …'


def pending_decisions(store, cid):
    """Decision points a representative would see now, with stable keys."""
    out = []
    for m in store.query("SELECT message_id FROM messages WHERE case_id=? AND direction='outbound' AND status='draft' ORDER BY created_at", (cid,)):
        out.append({'kind': 'draft', 'target_id': m['message_id'], 'key': 'draft:' + m['message_id']})
    for r in store.query("SELECT rma_id,status FROM rmas WHERE case_id=? AND status IN ('proposed','failed') ORDER BY created_at", (cid,)):
        out.append({'kind': 'movement' if r['status'] == 'proposed' else 'movement_failed', 'target_id': r['rma_id'],
                    'key': f"movement:{r['rma_id']}:{r['status']}"})
    for q in store.query("SELECT notification_id FROM qa_notifications WHERE case_id=? AND status='open'", (cid,)):
        out.append({'kind': 'qa', 'target_id': q['notification_id'], 'key': 'qa:' + q['notification_id']})
    case = store.case(cid) or {}
    for item in store.query("SELECT item_id,action FROM checklist_items WHERE case_id=? AND status='requires_approval' AND approval_decision IS NULL "
                            "AND action IN ('verify_identity','assess_device_revision') ORDER BY seq", (cid,)):
        if item['action'] == 'verify_identity' and not case.get('identity_verified'):
            out.append({'kind': 'identity', 'target_id': item['item_id'], 'key': 'identity:' + item['item_id']})
        if item['action'] == 'assess_device_revision' and not case.get('revision_confirmation'):
            out.append({'kind': 'revision', 'target_id': item['item_id'], 'key': 'revision:' + item['item_id']})
    return out


DRAFT_SEGMENT_CHARS = 6000


def reviewer_budget_chars():
    """Characters of observation the reviewer route can take: requested
    num_ctx minus reserved output and the system prompt, at a conservative
    3.5 characters per token (the same ratio class the fit check estimates)."""
    from . import inference
    s = inference.current()
    route = ((s or {}).get('snapshot') or {}).get('routes', {}).get('lab_reviewer') or {}
    options = route.get('options') or {}
    num_ctx = int(options.get('num_ctx') or 8192)
    num_predict = int(options.get('num_predict') or REVIEWER_MAX_TOKENS)
    return max(4000, int((num_ctx - num_predict - 700) * 3.5)), {'num_ctx': num_ctx, 'num_predict': num_predict}


def _sha(text):
    return hashlib.sha256((text or '').encode()).hexdigest()


def _sources(store, cid, message):
    """Authoritative evidence behind a draft: content, provenance, applicability."""
    from . import qms
    lookup = {c.chunk_id: c for c in qms.load_chunks()}
    ids = list(dict.fromkeys((json.loads((message or {}).get('metadata') or '{}').get('source_chunk_ids') or []) +
                             [r['chunk_id'] for r in store.query("SELECT chunk_id FROM retrieved_sources WHERE case_id=? AND status='USED'", (cid,))]))
    items = []
    for chunk_id in ids:
        c = lookup.get(chunk_id)
        if not c:
            items.append({'id': 'source:' + chunk_id, 'kind': 'source', 'content': {'chunk_id': chunk_id, 'note': 'not found in the controlled corpus'}})
            continue
        items.append({'id': 'source:' + chunk_id, 'kind': 'source', 'content': {
            'chunk_id': chunk_id, 'document_id': c.document_id, 'procedure_id': c.procedure_id, 'title': c.title, 'section': c.section_path,
            'model': c.model, 'applicable_revisions': c.applicable_revisions, 'approval_status': c.approval_status,
            'document_version': c.document_version, 'text': c.text}})
    return items


def observe(store, cid, point, budget_chars=None):
    """The representative's view for one decision. No scenario data.

    Everything the decision needs is presented in full (no fixed clipping).
    When the material exceeds the reviewer's context budget, lower-priority
    items are withheld VISIBLY (evidence_index / withheld) and can be
    requested; draft parts not yet inspected block approval (coordinator)."""
    budget, ctx = (budget_chars, None) if budget_chars else reviewer_budget_chars()
    case = store.case(cid) or {}
    view = {'decision_point': point['kind'], 'target_id': point['target_id'], 'previous_attempt': point.get('previous_attempt'),
            'case': {k: case.get(k) for k in ('case_id', 'subject', 'channel', 'status', 'priority', 'priority_reason', 'customer_name',
                                              'serial_number', 'model', 'revision', 'revision_confirmation', 'identity_verified',
                                              'verification_points', 'safety_hold', 'classification', 'is_adverse_event',
                                              'tracking_status', 'assigned_to')}}
    items = []  # (priority, item): lower number = more essential
    if case.get('serial_number'):
        view['erp_record_for_serial_on_case'] = store.one('SELECT c.customer_name,c.date_of_birth,d.serial_number,d.model,d.revision FROM devices d '
                                                          'JOIN customers c ON c.account_id=d.account_id WHERE d.serial_number=?', (case['serial_number'],))
    if point['kind'] == 'draft':
        draft = store.one('SELECT * FROM messages WHERE message_id=?', (point['target_id'],))
        body = (draft or {}).get('body') or ''
        val = store.one('SELECT disposition,issues,policy_results FROM artifact_validation_records WHERE artifact_id=? ORDER BY cycle DESC,at DESC LIMIT 1',
                        (point['target_id'],))
        rev = store.one('SELECT passed,payload FROM response_reviews WHERE message_id=? ORDER BY at DESC LIMIT 1', (point['target_id'],))
        # Parts small enough that any one part fits the reviewer's budget, so
        # every part can be inspected (and requested) individually.
        size = max(800, min(DRAFT_SEGMENT_CHARS, budget // 3))
        segments = [body[i:i + size] for i in range(0, len(body), size)] or ['']
        view['draft'] = {'message_id': point['target_id'], 'sha256': _sha(body), 'characters': len(body), 'parts': len(segments),
                         'kind': json.loads((draft or {}).get('metadata') or '{}').get('kind'),
                         'validation_disposition': val['disposition'] if val else 'not validated',
                         'validation_issues': json.loads(val['issues'] or '[]') if val else [],
                         'independent_review_passed': bool(rev['passed']) if rev else None,
                         'independent_review_issues': (json.loads(rev['payload'] or '{}').get('issues') or []) if rev else []}
        if point.get('exclude_draft_parts'):
            # Final decision on an artifact reviewed in parts: the parts were
            # inspected section by section; their notes travel instead.
            view['draft']['parts_reviewed_in_session'] = [f'draft:part{i}' for i in range(1, len(segments) + 1)]
        else:
            for i, part in enumerate(segments, 1):
                items.append((0, {'id': f'draft:part{i}', 'kind': 'draft_part', 'content': part}))
        items += [(1, s) for s in _sources(store, cid, draft)]
    if point['kind'] in ('movement', 'movement_failed'):
        r = store.one('SELECT * FROM rmas WHERE rma_id=?', (point['target_id'],))
        prereq = store.one('SELECT rma_id,received_at,received_by FROM rmas WHERE rma_id=?', (r.get('prerequisite_rma_id'),)) if r and r.get('prerequisite_rma_id') else None
        view['proposal'] = {k: r.get(k) for k in ('rma_id', 'movement_type', 'direction', 'items', 'reason', 'status')} if r else None
        view['operational_prerequisites'] = {
            'identity_verified': bool(case.get('identity_verified')), 'verification_points': case.get('verification_points'),
            'prerequisite_original_received': (bool(prereq.get('received_at')) if prereq else None),
            'prerequisite_received_by': (prereq or {}).get('received_by'),
            'required_approvals': [c['action'] for c in store.query("SELECT action FROM checklist_items WHERE case_id=? AND status='requires_approval'", (cid,))]}
    if point['kind'] == 'qa':
        q = store.one('SELECT * FROM qa_notifications WHERE notification_id=?', (point['target_id'],))
        view['qa_notification'] = {k: (q or {}).get(k) for k in ('summary', 'body', 'severity', 'status')}
        view['qa_options'] = QA_DETERMINATIONS
    if point['kind'] in ('identity', 'revision'):
        item = store.one('SELECT * FROM checklist_items WHERE item_id=?', (point['target_id'],))
        view['checklist_item'] = {k: (item or {}).get(k) for k in ('action', 'description', 'tool_result', 'evidence', 'missing_info')}
    for n, m in enumerate(reversed(store.query('SELECT * FROM messages WHERE case_id=? ORDER BY created_at', (cid,)))):
        if m['status'] == 'draft' and m['message_id'] != point['target_id']:
            continue
        items.append((2 if n < 4 else 4, {'id': 'message:' + m['message_id'], 'kind': 'conversation',
                                          'content': {'direction': m['direction'], 'status': m['status'], 'author': m['author'], 'body': m['body']}}))
    latest = {}
    for f in store.query('SELECT role,payload FROM specialist_findings WHERE case_id=? ORDER BY at', (cid,)):
        latest[f['role']] = json.loads(f['payload'] or '{}')
    for role, p in latest.items():
        summary = {k: p.get(k) for k in ('blocked', 'missing_evidence', 'recommended_next_action', 'selected_action', 'mode', 'outcome', 'conflicts') if p.get(k)}
        if summary:
            items.append((2, {'id': 'finding:' + role, 'kind': 'agent_finding', 'content': {'role': role, **summary}}))
    items.append((3, {'id': 'checklist', 'kind': 'checklist', 'content': [
        {'action': c['action'], 'status': c['status'], 'missing_info': c['missing_info']}
        for c in store.query("SELECT action,status,missing_info FROM checklist_items WHERE case_id=? AND status IN ('requires_approval','failed','pending') ORDER BY seq DESC LIMIT 12", (cid,))]}))
    items.append((3, {'id': 'operations', 'kind': 'operations', 'content': {
        'proposals': [{k: r[k] for k in ('rma_id', 'movement_type', 'status')} for r in store.query('SELECT * FROM rmas WHERE case_id=?', (cid,))],
        'shipments': [{k: s[k] for k in ('direction', 'status', 'contents')} for s in store.query('SELECT * FROM sim_shipments WHERE case_id=?', (cid,))],
        'open_qa': [q['notification_id'] for q in store.query("SELECT notification_id FROM qa_notifications WHERE case_id=? AND status='open'", (cid,))]}}))
    items.append((4, {'id': 'notes', 'kind': 'notes', 'content': [{'author': n['author'], 'body': n['body']}
                                                                   for n in store.query('SELECT * FROM case_notes WHERE case_id=? ORDER BY at DESC LIMIT 6', (cid,))]}))
    requested = set(point.get('requested_evidence') or [])
    ordered = sorted(items, key=lambda pi: (0 if pi[1]['id'] in requested else 1, pi[0]))
    view['options'] = list(OPTIONS[point['kind']])
    view['evidence'], withheld = [], []
    used = len(json.dumps(view, default=str))
    for _, item in ordered:
        size = len(json.dumps(item, default=str))
        if used + size <= budget:
            view['evidence'].append(item);used += size
        else:
            withheld.append({'id': item['id'], 'kind': item['kind'], 'characters': size})
    view['withheld'] = withheld
    if withheld:
        view['options'] = view['options'] + ['request_evidence']
        view['evidence_note'] = ('Some material did not fit in this review and is listed in withheld. Request it by id before deciding if it '
                                 'matters; do not approve content you have not seen.')
    view['_presentation'] = {'budget_chars': budget, 'presented_chars': used, 'context': ctx,
                             'presented_ids': [i['id'] for i in view['evidence']], 'withheld_ids': [w['id'] for w in withheld]}
    return view


def schema(kind, withheld_ids=()):
    options = OPTIONS[kind] + (['request_evidence'] if withheld_ids else [])
    props = {
        'decision': {'type': 'string', 'enum': options},
        # rc.25: 400-character caps cut rationales mid-sentence in rc.24
        # (p95 exactly 400); raised; the output budget is REVIEWER_MAX_TOKENS (round 2).
        'rationale': {'type': 'string', 'maxLength': 600},
        'revision_instructions': {'type': 'string', 'maxLength': 600},
        'override_rationale': {'type': 'string', 'maxLength': 300},
        'determination': {'type': 'string', 'enum': QA_DETERMINATIONS + ['n/a']},
        'escalate_to': {'type': 'string', 'enum': ESCALATION_TARGETS + ['n/a']},
        'requested_evidence': {'type': 'array', 'items': {'type': 'string', 'enum': list(withheld_ids) or ['none']}, 'maxItems': 8}}
    if kind == 'draft':
        from .objection_grounding import OBJECTION_SCHEMA
        props['objections'] = OBJECTION_SCHEMA
    return {'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False}


def decide(view):
    """One reviewer decision from the reviewer's own model (route lab_reviewer)."""
    from . import generation, inference
    shown = {k: v for k, v in view.items() if not k.startswith('_')}
    with telemetry_identity(view), inference.specialist('lab_reviewer'):
        model = inference.model()
        value = generation.compose_structured(REVIEWER_SYSTEM, shown, schema(view['decision_point'], [w['id'] for w in view.get('withheld') or []]),
                                              purpose='lab_reviewer', max_tokens=REVIEWER_MAX_TOKENS)
    return value, model


def command_for(point, decision, model):
    """Translate a reviewer decision into an application command (or None)."""
    who = actor(model)
    why = ('Agentic Testing reviewer: ' + (decision.get('rationale') or '').strip())[:500] or 'Agentic Testing reviewer decision'
    base = {'authorizer': who, 'rationale': why, 'target_id': point['target_id']}
    d, kind = decision['decision'], point['kind']
    if d in ('defer', 'request_evidence'):
        return None
    if d == 'escalate':
        target = decision.get('escalate_to') if decision.get('escalate_to') in ESCALATION_TARGETS else 'senior_representative'
        return [{**base, 'action': 'add_note', 'body': f'Escalated to {target}: {why}', 'command_id': 'ESC-' + point['key']},
                {**base, 'action': 'assign', 'assignee': target}]
    if kind == 'draft':
        if d == 'approve_send':
            # Bound to the exact version reviewed (re-review if it changed).
            return {**base, 'action': 'send_email', 'review_override': decision.get('override_rationale') or '',
                    'reviewed_sha256': point.get('reviewed_sha256'), 'reviewed_case_state': point.get('reviewed_case_state')}
        if d == 'request_revision':
            # Structured objections travel with the command and are grounded
            # against the authorized sources that were actually presented.
            return {**base, 'action': 'revise_email', 'rationale': (decision.get('revision_instructions') or why)[:500],
                    'objections': decision.get('objections') or [], 'command_id': 'REV-' + point['key'],
                    'presented_source_ids': point.get('presented_source_ids') or []}
        return {**base, 'action': 'reject_email'}
    if kind == 'movement':
        return {**base, 'action': 'approve_movement' if d == 'approve' else 'reject_movement'}
    if kind == 'movement_failed':
        return {**base, 'action': 'retry_movement' if d == 'retry' else 'reject_movement'}
    if kind == 'qa':
        determination = decision.get('determination')
        if determination not in QA_DETERMINATIONS:
            raise ValueError('Reviewer chose determine without a valid structured determination')
        return {**base, 'action': 'determine_qa', 'determination': determination}
    if kind == 'identity':
        return {**base, 'action': 'attest_identity' if d == 'attest_identity' else 'decline_identity'}
    if kind == 'revision':
        return {**base, 'action': 'confirm_revision' if d == 'confirm_revision' else 'reject_revision'}
    if kind == 'disposition':
        if d == 'resolve':
            return {**base, 'action': 'resolve_case'}
        if d == 'close_unresolved':
            return {**base, 'action': 'close_unresolved'}
        return [{**base, 'action': 'add_note', 'body': 'Reviewer requests further investigation: ' + why, 'command_id': 'INV-' + point['key']},
                {**base, 'action': 'investigate_followup'}]
    raise ValueError(f'Unsupported reviewer decision {d} for {kind}')


CASE_STATE_FIELDS = ('identity_verified', 'account_id', 'serial_number', 'revision', 'revision_confirmation', 'safety_hold',
                     'classification', 'is_adverse_event', 'closed_at')


def case_state(store, cid):
    """Hash of the case facts an approval depends on (rc.25): an approval is
    bound to the exact artifact AND this state, and is void after a material change."""
    case = store.case(cid) or {}
    return hashlib.sha256(json.dumps({k: case.get(k) for k in CASE_STATE_FIELDS}, sort_keys=True, default=str).encode()).hexdigest()[:24]


def view_hash(view):
    return hashlib.sha256(json.dumps(view, sort_keys=True, default=str).encode()).hexdigest()[:16]


def worst_case_characters(node):
    """Upper bound on the characters a grammar-constrained reply to ``node``
    can produce (JSON punctuation and keys included)."""
    kind = node.get('type')
    if 'enum' in node:
        return max(len(str(v)) for v in node['enum']) + 2
    if kind == 'string':
        return int(node.get('maxLength') or 10 ** 6) + 2
    if kind == 'array':
        return 2 + int(node.get('maxItems') or 10 ** 3) * (worst_case_characters(node['items']) + 2)
    if kind == 'object':
        props = node.get('properties') or {}
        return 2 + sum(len(k) + 4 + worst_case_characters(v) for k, v in props.items())
    return 16


def worst_case_tokens(kind='draft', withheld_ids=()):
    return int(worst_case_characters(schema(kind, withheld_ids)) / REVIEWER_WORST_CHARS_PER_TOKEN) + 1


def session_envelope_tokens():
    """Measured rc.24 maximum plus everything rc.25 lets the reply add."""
    from .objection_grounding import OBJECTION_SCHEMA
    added = RC25_PROSE_GROWTH_CHARS + len('objections') + 4 + worst_case_characters(OBJECTION_SCHEMA)
    return int(REVIEWER_OBSERVED_MAX_TOKENS + added / REVIEWER_WORST_CHARS_PER_TOKEN) + 1
