"""Rolling pass/fail totals (1 h / 8 h / 24 h) and the results lines in the service log."""
import logging
import time

from arena_service.server import report_results
from arena_service.tests.test_service import setup_ticketing


def run(env, producer, assignment, head, outcome, back_home=True):
    events = [env.ev("assignment.created", assignment_id=assignment, agent_id="independent_validation", kind="inference"),
              env.ev("assignment.moved", assignment_id=assignment, to_location=head, state="executing")]
    if back_home:   # the result is reported after the work returns to its orchestrator
        events.append(env.ev("assignment.moved", assignment_id=assignment, to_location="orch:capstone/main", state="executing"))
    events.append(env.ev("assignment.state", assignment_id=assignment, state=outcome))
    producer.publish(events)


def test_results_count_for_cluster_orchestrator_and_last_head(env):
    producer = setup_ticketing(env)
    run(env, producer, "A", "head:head-192-0-2-3", "completed")
    run(env, producer, "B", "head:head-192-0-2-3", "failed", back_home=False)
    run(env, producer, "C", "head:head-192-0-2-6", "completed")
    run(env, producer, "D", "head:head-192-0-2-6", "canceled")          # neither pass nor fail
    counts = env.viewer("head-192-0-2-3").snapshot("cluster")["result_counts"]
    assert counts["cluster"]["1h"] == {"pass": 2, "fail": 1}
    assert counts["orch:capstone/main"]["24h"] == {"pass": 2, "fail": 1}
    assert counts["head:head-192-0-2-3"]["8h"] == {"pass": 1, "fail": 1}
    assert counts["head:head-192-0-2-6"]["1h"] == {"pass": 1, "fail": 0}


def test_windows_roll_and_restarts_keep_counts(env):
    producer = setup_ticketing(env)
    run(env, producer, "A", "head:head-192-0-2-3", "completed")
    later = time.time() + 2 * 3600                                          # two hours on
    counts = env.store.result_counts(later)["head:head-192-0-2-3"]
    assert counts["1h"] == {"pass": 0, "fail": 0} and counts["8h"] == {"pass": 1, "fail": 0}
    assert env.store.result_counts(time.time() + 25 * 3600) == {}          # past 24 h


def test_failure_warning_and_daily_line(env, caplog):
    producer = setup_ticketing(env)
    for n in range(3):
        run(env, producer, f"F{n}", "head:head-192-0-2-6", "failed")
    memory = {"day": "2000-01-01"}
    with caplog.at_level(logging.INFO, logger="arena_service"):
        report_results(env.store, memory, threshold=3)
        report_results(env.store, memory, threshold=3)                     # same hour: no second warning
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["head:head-192-0-2-6: 3 failed tasks in the last hour (warning threshold 3)"]
    daily = [r.getMessage() for r in caplog.records if r.getMessage().startswith("daily results")]
    assert "daily results head:head-192-0-2-6: 0 passed, 3 failed in the last 24 h" in daily and len(daily) == 3
