from pathlib import Path
from types import SimpleNamespace
import time

from capstone_rag import experiments
from capstone_rag import inference
from capstone_rag.agentic_methodologies import candidates
from capstone_rag.ai_settings import Registry
from capstone_rag.candidate_execution import execution_plan


def _queued(manager, root, number):
    directory = root / f"RUN-{number}"
    directory.mkdir()
    manager._store_run({
        "id": f"RUN-{number}", "experiment_id": "EXP-RC10",
        "directory": str(directory), "status": "queued",
        "profile": {"id": "gateway-default", "name": "Gateway", "routes": {}},
    })


def test_every_single_agent_methodology_has_a_bounded_repair_limit():
    plans = [execution_plan(row) for row in candidates(["B"], ["1", "2", "3", "4"])]
    assert {plan["adapter"] for plan in plans} == {"single_direct"}
    assert {plan["repair_limit"] for plan in plans} == {2}


def test_failure_limit_records_why_dispatch_stopped_and_preserves_unstarted_trials(tmp_path, monkeypatch):
    registry = Registry(tmp_path / "registry.db")
    manager = experiments.Manager(registry)
    manager.save({
        "id": "EXP-RC10", "status": "running", "directory": str(tmp_path),
        "concurrency": 1, "failure_limit": 1, "planned_runs": 3,
        "created_at": time.time(), "stop_requested": False,
    })
    for number in range(3):
        _queued(manager, tmp_path, number)

    def fail(run):
        failed = {**run, "status": "failed", "error": "synthetic failure"}
        manager._store_run(failed)
        return failed

    monkeypatch.setattr(manager, "execute", fail)
    manager._campaign("EXP-RC10")

    campaign = registry.get("experiments", "EXP-RC10")
    assert campaign["status"] == "incomplete"
    assert campaign["termination_reason"] == "failure_limit_reached"
    assert campaign["observed_failures"] == 1
    assert campaign["queued_runs_preserved"] == 2
    assert campaign["peak_active_workers"] == 1
    assert campaign["dispatched_runs"] == 1
    assert campaign["finished_at"] >= campaign["created_at"]
    assert campaign["elapsed_ms"] > 0
    assert campaign["throughput_runs_per_hour"] > 0
    assert [run["status"] for run in manager.runs("EXP-RC10")].count("queued") == 2


def test_terminal_preserved_queue_is_reported_as_not_run_not_waiting(tmp_path, monkeypatch):
    from capstone_rag.webapp.routers import experiments as router

    registry = Registry(tmp_path / "registry.db")
    manager = experiments.Manager(registry)
    manager.save({
        "id": "EXP-RC10", "status": "incomplete", "directory": str(tmp_path),
        "failure_limit": 1, "termination_reason": "failure_limit_reached",
    })
    for number in range(2):
        _queued(manager, tmp_path, number)
    failed = manager.runs("EXP-RC10")[0]
    failed.update(status="failed", error="synthetic failure")
    manager._store_run(failed)
    monkeypatch.setattr(router, "manager", lambda: manager)

    report = router.detail("EXP-RC10")
    assert report["progress"]["waiting_or_running"] == 0
    assert report["progress"]["active_runs"] == 0
    assert report["progress"]["not_run"] == 1
    assert report["progress"]["termination_reason"] == "failure_limit_reached"
    assert "inactive" in report["note"]


def test_results_ui_explains_failure_stop_and_can_cancel_remaining_trials():
    from lab_js import lab_js_text
    text = lab_js_text()
    for phrase in (
        "Stopped after the", "preserved trials were not run",
        "Not run", "Clear remaining queue",
    ):
        assert phrase in text


def test_agentic_plan_reports_parallel_flows_and_work_conserving_waves(monkeypatch):
    from capstone_rag.webapp.routers import lab

    available={"name":"qwen3.5:4b","heads":["head-192-0-2-3"],"blocked":{}}
    monkeypatch.setattr(lab,"state",lambda:{"models":[available]})
    report=lab.plan({"experiment":"agentic-evaluation","models":["qwen3.5:4b"],
        "case_count":1,"repetitions":1,"concurrency":16,"methodology_matrix":{}})
    assert report["runs"]==32
    assert report["parallel_flows"]==16
    assert report["waves"]==2


def test_open_developer_session_is_renewed_instead_of_expiring_mid_run(monkeypatch):
    from capstone_rag.webapp import developer_auth

    state=SimpleNamespace(developer_token="fixture",developer_sessions={})
    request=SimpleNamespace(app=SimpleNamespace(state=state),cookies={})
    monkeypatch.setattr(developer_auth.time,"time",lambda:1000.0)
    session_id,_=developer_auth.create_session(request,"fixture",role="developer")
    original=state.developer_sessions[session_id]["expires_at"]
    request.cookies={developer_auth.COOKIE:session_id}
    monkeypatch.setattr(developer_auth.time,"time",lambda:2000.0)
    renewed=developer_auth.renew_developer_session(request)
    assert renewed["expires_at"]>original
    assert renewed["expires_at"]==2000.0+developer_auth.SESSION_SECONDS


def test_testing_lab_route_failure_is_raised_immediately():
    state={"error":"The read operation timed out",
        "store":SimpleNamespace(arena_experiment_run_id="RUN-FAST-FAIL")}
    token=inference._SESSION.set(state)
    try:
        try:
            inference.raise_if_failed()
        except RuntimeError as exc:
            assert "read operation timed out" in str(exc)
        else:
            raise AssertionError("Testing Lab failure was silently continued")
    finally:
        inference._SESSION.reset(token)


def test_operational_controlled_failure_is_not_changed():
    token=inference._SESSION.set({"error":"temporary", "store":SimpleNamespace()})
    try:
        assert inference.raise_if_failed() is None
    finally:
        inference._SESSION.reset(token)
