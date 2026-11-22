"""Ticketing adapter against the 133 real Phase 4.2 distributed-trace events.

The fixture is real persisted Capstone output (isolated database, real
inference on heads .3/.6/.7), re-timed to the present so the service's
freshness window accepts it. The adapter and service run on this workstation.
"""
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arena_service import appearance
from arena_service.adapters.capstone.runner import Adapter
from arena_service.adapters.capstone.translate import DECLARED, LAB_PARTICIPANTS, SUB_ROLE_ACTIONS, Translator
from arena_service.client import Client

TRACE = Path(__file__).resolve().parent / "fixtures" / "distributed_case_trace.json"   # real Capstone Phase 4.2 output
COLUMNS = ("sequence", "event_id", "schema_version", "timestamp", "event_type", "case_id", "workflow_id", "task_id",
           "agent_id", "sender_agent_id", "receiver_agent_id", "correlation_id", "logical_home_node", "execution_node",
           "tool_node", "inference_node", "model_id", "endpoint_id", "task_status", "summary", "evidence_refs", "detail")


def trace_rows():
    rows = json.loads(TRACE.read_text(encoding="utf-8"))["arena_events"]
    latest = max(datetime.fromisoformat(r["timestamp"]) for r in rows)
    shift = datetime.now(timezone.utc) - timedelta(seconds=5) - latest
    for row in rows:
        row["timestamp"] = (datetime.fromisoformat(row["timestamp"]) + shift).isoformat()
    return rows


def capstone_db(path, rows):
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE arena_events ({', '.join(c + ' TEXT' if c != 'sequence' else 'sequence INTEGER PRIMARY KEY' for c in COLUMNS)})")
    db.execute("CREATE TABLE agent_registry (agent_id TEXT PRIMARY KEY, display_name TEXT, role TEXT, color TEXT, "
               "logical_home_node TEXT, retired INTEGER, created_at TEXT, retired_at TEXT)")
    for row in rows:
        db.execute(f"INSERT INTO arena_events VALUES ({','.join('?' * len(COLUMNS))})",
                   tuple(row[c] if not isinstance(row[c], (dict, list)) else json.dumps(row[c]) for c in COLUMNS))
        if row["event_type"] == "agent.registered":
            detail = json.loads(row["detail"]) if isinstance(row["detail"], str) else row["detail"]
            db.execute("INSERT INTO agent_registry VALUES (?,?,?,?,?,0,?,NULL)",
                       (row["agent_id"], row["summary"].replace(" registered", ""), detail.get("role"), "x",
                        "coordinator-192.0.2.1", row["timestamp"]))
    db.commit()
    db.close()


@pytest.fixture
def ticketing(env, tmp_path):
    rows = trace_rows()
    db = tmp_path / "operations.db"
    capstone_db(db, rows)
    client = env.app("capstone", "Ticketing")
    for n in (3, 6, 7):
        env.head(f"head-192-0-2-{n}")
    config = {"capstone_db": str(db), "checkpoint_file": str(tmp_path / "checkpoint.json"), "start_from": 0}
    return env, db, config, client, rows


def run_all(adapter):
    adapter.sync_registry()
    while adapter.step():
        pass


def moves(env, location):
    return [(e["result"]["agent_id"], e["from_location"], e["to_location"])
            for e in env.store.events_after(0, view="location:" + location, limit=1000)["events"]
            if e["type"] == "assignment.moved" and e["applied"]]


def test_real_trace_dispatch_and_return(ticketing):
    env, db, config, client, rows = ticketing
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    adapter = Adapter(config, client)
    run_all(adapter)
    assert adapter.rejected == 0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before          # Capstone DB never written
    home = "orch:capstone/main"
    assert moves(env, "head:head-192-0-2-3") == [("erp_research", home, "head:head-192-0-2-3"),
                                                 ("erp_research", "head:head-192-0-2-3", home)]
    assert moves(env, "head:head-192-0-2-6") == [("ticket_interpretation", home, "head:head-192-0-2-6"),
                                                 ("ticket_interpretation", "head:head-192-0-2-6", home)]
    assert moves(env, "head:head-192-0-2-7") == [("independent_validation", home, "head:head-192-0-2-7"),
                                                 ("independent_validation", "head:head-192-0-2-7", home)]
    on3 = [e for e in env.store.events_after(0, view="location:head:head-192-0-2-3", limit=1000)["events"]
           if e["result"] and "assignment_key" in e["result"] and e["result"]["agent_id"] == "erp_research"]
    assert [e["result"]["state"] for e in on3][:3] == ["waiting", "executing", "executing"]
    assert on3[1]["payload"]["visual_action"] == "inference"
    assert on3[0]["payload"]["source_ref"].endswith("E1:task_from_workflow_id")
    assert on3[0]["result"]["task_id"] == "EXEC-TASK-db2df8dd5b3c40e49285dd34bd21fe56"
    for n in (3, 6, 7):                                                  # nobody left on a head after work resumed
        assert env.store.snapshot(f"location:head:head-192-0-2-{n}")["assignments"] == []
    snap = env.store.snapshot()
    names = {a["agent_id"]: a["display_name"] for a in snap["agents"]}
    assert names["independent_validation"] == "Ticketing Independent Validator"
    assert len(snap["agents"]) == len(DECLARED), "every declared agent is in its room from the start (Arena 2.5.0)"
    review = [a for a in snap["assignments"] if a["wait_reason"] == "human"]
    assert [(a["agent_id"], a["state"]) for a in review] == [("coordinator", "waiting")]


