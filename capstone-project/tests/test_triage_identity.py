"""Triage rules, two-point verification, and device-revision provenance.

No model is involved in any of this, so these tests are fast and exact.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from capstone_rag import config, identity, store as store_mod, triage  # noqa: E402


# =========================================================================
# Complaint definition
# =========================================================================

def test_request_to_buy_goggles_is_not_a_complaint():
    t = triage.triage("Hi, I would like to buy a spare pair of goggles for "
                      "my P4800. How much are they?")
    assert t.complaint.is_complaint is False
    assert t.complaint.category == "not_a_complaint"


def test_goggles_missing_from_the_box_is_a_complaint():
    t = triage.triage("The goggles that should have come with the device "
                      "were missing from the box.")
    assert t.complaint.is_complaint is True
    assert t.complaint.category == "missing_component"


def test_missing_component_beats_purchase_intent_in_the_same_message():
    t = triage.triage("The goggles were missing from the box - how much "
                      "would it cost to order a replacement pair?")
    assert t.complaint.is_complaint is True
    assert t.complaint.category == "missing_component"


def test_broken_equipment_is_a_complaint():
    t = triage.triage("The unit is completely dead, no lights at all.")
    assert t.complaint.is_complaint is True
    assert t.complaint.category == "malfunction"


def test_cannot_operate_is_a_complaint():
    t = triage.triage("I cannot figure out how to start a session, the "
                      "instructions are unclear.")
    assert t.complaint.is_complaint is True
    assert t.complaint.category == "cannot_operate"


# =========================================================================
# Adverse events
# =========================================================================

def test_reported_burn_is_a_confirmed_adverse_event():
    t = triage.triage("I have a burn on my arm after yesterday's session "
                      "and went to urgent care.")
    assert t.adverse_event.is_adverse_event is True
    assert t.adverse_event.certainty == "confirmed"
    assert t.priority == triage.CRITICAL


def test_mild_redness_is_a_suspected_adverse_event_for_human_review():
    t = triage.triage("After my session I had some mild redness in the "
                      "treated area.")
    assert t.adverse_event.is_adverse_event is True
    assert t.adverse_event.certainty == "suspected"
    assert t.adverse_event.requires_human_determination is True
    assert t.priority == triage.CRITICAL


def test_burning_smell_is_a_hazard_not_an_injury():
    """'burning smell' contains 'burn'. It must not become an injury report."""
    t = triage.triage("There was a burning smell from the unit and the "
                      "casing was too hot to touch.")
    assert t.adverse_event.is_adverse_event is False
    assert t.adverse_event.certainty == "none"
    assert t.adverse_event.hazard_indicators
    assert t.classification == "device_safety_hazard"
    assert t.priority == triage.CRITICAL
    assert t.adverse_event.requires_human_determination is True


def test_hazard_plus_reaction_is_a_suspected_adverse_event():
    t = triage.triage("I noticed redness afterwards and there was a smell "
                      "of burning from the unit.")
    assert t.adverse_event.is_adverse_event is True
    assert t.adverse_event.certainty == "suspected"
    assert t.priority == triage.CRITICAL


def test_critical_cases_require_immediate_escalation():
    t = triage.triage("I have a blister where the device touched my skin.")
    assert triage.requires_immediate_escalation(t) is True
    ordinary = triage.triage("The unit will not power on.")
    assert triage.requires_immediate_escalation(ordinary) is False


# =========================================================================
# Priority
# =========================================================================

def test_frustration_alone_does_not_raise_priority():
    angry = triage.triage(
        "This is absolutely ridiculous and unacceptable. I am furious. "
        "When is my order arriving?")
    calm = triage.triage("When is my order arriving?")
    assert angry.priority == calm.priority == triage.LOW
    assert angry.signals["frustration_detected"]
    assert angry.signals["frustration_affected_priority"] is False


def test_cannot_use_needed_equipment_is_high():
    t = triage.triage("The unit is completely dead so I cannot use it for "
                      "my treatments at all.")
    assert t.priority == triage.HIGH


def test_repeated_unresolved_failure_is_high():
    t = triage.triage("The lamp has failed again, this is the third time "
                      "and it is still not fixed.")
    assert t.priority == triage.HIGH


def test_repeated_unsuccessful_contact_is_high():
    t = triage.triage("I have called twice about my broken unit and no one "
                      "has got back to me.")
    assert t.priority == triage.HIGH


def test_ordinary_enquiry_is_low():
    assert triage.triage("Can you tell me the tracking number for my "
                         "order?").priority == triage.LOW


# =========================================================================
# Quality classification
# =========================================================================

def test_colour_mismatch_is_a_qc_discrepancy_not_a_defect_conclusion():
    t = triage.triage("The power cord that arrived is white but the manual "
                      "shows a black power cord.")
    assert t.quality.qc_discrepancy is True
    assert t.quality.qa_document_concern is False
    assert t.classification == "qc_discrepancy"
    assert "not an established manufacturing defect" in t.quality.reason


def test_documentation_problem_is_a_qa_concern():
    t = triage.triage("The manual says to press the blue button but there "
                      "is no blue button; the instructions are wrong.")
    assert t.quality.qa_document_concern is True
    assert t.classification == "qa_document_concern"


def test_qc_and_qa_are_distinct_from_complaints_and_adverse_events():
    qc = triage.triage("The power cord is white but the spec says black.")
    assert qc.adverse_event.is_adverse_event is False
    assert qc.complaint.is_complaint is False
    assert qc.quality.qc_discrepancy is True


# =========================================================================
# Two-point verification
# =========================================================================

@pytest.fixture(scope="module")
def st():
    s = store_mod.get_store()
    if not s.one("SELECT 1 c FROM customers LIMIT 1"):
        pytest.skip("run scripts/import_synthetic.py first")
    return s


@pytest.fixture(scope="module")
def known(st):
    dev = st.one("SELECT * FROM devices WHERE serial_number='98148469'")
    cust = st.customer(dev["account_id"])
    return cust, dev


def test_name_plus_serial_verifies(st, known):
    cust, dev = known
    claim = identity.Claim(name=cust["customer_name"],
                           serial=dev["serial_number"])
    res = identity.verify(st, claim)
    assert res.verified is True
    assert set(res.points_matched) == {identity.POINT_NAME,
                                       identity.POINT_SERIAL}


def test_name_plus_dob_verifies_without_a_serial(st):
    cust = st.one("SELECT * FROM customers WHERE date_of_birth IS NOT NULL "
                  "LIMIT 1")
    claim = identity.Claim(name=cust["customer_name"],
                           dob=cust["date_of_birth"])
    res = identity.verify(st, claim)
    assert res.verified is True
    assert identity.POINT_DOB in res.points_matched


def test_one_point_alone_is_not_enough(st, known):
    cust, dev = known
    res = identity.verify(st, identity.Claim(serial=dev["serial_number"]))
    assert res.verified is False
    assert res.points_matched == [identity.POINT_SERIAL]
    assert res.needs_more_information is True


def test_phone_is_a_locator_not_a_verification_point(st, known):
    cust, _dev = known
    res = identity.verify(st, identity.Claim(phone=cust["phone"]))
    assert res.verified is False, "a phone number alone must not verify"
    assert "phone" in res.locators_used
    assert identity.POINT_NAME not in res.points_matched


def test_wrong_dob_is_a_contradiction_not_a_near_miss(st):
    cust = st.one("SELECT * FROM customers WHERE date_of_birth IS NOT NULL "
                  "LIMIT 1")
    res = identity.verify(st, identity.Claim(name=cust["customer_name"],
                                             dob="1900-01-01"))
    assert res.verified is False
    assert res.contradictions
    assert res.needs_more_information is False


def test_serial_is_never_fuzzy_matched(st, known):
    """One transposed digit is a different unit, not a typo to correct."""
    cust, dev = known
    bad = dev["serial_number"][:-2] + "99"
    res = identity.verify(st, identity.Claim(name=cust["customer_name"],
                                             serial=bad))
    assert res.verified is False
    assert any("matched exactly" in c for c in res.contradictions)


def test_serial_belonging_to_another_account_is_a_contradiction(st, known):
    cust, _dev = known
    other = st.one("SELECT * FROM devices WHERE account_id != ? LIMIT 1",
                   (cust["account_id"],))
    res = identity.verify(st, identity.Claim(name=cust["customer_name"],
                                             serial=other["serial_number"]))
    assert res.verified is False
    assert any("different account" in c for c in res.contradictions)


def test_unknown_details_locate_nobody(st):
    res = identity.verify(st, identity.Claim(name="Nobody Atall",
                                             dob="1970-01-01"))
    assert res.verified is False
    assert res.account_id is None


def test_request_message_does_not_ask_for_what_was_already_given(st, known):
    cust, dev = known
    res = identity.verify(st, identity.Claim(serial=dev["serial_number"]))
    msg = identity.request_message(res)
    assert "serial number" not in msg.lower()
    assert "date of birth" in msg.lower() or "name on the account" in msg.lower()
    assert msg.startswith("Thank you for contacting us. I'd be happy to help.")
    assert all(term not in msg.lower() for term in ("erp", "qms", "mismatch"))


def test_request_message_asks_only_for_remaining_evidence(st, known):
    cust, _dev = known
    res = identity.verify(st, identity.Claim(name=cust["customer_name"]))
    msg = identity.request_message(res)
    assert "name on the account" not in msg.lower()
    assert "could you please provide" in msg.lower()
    assert all(term not in msg.lower() for term in
               ("erp", "qms", "approval", "replacement", "refund"))


def test_claim_extraction_from_free_text():
    c = identity.Claim.from_text(
        "Hi, my DOB is 1986-10-19 and the serial is 98148469. "
        "You can reach me on (895) 907-8490.")
    assert c.dob == "1986-10-19"
    assert c.serial == "98148469"
    assert c.phone


@pytest.mark.parametrize("presented,canonical", [
    ("68177dv5", "68177-DV5"),
    ("68177 DV5", "68177-DV5"),
    ("68177 - dv5", "68177-DV5"),
    ("98 148 469", "98148469"),
    ("9814-8469", "98148469"),
])
def test_serial_presentation_variants_resolve_to_one_canonical_value(
        presented, canonical):
    known_serials = ("68177-DV5", "98148469")
    claim = identity.Claim.from_text(
        f"Serial number: {presented}", known_serials=known_serials)
    assert claim.serial == canonical
    assert claim.serial_issue is None


def test_serial_normalisation_never_substitutes_characters():
    resolution = identity.resolve_serial("68177-DVS", ("68177-DV5",))
    assert resolution.canonical is None
    assert resolution.status == "malformed"


def test_ambiguous_normalised_serial_is_not_guessed():
    known = ("12345-AB6", "12345AB6")
    resolution = identity.resolve_serial("12345 AB6", known)
    assert resolution.status == "ambiguous"
    assert resolution.canonical is None
    assert set(resolution.matches) == set(known)


def test_exact_canonical_serial_wins_even_if_normalised_key_is_ambiguous():
    known = ("12345-AB6", "12345AB6")
    resolution = identity.resolve_serial("12345-AB6", known)
    assert resolution.status == "matched"
    assert resolution.canonical == "12345-AB6"


# =========================================================================
# Revision provenance (rule RV-1)
# =========================================================================

def test_verified_revision_may_drive_troubleshooting(st):
    dev = st.device("98148469")
    assert dev["revision_status"] == config.REVISION_VERIFIED
    a = identity.assess_revision(dev)
    assert a.usable_for_troubleshooting is True


def test_inferred_revision_may_not_drive_troubleshooting_alone(st):
    dev = st.device("52385502")
    assert dev["revision_status"] == config.REVISION_INFERRED
    a = identity.assess_revision(dev)
    assert a.usable_for_troubleshooting is False
    assert "not been verified" in a.reason


def test_human_confirmation_unlocks_an_inferred_revision(st):
    dev = st.device("52385502")
    a = identity.assess_revision(dev, human_confirmation="rep.jordan")
    assert a.usable_for_troubleshooting is True
    assert a.human_confirmed_by == "rep.jordan"


def test_contradictory_revision_is_never_usable(st):
    dev = st.device("88445165")
    assert dev["revision_status"] == config.REVISION_CONTRADICTORY
    a = identity.assess_revision(dev)
    assert a.usable_for_troubleshooting is False
    assert "contradict" in a.reason


def test_most_of_the_fleet_is_inferred_not_verified(st):
    """Guards against a rule that quietly promotes inferences to verified."""
    rows = st.query("SELECT revision_status, COUNT(*) c FROM devices "
                    "GROUP BY revision_status")
    counts = {r["revision_status"]: r["c"] for r in rows}
    assert counts.get(config.REVISION_INFERRED, 0) > 0
    assert counts.get(config.REVISION_VERIFIED, 0) > 0
    assert counts.get(config.REVISION_INFERRED, 0) > counts.get(
        config.REVISION_VERIFIED, 0)
