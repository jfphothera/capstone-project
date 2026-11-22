"""Synthetic dataset, safety variation, and replay-isolation tests."""

from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from capstone_rag import config, identity, scenarios, triage  # noqa: E402
from capstone_rag.store import Store  # noqa: E402


def test_dataset_has_exact_independent_match_and_email_rates():
    rows, report = scenarios.build(count=1000, persist=False)
    assert len(rows) == 1000
    assert report["erp_match"] == {"count": 900, "percent": 90.0}
    assert report["genuinely_unmatched"] == 100
    assert report["sender_email_in_erp"] == {"count": 100,
                                             "percent": 10.0}
    assert all(not (not r.expected_erp_match and r.sender_email_in_erp)
               for r in rows)
    assert report["referential_integrity"] == "PASS"


def test_dataset_has_followups_heldout_rows_and_qms_gaps():
    _, report = scenarios.build(count=1000, persist=False)
    assert report["requires_follow_up"] >= 300
    assert report["split"] == {"held_out": 200, "development": 800}
    assert report["qms"]["covered"] > 0
    assert report["qms"]["deliberate_gaps"] > 0


def test_serial_correction_clears_an_earlier_format_error():
    current = identity.Claim.from_text(
        "Serial number: XL-0099.", known_serials=("98148469",))
    assert current.serial_issue == "malformed"
    fresh = identity.Claim.from_text(
        "Sorry, the correct serial is 98 148 469.",
        known_serials=("98148469",))
    scenarios._merge_claim(current, fresh, correction=True)
    assert current.serial == "98148469"
    assert current.serial_issue is None


def test_safety_language_and_explicit_negative_cases():
    confirmed = triage.triage("The unit caused pain, swelling and skin damage")
    assert confirmed.adverse_event.certainty == "confirmed"
    assert confirmed.priority == "Critical"
    hazard = triage.triage("There was an unusual electrical odor from the unit")
    assert hazard.classification == "device_safety_hazard"
    negative = triage.triage("No smoke, no overheating and no injury")
    assert negative.adverse_event.certainty == "none"
    assert not negative.adverse_event.hazard_indicators


def test_fast_evaluation_does_not_write_the_demo_database(tmp_path):
    before = os.path.getsize(config.DB_PATH)
    con = sqlite3.connect(config.DB_PATH)
    before_changes = con.execute("SELECT total_changes()").fetchone()[0]
    con.close()
    report = scenarios.run(limit=10, out_dir=str(tmp_path))
    assert report["cases"] == 10
    assert report["mode"] == "fast_deterministic"
    assert os.path.exists(report["exports"]["json"])
    assert os.path.getsize(config.DB_PATH) == before
    con = sqlite3.connect(config.DB_PATH)
    assert con.execute("SELECT total_changes()").fetchone()[0] == before_changes
    con.close()


def test_serial_grade_is_not_evaluated_when_identity_used_name_and_dob():
    rows, _ = scenarios.build(count=1000, persist=False)
    row = next(
        candidate for candidate in rows
        if candidate.expected_erp_match
        and identity.Claim.from_text(candidate.initial_message).serial is None
        and not candidate.follow_up_messages
    )
    report = scenarios._run_fast([row], Store(config.DB_PATH))
    grade = report["details"][0]["grades"]["serial_canonicalization"]
    assert grade["status"] == "NOT_EVALUATED"


def test_compare_requires_the_same_denominator(tmp_path):
    import json
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"run_id": "A", "cases": 10, "passed": 8}))
    b.write_text(json.dumps({"run_id": "B", "cases": 10, "passed": 9}))
    out = scenarios.compare(str(a), str(b))
    assert out["denominator"] == 10
    assert out["change"] == 1