def test_task_started_on_head_worker_stays_at_orchestrator(ticketing):
    env, db, config, client, rows = ticketing
    run_all(Adapter(config, client))
    started = [e for e in env.store.events_after(0, limit=1000)["events"]
               if e["payload"].get("source_event_type") == "task.started"
               and e["payload"].get("assignment_id") == "EXEC-TASK-db2df8dd5b3c40e49285dd34bd21fe56"]
    assert started and all(e["to_location"] == "orch:capstone/main" for e in started)


def test_restart_mid_stream_is_lossless_and_duplicate_free(ticketing, tmp_path):
    env, db, config, client, rows = ticketing
    first = Adapter(config, client)
    first.sync_registry()
    first.step(limit=50)
    second = Adapter(config, client)                    # simulated restart from the durable checkpoint
    assert second.sequence == 50
    while second.step(limit=17):
        pass
    split = {a["assignment_key"]: (a["location_id"], a["state"]) for a in env.store.snapshot()["assignments"]}
    statuses = [e["applied"] for e in env.store.events_after(0, limit=1000)["events"]]
    # Re-running the whole trace again is idempotent: every event is a duplicate.
    replay = Adapter(dict(config, checkpoint_file=str(tmp_path / "fresh.json")), client)
    replay.sync_registry()
    while replay.step():
        pass
    assert len(env.store.events_after(0, limit=1000)["events"]) == len(statuses)
    assert {a["assignment_key"]: (a["location_id"], a["state"]) for a in env.store.snapshot()["assignments"]} == split


def test_service_unavailable_keeps_checkpoint(ticketing):
    env, db, config, client, rows = ticketing
    down = Client("http://127.0.0.1:9", client.credential_id, "x", timeout=2)
    adapter = Adapter(config, down)
    with pytest.raises(OSError):
        adapter.step()
    assert json.loads(Path(config["checkpoint_file"]).read_text())["sequence"] == 0


def test_first_start_does_not_replay_history(ticketing):
    env, db, config, client, rows = ticketing
    adapter = Adapter(dict(config, start_from="now"), client)
    adapter.sync_registry()
    assert adapter.step() == 0 and adapter.sequence == len(rows)
    assert env.store.snapshot()["assignments"] == []


def test_heartbeat_only_while_capstone_is_healthy(ticketing):
    env, db, config, client, rows = ticketing
    adapter = Adapter(dict(config, capstone_health_url="http://127.0.0.1:9/api/health"), client)
    assert adapter.heartbeat() is False
    orch = env.store.registry()["orchestrators"][0]
    assert orch["producer_state"] == "offline"


def test_unknown_types_are_skipped_not_guessed():
    t = Translator()
    out = t.translate({"sequence": 1, "event_id": "X", "timestamp": "2026-10-31T00:00:00+00:00",
                       "event_type": "quantum.flux", "agent_id": "a", "task_id": "T"})
    assert out == [] and t.skipped == {"quantum.flux": 1}


class Flaky:
    """Real client that drops every third publish mid-run, like a service outage."""

    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def publish(self, events):
        self.calls += 1
        if self.calls % 3 == 0:
            raise ConnectionResetError("simulated outage")
        return self.inner.publish(events)


def test_publish_failures_never_half_apply_translation(ticketing):
    env, db, config, client, rows = ticketing
    flaky = Flaky(client)
    adapter = Adapter(config, flaky)
    for _ in range(200):
        try:
            adapter.sync_registry()
            if not adapter.step(limit=5):
                break
        except OSError:
            adapter = Adapter(config, flaky) if flaky.calls % 2 else adapter     # sometimes also restart
    assert adapter.rejected == 0
    assert moves(env, "head:head-192-0-2-3") == [("erp_research", "orch:capstone/main", "head:head-192-0-2-3"),
                                                 ("erp_research", "head:head-192-0-2-3", "orch:capstone/main")]


def _rc4_row(seq, kind, **fields):
    """A Capstone 6.0.0-rc.4 remote_services row (tool.* with execution_node and tool_node naming the service's head)."""
    return dict({"sequence": seq, "event_id": f"R{seq}", "timestamp": "2026-11-07T21:00:00+00:00", "event_type": kind,
                 "agent_id": "qms_research"}, **fields)


