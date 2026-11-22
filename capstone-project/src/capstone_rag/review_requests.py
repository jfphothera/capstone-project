"""Stable review-request identity and closure (rc.25, A1 addition).

Before rc.25 every resume of a paused workflow emitted another
``human_review.requested`` row without a task id, and nothing ever closed it,
so Agent Arena kept the figures "waiting" (881 on the hub, 2026-11-19).

Now:

* a review request has a stable ``task_id`` (``review:<workflow>:<hash of the
  pending item ids>``) and is emitted ONCE per pending set, not per resume;
* ``human_review.resolved`` closes it when it is handled, with
  ``detail.outcome``: ``approved`` | ``rejected`` | ``mixed`` (items decided both
  ways) | ``superseded`` (a newer review of the same case replaced it while
  items were still open) | ``case_closed``;
* a confirmed worker stop closes an open request through
  ``lab_reconciliation`` (``task.interrupted`` with the same ``task_id``).

Only identifiers and statuses are recorded, never message text.
"""
from __future__ import annotations

import hashlib
import json

EVENT_REQUESTED = 'human_review.requested'
EVENT_RESOLVED = 'human_review.resolved'
_APPROVED = {'sent', 'approved', 'executed', 'received', 'completed', 'in_transit', 'delivered'}
_REJECTED = {'rejected', 'canceled', 'cancelled', 'validation_failed', 'failed_closed'}


def task_id(workflow_id, items):
    ids = sorted(str(i.get('id')) for i in items or [])
    return f"review:{workflow_id}:" + hashlib.sha256(json.dumps(ids).encode()).hexdigest()[:12]


def _open(store, case_id):
    rows = store.query("SELECT task_id,workflow_id,detail FROM arena_events WHERE case_id=? AND event_type=? AND task_id IS NOT NULL ORDER BY sequence",
                       (case_id, EVENT_REQUESTED))
    closed = {r['task_id'] for r in store.query("SELECT task_id FROM arena_events WHERE case_id=? AND event_type=?", (case_id, EVENT_RESOLVED))}
    seen, out = set(), []
    for r in rows:
        if r['task_id'] in closed or r['task_id'] in seen:
            continue
        seen.add(r['task_id'])
        detail = json.loads(r['detail'] or '{}')
        out.append({'task_id': r['task_id'], 'workflow_id': r['workflow_id'], 'items': detail.get('items') or []})
    return out


def _status(store, item):
    if item.get('kind') == 'email':
        row = store.one('SELECT status FROM messages WHERE message_id=?', (item['id'],))
    else:
        row = store.one('SELECT status FROM rmas WHERE rma_id=?', (item['id'],))
    return (row or {}).get('status') or 'missing'


def _close(store, case_id, request, outcome, reason, items, superseded_by=None):
    from . import telemetry
    telemetry.emit(store, EVENT_RESOLVED, case_id=case_id, workflow_id=request['workflow_id'], task_id=request['task_id'],
                   agent_id='coordinator', correlation_id=request['task_id'], task_status=outcome,
                   summary=f'Human review {outcome}', evidence_refs=[i['id'] for i in items],
                   detail={'outcome': outcome, 'reason': reason, 'items': items, 'superseded_by': superseded_by,
                           'review_task_id': request['task_id'], 'contract': 'capstone.review-request/1'})


def settle(store, case_id, *, superseded_by=None):
    """Close every open review request of the case that is now handled."""
    try:
        requests = _open(store, case_id)
    except Exception:
        return 0
    if not requests:
        return 0
    case = store.case(case_id) or {}
    closed = 0
    for request in requests:
        if request['task_id'] == superseded_by:
            continue
        items = [{**i, 'status': _status(store, i)} for i in request['items']]
        pending = [i for i in items if i['status'] not in _APPROVED | _REJECTED]
        if case.get('closed_at') or case.get('status') in ('Resolved', 'Closed'):
            outcome, reason = 'case_closed', 'the case was closed (' + ('resolved' if case.get('resolved') else 'unresolved') + ')'
        elif pending and superseded_by:
            outcome, reason = 'superseded', 'a newer review request for this case replaced it'
        elif pending or not items:
            continue
        else:
            approved = [i for i in items if i['status'] in _APPROVED]
            outcome = 'approved' if len(approved) == len(items) else ('rejected' if not approved else 'mixed')
            reason = 'every pending item was decided'
        _close(store, case_id, request, outcome, reason, items, superseded_by)
        closed += 1
    return closed


def request(store, case_id, workflow_id, items, *, reason):
    """Emit the request once per pending set; supersede older open ones."""
    from . import telemetry
    tid = task_id(workflow_id, items)
    if store.one('SELECT 1 FROM arena_events WHERE task_id=? AND event_type=?', (tid, EVENT_REQUESTED)):
        return tid
    settle(store, case_id, superseded_by=tid)
    telemetry.emit(store, EVENT_REQUESTED, case_id=case_id, workflow_id=workflow_id, task_id=tid, agent_id='coordinator',
                   correlation_id=tid, task_status='waiting', summary='Named human review required',
                   evidence_refs=[i['id'] for i in items],
                   detail={'items': [{'id': i['id'], 'kind': i['kind']} for i in items], 'reason': reason, 'contract': 'capstone.review-request/1'})
    return tid
