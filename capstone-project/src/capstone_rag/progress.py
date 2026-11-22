"""Investigation progress read model for one ticket (rc.27).

Built only from persisted, observed records (activity_events of the case, plus the case status); never from stage
names alone. Each investigation pass has its own investigation_id; the current pass is shown, previous passes are
listed separately and never merged into it. "Investigation completed" means the analysis pass ended, not that the
case is resolved. Reading this never starts inference.
"""
from __future__ import annotations

import json

from . import caseflow

STAGES = (
    ('intake', 'Intake and triage', {'read_message', 'assess_priority', 'classify_complaint', 'interpretation',
                                     'identify_missing_information'}, {'ticket_interpretation'}),
    ('safety', 'Safety screening', {'screen_adverse_event', 'evaluate_complaint', 'evaluate_quality_signals', 'guardrail'},
     {'safety_complaint'}),
    ('erp', 'ERP identity and device evidence', {'identify_customer', 'verify_identity', 'check_erp_records',
                                                 'review_case_history', 'assess_device_revision'}, {'erp_research'}),
    ('qms', 'QMS retrieval and applicability', {'search_qms', 'consult_operational_procedures',
                                                'validate_procedure_applicability'}, {'qms_research'}),
    ('planning', 'Planning and branch decision', {'planning', 'determine_product_movement'}, {'resolution_planning'}),
    ('response', 'Proposed response or operation', {'prepare_response', 'response', 'disclosure', 'consistency',
                                                    'generation'}, {'customer_communication', 'documentation', 'operations'}),
    ('checks', 'Independent checks', {'correction'}, {'communication_review', 'independent_validation'}),
)
LABELS = {key: label for key, label, _, _ in STAGES}
RUNNING = {'started', 'requested', 'running', 'retrying', 'in_progress'}
FAILED = {'failed', 'error'}
EVENT_LIMIT = 400