def test_qms_and_erp_calls_travel_to_their_heads_and_back():
    t = Translator()
    t.translate(_rc4_row(1, "task.started", task_id="T1"))
    head3 = {"execution_node": "head-192-0-2-3", "tool_node": "head-192-0-2-3"}
    out = t.translate(_rc4_row(2, "tool.started", task_id="T1", correlation_id="T1", **head3))
    assert [(e["type"], e["from_location"], e["to_location"], e["visual_action"]) for e in out] == [
        ("assignment.moved", "orch:capstone/main", "head:head-192-0-2-3", "tool_use")]
    out = t.translate(_rc4_row(3, "tool.completed", task_id="T1", correlation_id="T1", **head3))
    assert [(e["type"], e["from_location"], e["to_location"]) for e in out] == [
        ("assignment.moved", "head:head-192-0-2-3", "orch:capstone/main")]
    assert t.assignments["T1"]["terminal"] is False, "the task goes on at the orchestrator"
    # A failed ERP call returns too, drawn as error handling.
    t.translate(_rc4_row(4, "tool.started", task_id="T1", correlation_id="T1", execution_node="head-192-0-2-4", tool_node="head-192-0-2-4"))
    out = t.translate(_rc4_row(5, "tool.failed", task_id="T1", correlation_id="T1", execution_node="head-192-0-2-4", tool_node="head-192-0-2-4"))
    assert [(e["to_location"], e["visual_action"]) for e in out] == [("orch:capstone/main", "error_handling")]


def test_service_call_without_a_task_is_its_own_trip():
    t = Translator()
    head4 = {"execution_node": "head-192-0-2-4", "tool_node": "head-192-0-2-4"}
    out = t.translate(_rc4_row(1, "tool.started", **head4))          # Capstone had no task context: no task, no correlation
    kinds = [e["type"] for e in out]
    assert kinds == ["agent.registered", "assignment.created", "assignment.moved"] and out[-1]["to_location"] == "head:head-192-0-2-4"
    key = out[1]["assignment_id"]
    out = t.translate(_rc4_row(2, "tool.completed", **head4))        # paired with its start by agent and head
    assert [(e["type"], e.get("to_location"), e.get("state")) for e in out] == [
        ("assignment.moved", "orch:capstone/main", "executing"), ("assignment.state", None, "completed")]
    assert all(e["assignment_id"] == key for e in out) and t.open_tools == {}
    # The pairing survives a restart (the adapter keeps the translator's state).
    t2 = Translator(state=t.state())
    t2.translate(_rc4_row(3, "tool.started", **head4))
    t3 = Translator(state=t2.state())
    assert [e["type"] for e in t3.translate(_rc4_row(4, "tool.completed", **head4))] == ["assignment.moved", "assignment.state"]


def test_tools_at_the_orchestrator_stay_home():
    t = Translator()
    t.translate(_rc4_row(1, "task.started", task_id="T1"))
    out = t.translate(_rc4_row(2, "tool.completed", task_id="T1", tool_node="orch:capstone/main"))
    assert [e["type"] for e in out] == ["assignment.state"] and t.assignments["T1"]["location"] == "orch:capstone/main"


def test_heartbeats_renew_the_lease_of_known_running_work():
    t, stamp = Translator(), "2026-10-31T00:00:00+00:00"
    row = lambda seq, kind, **f: dict({"sequence": seq, "event_id": f"E{seq}", "timestamp": stamp, "event_type": kind,
                                       "agent_id": "intake"}, **f)
    t.translate(row(1, "task.started", task_id="T1"))
    out = t.translate(row(2, "task.heartbeat", task_id="T1"))
    assert [(e["type"], e["assignment_id"], e["lease_seconds"]) for e in out] == [("assignment.heartbeat", "T1", 120)]
    out = t.translate(row(3, "task.heartbeat", task_id="T1", detail=json.dumps({"lease_seconds": 300})))
    assert out[0]["lease_seconds"] == 300
    assert t.translate(row(4, "task.heartbeat", task_id="NOPE")) == []        # unknown work is never invented
    t.translate(row(5, "task.completed", task_id="T1"))
    assert t.translate(row(6, "task.heartbeat", task_id="T1")) == []          # finished work stays finished
    assert t.skipped == {"task.heartbeat:unknown_work": 2}


RUN = "RUN-9276628e56ae48b0bc1cc35a0d82655f"          # an agentic Testing Lab run (EXP-8608251d..., 2026-11-18)


