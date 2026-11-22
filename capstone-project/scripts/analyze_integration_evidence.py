"""Create focused QMS gap and development-only failure review artifacts."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    rows = read_jsonl(Path(args.dataset))
    by_id = {row["scenario_id"]: row for row in rows}
    run = json.loads(Path(args.run).read_text(encoding="utf-8"))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    reviewed = []
    for detail in run["details"]:
        if detail.get("split") != "development":
            continue
        relevant = [name for name in detail.get("failures", [])
                    if name in {"classification", "priority"}]
        if not relevant:
            continue
        source = by_id[detail["scenario_id"]]
        reviewed.append({
            "scenario_id": detail["scenario_id"],
            "category": detail["category"],
            "failure_dimensions": relevant,
            "subject": source["subject"],
            "message_excerpt": " ".join(source["initial_message"].split())[:240],
            "expected_classification": detail["expected"]["classification"],
            "actual_classification": detail["actual"]["classification"],
            "expected_priority": detail["expected"]["priority"],
            "actual_priority": detail["actual"]["priority"],
        })

    failure_report = {
        "scope": "development split only",
        "source_run": run["run_id"],
        "development_cases": sum(d.get("split") == "development"
                                 for d in run["details"]),
        "reviewed_failure_cases": len(reviewed),
        "by_category": dict(Counter(row["category"] for row in reviewed)),
        "by_transition": dict(Counter(
            f"{row['expected_classification']} -> {row['actual_classification']}"
            for row in reviewed
            if "classification" in row["failure_dimensions"])),
        "priority_transitions": dict(Counter(
            f"{row['expected_priority']} -> {row['actual_priority']}"
            for row in reviewed
            if "priority" in row["failure_dimensions"])),
        "cases": reviewed,
        "held_out_policy": (
            "Held-out failures were not inspected or used for tuning by this analysis. "
            "Aggregate full-run metrics remain visible in the source evidence."
        ),
    }
    (output / "development-failure-analysis.json").write_text(
        json.dumps(failure_report, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Development complaint and priority failure analysis", "",
        f"Source: `{run['run_id']}`. Scope: development split only "
        f"({failure_report['development_cases']} cases).", "",
        f"Reviewed cases with complaint/classification or priority errors: "
        f"**{len(reviewed)}**.", "",
        "Held-out case details were not inspected or used for tuning in this "
        "analysis. Aggregate metrics remain in the source run.", "",
        "## Pattern counts", "",
        f"- Categories: `{failure_report['by_category']}`",
        f"- Classification transitions: `{failure_report['by_transition']}`",
        f"- Priority transitions: `{failure_report['priority_transitions']}`",
        "", "## Case review", "",
        "| Scenario | Category | Dimensions | Expected → actual classification | "
        "Expected → actual priority | Subject |",
        "|---|---|---|---|---|---|",
    ]
    for row in reviewed:
        lines.append(
            f"| {row['scenario_id']} | {row['category']} | "
            f"{', '.join(row['failure_dimensions'])} | "
            f"{row['expected_classification']} → {row['actual_classification']} | "
            f"{row['expected_priority']} → {row['actual_priority']} | "
            f"{row['subject'].replace('|', '/')} |"
        )
    (output / "development-failure-analysis.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")

    gaps = [row for row in rows if row.get("qms_required") and
            not row.get("expected_procedure")]
    policy_reasons = {
        "identity_unverified_blocks_device_instructions",
        "safety_hold_blocks_ordinary_troubleshooting",
    }
    gap_report = {
        "total_qms_required": sum(bool(row.get("qms_required")) for row in rows),
        "covered": sum(bool(row.get("qms_required") and
                            row.get("expected_procedure")) for row in rows),
        "deliberate_gaps": len(gaps),
        "by_reason": dict(Counter(row.get("qms_gap_reason") for row in gaps)),
        "by_disposition": dict(Counter(
            "runtime_guardrail" if row.get("qms_gap_reason") in policy_reasons
            else "scope_or_content_gap" for row in gaps)),
        "cases": [{
            "scenario_id": row["scenario_id"],
            "split": row["split"],
            "category": row["category"],
            "model": row.get("device_model"),
            "revision": row.get("device_revision"),
            "reason": row.get("qms_gap_reason"),
        } for row in gaps],
    }
    (output / "qms-gap-review-20261027.json").write_text(
        json.dumps(gap_report, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# QMS deliberate-gap review", "",
        f"Of {gap_report['total_qms_required']} QMS-relevant scenarios, "
        f"{gap_report['covered']} have an applicable customer-facing procedure "
        f"and {gap_report['deliberate_gaps']} correctly abstain.", "",
        "The gap count is not a count of missing documents. Identity and safety "
        "guards deliberately block ordinary troubleshooting; other rows represent "
        "unsupported scope, restricted audience, revision exclusions, missing "
        "error-code content, or insufficiently specific symptoms.", "",
        f"- Disposition: `{gap_report['by_disposition']}`",
        f"- Reasons: `{gap_report['by_reason']}`", "",
        "## Every abstention", "",
        "| Scenario | Split | Category | Device | Revision | Reason |",
        "|---|---|---|---|---|---|",
    ]
    for row in gap_report["cases"]:
        lines.append(
            f"| {row['scenario_id']} | {row['split']} | {row['category']} | "
            f"{row['model'] or '-'} | {row['revision'] or '-'} | {row['reason']} |"
        )
    (output / "qms-gap-review-20261027.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
