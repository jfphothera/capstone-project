from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from capstone_rag import research


def evidence(*, trials=2, scored=2, correct=2, violations=0, missing=True):
    group = {
        "node_id": "node-a", "model": "model-a", "trials": trials,
        "scored": scored, "correct": correct,
        "critical_violations": violations,
        "latency_ms": {"median": 25},
        "host_utilization": None if missing else {"cpu": 30},
        "cost_per_success": None,
    }
    return {"format": "capstone-experiment-comparison-v1",
            "comparisons": [group], "limitations": ["fixture limitation"]}


def run(run_id="RUN-1", *, status="completed", snapshot="a" * 64):
    return {"id": run_id, "status": status, "iteration": 1,
            "source_revision": "b" * 64, "source_snapshot_sha256": snapshot,
            "scenario": {"id": "synthetic-1", "evaluation_split": "holdout"},
            "profile": {"id": "profile-a", "configuration_hash": "c" * 64},
            "metrics": {"all_scored_checks_passed": True,
                        "critical_violations": []}}


def test_research_is_read_only_hashes_inputs_and_preserves_split():
    experiment = {"id": "EXP-1", "status": "completed",
                  "source_revision": "b" * 64, "snapshot_sha256": "a" * 64}
    runs = [run()]
    comparison = evidence()
    before = copy.deepcopy((experiment, runs, comparison))

    report = research.build_report(experiment, runs, comparison)

    assert (experiment, runs, comparison) == before
    assert report["format"] == "capstone-research-report-v1"
    assert len(report["input_sha256"]) == len(report["report_sha256"]) == 64
    assert report["provenance"]["run_ids"] == ["RUN-1"]
    assert report["provenance"]["evaluation_splits"] == ["holdout"]


def test_insufficient_evidence_abstains_and_null_measurements_stay_missing():
    report = research.build_report(
        {"id": "EXP-1", "source_revision": "b" * 64,
         "snapshot_sha256": "a" * 64}, [run()], evidence())
    assert report["abstentions"]
    assert "host_utilization" in report["observations"]["missing_measurements"]
    assert report["proposals"][0]["proposal_id"] == "repeat-controlled-comparison"
    assert all(item["human_review_required"] for item in report["proposals"])


def test_comparable_observations_only_propose_holdout_validation():
    comparison = evidence(missing=False)
    second = copy.deepcopy(comparison["comparisons"][0])
    second.update(node_id="node-b", model="model-b", correct=1,
                  latency_ms={"median": 10})
    comparison["comparisons"].append(second)
    runs = [run("RUN-1"), run("RUN-2")]
    report = research.build_report(
        {"id": "EXP-1", "status": "completed", "source_revision": "b" * 64,
         "snapshot_sha256": "a" * 64}, runs, comparison)
    proposal = report["proposals"][0]
    assert proposal["proposal_id"] == "validate-observed-candidate"
    assert "not a causal or statistical winner" in proposal["basis"]
    assert report["human_review_required"] is True


def test_research_contract_cannot_authorize_or_apply_actions():
    report = research.build_report({"id": "EXP-1"}, [],
                                   {"format": "capstone-experiment-comparison-v1",
                                    "comparisons": [], "limitations": []})
    assert report["automatic_actions_permitted"] == []
    assert set(report["prohibited_actions"]) == set(research.PROHIBITED_ACTIONS)
    assert {"modify_controlled_qms", "send_customer_communication",
            "deploy_source_change", "promote_model_or_policy"} <= set(
                report["prohibited_actions"])


def test_unknown_comparison_contract_fails_closed():
    with pytest.raises(ValueError, match="Unsupported"):
        research.build_report({}, [], {"format": "future-contract"})


def test_research_endpoint_reuses_existing_comparison_contract(monkeypatch):
    from capstone_rag.webapp.routers import experiments as router

    class Registry:
        @staticmethod
        def get(collection, key):
            assert (collection, key) == ("experiments", "EXP-1")
            return {"id": "EXP-1", "status": "completed"}

    fake_manager = SimpleNamespace(registry=Registry(), runs=lambda _eid: [])
    fake_store = SimpleNamespace(query=lambda _sql: [])
    monkeypatch.setattr(router, "manager", lambda: fake_manager)
    monkeypatch.setattr(router, "get_runtime", lambda: SimpleNamespace(store=fake_store))

    report = router.research("EXP-1")
    assert report["format"] == research.FORMAT
    assert report["provenance"]["comparison_format"] == "capstone-experiment-comparison-v1"
    assert report["abstentions"][0]["claim"] == "No performance conclusion"