def _lab_rows(stamp):
    """Capstone 6.0.0-rc.23 mirrored rows of one Testing Lab run, shaped like the 2026-11-18 evidence: a workflow agent's
    task and inference on qwen3.5:4b, then two reviewer (qwen3:4b) calls, one completed on head .5 and one that timed
    out on head .3 (Gateway REQ-1bfb6acd... "timed out"). Every mirrored row carries the run id as workflow_id."""
    rows, seq = [], 0

    def add(kind, agent, **f):
        nonlocal seq
        seq += 1
        detail = f.pop("detail", {})
        rows.append({**{c: None for c in COLUMNS}, "sequence": seq, "event_id": f"ARENA-LAB-{seq:02d}", "schema_version": "1.0",
                     "timestamp": stamp, "event_type": kind, "agent_id": agent, "workflow_id": RUN, "case_id": "CASE-LAB",
                     "summary": "EXPERIMENT " + f.pop("summary", kind), "evidence_refs": "[]", "detail": json.dumps(detail), **f})
    add("task.started", "ticket_interpretation", task_id="EXEC-TASK-LAB1", correlation_id="EXEC-TASK-LAB1")
    add("inference.queued", "ticket_interpretation", task_id="EXEC-TASK-LAB1", correlation_id="CALL-sut1", model_id="qwen3.5:4b")
    add("inference.started", "ticket_interpretation", task_id="EXEC-TASK-LAB1", correlation_id="CALL-sut1", model_id="qwen3.5:4b",
        inference_node="head-192-0-2-2")
    add("inference.completed", "ticket_interpretation", task_id="EXEC-TASK-LAB1", correlation_id="CALL-sut1", model_id="qwen3.5:4b",
        inference_node="head-192-0-2-2")
    add("task.completed", "ticket_interpretation", task_id="EXEC-TASK-LAB1", correlation_id="EXEC-TASK-LAB1")
    add("agent.registered", "lab_reviewer", correlation_id="lab_reviewer", summary="Testing Lab Reviewer registered",
        task_status="registered", detail={"role": "lab-reviewer"})
    for call, head, end, key in (("CALL-c9391fa614a040b3bec0fba5f4c3a1bd", "head-192-0-2-5", "inference.completed", "draft:MSG-LAB1"),
                                 ("CALL-9d2b8dc5ee56443199774eac36a03ecd", "head-192-0-2-3", "inference.failed", "qa:QA-LAB1")):
        corr = {"route_role": "lab_reviewer", "logical_agent_status": "known", "request_id": call, "task_id": key, "workflow_id": RUN}
        add("inference.queued", "lab_reviewer", task_id=key, correlation_id=call, model_id="qwen3:4b", endpoint_id="gateway",
            task_status="queued", summary="lab reviewer inference queued", detail=corr)
        add("inference.started", "lab_reviewer", task_id=key, correlation_id=call, model_id="qwen3:4b", inference_node=head,
            endpoint_id=head, task_status="running", summary="Gateway dispatched model execution",
            detail={**corr, "gateway_request_id": "REQ-" + call[5:]})
        add(end, "lab_reviewer", task_id=key, correlation_id=call, model_id="qwen3:4b", inference_node=head, endpoint_id=head,
            task_status=end.split(".")[1], summary="lab reviewer inference " + end.split(".")[1], detail=corr)
    add("agent.registered", "lab_reviewer", correlation_id="lab_reviewer", summary="Testing Lab Reviewer registered",
        task_status="registered", detail={"role": "lab-reviewer"})     # the next run registers it again
    return rows


def test_lab_reviewer_calls_are_its_own_assignments_outside_the_workflow(env, tmp_path):
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    db = tmp_path / "operations.db"
    capstone_db(db, [])         # the reviewer registers in the run's isolated store, so the live agent_registry lacks it
    with sqlite3.connect(db) as live:
        live.executemany(f"INSERT INTO arena_events VALUES ({','.join('?' * len(COLUMNS))})",
                         [tuple(row[c] for c in COLUMNS) for row in _lab_rows(stamp)])
    client = env.app("capstone", "Ticketing")
    for n in (2, 3, 5):
        env.head(f"head-192-0-2-{n}")
    adapter = Adapter({"capstone_db": str(db), "checkpoint_file": str(tmp_path / "cp.json"), "start_from": 0}, client)
    run_all(adapter)
    assert adapter.rejected == 0 and adapter.translator.skipped == {}
    agents = {a["agent_id"]: a for a in env.store.snapshot()["agents"]}
    assert (agents["lab_reviewer"]["display_name"], agents["lab_reviewer"]["role"]) == ("Ticketing Testing Lab Reviewer", "lab-reviewer")
    events = env.store.events_after(0, limit=1000)["events"]
    assert sum(e["type"] == "agent.registered" and e["payload"]["agent_id"] == "lab_reviewer" for e in events) == 1
    reviewer = {e["payload"]["assignment_id"]: e["payload"] for e in events
                if e["type"] == "assignment.created" and e["payload"]["agent_id"] == "lab_reviewer"}
    assert set(reviewer) == {"CALL-c9391fa614a040b3bec0fba5f4c3a1bd", "CALL-9d2b8dc5ee56443199774eac36a03ecd"}
    assert {(p["kind"], p["workflow_id"]) for p in reviewer.values()} == {("inference", RUN + "/lab_reviewer")}
    assert {p["task_id"] for p in reviewer.values()} == {"draft:MSG-LAB1", "qa:QA-LAB1"}
    trips = {(e["payload"]["assignment_id"], e["to_location"], e["payload"].get("model_id")) for e in events
             if e["type"] == "assignment.moved" and e["payload"]["assignment_id"].startswith("CALL-") and e["applied"]}
    assert ("CALL-c9391fa614a040b3bec0fba5f4c3a1bd", "head:head-192-0-2-5", "qwen3:4b") in trips
    assert ("CALL-9d2b8dc5ee56443199774eac36a03ecd", "head:head-192-0-2-3", "qwen3:4b") in trips
    ends = {e["payload"]["assignment_id"]: (e["result"]["state"], e["payload"]["visual_action"], e["result"]["agent_id"])
            for e in events if e["type"] == "assignment.state" and e["result"] and e["result"]["state"] in ("completed", "failed")}
    assert ends["CALL-c9391fa614a040b3bec0fba5f4c3a1bd"] == ("completed", "completion", "lab_reviewer")
    assert ends["CALL-9d2b8dc5ee56443199774eac36a03ecd"] == ("failed", "error_handling", "lab_reviewer")
    assert env.store.result_counts()["head:head-192-0-2-3"]["1h"]["fail"] == 1      # the timed-out call counts where it ran
    # The run's own workflow keeps exactly its workflow agents; the reviewer has its own scope.
    workflow = env.store.snapshot(f"workflow:capstone/{RUN}")
    assert {a["agent_id"] for a in workflow["agents"]} == {"ticket_interpretation"}
    assert {a["agent_id"] for a in env.store.snapshot(f"workflow:capstone/{RUN}/lab_reviewer")["agents"]} == {"lab_reviewer"}


