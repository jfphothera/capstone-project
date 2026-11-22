"""Build the separate deterministic Phase 1 regression/challenge corpus.

The generator reads authoritative synthetic relationships from an operational
SQLite snapshot and writes only ``data/phase1/challenge_scenarios.jsonl`` (or
an explicitly supplied path).  It never edits the original 1,000-ticket pool.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "operations.db"
DEFAULT_OUT = ROOT / "data" / "phase1" / "challenge_scenarios.jsonl"
PROTECTED = {
    (ROOT / "data" / "ticket_log.jsonl").resolve(),
    (ROOT / "data" / "evaluation" / "scenarios.jsonl").resolve(),
}


def rows(connection, where, parameters=()):
    return [dict(row) for row in connection.execute(
        "SELECT serial_number,model,revision,revision_status,account_id "
        "FROM devices WHERE " + where + " ORDER BY serial_number", parameters)]


def fixture(connection, *, model=None, status=None, revision=None):
    clauses, values = [], []
    for column, value in (("model", model), ("revision_status", status),
                          ("revision", revision)):
        if value is not None:
            clauses.append(column + "=?")
            values.append(value)
    found = rows(connection, " AND ".join(clauses), values)
    if not found:
        raise RuntimeError(f"No authoritative fixture for {clauses} {values}")
    return found[0]


def scenario(identifier, category, message, expected, device=None, turns=None):
    return {
        "scenario_id": identifier,
        "dataset": "phase1_integrity_challenge_v1",
        "category": category,
        "message": message,
        "turns": turns or [],
        "device": device,
        "expected": expected,
        "authority": "relationships read from operational SQLite; no invented match",
    }


def build(connection):
    by_model = {model: fixture(connection, model=model, status="verified")
                for model in ("P2400", "P3410", "P4800", "Series-M",
                              "3-Series", "Home-NB")}
    inferred = fixture(connection, model="P4800", status="inferred", revision="A")
    rev_b = fixture(connection, model="P4800", status="verified", revision="B")
    rev_c = fixture(connection, model="P4800", status="verified", revision="C")
    contradictory = fixture(connection, status="contradictory")

    out = []
    for n, (model, device) in enumerate(by_model.items(), 1):
        out.append(scenario(
            f"P1-MODEL-{n:02d}", "supported_model_family",
            f"My {model} will not power on. Serial: {device['serial_number']}",
            {"model": model, "serial_exact": True,
             "family_guidance_allowed": True, "safety_hold": False}, device))
    out.extend([
        scenario("P1-REV-INFERRED", "inferred_revision",
                 f"My P4800 will not power on. Serial: {inferred['serial_number']}",
                 {"exact_revision_procedure": False,
                  "family_guidance_allowed": True,
                  "human_revision_review": True}, inferred),
        scenario("P1-REV-B-GAP", "coverage_gap",
                 f"My P4800 will not power on. Serial: {rev_b['serial_number']}",
                 {"exact_revision_procedure": False,
                  "family_guidance_allowed": True,
                  "coverage_gap_reported": True}, rev_b),
        scenario("P1-REV-C", "verified_revision",
                 f"My P4800 will not power on. Serial: {rev_c['serial_number']}",
                 {"procedure_id": "PROC-P4800-PWR-C",
                  "family_guidance_allowed": True}, rev_c),
        scenario("P1-REV-CONTRADICT", "contradictory_revision",
                 f"The unit will not power on. Serial: {contradictory['serial_number']}",
                 {"exact_revision_procedure": False,
                  "human_revision_review": True}, contradictory),
        scenario("P1-SERIAL-DOB", "dob_serial_collision",
                 "My serial number is 1946-11-18.",
                 {"serial": None, "dob": "1946-11-18"}),
        scenario("P1-SERIAL-MALFORMED", "malformed_serial", "Serial: ZZ00041",
                 {"serial": None, "serial_issue": "malformed"}),
        scenario("P1-SERIAL-AMBIGUOUS", "conflicting_serials",
                 f"The labels show {by_model['P2400']['serial_number']} and {by_model['P3410']['serial_number']}.",
                 {"serial": None, "serial_issue": "conflicting"}),
        scenario("P1-UNKNOWN-MODEL", "unsupported_model", "My Aquex 100XL is not working.",
                 {"supported_model": None, "human_clarification": True}),
        scenario("P1-MISSING-HOMENB", "missing_accessory",
                 f"My Home-NB delivery is missing the goggles. Serial: {by_model['Home-NB']['serial_number']}",
                 {"procedure_id": "PROC-GEN-MISSING-ACCESSORY",
                  "movement_requires_human_approval": True}, by_model["Home-NB"]),
        scenario("P1-MISSING-P4800", "missing_accessory",
                 f"My P4800 delivery is missing its mains cord. Serial: {by_model['P4800']['serial_number']}",
                 {"procedure_id": "PROC-GEN-MISSING-ACCESSORY",
                  "movement_requires_human_approval": True}, by_model["P4800"]),
        scenario("P1-SAFETY", "adverse_event_safety",
                 f"My P4800 smells like it is burning. Serial: {by_model['P4800']['serial_number']}",
                 {"safety_hold": True, "ordinary_troubleshooting": False}, by_model["P4800"]),
        scenario("P1-PHONE-NOREPLY", "phone_no_reply",
                 "Automated call-back request logged from inbound IVR session.",
                 {"customer_email_draft": False, "internal_contact_task": True}),
        scenario("P1-MULTITURN", "multi_turn_clarification",
                 "My unit will not power on.",
                 {"accepted_serial_not_requested_again": True}, by_model["P2400"],
                 turns=[f"The serial number is {by_model['P2400']['serial_number']}."]),
        scenario("P1-RMA-APPROVAL", "approval_boundary",
                 f"Please replace my broken P3410. Serial: {by_model['P3410']['serial_number']}",
                 {"proposal_may_be_created": True, "shipment_executed": False,
                  "named_human_approval_required": True}, by_model["P3410"]),
        scenario("P1-SHIPPING", "shipping_status", "Where is order SO-UNKNOWN?",
                 {"invent_tracking": False, "clarification_or_no_match": True}),
        scenario("P1-SUPERSEDED", "superseded_document_trap",
                 f"My P4800 will not power on. Serial: {by_model['P4800']['serial_number']}",
                 {"document_id_forbidden": "QMS-DOC-0103"}, by_model["P4800"]),
    ])
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    output = args.out.resolve()
    if output in PROTECTED:
        raise SystemExit("Refusing to overwrite an original ticket/evaluation population")
    if output.exists() and not args.force:
        raise SystemExit(f"{output} exists; pass --force to reproduce it")
    source = sqlite3.connect(f"file:{args.db.resolve().as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    scenarios = build(source)
    source.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, sort_keys=True) + "\n"
                              for row in scenarios), encoding="utf-8")
    print(f"wrote {len(scenarios)} scenarios to {output}")


if __name__ == "__main__":
    main()