def _epoch(value):
    """rc.28: activity times are ISO text (store.now()); rc.27 subtracted them as numbers and every progress request
    with two events in a step failed, freezing the panel on its first answer."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _evidence(row):
    try:
        value = json.loads(row['evidence'] or '{}')
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def stage_of(category, evidence, summary=''):
    if category == 'stage_execution':
        stage = str(evidence.get('stage') or '')
        if stage in ('planning', 'planning_model'):
            return 'planning'
        if stage in ('clarity_review', 'critic_judge'):
            return 'checks'
        return None
    role = evidence.get('role') or evidence.get('specialist') or evidence.get('purpose_role')
    if not role and category in ('specialist', 'inference', 'model') and summary:
        role = summary.split()[0]   # e.g. 'erp_research started'
    for key, _, categories, roles in STAGES:
        if category in categories:
            return key
    for key, _, _, roles in STAGES:
        if role in roles:
            return key
    return None


def _status(row):
    status = (row['status'] or '').lower()
    summary = (row['summary'] or '')
    if status in FAILED:
        return 'failed'
    if status in RUNNING:
        return 'running'
    if 'not applicable' in summary.lower():
        return 'not_applicable'
    if summary.lower().startswith('blocked') or ' blocked' in summary.lower():
        return 'blocked'
    return 'completed'


def _compact(row, evidence, stage):
    detail = {k: evidence[k] for k in ('role', 'model', 'inference_node', 'resource_id', 'stage', 'mode', 'selected_action',
                                       'purpose', 'snapshot_id', 'profile_id', 'configuration_hash', 'methodology_id',
                                       'generation_mode', 'source_chunk_ids', 'exit_reason') if k in evidence}
    if row['category'] == 'planning' and isinstance(evidence.get('entry'), dict):
        detail['branch'] = bool(evidence['entry'].get('enter') or evidence['entry'].get('branch'))
    return {'event_id': row['event_id'], 'at': _epoch(row['at']), 'category': row['category'], 'status': _status(row),
            'summary': (row['summary'] or '')[:300], 'duration_ms': row['duration_ms'], 'stage': stage, 'detail': detail}


def _branch_outcome(events):
    for e in reversed(events):
        if e['category'] == 'planning' and 'Tree-of-thought entry' in e['summary']:
            tail = e['summary'].split(':', 1)[-1].strip().lower()
            if tail.startswith('branch'):
                return 'Branched: alternatives compared (selective tree of thought)'
            if 'single' in tail:
                return 'Single path: the entry rule did not branch'
    return None


def case_progress(store, case_id, after=0):
    case = store.case(case_id)
    if not case:
        return None
    passes = store.query("SELECT investigation_id, MIN(at) AS started, MAX(at) AS last, COUNT(*) AS n FROM activity_events "
                         "WHERE case_id=? AND investigation_id IS NOT NULL GROUP BY investigation_id ORDER BY started", (case_id,))
    current = passes[-1]['investigation_id'] if passes else None
    rows = store.query("SELECT event_id, at, category, status, summary, duration_ms, evidence FROM activity_events "
                       "WHERE case_id=? AND investigation_id=? ORDER BY event_id DESC LIMIT ?", (case_id, current, EVENT_LIMIT)) if current else []
    rows = list(reversed(rows))
    events = []
    for row in rows:
        evidence = _evidence(row)
        events.append(_compact(row, evidence, stage_of(row['category'], evidence, row['summary'] or '')))
    stages = []
    for key, label, _, _ in STAGES:
        mine = [e for e in events if e['stage'] == key]
        if not mine:
            stages.append({'key': key, 'label': label, 'status': 'not_started', 'outcome': 'No recorded work in this pass',
                           'at': None, 'elapsed_ms': None, 'events': []})
            continue
        last = mine[-1]
        status = 'failed' if any(e['status'] == 'failed' for e in mine[-3:]) and last['status'] != 'completed' else last['status']
        outcome = last['summary']
        if key == 'planning':
            outcome = _branch_outcome(mine) or outcome
        stages.append({'key': key, 'label': label, 'status': status, 'outcome': outcome, 'at': last['at'],
                       'elapsed_ms': int((mine[-1]['at'] - mine[0]['at']) * 1000) if len(mine) > 1 and mine[-1]['at'] is not None and mine[0]['at'] is not None else None,
                       'events': mine[-8:]})
    ai_state = case.get('ai_state')
    status = case.get('status')
    if ai_state in (caseflow.QUEUED, caseflow.RETRIABLE):
        waiting = ('queued', 'Queued: waiting for the case worker')
    elif ai_state == caseflow.PROCESSING:
        waiting = ('running', 'Investigation running')
    elif status == caseflow.AWAITING:
        waiting = ('waiting_for_customer', 'Waiting for the customer or a shipment event')
    elif status in (caseflow.CLOSED, caseflow.RESOLVED):
        waiting = ('closed', 'Case ' + status.lower())
    elif ai_state == caseflow.FAILED:
        waiting = ('failed', 'Investigation could not complete: handed to the representative')
    else:
        waiting = ('waiting_for_human', 'Investigation pass ended: waiting for the representative')
    stages.append({'key': 'waiting', 'label': 'Waiting for the representative', 'status': waiting[0], 'outcome': waiting[1],
                   'at': _epoch(case.get('updated_at')), 'elapsed_ms': None, 'events': []})
    policy = next((e['detail'] for e in reversed(events) if e['category'] in ('methodology', 'configuration')), {})
    return {'case_id': case_id, 'ai_state': ai_state, 'status': status, 'current_pass': current,
            'cursor': max([e['event_id'] for e in events] or [int(after or 0)]),
            'new_events': [e for e in events if e['event_id'] > int(after or 0)],
            'stages': stages, 'policy': policy,
            'previous_passes': [{'investigation_id': p['investigation_id'], 'started': _epoch(p['started']), 'last': _epoch(p['last']),
                                 'events': p['n']} for p in passes[:-1]][-10:]}
