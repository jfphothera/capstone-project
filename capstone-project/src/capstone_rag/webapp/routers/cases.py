"""Cases, the checklist, revision confirmation and customer correspondence.

Each handler does three things and no more: authorise the request, call the
module that owns the rule, and return the refreshed view. The rules
themselves - who may approve what, what an approval causes, what a rejection
blocks - are in `review`, `caseflow` and `erp_sim`, so an HTTP client cannot
reach a different set of rules from the one the tests exercise.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ... import caseflow, review, triage
from .. import views
from ..runtime import get_runtime
from ..schemas import (
    Assignment, Authorised, CloseCase, ItemDecision, PriorityOverride,
    Regenerate, RevisionConfirmation, SendReply, TicketIn,
)

router = APIRouter(prefix="/api", tags=["cases"])


def _case_or_404(store, case_id):
    case = store.case(case_id)
    if case is None:
        raise HTTPException(404, f"no case {case_id}")
    return case


@router.get("/cases")
def list_cases(q: str = "", priority: str = "", status: str = "",
               assigned: str = "", include_closed: bool = False,
               sort: str = "priority",
               limit: int = Query(default=200, ge=1, le=1000)):
    rt = get_runtime()
    return views.inbox(rt.store, query=q, priority=priority, status=status,
                       assigned=assigned, include_closed=include_closed,
                       sort=sort, limit=limit)


@router.post("/cases", status_code=201)
def create_case(body: TicketIn):
    """Raise a case by hand. Used by the demonstration and the test view."""
    rt = get_runtime()
    case_id = rt.flow.intake(body.subject, body.body,
                             account_id=body.account_id,
                             channel=body.channel, author=body.author)
    case = rt.store.case(case_id)
    rt.ops.queue.submit(case_id, case["priority"])
    rt.announce_case(case_id)
    return {"case_id": case_id, "priority": case["priority"],
            "status": case["status"]}


@router.get("/cases/{case_id}/progress")
def case_progress(case_id: str, after: int = 0):
    """rc.27: observed investigation progress of one ticket since an event cursor (read-only; never starts work)."""
    from ... import progress
    payload = progress.case_progress(get_runtime().store, case_id, after)
    if payload is None:
        raise HTTPException(404, f"no case {case_id}")
    return payload


@router.get("/cases/{case_id}")
def get_case(case_id: str):
    rt = get_runtime()
    payload = views.case_workspace(rt.store, case_id)
    if payload is None:
        raise HTTPException(404, f"no case {case_id}")
    return payload


@router.post("/cases/{case_id}/priority")
def override_priority(case_id: str, body: PriorityOverride):
    rt = get_runtime()
    _case_or_404(rt.store, case_id)
    out = review.override_priority(rt.store, case_id, body.priority,
                                  body.reason, body.authorizer)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason", "could not override"))
    rt.announce_case(case_id, "case_updated")
    return {**out, "case": views.case_workspace(rt.store, case_id)["case"]}


@router.post("/cases/{case_id}/assign")
def assign_case(case_id: str, body: Assignment):
    rt = get_runtime()
    _case_or_404(rt.store, case_id)
    out = review.assign(rt.store, case_id, body.assignee, body.authorizer)
    rt.announce_case(case_id, "case_updated")
    return out


@router.post("/cases/{case_id}/investigate")
def investigate_now(case_id: str):
    """Re-queue the case. The worker is single and shared; this only queues."""
    rt = get_runtime()
    case = _case_or_404(rt.store, case_id)
    queued = rt.ops.queue.submit(case_id, case["priority"])
    rt.store.audit("investigation_requested", actor=rt.representative,
                   case_id=case_id,
                   summary="re-queued for investigation"
                           if queued else "already queued or in flight")
    return {"queued": queued, "queue": rt.ops.queue.snapshot()}


@router.post("/cases/{case_id}/revision-confirmation")
def confirm_revision(case_id: str, body: RevisionConfirmation):
    """Rule RV-1's missing half. See caseflow.confirm_revision."""
    rt = get_runtime()
    _case_or_404(rt.store, case_id)
    out = rt.flow.confirm_revision(case_id, decision=body.decision,
                                   authorizer=body.authorizer,
                                   note=body.note)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason", "could not confirm"))
    rt.announce_case(case_id, "case_updated")
    return out


@router.post("/cases/{case_id}/close")
def close_case(case_id: str, body: CloseCase):
    rt = get_runtime()
    _case_or_404(rt.store, case_id)
    out = review.close_case(rt.store, case_id, body.authorizer,
                            note=body.resolution or body.note,
                            resolved=body.resolved)
    if not out.get("ok"):
        # rc.25 (C5): a refused closure (e.g. a held safety report) is an
        # error the representative must see, never a silent no-op.
        raise HTTPException(409, out.get("reason") or "the case cannot be closed now")
    rt.announce_case(case_id, "case_updated")
    return out


@router.get("/cases/{case_id}/audit")
def case_audit(case_id: str):
    rt = get_runtime()
    _case_or_404(rt.store, case_id)
    return {"events": rt.store.audit_trail(case_id),
            "evidence": views.evidence(rt.store, case_id)}


