"""Arena 2.5.1: the hub stays responsive (2026-11-19, DESKTOP-REDACT10 dropped out for seconds at a time).

Each snapshot recounted 24 h of results from the event log while holding the store lock (about 5 s on the hub with
250k events), so the admin console's 2 s health check timed out and showed "Service not answering". Results now live
in their own table, /v2/health never waits for the store lock, and work with no lease can expire.
"""
import json
import threading
import time
import urllib.request

from arena_service.adapters.capstone.translate import LAB_REVIEW_LEASE, Translator
from arena_service.admin_console import Console
from arena_service.store import STALE_UNLEASED, Store
from arena_service.tests.test_result_counts import run
from arena_service.tests.test_service import setup_ticketing


def test_results_table_agrees_with_the_event_log_rule_and_backfills_once(env, tmp_path):
    producer = setup_ticketing(env)
    run(env, producer, "A", "head:head-192-0-2-3", "completed")
    run(env, producer, "B", "head:head-192-0-2-3", "failed", back_home=False)
    run(env, producer, "C", "head:head-192-0-2-6", "completed")
    run(env, producer, "D", "head:head-192-0-2-6", "canceled")
    live = env.store.result_counts()
    # A database from before 2.5.1: no results rows, no backfill mark. Opening it rebuilds the same totals.
    env.store.db.execute("DELETE FROM results")
    env.store.db.execute("DELETE FROM meta WHERE key='results_backfilled'")
    reopened = Store(env.config["db_path"])
    assert reopened.result_counts() == live
    assert live["cluster"]["1h"] == {"pass": 2, "fail": 1} and live["head:head-192-0-2-3"]["24h"] == {"pass": 1, "fail": 1}
    assert reopened.meta("results_backfilled")
    assert Store(env.config["db_path"]).result_counts() == live          # later opens do not count twice


def test_health_answers_while_the_store_is_busy(env):
    started, release = threading.Event(), threading.Event()

    def long_read():
        with env.store.lock:                                                # a long snapshot or maintenance pass
            started.set()
            release.wait(5)

    worker = threading.Thread(target=long_read, daemon=True)
    worker.start()
    started.wait(2)
    try:
        t = time.perf_counter()
        with urllib.request.urlopen(env.url + "/v2/health", timeout=2) as response:
            body = json.load(response)
        assert time.perf_counter() - t < 1.0 and body["service_id"] == env.store.service_id
        assert body["latest_seq"] == env.store.latest_seq()
    finally:
        release.set()
        worker.join(5)


def test_latest_seq_follows_every_recorded_event(env):
    producer = setup_ticketing(env)
    before = env.store.latest_seq()
    run(env, producer, "A", "head:head-192-0-2-3", "completed")
    assert env.store.latest_seq() == env.store._one("SELECT MAX(seq) AS s FROM events")["s"] > before


def test_unleased_queued_work_expires_after_a_day_and_leased_work_keeps_its_rule(env):
    producer = setup_ticketing(env)
    producer.publish([env.ev("assignment.created", assignment_id="Q", agent_id="independent_validation", kind="orchestration")])
    row = env.store._one("SELECT state,lease_expires_at FROM assignments WHERE assignment_id='Q'")
    assert row["state"] == "queued" and row["lease_expires_at"] is None
    assert env.store.maintain(now=time.time() + 3600) == 0                 # an hour queued is normal
    assert env.store.maintain(now=time.time() + STALE_UNLEASED + 60) == 1
    assert env.store._one("SELECT state FROM assignments WHERE assignment_id='Q'")["state"] == "expired"
    assert env.store.result_counts(time.time() + STALE_UNLEASED + 60) == {}  # expired is neither pass nor fail


def _row(seq, kind, workflow, **extra):
    row = {"sequence": seq, "event_id": f"ARENA-{seq}", "timestamp": "2026-11-19T10:00:00+00:00", "event_type": kind,
           "workflow_id": workflow, "agent_id": "coordinator", "task_id": None, "summary": "x", "detail": None}
    row.update(extra)
    return row


def _states(events):
    return [(e["assignment_id"], e.get("state")) for e in events if e["type"] == "assignment.state"]


def test_a_workflows_review_request_closes_when_superseded_resumed_or_ended():
    t = Translator()
    first = t.translate(_row(1, "human_review.requested", "WF-1"))
    assert ("REVIEW-ARENA-1", "waiting") in _states(first)
    again = t.translate(_row(2, "human_review.requested", "WF-1"))          # the interrupt loop asks again
    assert ("REVIEW-ARENA-1", "canceled") in _states(again) and ("REVIEW-ARENA-2", "waiting") in _states(again)
    resumed = t.translate(_row(3, "task.started", "WF-1", task_id="TASK-9"))
    assert ("REVIEW-ARENA-2", "canceled") in _states(resumed)
    t.translate(_row(4, "human_review.requested", "WF-2"))
    other = t.translate(_row(5, "task.started", "WF-3", task_id="TASK-7"))  # another workflow leaves it alone
    assert not any(a.startswith("REVIEW") for a, _ in _states(other))
    ended = t.translate(_row(6, "workflow.completed", "WF-2", task_id="TASK-8", agent_id=None))
    assert ("REVIEW-ARENA-4", "canceled") in _states(ended)
    assert not any(s == "completed" for _, s in _states(again + resumed + ended))   # never counted as a pass
    restored = Translator(state=json.loads(json.dumps(t.state())))         # survives an adapter restart
    assert restored.reviews == t.reviews


def test_testing_lab_review_requests_get_the_short_lease():
    t = Translator()
    lab = t.translate(_row(1, "human_review.requested", "RUN-abc", summary="EXPERIMENT review"))
    human = t.translate(_row(2, "human_review.requested", "WF-9"))
    lease = lambda evs: [e.get("lease_seconds") for e in evs if e["type"] == "assignment.state"][0]
    assert lease(lab) == LAB_REVIEW_LEASE and lease(human) == 86400


def test_admin_console_shows_one_missed_check_as_slow_and_two_as_down(tmp_path):
    console = Console.__new__(Console)
    console.config = {"host": "127.0.0.1", "port": 1}                    # nothing listens there
    first, second = console.service_health(), console.service_health()
    assert first["up"] and first["slow"] and not second["up"]
