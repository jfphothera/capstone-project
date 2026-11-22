"""Execute the bounded Phase 6 candidate and research-loop acceptance campaign."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from capstone_rag import config, experiments
from capstone_rag.ai_settings import Registry
from capstone_rag.evaluation_engine import compare
from capstone_rag.research_loop import ResearchLedger
from capstone_rag.store import Store


B_CHUNKS = "31c7e224f45b9ef88dfe0235bca8364ca8c7ca850324d3f8e434607eeae732a3"
B_INDEX = "ee4005ac5e04d97b867e31a5d42fad6b61c428ad64bb1b0dd491d87c21d8c1e8"
C_CHUNKS = "2609a02590ed1d2e456c391e29845d7aa6d330c30188932481ebe503b73125de"
C_INDEX = "2b55e93e8c77fb91c62595f3b47ac00587c54120d5dcfd692b1aea090201423a"
CURATOR = {
    "candidate-b-direct": {"chunk_artifact_hash": B_CHUNKS, "index_hash": B_INDEX},
    "candidate-c-tot": {"chunk_artifact_hash": C_CHUNKS, "index_hash": C_INDEX},
}


def _copy_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError(f"Refusing to overwrite acceptance database: {target}")
    with sqlite3.connect(source) as old, sqlite3.connect(target) as new:
        old.backup(new)


def _wait(manager: experiments.Manager, experiment_id: str, ceiling: int) -> tuple[dict, list[dict]]:
    thread = manager.threads[experiment_id]
    deadline = time.time() + ceiling
    last = None
    while thread.is_alive() and time.time() < deadline:
        rows = manager.runs(experiment_id)
        summary = {
            state: sum(row["status"] == state for row in rows)
            for state in ("queued", "running", "paused", "completed", "failed")
        }
        if summary != last:
            print(json.dumps({"experiment_id": experiment_id, "runs": summary}), flush=True)
            last = summary
        thread.join(2)
    if thread.is_alive():
        manager.stop(experiment_id)
        raise TimeoutError(f"Campaign {experiment_id} exceeded {ceiling} seconds")
    return manager.registry.get("experiments", experiment_id), manager.runs(experiment_id)


def _spec(name: str, architectures: list[str], scenarios: list[str], repetitions: int) -> dict:
    return {
        "name": name,
        "level": "system",
        "profile_ids": ["gateway-default"],
        "architecture_ids": architectures,
        "curator_retrievals": {
            key: value for key, value in CURATOR.items() if key in architectures
        },
        "scenario_ids": scenarios,
        "repetitions": repetitions,
        "concurrency": 1,
        "seed": 61,
        "approval_mode": "scripted",
        "timeout_seconds": 1200,
        "failure_limit": 3,
        "max_trials": len(architectures) * len(scenarios) * repetitions,
        "token_limit": 240,
        "minimum_free_disk_bytes": 64 * 1024 * 1024,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--ceiling", type=int, default=3600)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    registry = Registry(output / "ai_configuration.db")
    availability = registry.validate_available(registry.get("profiles", "gateway-default"))
    if not availability["passed"]:
        raise SystemExit("Required local generation profile is unavailable: " + json.dumps(availability))

    source_path = output / "operations.db"
    _copy_database(Path(config.DB_PATH), source_path)
    source = Store(str(source_path))
    manager = experiments.Manager(registry)
    try:
        initial_spec = _spec(
            "Phase 6 A/B/C retrieval, multi-turn, and recovery acceptance",
            ["candidate-a", "candidate-b-direct", "candidate-c-tot"],
            ["corrected-identity-multiturn", "transient-tool-recovery"],
            1,
        )
        initial_plan = experiments.preview(registry, initial_spec)
        initial = manager.launch(initial_plan, source)
        initial_record, initial_runs = _wait(manager, initial["id"], args.ceiling)
        initial_comparison = compare(initial_runs)

        ledger = ResearchLedger(registry)
        iteration = ledger.propose(
            initial_comparison,
            {
                "max_iterations": 3,
                "max_trials_per_iteration": 8,
                "max_concurrency": 2,
                "max_model_calls": 120,
                "max_tokens": 60000,
                "allowed_architecture_ids": ["candidate-a", "candidate-c-tot"],
                "allowed_profile_ids": ["gateway-default"],
                "allowed_scenario_ids": ["customer-nonresponse"],
                "evidence_refs": [initial["id"]],
                "exploration_reason": "Measure a paired guarded baseline and bounded tree candidate on held-out reviewer correction.",
            },
            "phase6-acceptance-operator",
        )
        approved = ledger.approve(
            iteration["id"],
            "phase6-acceptance-approver",
            "Authorize only the bounded synthetic paired Testing Lab follow-up.",
        )
        follow_spec = _spec(
            "Phase 6 approved paired holdout follow-up",
            ["candidate-a", "candidate-c-tot"],
            ["customer-nonresponse"],
            2,
        )
        follow_spec.update(
            research_iteration_id=approved["id"],
            model_call_budget=120,
            total_token_budget=60000,
        )
        follow_plan = experiments.preview(registry, follow_spec)
        dispatched = ledger.dispatch(
            approved["id"],
            follow_plan,
            manager,
            source,
            "phase6-acceptance-dispatcher",
            "Execute the approved paired holdout comparison against the same isolated source database.",
        )
        follow_id = dispatched["experiment"]["id"]
        follow_record, follow_runs = _wait(manager, follow_id, args.ceiling)
        follow_comparison = compare(follow_runs)
        proposed_followup = ledger.propose_followup(
            approved["id"],
            follow_comparison,
            {"evidence_refs": [follow_id]},
        )

        report = {
            "format": "capstone-phase6-acceptance-v1",
            "generated_at": time.time(),
            "evidence_class": "physical_local_development_inference_and_isolated_synthetic_actions",
            "gateway_physical_execution_claimed": False,
            "initial_campaign": initial_record,
            "initial_runs": initial_runs,
            "initial_comparison": initial_comparison,
            "approved_iteration": ledger.get(approved["id"]),
            "followup_campaign": follow_record,
            "followup_runs": follow_runs,
            "followup_comparison": follow_comparison,
            "next_bounded_followup": proposed_followup,
            "curator_artifacts": CURATOR,
            "limitations": [
                "Inference ran on the local development endpoint, not through the physical Gateway.",
                "All customer, reviewer, tool-failure, and business actions are isolated synthetic fixtures.",
                "No candidate is promoted and no statistical winner is inferred.",
            ],
        }
        report_path = output / "acceptance-report.json"
        report_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        summary = {
            "report": str(report_path),
            "initial": {"id": initial["id"], "status": initial_record["status"]},
            "followup": {"id": follow_id, "status": follow_record["status"]},
            "next_followup": proposed_followup["id"],
            "run_states": {
                state: sum(row["status"] == state for row in initial_runs + follow_runs)
                for state in sorted({row["status"] for row in initial_runs + follow_runs})
            },
        }
        print(json.dumps(summary, indent=2), flush=True)
        return 0 if all(
            row["status"] == "completed" for row in initial_runs + follow_runs
        ) else 1
    finally:
        source.close()


if __name__ == "__main__":
    raise SystemExit(main())
