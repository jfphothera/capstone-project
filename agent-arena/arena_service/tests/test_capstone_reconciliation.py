"""Arena 2.5.1 adapter, Capstone rc.25 A1: a stopped Testing Lab worker's open work.

Capstone writes, after a CONFIRMED worker exit, one inference.interrupted / task.interrupted row per assignment the
trial still owned (capstone_rag/lab_reconciliation.py, detail.reconciliation), and later, separately, an
inference.late_result when the Gateway finished the call anyway. Rows here have the shape closure_rows() writes.
"""
import json
from datetime import datetime, timedelta, timezone

from arena_service.adapters.capstone.translate import Translator

RUN = "RUN-35e17217364d4702aff4662dfc177ee6"
_seq = [0]


def row(kind, **fields):
    _seq[0] += 1
    base = {"sequence": _seq[0], "event_id": f"ARENA-T{_seq[0]}", "timestamp": (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
            "event_type": kind, "case_id": "CASE-1", "workflow_id": RUN, "task_id": None, "agent_id": "lab_reviewer",
            "sender_agent_id": None, "receiver_agent_id": None, "correlation_id": None, "inference_node": None,
            "execution_node": None, "tool_node": None, "model_id": "qwen3:4b", "task_status": None,
            "summary": "EXPERIMENT review call", "detail": json.dumps({"route_role": "reviewer"})}
    base.update(fields)
    return base


def interruption(kind, **fields):
    rec = {"version": "capstone.lab-reconciliation/1", "exit_kind": "timeout_kill", "reason": "worker hard timeout",
           "worker_exit_confirmed_at": "2026-11-18T16:11:27+00:00", "trial_id": RUN, "attempt_id": "1",
           "request_id": fields.get("correlation_id"), "gateway_request_id": "REQ-1", "logical_agent_id": fields.get("agent_id", "lab_reviewer"),
           "inference_outcome": "not_observed_by_caller" if kind.startswith("inference") else None}
    return row(kind, task_status="interrupted", summary="EXPERIMENT Assignment interrupted: worker timeout kill",
               detail=json.dumps({"reconciliation": rec, "caller_interrupted": True, "route_role": "reviewer"}), **fields)


def states(events):
    return [(e["assignment_id"], e["type"], e.get("state"), e.get("to_location")) for e in events if e["type"].startswith("assignment.")]


def test_an_interrupted_call_on_a_head_ends_canceled_there_never_completed_or_failed():
    t = Translator()
    call = "CALL-5f51088294624b689e49ace80ad7d366"
    t.translate(row("inference.queued", correlation_id=call, inference_node="head-192-0-2-7"))
    t.translate(row("inference.started", correlation_id=call, inference_node="head-192-0-2-7"))
    out = t.translate(interruption("inference.interrupted", correlation_id=call, inference_node="head-192-0-2-7"))
    kinds = states(out)
    assert [(k[1], k[2]) for k in kinds] == [("assignment.state", "canceled")]
    final = [e for e in out if e["type"] == "assignment.state"][0]
    assert final["label"] == "Interrupted timeout kill" and "visual_action" not in final
    late = t.translate(row("inference.late_result", correlation_id=call, task_status="completed",
                           detail=json.dumps({"late_result": {"linked_interruption_event_id": "ARENA-RECON-x"}})))
    assert late == []                                           # Capstone keeps it; never replayed, never a pass
    again = t.translate(interruption("inference.interrupted", correlation_id=call))
    assert again == [] and t.skipped["inference.interrupted:unknown_or_finished"] == 1   # a re-flushed outbox


def test_an_interrupted_queued_task_is_canceled():
    t = Translator()
    t.translate(row("task.queued", task_id="TASK-7", agent_id="resolution_planning", summary="EXPERIMENT plan",
                    detail=None))
    out = t.translate(interruption("task.interrupted", task_id="TASK-7", agent_id="resolution_planning"))
    assert states(out) == [("TASK-7", "assignment.state", "canceled", None)]


def test_a_review_request_closes_on_its_resolved_row_or_a_worker_stop():
    t = Translator()
    rid = f"review:{RUN}:0123456789ab"
    opened = t.translate(row("human_review.requested", agent_id="coordinator", task_id=rid, correlation_id=rid, task_status="waiting",
                             detail=json.dumps({"items": [{"id": "DRAFT-2", "kind": "email"}]})))
    assert (rid, "assignment.state", "waiting", None) in states(opened)
    again = t.translate(row("human_review.requested", agent_id="coordinator", task_id=rid, task_status="waiting"))   # a resume: same id
    assert not any(s[2] == "canceled" for s in states(again))
    done = t.translate(row("human_review.resolved", agent_id="coordinator", task_id=rid, task_status="approved",
                           detail=json.dumps({"outcome": "approved", "items": [{"id": "DRAFT-2", "kind": "email", "status": "approved"}]})))
    final = [e for e in done if e["type"] == "assignment.state"]
    assert [(e["assignment_id"], e["state"], e["label"]) for e in final] == [(rid, "canceled", "Review approved")]
    assert RUN not in t.reviews
    rid2 = f"review:{RUN}:fedcba987654"
    t.translate(row("human_review.requested", agent_id="coordinator", task_id=rid2, task_status="waiting"))
    stop = t.translate(interruption("task.interrupted", task_id=rid2, agent_id="coordinator"))
    assert states(stop) == [(rid2, "assignment.state", "canceled", None)]
    late = t.translate(row("human_review.resolved", agent_id="coordinator", task_id=rid2, task_status="superseded"))
    assert late == [] and t.skipped["human_review.resolved:unknown_or_finished"] == 1


def test_an_unknown_inference_row_is_skipped_not_taken_as_completed():
    t = Translator()
    out = t.translate(row("inference.retrying", correlation_id="CALL-9", inference_node="head-192-0-2-3"))
    assert states(out) == [] and t.skipped["inference.retrying:unknown_inference_type"] == 1
    assert "CALL-9" not in t.assignments


def test_the_hub_accepts_the_events_and_counts_no_result(env):
    """End to end through the event service's contract checks: the interruption is accepted and is neither pass nor fail."""
    producer = env.app("capstone", "Ticketing")
    env.head("head-192-0-2-7")
    t = Translator(declare_all=True)
    call = "CALL-e2e"
    events = []
    for r in (row("inference.queued", correlation_id=call, inference_node="head-192-0-2-7"),
              row("inference.started", correlation_id=call, inference_node="head-192-0-2-7"),
              interruption("inference.interrupted", correlation_id=call, inference_node="head-192-0-2-7")):
        events += t.translate(r)
    for e in events:
        e["occurred_at"] = e["occurred_at"].replace("+00:00", "Z") if e["occurred_at"].endswith("+00:00") else e["occurred_at"]
    answer = producer.publish(events)
    bad = [r for r in answer["results"] if r["status"] not in ("accepted", "recorded")]
    assert not bad, bad
    key = next(e["assignment_id"] for e in events if e["type"] == "assignment.state" and e.get("state") == "canceled")
    row_ = env.store._one("SELECT state,location_id FROM assignments WHERE assignment_id=?", (key,))
    assert row_ == {"state": "canceled", "location_id": "head:head-192-0-2-7"}
    assert env.store.result_counts() == {}
    # The admin hub's head traffic board: the trip shows "canceled", not "unknown" (lease lapsed) and not "failed".
    from arena_service.admin_console import Console
    board = Console({"host": "127.0.0.1", "port": 1, "db_path": env.config["db_path"]}).traffic()
    assert [t["status"] for t in board["arrivals"]] == ["canceled"] and board["departures"] == []
