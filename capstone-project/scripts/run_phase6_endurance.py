"""Run a bounded coordinator endurance and durable restart-recovery drill."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from capstone_rag import config, experiments
from capstone_rag.ai_settings import Registry
from capstone_rag.store import Store


def _copy_database(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as old, sqlite3.connect(target) as new:
        old.backup(new)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = output / "operations.db"
    if database.exists():
        raise ValueError(f"Refusing to overwrite endurance evidence: {database}")
    _copy_database(Path(config.DB_PATH), database)
    source = Store(str(database))
    registry = Registry(output / "ai_configuration.db")
    manager = experiments.Manager(registry)
    try:
        specification = {
            "name": "Phase 6 bounded coordinator endurance",
            "level": "system",
            "profile_ids": ["gateway-default"],
            "architecture_ids": ["candidate-a"],
            "scenario_ids": ["phone-log"],
            "repetitions": 1,
            "duration_seconds": 30,
            "max_trials": 12,
            "concurrency": 1,
            "approval_mode": "scripted",
            "token_limit": 64,
            "timeout_seconds": 120,
            "failure_limit": 2,
            "minimum_free_disk_bytes": 64 * 1024 * 1024,
        }
        planned = experiments.preview(registry, specification)
        launched = manager.launch(planned, source)
        manager.threads[launched["id"]].join(300)
        if manager.threads[launched["id"]].is_alive():
            manager.stop(launched["id"])
            raise TimeoutError("Endurance campaign exceeded five minutes")
        campaign = registry.get("experiments", launched["id"])
        runs = manager.runs(launched["id"])

        drill_id = "EXP-phase6-restart-drill"
        manager.save({"id": drill_id, "status": "running", "created_at": time.time()})
        drill_root = output / "restart-drill"
        before = {}
        for state in ("completed", "running", "queued"):
            directory = drill_root / state
            directory.mkdir(parents=True, exist_ok=True)
            identifier = "DRILL-" + state
            record = {
                "id": identifier,
                "experiment_id": drill_id,
                "status": state,
                "directory": str(directory),
                "evidence_marker": "must-survive-reconciliation",
            }
            manager._store_run(record)
            before[identifier] = record
        manager.recover()
        after = {row["id"]: row for row in manager.runs(drill_id)}
        recovery_passed = (
            after["DRILL-completed"]["status"] == "completed"
            and after["DRILL-running"]["status"] == "failed"
            and after["DRILL-queued"]["status"] == "queued"
            and all(row["evidence_marker"] == "must-survive-reconciliation" for row in after.values())
            and registry.get("experiments", drill_id)["status"] == "incomplete"
        )

        task_script = (Path(config.PROJECT_ROOT) / "deploy" / "windows" / "Register-CapstoneTask.ps1").read_text(encoding="utf-8")
        service_contract = {
            "local_service_identity": (
                "S-1-5-19" in task_script or "NT AUTHORITY\\LOCAL SERVICE" in task_script
            ),
            "startup_trigger": "New-ScheduledTaskTrigger -AtStartup" in task_script,
            "browser_process_required": False,
        }
        report = {
            "format": "capstone-phase6-endurance-v1",
            "generated_at": time.time(),
            "campaign": campaign,
            "runs": runs,
            "completed_runs": sum(row["status"] == "completed" for row in runs),
            "expected_review_pauses": sum(
                row["status"] == "paused"
                and bool((row.get("metrics") or {}).get("intentional_human_pause"))
                for row in runs
            ),
            "failed_runs": sum(row["status"] == "failed" for row in runs),
            "restart_recovery": {"before": before, "after": after, "passed": recovery_passed},
            "service_contract": service_contract,
            "limitations": [
                "This is a 30-second bounded coordinator soak, not a long-duration production load claim.",
                "The phone-log fixture intentionally performs no physical model inference or business action.",
            ],
        }
        report_path = output / "endurance-report.json"
        report_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        passed = (
            campaign["status"] in ("completed", "paused")
            and bool(runs)
            and not report["failed_runs"]
            and all(
                row["status"] == "completed"
                or (
                    row["status"] == "paused"
                    and bool((row.get("metrics") or {}).get("intentional_human_pause"))
                )
                for row in runs
            )
            and recovery_passed
            and all(service_contract.values())
        )
        print(json.dumps({
            "report": str(report_path),
            "campaign_id": launched["id"],
            "status": campaign["status"],
            "runs": len(runs),
            "restart_recovery_passed": recovery_passed,
            "service_contract": service_contract,
        }, indent=2))
        return 0 if passed else 1
    finally:
        source.close()


if __name__ == "__main__":
    raise SystemExit(main())