def test_lab_reviewer_is_never_folded_into_a_workflow_task():
    t, stamp = Translator(), "2026-11-18T07:30:00+00:00"
    t.translate({"sequence": 1, "event_id": "E1", "timestamp": stamp, "event_type": "task.started", "agent_id": "supervisor",
                 "task_id": "EXEC-TASK-X", "workflow_id": "EXEC-TASK-X"})
    out = t.translate({"sequence": 2, "event_id": "E2", "timestamp": stamp, "event_type": "inference.queued", "agent_id": "lab_reviewer",
                       "task_id": "EXEC-TASK-X", "workflow_id": "EXEC-TASK-X", "correlation_id": "CALL-R", "model_id": "qwen3:4b"})
    created = [e for e in out if e["type"] == "assignment.created"]
    assert [(e["assignment_id"], e["agent_id"], e["kind"]) for e in created] == [("CALL-R", "lab_reviewer", "inference")]
    assert t.assignments["EXEC-TASK-X"]["location"] == "orch:capstone/main"         # the workflow task did not move
    registered = [e for e in out if e["type"] == "agent.registered"]
    assert [(e["agent_id"], e["role"], e["display_name"]) for e in registered] == [
        ("lab_reviewer", "lab-reviewer", "Ticketing Testing Lab Reviewer")]
    # A call with neither an agent nor a route_role is still skipped, never guessed (with a route_role it is a chat
    # turn or Testing Lab role screening: test_calls_without_an_agent_are_chat_or_lab_role_screening).
    assert t.translate({"sequence": 3, "event_id": "E3", "timestamp": stamp, "event_type": "inference.failed",
                        "correlation_id": "CALL-OLD", "model_id": "qwen3:4b"}) == []
    assert t.skipped == {"inference.failed": 1}


def test_days_of_tickets_do_not_accumulate():
    # 2.3.5 and earlier kept every task ever seen and rewrote all of it after each batch.
    t, stamp = Translator(), "2026-11-10T00:00:00+00:00"
    row = lambda seq, kind, **f: dict({"sequence": seq, "event_id": f"E{seq}", "timestamp": stamp, "event_type": kind,
                                       "agent_id": "intake"}, **f)
    seq = 0
    for n in range(20000):
        seq += 1; t.translate(row(seq, "task.started", task_id=f"T{n}"))
        seq += 1; t.translate(row(seq, "task.completed", task_id=f"T{n}"))
        t.prune()
    seq += 1; t.translate(row(seq, "task.started", task_id="RUNNING"))
    t.prune()
    assert len(t.assignments) <= 2 * Translator.KEEP_FINISHED + 1
    assert "RUNNING" in t.assignments and "T19999" in t.assignments, "running and recent work are kept"
    seq += 1
    assert t.translate(row(seq, "task.heartbeat", task_id="T19999")) == [], "recent finished work stays finished"
    # A checkpoint from before the fix is trimmed on load.
    big = {"assignments": {f"X{i}": {"location": "orch:capstone/main", "kind": "orchestration", "terminal": True, "agent": "a"}
                           for i in range(10000)}, "open_tools": {f"a@head:{i}": f"K{i}" for i in range(1000)}}
    t2 = Translator(state=big); t2.prune()
    assert len(t2.assignments) == Translator.KEEP_FINISHED and len(t2.open_tools) == Translator.KEEP_OPEN_TOOLS


# ------------------------------------------------------------------ figures (Arena 2.4.0)
def _row(seq, kind, agent=None, stamp="2026-11-18T09:00:00+00:00", detail=None, **f):
    return dict({"sequence": seq, "event_id": f"F{seq}", "timestamp": stamp, "event_type": kind, "agent_id": agent,
                 "detail": json.dumps(detail or {})}, **f)