# --- checklist -------------------------------------------------------------

@router.post("/checklist/{item_id}/decision")
def decide_item(item_id: str, body: ItemDecision):
    """Approve or reject one finding.

    The API refuses a decision on a step that is not a human decision. The UI
    does not offer buttons there either, but the UI is not the control.
    """
    rt = get_runtime()
    item = rt.store.one("SELECT * FROM checklist_items WHERE item_id=?",
                        (item_id,))
    if item is None:
        raise HTTPException(404, f"no checklist item {item_id}")
    newer=rt.store.one('SELECT item_id FROM checklist_items WHERE case_id=? AND action=? AND seq>? LIMIT 1',
        (item['case_id'],item['action'],item['seq']))
    if newer:
        raise HTTPException(409,'This is a historical finding; decide the latest investigation result instead')
    if item["approval_decision"]:
        # Checked before the "is this even a decision" guard: once an item is
        # decided it no longer offers one, and a repeat click is a conflict
        # with the existing decision rather than a bad request.
        raise HTTPException(
            409, f"{item_id} is already {item['approval_decision']} by "
                 f"{item['authorizer']}")
    from ... import presentation
    described = presentation.describe_item(item)
    if not described["offers_decision"]:
        raise HTTPException(
            400, f"'{described['label']}' is an automatic "
                 f"{described['decision_class_label'].lower()} and takes no "
                 f"approval; it is recorded for audit only")
    out = review.decide_item(rt.store, item_id, body.decision,
                             body.authorizer, feedback=body.feedback)
    if not out.get("ok"):
        raise HTTPException(409, out.get("reason", "already decided"))
    rt.announce_case(item["case_id"], "case_updated")
    return out


# --- customer correspondence ----------------------------------------------

@router.post("/messages/{message_id}/send")
def send_message(message_id: str, body: SendReply):
    """The only way anything reaches the customer, simulated outbox."""
    rt = get_runtime()
    msg = rt.store.one("SELECT * FROM messages WHERE message_id=?",
                       (message_id,))
    if msg is None:
        raise HTTPException(404, f"no message {message_id}")
    out = review.approve_and_send(rt.store, msg["case_id"], message_id,
                                  body.body, body.authorizer, body.review_override)
    if not out.get("ok"):
        raise HTTPException(409, out.get("reason", "could not send"))
    rt.announce_case(msg["case_id"], "case_updated")
    return out


@router.post("/messages/{message_id}/reject")
def reject_message(message_id: str, body: Authorised):
    rt = get_runtime()
    msg = rt.store.one("SELECT * FROM messages WHERE message_id=?",
                       (message_id,))
    if msg is None:
        raise HTTPException(404, f"no message {message_id}")
    if msg["status"] != "draft":
        raise HTTPException(409, f"{message_id} is already {msg['status']}")
    out = review.reject_draft(rt.store, msg["case_id"], message_id,
                              body.authorizer, note=body.note)
    rt.announce_case(msg["case_id"], "case_updated")
    return out


@router.post("/messages/{message_id}/regenerate")
def regenerate_message(message_id: str, body: Regenerate):
    """Reflexion with a person in it: the correction drives the new draft.

    If the agent cannot produce a reply that passes source verification the
    new draft is still returned, still as a *draft*, and the response says it
    was not verified. It is never sent on the strength of having been
    regenerated.
    """
    rt = get_runtime()
    msg = rt.store.one("SELECT * FROM messages WHERE message_id=?",
                       (message_id,))
    if msg is None:
        raise HTTPException(404, f"no message {message_id}")
    out = review.regenerate_reply(rt.ops, msg["case_id"], message_id,
                                  body.feedback, body.authorizer)
    # A replacement that failed validation is still returned as a draft (its
    # own validation record blocks sending); a stopped revision is an error.
    if not out.get("ok") and not out.get("replaces"):
        raise HTTPException(400, out.get("summary") or out.get("reason") or "could not revise the draft")
    rt.announce_case(msg["case_id"], "case_updated")
    return out


# --- reference -------------------------------------------------------------

@router.get("/reference")
def reference():
    """Vocabularies the frontend renders, served rather than duplicated."""
    from ... import presentation
    return {
        "statuses": list(caseflow.STATUSES),
        "priorities": [triage.CRITICAL, triage.HIGH, triage.MEDIUM,
                       triage.LOW],
        "ai_states": [caseflow.QUEUED, caseflow.PROCESSING,
                      caseflow.COMPLETED, caseflow.FAILED,
                      caseflow.RETRIABLE],
        "outcomes": [presentation.COMPLETED, presentation.NO_CONCERN,
                     presentation.MATCH_FOUND, presentation.NO_MATCH,
                     presentation.INFO_MISSING, presentation.UNCERTAIN,
                     presentation.BLOCKED, presentation.HUMAN_REQUIRED,
                     presentation.APPROVED, presentation.REJECTED,
                     presentation.EXECUTED, presentation.FAILED,
                     presentation.NOT_APPLICABLE],
        "decision_classes": presentation.CLASS_LABEL,
        "panels": [presentation.PANEL_FINDINGS, presentation.PANEL_ATTENTION,
                   presentation.PANEL_NEXT],
    }
