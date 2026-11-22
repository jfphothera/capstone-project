"""Quality and safety review.

Reported injury, possible adverse events, device hazards without injury, QC
discrepancies and QA document concerns, with the customer's own words kept
beside the classification.

No reportability determination is made here. The workflow is a *simulated*
QA and regulatory review: a person records a determination, it changes the
case classification and the QA record, and nothing is filed anywhere.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import caseflow, review, triage
from .. import views
from ..runtime import get_runtime
from ..schemas import Determination

router = APIRouter(prefix="/api", tags=["quality"])


@router.get("/quality")
def quality_review():
    rt = get_runtime()
    return views.quality(rt.store)


@router.get("/notifications")
def notifications(status: str = ""):
    rt = get_runtime()
    sql = "SELECT * FROM qa_notifications WHERE run_id IS NULL"
    params: list = []
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT 300"
    return {"notifications": rt.store.query(sql, tuple(params))}


@router.post("/notifications/{notification_id}/determination")
def determine(notification_id: str, body: Determination):
    """Record the human decision on an uncertain adverse event.

    The determination is consequential: it changes how the case is
    classified and is written to the QA record with the name of whoever
    made it. It does not decide reportability.
    """
    rt = get_runtime()
    note = rt.store.one(
        "SELECT * FROM qa_notifications WHERE notification_id=?",
        (notification_id,))
    if note is None:
        raise HTTPException(404, f"no notification {notification_id}")
    if note["status"] == "determined":
        raise HTTPException(409, f"{notification_id} already has a "
                                 f"determination")
    out = review.determine_adverse_event(rt.store, notification_id,
                                        body.determination, body.authorizer,
                                        rationale=body.rationale or body.note)

    # A determination that confirms harm raises the case classification and
    # keeps it with a person; one that rules it out records that too and
    # releases the case back to the ordinary workflow.
    case_id = note["case_id"]
    if case_id:
        rt.announce_case(case_id, "case_updated")
    return {**out, "confirmed": body.determination}