def test_agent_views_carry_each_figure(ticketing):
    env, db, config, client, rows = ticketing
    run_all(Adapter(config, client))
    agents = env.store.snapshot()["agents"]
    assert len(agents) == len(DECLARED)
    for a in agents:
        look = appearance.appearance("capstone", a["agent_id"])
        assert (a["color"], a["body"], a["palette"], a["appearance_group"]) == (
            look["color"], look["body"], look["palette"], look["group"])
        assert {"agent_key", "display_name", "role", "capabilities", "home_location", "retired"} <= set(a)   # old fields kept
    assert len({a["color"] for a in agents}) == len(DECLARED)
    assert env.store.registry()["agents"] == agents
    registered = [e["result"] for e in env.store.events_after(0, limit=1000)["events"] if e["type"] == "agent.registered"]
    assert registered and all(r["body"] in appearance.BODIES and r["color"].startswith("#") for r in registered)
    # The stored hash color stays in the agents table (UNIQUE) for older records; only the view uses the palette.
    stored = env.store._one("SELECT color FROM agents WHERE agent_id='erp_research'")["color"]
    assert stored.startswith("hsl(")


def test_every_capstone_figure_has_its_own_color_and_the_lab_is_neutral():
    subs = [f"{p}.{s}" for p in appearance.CAPSTONE_WORKFLOW for s in appearance.SUB_ROLES]
    ids = [*appearance.CAPSTONE_WORKFLOW, *LAB_PARTICIPANTS, "chat_assistant", *subs]
    looks = {i: appearance.appearance("capstone", i) for i in ids}
    assert len({look["color"] for look in looks.values()}) == len(ids)
    assert set(LAB_PARTICIPANTS) == set(appearance.CAPSTONE_LAB)
    for agent in LAB_PARTICIPANTS:
        look = looks[agent]
        assert look["group"] == "lab" and look["palette"]["accent"] == appearance.LAB_ACCENT
        r, g, b = (int(look["color"][i:i + 2], 16) for i in (1, 3, 5))
        assert max(r, g, b) - min(r, g, b) <= 40, "neutral shell"
    assert looks["chat_assistant"]["group"] == "chat"
    assert {looks[s]["group"] for s in subs} == {"sub-role"} and {looks[s]["body"] for s in subs} == {"classic"}


def test_sub_role_call_is_its_own_figure_and_leaves_the_parent_task_alone():
    t = Translator(registry={"operations": {"display_name": "Operations", "role": "operations"}})
    t.translate(_row(1, "task.started", "operations", task_id="EXEC-TASK-OPS", workflow_id="WF-1"))
    route = {"route_role": "investigation", "logical_agent_status": "known"}
    call = dict(task_id="EXEC-TASK-OPS", workflow_id="WF-1", correlation_id="CALL-SUB", model_id="qwen3.5:4b")
    out = t.translate(_row(2, "inference.queued", "operations", detail=route, **call))
    registered = [(e["agent_id"], e["display_name"], e["role"]) for e in out if e["type"] == "agent.registered"]
    assert registered == [("operations.investigation", "Ticketing Operations - Investigation", "sub-role")]
    created = [e for e in out if e["type"] == "assignment.created"]
    assert [(e["assignment_id"], e["agent_id"], e["kind"], e["workflow_id"], e["task_id"], e["visual_action"]) for e in created] == [
        ("CALL-SUB", "operations.investigation", "inference", "WF-1/operations.investigation", "EXEC-TASK-OPS", "tool_use")]
    assert "E4" not in created[0]["source_ref"]
    out = t.translate(_row(3, "inference.started", "operations", detail=route, inference_node="head-192-0-2-5", **call))
    assert {e["assignment_id"] for e in out} == {"CALL-SUB"}
    assert [e["to_location"] for e in out if e["type"] == "assignment.moved"] == ["head:head-192-0-2-5"]
    hb = t.translate(_row(4, "inference.heartbeat", "operations", detail=route, **call))
    assert [(e["type"], e["assignment_id"]) for e in hb] == [("assignment.heartbeat", "CALL-SUB")]
    out = t.translate(_row(5, "inference.completed", "operations", detail=route, inference_node="head-192-0-2-5", **call))
    assert {e["assignment_id"] for e in out} == {"CALL-SUB"} and t.assignments["CALL-SUB"]["terminal"]
    parent = t.assignments["EXEC-TASK-OPS"]
    assert (parent["location"], parent["terminal"], parent["agent"]) == ("orch:capstone/main", False, "operations")
    # The parent's own call (its route role is not a sub-role) still runs inside its task, as before.
    out = t.translate(_row(6, "inference.started", "operations", detail={"route_role": "operations"},
                           inference_node="head-192-0-2-5", **dict(call, correlation_id="CALL-OWN")))
    assert {e["assignment_id"] for e in out} == {"EXEC-TASK-OPS"} and not any(e["type"] == "assignment.created" for e in out)
    # Each sub-role draws its own action; a parent unknown to the registry still gets a titled name.
    for n, (sub, action) in enumerate(sorted(SUB_ROLE_ACTIONS.items())):
        out = t.translate(_row(10 + n, "inference.queued", "erp_research", detail={"route_role": sub},
                               task_id="EXEC-TASK-OPS", workflow_id="WF-1", correlation_id=f"CALL-{n}"))
        made = [e for e in out if e["type"] == "assignment.created"][0]
        assert (made["agent_id"], made["visual_action"]) == (f"erp_research.{sub}", action)
    assert t.agents["erp_research.supervisor"] == "sub-role"
    assert {"retrieval_embedding": "retrieval", "tool_delegate": "tool_use", "reflection": "review", "peer_reviewer": "review",
            "supervisor": "evaluation", "investigation": "tool_use", "customer_communication": "review"} == SUB_ROLE_ACTIONS
    assert set(SUB_ROLE_ACTIONS) == set(appearance.SUB_ROLES)


