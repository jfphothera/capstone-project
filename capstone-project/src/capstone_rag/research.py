"""Deterministic, read-only Phase 5 research over experiment evidence.

This module deliberately has no database, network, inference, business-tool, or
deployment imports.  It turns the existing immutable experiment comparison
contract into reviewable proposals; it cannot apply a proposal.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


FORMAT = "capstone-research-report-v1"
PROHIBITED_ACTIONS = [
    "modify_controlled_qms",
    "authorize_customer_action",
    "send_customer_communication",
    "deploy_source_change",
    "promote_model_or_policy",
    "bypass_gateway_or_resource_authorization",
]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _run_evidence(run: dict) -> dict:
    """Select non-customer evidence and preserve the experiment split identity."""
    metrics = run.get("metrics") or {}
    scenario = run.get("scenario") or {}
    profile = run.get("profile") or {}
    return {
        "run_id": run.get("id"),
        "status": run.get("status"),
        "scenario_id": scenario.get("id"),
        "evaluation_split": scenario.get("evaluation_split", "synthetic"),
        "iteration": run.get("iteration"),
        "profile_id": profile.get("id"),
        "configuration_hash": profile.get("configuration_hash"),
        "source_revision": run.get("source_revision"),
        "source_snapshot_sha256": run.get("source_snapshot_sha256"),
        "scored": metrics.get("all_scored_checks_passed") is not None,
        "correct": metrics.get("all_scored_checks_passed"),
        "critical_violation_count": len(metrics.get("critical_violations") or []),
    }


def build_report(experiment: dict, runs: list[dict], comparison: dict) -> dict:
    """Return a proposal-only report without mutating any supplied object."""
    experiment = copy.deepcopy(experiment)
    runs = copy.deepcopy(runs)
    comparison = copy.deepcopy(comparison)
    if comparison.get("format") != "capstone-experiment-comparison-v1":
        raise ValueError("Unsupported or missing comparison contract")

    run_evidence = sorted((_run_evidence(run) for run in runs),
                          key=lambda row: str(row.get("run_id")))
    inputs = {
        "experiment_id": experiment.get("id"),
        "experiment_status": experiment.get("status"),
        "source_revision": experiment.get("source_revision"),
        "source_snapshot_sha256": experiment.get("snapshot_sha256"),
        "comparison": comparison,
        "runs": run_evidence,
    }
    groups = comparison.get("comparisons") or []
    limitations = list(comparison.get("limitations") or [])
    proposals: list[dict] = []
    abstentions: list[dict] = []

    missing = sorted({field for group in groups for field in
                      ("host_utilization", "cost_per_success")
                      if group.get(field) is None})
    if missing:
        limitations.append("Unmeasured fields remain null: " + ", ".join(missing) + ".")

    completed = [row for row in run_evidence if row["status"] == "completed"]
    fully_scored = all(group.get("scored") == group.get("trials") and
                       group.get("trials", 0) >= 2 for group in groups)
    snapshots = {row["source_snapshot_sha256"] for row in run_evidence
                 if row["source_snapshot_sha256"]}
    revisions = {row["source_revision"] for row in run_evidence if row["source_revision"]}
    comparable = len(groups) >= 2 and fully_scored and len(snapshots) <= 1 and len(revisions) <= 1

    if not groups:
        abstentions.append({"claim": "No performance conclusion",
                            "reason": "The comparison contains no observed model usage."})
    elif not comparable:
        reasons = []
        if len(groups) < 2:
            reasons.append("fewer than two observed model/node groups")
        if not fully_scored:
            reasons.append("groups need at least two fully scored trials")
        if len(snapshots) > 1 or len(revisions) > 1:
            reasons.append("mixed source snapshots or source revisions")
        abstentions.append({"claim": "No model, hardware, or causal winner",
                            "reason": "; ".join(reasons)})
        proposals.append({
            "proposal_id": "repeat-controlled-comparison",
            "kind": "experiment",
            "recommendation": "Run a bounded repeated comparison on one immutable source snapshot with complete scoring.",
            "evidence_refs": [row["run_id"] for row in run_evidence if row["run_id"]],
            "human_review_required": True,
        })
    else:
        ranked = sorted(groups, key=lambda group: (
            group.get("critical_violations", 0),
            -(group.get("correct", 0) / max(1, group.get("scored", 0))),
            (group.get("latency_ms") or {}).get("median", float("inf")),
            str(group.get("node_id")), str(group.get("model"))))
        candidate = ranked[0]
        proposals.append({
            "proposal_id": "validate-observed-candidate",
            "kind": "experiment",
            "recommendation": "Repeat the observed candidate under a pre-reviewed holdout and resource budget; do not promote it from this report.",
            "candidate": {"node_id": candidate.get("node_id"), "model": candidate.get("model")},
            "basis": "Observed safety violations, whole-workflow correctness, then median latency; not a causal or statistical winner.",
            "evidence_refs": [row["run_id"] for row in run_evidence if row["run_id"]],
            "human_review_required": True,
        })

    violations = sum(int(group.get("critical_violations", 0)) for group in groups)
    if violations:
        proposals.append({
            "proposal_id": "investigate-critical-violations",
            "kind": "safety_investigation",
            "recommendation": "Review critical violations before any optimization experiment continues.",
            "observed_critical_violations": violations,
            "evidence_refs": [row["run_id"] for row in run_evidence
                              if row["critical_violation_count"]],
            "human_review_required": True,
        })

    for proposal in proposals:
        if proposal.get("kind") == "experiment":
            proposal["bounded_resources"] = {
                "max_trials": min(100, int(experiment.get("max_trials", 100))),
                "concurrency": min(4, int(experiment.get("concurrency", 1))),
                "timeout_seconds_per_run": min(1200, int(experiment.get("timeout_seconds", 600))),
                "total_token_budget": experiment.get("total_token_budget"),
                "model_call_budget": experiment.get("model_call_budget"),
                "minimum_free_disk_bytes": int(experiment.get("minimum_free_disk_bytes", 268435456)),
            }

    report = {
        "format": FORMAT,
        "evidence_class": "synthetic" if run_evidence else "none",
        "measurement_status": "observed" if completed else "no_completed_measurements",
        "input_sha256": fingerprint(inputs),
        "provenance": {
            "experiment_id": experiment.get("id"),
            "source_revision": experiment.get("source_revision"),
            "source_snapshot_sha256": experiment.get("snapshot_sha256"),
            "comparison_format": comparison.get("format"),
            "run_ids": [row["run_id"] for row in run_evidence],
            "evaluation_splits": sorted({row["evaluation_split"] for row in run_evidence}),
        },
        "observations": {
            "run_count": len(run_evidence),
            "completed_run_count": len(completed),
            "comparison_group_count": len(groups),
            "critical_violation_count": violations,
            "missing_measurements": missing,
        },
        "proposals": proposals,
        "abstentions": abstentions,
        "limitations": list(dict.fromkeys(limitations)),
        "human_review_required": True,
        "automatic_actions_permitted": [],
        "prohibited_actions": PROHIBITED_ACTIONS,
    }
    report["report_sha256"] = fingerprint(report)
    return report
