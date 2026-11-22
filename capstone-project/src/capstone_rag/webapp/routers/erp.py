"""Simulated ERP movements, RMAs, shipments and tracking.

`propose_movement` authorises nothing and is not exposed here as an action -
proposals come out of the investigation. What is exposed is the decision, and
the decision needs a name on it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import erp_sim, review
from .. import views
from ..runtime import get_runtime
from ..schemas import Authorised, ShipmentAdvance, MovementApproval

router = APIRouter(prefix="/api", tags=["erp"])


@router.get("/rmas")
def list_rmas(status: str = ""):
    rt = get_runtime()
    return views.movements(rt.store, status=status)


@router.get("/rmas/{rma_id}")
def get_rma(rma_id: str):
    rt = get_runtime()
    row = rt.store.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,))
    if row is None:
        raise HTTPException(404, f"no RMA {rma_id}")
    return views._rma(rt.store, row)


@router.post("/rmas/{rma_id}/approve")
def approve_rma(rma_id: str, body: MovementApproval):
    """The real gate. Nothing is created until this call succeeds."""
    rt = get_runtime()
    res = rt.erp.approve(rma_id, body.authorizer, note=body.note, force_failure=body.simulate_failure)
    row = rt.store.one('SELECT case_id FROM rmas WHERE rma_id=?', (rma_id,))
    if row:
        rt.announce_case(row['case_id'], 'case_updated')
    if not res.ok:
        # A duplicate is a conflict, not a server error, and the existing
        # record is the correct answer to a repeated click.
        raise HTTPException(409 if res.duplicate else 400, res.reason)
    rma = rt.store.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,))
    if rma and rma["case_id"]:
        rt.announce_case(rma["case_id"], "case_updated")
    return {**res.as_dict(), "rma": views._rma(rt.store, rma)}


@router.post("/rmas/{rma_id}/reject")
def reject_rma(rma_id: str, body: Authorised):
    rt = get_runtime()
    res = rt.erp.reject(rma_id, body.authorizer, note=body.note)
    if not res.ok:
        raise HTTPException(409 if res.duplicate else 400, res.reason)
    rma = rt.store.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,))
    if rma and rma["case_id"]:
        rt.announce_case(rma["case_id"], "case_updated")
    return {**res.as_dict(), "rma": views._rma(rt.store, rma)}


@router.post("/rmas/{rma_id}/retry")
def retry_rma(rma_id: str, body: Authorised):
    """Recovery for a failed execution. Still needs a named person."""
    rt = get_runtime()
    rma = rt.store.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,))
    if rma is None:
        raise HTTPException(404, f"no RMA {rma_id}")
    if rma["status"] != erp_sim.FAILED:
        raise HTTPException(409, f"{rma_id} is {rma['status']}; only a failed "
                                 f"movement can be retried")
    # The retry must rest on this RMA's existing, unrevoked approval; it is
    # recorded as its own named approval row (erp_sim.retry).
    res = rt.erp.retry(rma_id, body.authorizer, body.note or "")
    if not res.ok and "no current recorded approval" in (res.reason or ""):
        raise HTTPException(409, res.reason)
    if rma["case_id"]:
        rt.announce_case(rma["case_id"], "case_updated")
    return {**res.as_dict(),
            "rma": views._rma(rt.store, rt.store.one(
                "SELECT * FROM rmas WHERE rma_id=?", (rma_id,)))}


@router.get("/shipments")
def list_shipments():
    rt = get_runtime()
    rows = rt.store.query(
        "SELECT * FROM sim_shipments WHERE run_id IS NULL "
        "ORDER BY created_at DESC LIMIT 300")
    for s in rows:
        s["events"] = rt.store.query(
            "SELECT * FROM shipment_events WHERE shipment_id=? ORDER BY id",
            (s["shipment_id"],))
    return {"shipments": rows}


@router.get("/shipments/{tracking_number}")
def track(tracking_number: str):
    rt = get_runtime()
    found = rt.erp.track(tracking_number)
    if found is None:
        raise HTTPException(404, f"no shipment {tracking_number}")
    return found


@router.get("/shipments/{tracking_number}/label", response_model=None)
def label(tracking_number: str):
    """The sample label, as text. Clearly marked invalid in the file itself."""
    rt = get_runtime()
    ship = rt.store.one(
        "SELECT * FROM sim_shipments WHERE tracking_number=?",
        (tracking_number,))
    if ship is None:
        raise HTTPException(404, f"no shipment {tracking_number}")
    path = ship["label_path"]
    try:
        with open(path, encoding="utf-8") as fh:
            return {"tracking_number": tracking_number, "path": path,
                    "text": fh.read()}
    except OSError as exc:
        raise HTTPException(404, f"label file unavailable: {exc}") from exc


@router.post("/shipments/{tracking_number}/advance")
def advance(tracking_number: str, body: ShipmentAdvance):
    """A simulated carrier scan. Events are recorded, never invented."""
    rt = get_runtime()
    try:
        from ...operations_workflow import record_carrier_event
        result = record_carrier_event(rt.store,rt.erp,tracking_number,body.status,body.location,body.note)
        out=result['shipment']
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if out is None:
        raise HTTPException(404, f"no shipment {tracking_number}")
    if out.get("case_id"):
        rt.announce_case(out["case_id"], "case_updated")
    return result