def test_calls_without_an_agent_are_chat_or_lab_role_screening():
    t = Translator()
    t.translate(_row(1, "task.started", "ticket_interpretation", task_id="EXEC-TASK-LAB2", workflow_id=RUN,
                     summary="EXPERIMENT task started"))
    chat = dict(workflow_id="INV-chat1", correlation_id="CALL-CHAT", model_id="qwen3.5:4b", summary="chat inference queued")
    out = t.translate(_row(2, "inference.queued", None, detail={"route_role": "chat", "logical_agent_status": "unavailable"}, **chat))
    assert [(e["agent_id"], e["display_name"], e["role"]) for e in out if e["type"] == "agent.registered"] == [
        ("chat_assistant", "Ticketing Chat", "chat")]
    made = [e for e in out if e["type"] == "assignment.created"][0]
    assert (made["assignment_id"], made["agent_id"], made["workflow_id"], made["visual_action"]) == (
        "CALL-CHAT", "chat_assistant", "INV-chat1/chat_assistant", "review")
    out = t.translate(_row(3, "inference.completed", None, detail={"route_role": "chat"}, **chat))
    assert [(e["assignment_id"], e["state"]) for e in out] == [("CALL-CHAT", "completed")]
    # A Testing Lab row (mirrored: "EXPERIMENT " summary, run id as workflow_id) with no agent is role screening,
    # never the run's workflow task even when it names that task.
    lab = dict(workflow_id=RUN, task_id="EXEC-TASK-LAB2", correlation_id="CALL-SCREEN", model_id="qwen3:4b",
               summary="EXPERIMENT investigation inference queued")
    out = t.translate(_row(4, "inference.queued", None, detail={"route_role": "investigation"}, **lab))
    assert [(e["agent_id"], e["display_name"], e["role"]) for e in out if e["type"] == "agent.registered"] == [
        ("lab_role_screening", "Ticketing Testing Lab Role Screening", "lab-role-screening")]
    made = [e for e in out if e["type"] == "assignment.created"][0]
    assert (made["assignment_id"], made["agent_id"], made["workflow_id"], made["visual_action"]) == (
        "CALL-SCREEN", "lab_role_screening", RUN + "/lab_role_screening", "evaluation")
    out = t.translate(_row(5, "inference.started", None, detail={"route_role": "investigation"},
                           inference_node="head-192-0-2-3", **lab))
    assert {e["assignment_id"] for e in out} == {"CALL-SCREEN"}
    assert t.assignments["EXEC-TASK-LAB2"]["location"] == "orch:capstone/main"
    # Neither an agent nor a route_role: still skipped.
    assert t.translate(_row(6, "inference.queued", None, correlation_id="CALL-X", workflow_id=RUN)) == []
    assert t.skipped == {"inference.queued": 1}


def test_new_lab_participants_keep_their_names_roles_and_scope():
    t = Translator()
    t.translate(_row(1, "task.started", "ticket_interpretation", task_id="EXEC-TASK-LAB3", workflow_id=RUN))
    expected = {"testing_lab_probe": ("Ticketing Testing Lab Probe", "lab-probe", "testing"),
                "lab-customer-simulator": ("Ticketing Testing Lab Customer", "lab-customer", "review"),
                "lab-environment-simulator": ("Ticketing Testing Lab Environment", "lab-environment", "tool_use")}
    for n, (agent, (name, role, action)) in enumerate(expected.items()):
        out = t.translate(_row(10 + n, "inference.queued", agent, detail={"route_role": agent}, task_id="EXEC-TASK-LAB3",
                               workflow_id=RUN, correlation_id=f"CALL-P{n}", summary="EXPERIMENT queued"))
        assert [(e["display_name"], e["role"]) for e in out if e["type"] == "agent.registered"] == [(name, role)]
        made = [e for e in out if e["type"] == "assignment.created"][0]
        assert (made["assignment_id"], made["workflow_id"], made["visual_action"]) == (f"CALL-P{n}", f"{RUN}/{agent}", action)
        # Registering again (every run) changes nothing.
        assert t.translate(_row(20 + n, "agent.registered", agent, summary=f"{name} registered", detail={"role": role})) == []
    # The environment's tool call naming the workflow task is its own trip, never the workflow task's.
    out = t.translate(_row(30, "tool.started", "lab-environment-simulator", task_id="EXEC-TASK-LAB3", workflow_id=RUN))
    made = [e for e in out if e["type"] == "assignment.created"]
    assert [(e["agent_id"], e["workflow_id"]) for e in made] == [("lab-environment-simulator", f"{RUN}/lab-environment-simulator")]
    assert t.assignments["EXEC-TASK-LAB3"]["agent"] == "ticket_interpretation"


