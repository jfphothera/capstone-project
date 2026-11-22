"""Trends, improvements and the audit history.

The numbers come from `analytics.Analytics`, which already separates live
business counts from evaluation replays by `run_id`. That separation is
surfaced rather than hidden: a trends view that counted a replay as ten more
unhappy customers would be worse than no trends view.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import analytics as analytics_mod, memory as memory_mod, review
from .. import views
from ..runtime import get_runtime
from ..schemas import ProposalReview

router = APIRouter(prefix="/api", tags=["analytics"])


@router.get("/analytics")
def report(run_id: str = ""):
    """Live operational metrics, with denominators."""
    rt = get_runtime()
    an = analytics_mod.Analytics(rt.store, run_id=run_id or None)
    data = an.report()
    data["proposed_actions"] = an.proposed_actions()
    data["scope"] = {
        "run_id": run_id or None,
        "note": ("Live cases only. Evaluation replays carry a run_id and are "
                 "counted separately, so a replay cannot inflate these "
                 "figures." if not run_id else
                 "One evaluation run. These are repeated evaluations of "
                 "stored scenarios, not unique customer cases."),
        "unique_cases": rt.store.one(
            "SELECT COUNT(*) c FROM cases WHERE run_id IS NULL")["c"],
        "evaluation_case_rows": rt.store.one(
            "SELECT COUNT(*) c FROM cases WHERE run_id IS NOT NULL")["c"],
        "evaluation_runs": rt.store.query(
            "SELECT run_id, started_at, finished_at, label, mode "
            "FROM eval_runs ORDER BY started_at DESC LIMIT 20"),
    }
    data["queue"] = rt.ops.queue.snapshot()
    durations = rt.store.one(
        "SELECT COUNT(*) n, "
        "AVG((julianday(a.at)-julianday(c.created_at))*86400.0) mean_s, "
        "MAX((julianday(a.at)-julianday(c.created_at))*86400.0) max_s "
        "FROM audit_events a JOIN cases c ON c.case_id=a.case_id "
        "WHERE a.event_type='ai_pre_investigation_completed' "
        "AND c.run_id IS NULL") or {}
    data["processing"] = {
        "completed": durations.get("n") or 0,
        "mean_seconds": (round(durations["mean_s"], 2)
                         if durations.get("mean_s") is not None else None),
        "max_seconds": (round(durations["max_s"], 2)
                        if durations.get("max_s") is not None else None),
        "basis": "arrival to completed investigation",
    }
    return data


@router.get("/proposals")
def proposals(status: str = ""):
    rt = get_runtime()
    sql = "SELECT * FROM improvement_proposals WHERE 1=1"
    params: list = []
    if status:
        sql += " AND review_status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT 200"
    rows = rt.store.query(sql, tuple(params))
    mem = _memory(rt)
    return {
        "proposals": rows,
        "memory_pending": [r.as_dict() if hasattr(r, "as_dict") else r
                           for r in mem.pending()],
        "memory_approved": [r.as_dict() if hasattr(r, "as_dict") else r
                            for r in mem.approved()][-25:],
        "boundary": ("An approved proposal changes how the operation works. "
                     "It can never override a controlled QMS document, the "
                     "model/revision compatibility mapping or the revision "
                     "register."),
    }


def _memory(rt) -> "memory_mod.ReviewedMemory":
    """The agent's own reviewed-memory store, not a second one.

    `caseflow.make_agent` points the agent's JSONL stores at a scratch
    directory so they do not compete with SQLite as a record of escalations.
    Reading from the default path here would show an empty, unrelated queue.
    """
    agent = getattr(rt.flow, "agent", None)
    store = getattr(agent, "reviewed_memory", None)
    return store or memory_mod.ReviewedMemory()


@router.post("/memory/{memory_id}/review")
def review_memory(memory_id: str, body: ProposalReview):
    """Approve or reject a reusable resolution the agent proposed.

    An approved memory is re-checked for applicability at recall time
    regardless, so approving one does not make it authoritative for a device
    it was never valid for.
    """
    rt = get_runtime()
    mem = _memory(rt)
    out = (mem.approve(memory_id, body.authorizer) if
           body.decision == "approved" else
           mem.reject(memory_id, body.authorizer))
    if out is None:
        raise HTTPException(404, f"no memory {memory_id}")
    rt.store.audit(f"reviewed_memory_{body.decision}", actor=body.authorizer,
                   summary=f"{memory_id} {body.decision}",
                   detail={"note": body.note})
    return {"ok": True, "memory": out.as_dict()}


@router.post("/proposals/{proposal_id}/review")
def review_proposal(proposal_id: str, body: ProposalReview):
    rt = get_runtime()
    out = review.review_proposal(rt.store, proposal_id, body.decision,
                                body.authorizer, note=body.note)
    if not out.get("ok"):
        raise HTTPException(409 if out.get("duplicate") else 404,
                            out.get("reason", "could not review"))
    return out


@router.get("/audit")
def audit(limit: int = 200, case_id: str = "", event_type: str = "",
          actor: str = ""):
    rt = get_runtime()
    return views.audit_feed(rt.store, limit=limit, case_id=case_id,
                            event_type=event_type, actor=actor)