def test_a_testing_lab_run_keeps_its_workflow_agent_count_with_every_new_figure(env, tmp_path):
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    rows = _lab_rows(stamp)
    seq = len(rows)

    def add(kind, agent, route_role, call, head=None, summary="EXPERIMENT call", workflow=RUN, task="EXEC-TASK-LAB1"):
        nonlocal seq
        seq += 1
        rows.append({**{c: None for c in COLUMNS}, "sequence": seq, "event_id": f"ARENA-FIG-{seq:02d}", "schema_version": "1.0",
                     "timestamp": stamp, "event_type": kind, "agent_id": agent, "workflow_id": workflow, "task_id": task,
                     "correlation_id": call, "model_id": "qwen3:4b", "inference_node": head, "summary": summary,
                     "evidence_refs": "[]", "detail": json.dumps({"route_role": route_role})})
    for agent, role, call in (("ticket_interpretation", "reflection", "CALL-F1"), ("testing_lab_probe", "testing_lab_probe", "CALL-F2"),
                              ("lab-customer-simulator", "lab_customer", "CALL-F3"),
                              ("lab-environment-simulator", "lab_environment", "CALL-F4"), (None, "investigation", "CALL-F5")):
        add("inference.queued", agent, role, call)
        add("inference.started", agent, role, call, head="head-192-0-2-5")
        add("inference.completed", agent, role, call, head="head-192-0-2-5")
    add("inference.queued", None, "chat", "CALL-F6", summary="chat inference queued", workflow="INV-1", task=None)
    db = tmp_path / "operations.db"
    capstone_db(db, [])
    with sqlite3.connect(db) as live:
        live.executemany(f"INSERT INTO arena_events VALUES ({','.join('?' * len(COLUMNS))})",
                         [tuple(row[c] for c in COLUMNS) for row in rows])
    client = env.app("capstone", "Ticketing")
    for n in (2, 3, 5):
        env.head(f"head-192-0-2-{n}")
    adapter = Adapter({"capstone_db": str(db), "checkpoint_file": str(tmp_path / "cp.json"), "start_from": 0}, client)
    run_all(adapter)
    assert adapter.rejected == 0 and adapter.translator.skipped == {}
    agents = {a["agent_id"]: a for a in env.store.snapshot()["agents"]}
    assert set(DECLARED) <= set(agents), "Arena 2.5.0: every declared agent stands in the room, idle until called on"
    assert {k: (a["display_name"], a["role"], a["appearance_group"]) for k, a in agents.items() if k in (
        "ticket_interpretation", "ticket_interpretation.reflection", "lab_reviewer", "testing_lab_probe", "lab-customer-simulator",
        "lab-environment-simulator", "lab_role_screening", "chat_assistant")} == {
        "ticket_interpretation": ("Ticketing Ticket Interpretation", "unspecified", "workflow"),
        "ticket_interpretation.reflection": ("Ticketing Ticket Interpretation - Reflection", "sub-role", "sub-role"),
        "lab_reviewer": ("Ticketing Testing Lab Reviewer", "lab-reviewer", "lab"),
        "testing_lab_probe": ("Ticketing Testing Lab Probe", "lab-probe", "lab"),
        "lab-customer-simulator": ("Ticketing Testing Lab Customer", "lab-customer", "lab"),
        "lab-environment-simulator": ("Ticketing Testing Lab Environment", "lab-environment", "lab"),
        "lab_role_screening": ("Ticketing Testing Lab Role Screening", "lab-role-screening", "lab"),
        "chat_assistant": ("Ticketing Chat", "chat", "chat")}
    assert len({a["color"] for a in agents.values()}) == len(agents)
    assert {a["agent_id"] for a in env.store.snapshot(f"workflow:capstone/{RUN}")["agents"]} == {"ticket_interpretation"}
    for agent in ("ticket_interpretation.reflection", "testing_lab_probe", "lab-customer-simulator", "lab-environment-simulator",
                  "lab_role_screening", "lab_reviewer"):
        assert {a["agent_id"] for a in env.store.snapshot(f"workflow:capstone/{RUN}/{agent}")["agents"]} == {agent}
    events = env.store.events_after(0, limit=1000)["events"]
    parent = [e for e in events if e["payload"].get("assignment_id") == "EXEC-TASK-LAB1" and e["applied"]]
    assert [e["to_location"] for e in parent if e["type"] == "assignment.moved"] == ["head:head-192-0-2-2", "orch:capstone/main"]
    assert [e["result"]["state"] for e in parent if e["type"] == "assignment.state"][-1] == "completed"
