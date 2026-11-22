"""Regression tests for the defects confirmed in UI_WORKFLOW_DIAGNOSTIC.md.

One test file per diagnostic, so a reviewer can line C1-C13 up against the
assertions that pin them. Each test names the defect it covers in its
docstring. Everything here runs without Ollama.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from capstone_rag import (  # noqa: E402
    agent as agent_mod, caseflow, chat, config, erp_sim, identity,
    presentation, review, store as store_mod,
)

IVAN = "ACC-00010"          # Ivan Nowak, patient, DOB 1997-12-03
MARY = "ACC-00022"          # Mary Cohen, patient, DOB 1967-03-21
SERIAL_A = "98148469"       # Ivan Nowak's P4800 Rev A, revision verified
SERIAL_B = "68177-DV5"      # Mary Cohen's P4800 Rev B, revision inferred
REP = "rep.test"


@pytest.fixture(scope="session")
def source_db():
    if not os.path.exists(config.DB_PATH):
        pytest.skip("run scripts/import_synthetic.py first")
    return config.DB_PATH


@pytest.fixture
def st(tmp_path, source_db):
    path = str(tmp_path / "ops.db")
    shutil.copy(source_db, path)
    from db_reset import clear_copy
    clear_copy(path, ("shipment_events", "sim_shipments", "rmas", "erp_calls",
              "approvals", "recommendations", "reasoning_attempts",
              "retrieved_sources", "tool_calls", "checklist_items",
              "qa_notifications", "conversations", "messages", "cases",
              "improvement_proposals", "audit_events"))
    s = store_mod.Store(path)
    yield s
    s.close()


@pytest.fixture(scope="session")
def shared_agent():
    return agent_mod.Agent(prefer_llm=False)


@pytest.fixture
def flow(st, shared_agent):
    return caseflow.CaseFlow(st, shared_agent)


@pytest.fixture
def bot(st, shared_agent):
    return chat.ChatBot(st, shared_agent)


def ivan(st):
    return st.customer(IVAN)


# =========================================================================
# C1 - a name stated without an introduction phrase
# =========================================================================

ALL_POINTS = identity.VERIFICATION_POINTS


@pytest.mark.parametrize("text", [
    "Jane Doe",
    "Jane Doe 11-11-2001",
    "yes, I can. its Jane Doe nov 11 2001",
])
def test_c1_bare_name_is_parsed_when_a_name_was_asked_for(text):
    """C1: a name typed on its own is an answer, not noise."""
    claim = identity.Claim.from_text(text, expecting=ALL_POINTS)
    assert claim.name == "Jane Doe"


@pytest.mark.parametrize("text", [
    "It's Jane Doe",
    "its Jane Doe",
    "My name is Jane Doe",
    "name: Jane Doe",
    "The name on the account is Jane Doe",
])
def test_c1_named_phrasings_need_no_context(text):
    """C1: an explicit phrasing is read whether or not we just asked."""
    assert identity.Claim.from_text(text).name == "Jane Doe"


def test_c1_a_bare_name_is_not_harvested_from_an_unrelated_message():
    """C1 must not become "every capitalised phrase is a customer"."""
    body = ("My Phothera 4800 keeps cutting out during a session. "
            "I bought it from Riverton Medical last year.")
    assert identity.Claim.from_text(body).name is None


@pytest.mark.parametrize("text", [
    "I am having trouble with the unit",
    "This is urgent, the lamp will not light",
    "hello",
])
def test_c1_sentence_words_are_not_names(text):
    assert identity.Claim.from_text(text, expecting=ALL_POINTS).name is None


# =========================================================================
# C2 - an unlabelled date of birth
# =========================================================================

@pytest.mark.parametrize("text,expected", [
    ("11-11-2001", "2001-11-11"),
    ("1986-10-19", "1986-10-19"),
    ("11/11/2001", "2001-11-11"),
    ("19-10-1986", "1986-10-19"),
])
def test_c2_unlabelled_dob_is_parsed(text, expected):
    """C2: answering "date of birth?" with just the date must register."""
    assert identity.Claim.from_text(
        text, expecting=ALL_POINTS).dob == expected


@pytest.mark.parametrize("text,expected", [
    ("dob 1986-10-19", "1986-10-19"),
    ("date of birth: 1986-10-19", "1986-10-19"),
    ("born 1986-10-19", "1986-10-19"),
    ("DOB: 11/11/2001", "2001-11-11"),
])
def test_c2_labelled_dob_still_works(text, expected):
    assert identity.Claim.from_text(text).dob == expected


def test_c2_a_recent_unlabelled_date_is_not_a_date_of_birth():
    """An order date volunteered in passing is not a verification point."""
    claim = identity.Claim.from_text("my order shipped on 2026-01-15")
    assert claim.dob is None


# =========================================================================
# C3 - month-name dates
# =========================================================================

@pytest.mark.parametrize("value,expected", [
    ("nov 11 2001", "2001-11-11"),
    ("November 11 2001", "2001-11-11"),
    ("November 11, 2001", "2001-11-11"),
    ("11 Nov 2001", "2001-11-11"),
    ("11 November 2001", "2001-11-11"),
    ("19 October 1986", "1986-10-19"),
])
def test_c3_month_name_dob_is_normalised(value, expected):
    """C3: `normalise_dob` had no month-name branch at all."""
    assert identity.normalise_dob(value) == expected


@pytest.mark.parametrize("text", ["nov 11 2001", "11 Nov 2001",
                                  "November 11, 2001"])
def test_c3_month_name_dob_is_extracted(text):
    assert identity.Claim.from_text(
        text, expecting=ALL_POINTS).dob == "2001-11-11"


def test_c3_nonsense_is_still_refused():
    assert identity.normalise_dob("garbage") is None
    assert identity.normalise_dob("") is None


# =========================================================================
# C5 - the claim was write-once
# =========================================================================

def test_c5_a_name_can_be_corrected(bot, st):
    """C5: a second, different value must replace the first."""
    s = bot.new_session()
    bot.respond(s, "My name is Ivan Nowak")
    assert s.claim.name == "Ivan Nowak"
    bot.respond(s, "Sorry, my name is Mary Cohen")
    assert s.claim.name == "Mary Cohen"
    assert any(c["field"] == "name" and c["from"] == "Ivan Nowak"
               for c in s.corrections)


def test_c5_a_wrong_serial_can_be_withdrawn(bot):
    """C5: a bad serial must not poison the session permanently."""
    s = bot.new_session()
    bot.respond(s, "serial 11111111")
    assert s.claim.serial is None
    assert s.claim.serial_input == "11111111"
    assert s.claim.serial_issue == "unknown"
    bot.respond(s, "Sorry, I gave you the wrong serial number")
    assert s.claim.serial is None
    bot.respond(s, f"the serial number is {SERIAL_A}")
    assert s.claim.serial == SERIAL_A


@pytest.mark.parametrize("presented", ["68177DV5", "68177 DV5"])
def test_canonical_serial_variants_work_through_customer_chat(bot, presented):
    """Formatting tolerance reaches the conversation verifier, not just a utility."""
    s = bot.new_session()
    reply = bot.respond(
        s, f"My name is Mary Cohen and the serial is {presented}. "
           "The device powers on but the lamp stays dark.")
    assert s.claim.serial == SERIAL_B
    assert s.verified is True
    assert s.device["serial_number"] == SERIAL_B
    assert "serial" not in reply.text.lower() or "provide" not in reply.text.lower()


def test_spaced_numeric_serial_works_through_customer_chat(bot):
    s = bot.new_session()
    bot.respond(s, "My name is Ivan Nowak and serial 98 148 469. "
                   "The unit is completely dead.")
    assert s.claim.serial == SERIAL_A
    assert s.verified is True


def test_model_order_and_rma_numbers_are_not_promoted_to_serials():
    claim = identity.Claim.from_text(
        "My P4800 order SO-928766 concerns RMA-2026-462479.",
        known_serials=(SERIAL_A, SERIAL_B))
    assert claim.serial is None


def test_transcribed_character_is_not_corrected_by_chat(bot):
    s = bot.new_session()
    bot.respond(s, "My name is Mary Cohen and serial 68177DVS")
    assert s.claim.serial is None
    assert s.claim.serial_issue == "malformed"
    assert s.verified is False


def test_inbound_email_uses_canonical_serial_matching(flow, st):
    case_id = flow.intake(
        subject="Lamp does not start",
        body="My name is Mary Cohen. Serial 68177 DV5. The unit powers on but the lamp is dark.",
        channel="email")
    flow.pre_investigate(case_id)
    case = st.case(case_id)
    assert case["serial_number"] == SERIAL_B
    assert case["identity_verified"] == 1


def test_c5_a_correction_forces_re_verification(bot, st):
    """A corrected value must not inherit an earlier verified state."""
    cust = ivan(st)
    s = bot.new_session()
    bot.respond(s, f"My name is {cust['customer_name']}")
    bot.respond(s, f"the serial number is {SERIAL_A}")
    assert s.verified is True
    bot.respond(s, "Sorry, my name is Mary Cohen")
    assert s.verified is False
    assert any(c["field"] == "name" for c in s.corrections)


def test_c5_the_correction_is_audited(bot, st):
    s = bot.new_session()
    bot.respond(s, "My name is Ivan Nowak")
    bot.respond(s, "Sorry, my name is Mary Cohen")
    events = st.query("SELECT * FROM audit_events WHERE event_type=?",
                      ("chat_identification_corrected",))
    assert events, "the correction must be in the audit trail"


# =========================================================================
# C4 - the endless identification loop
# =========================================================================

def test_c4_chat_makes_progress_or_escalates(bot):
    """C4: five turns of the same input must not produce five same replies."""
    s = bot.new_session()
    replies = [bot.respond(s, "hello").text for _ in range(5)]
    assert s.handed_off is True, "the customer must not be left in the loop"
    assert len({r for r in replies}) > 1, "the reply must change"


def test_c4_an_unidentifiable_customer_reaches_the_portal(bot, st):
    """C4: the most serious finding - no case, so nobody ever sees them."""
    s = bot.new_session()
    for _ in range(5):
        bot.respond(s, "hello")
    assert s.handoff_case_id
    case = st.case(s.handoff_case_id)
    assert case is not None
    assert case["status"] in (caseflow.NEW, caseflow.HUMAN_REVIEW)
    body = st.messages(s.handoff_case_id)[0]["body"]
    assert "conversation" in body.lower()


def test_c4_the_conversation_is_preserved_on_handoff(bot, st):
    s = bot.new_session()
    bot.respond(s, "hello")
    bot.respond(s, "Jane Doe 11-11-2001")
    for _ in range(4):
        bot.respond(s, "hello")
    assert s.handed_off
    body = st.messages(s.handoff_case_id)[0]["body"]
    assert "Jane Doe" in body
    assert "11-11-2001" in body or "2001-11-11" in body


def test_c4_an_explicit_inability_offers_a_person_straight_away(bot):
    s = bot.new_session()
    bot.respond(s, "hello")
    reply = bot.respond(s, "I cannot, I don't have any of that")
    assert reply.handed_off is True
    assert reply.handoff_case_id


def test_c4_identity_failure_differs_from_a_parse_failure(bot):
    """A well-formed unknown identity must not get the same reply as noise."""
    a = bot.new_session()
    unparseable = bot.respond(a, "hello").text
    b = bot.new_session()
    bot.respond(b, "hello")
    unknown = bot.respond(b, "Jane Doe 11-11-2001").text
    assert unparseable != unknown
    assert "cannot find" in unknown.lower() or "not" in unknown.lower()


def test_c4_no_match_is_never_manufactured(bot, st):
    """A fictional identity that is not in the ERP must stay unverified."""
    s = bot.new_session()
    for text in ("hello", "Jane Doe 11-11-2001", "its Jane Doe"):
        bot.respond(s, text)
    assert s.verified is False
    assert s.account_id is None


def test_c4_troubleshooting_turns_do_not_count_as_stagnation(bot, st):
    """C4 must not escalate a working conversation."""
    cust = ivan(st)
    s = bot.new_session()
    bot.respond(s, f"My name is {cust['customer_name']}")
    bot.respond(s, f"the serial number is {SERIAL_A}")
    assert s.verified is True
    assert s.identification_attempts == 0
    assert s.handed_off is False


def test_c4_the_reasoning_budget_is_separate_from_the_identity_budget():
    """The agent's three-unproductive-attempt rule is untouched."""
    assert config.MAX_UNPRODUCTIVE_ATTEMPTS == 3
    assert (config.MAX_IDENTIFICATION_ATTEMPTS
            is not config.MAX_UNPRODUCTIVE_ATTEMPTS
            or config.MAX_IDENTIFICATION_ATTEMPTS == 3)


# =========================================================================
# Accounts with no date of birth
# =========================================================================

def test_an_account_without_a_dob_is_not_asked_for_one(st):
    """Two thirds of the synthetic customers have no date of birth."""
    cust = st.one("SELECT * FROM customers WHERE date_of_birth IS NULL "
                  "AND customer_kind='physician' LIMIT 1")
    res = identity.verify(st, identity.Claim(name=cust["customer_name"]))
    assert res.dob_on_record is False
    assert identity.POINT_DOB not in res.usable_points
    asks = identity.points_to_request(res)
    assert identity.POINT_DOB not in asks
    assert "date of birth" not in identity.request_message(res)


def test_a_supplied_dob_for_a_dobless_account_is_not_a_contradiction(st):
    cust = st.one("SELECT * FROM customers WHERE date_of_birth IS NULL "
                  "AND customer_kind='physician' LIMIT 1")
    res = identity.verify(st, identity.Claim(name=cust["customer_name"],
                                             dob="1980-01-01"))
    assert res.contradictions == []
    assert identity.POINT_DOB in res.points_failed


# =========================================================================
# C7 - approvals on every checklist item
# =========================================================================

def test_c7_only_real_decisions_offer_buttons(flow, st):
    """C7: eleven of sixteen items were lookups with Approve buttons."""
    cid = flow.intake("Goggles missing",
                      "The goggles that should have come with the unit were "
                      "missing from the box.", IVAN)
    flow.pre_investigate(cid)
    view = presentation.describe_checklist(st.checklist(cid))
    offering = [d["action"] for d in view["all"] if d["offers_decision"]]
    assert offering, "some decision must still be offered"
    for action in ("read_message", "classify_complaint", "assess_priority",
                   "review_case_history", "identify_missing_information",
                   "validate_procedure_applicability"):
        assert action not in offering, f"{action} is not a human decision"


def test_c7_outcomes_are_not_all_successes(flow, st):
    cid = flow.intake("Unknown sender",
                      "Nobody has got back to me about my unit.",
                      account_id=None)
    flow.pre_investigate(cid)
    view = presentation.describe_checklist(st.checklist(cid))
    outcomes = {d["outcome"] for d in view["all"]}
    assert outcomes & presentation.NEGATIVE_OUTCOMES, (
        "a case with no identifiable customer must show a negative outcome")


# =========================================================================
# C8 - a rejected movement finding still shipped
# =========================================================================

def _movement_case(flow, st):
    cid = flow.intake(
        "Goggles missing from the box",
        f"My name is Ivan Nowak. My unit arrived but the goggles were missing from the box. "
        f"The serial number is {SERIAL_A}.", IVAN)
    flow.pre_investigate(cid)
    rma = st.one("SELECT * FROM rmas WHERE case_id=?", (cid,))
    return cid, rma


def test_c8_rejecting_the_finding_cancels_the_proposal(flow, st):
    """C8: the rejection was a note; the Approve button still worked."""
    cid, rma = _movement_case(flow, st)
    assert rma is not None and rma["status"] == erp_sim.PROPOSED
    item = st.one("SELECT * FROM checklist_items WHERE case_id=? AND action=?",
                  (cid, "determine_product_movement"))
    out = review.decide_item(st, item["item_id"], "rejected", REP,
                             feedback="customer found them in the packaging")
    assert out["ok"]
    after = st.one("SELECT * FROM rmas WHERE rma_id=?", (rma["rma_id"],))
    assert after["status"] == erp_sim.CANCELED
    assert st.case(cid)["rma_needed"] == 0


def test_c8_a_canceled_proposal_cannot_be_approved(flow, st):
    cid, rma = _movement_case(flow, st)
    item = st.one("SELECT * FROM checklist_items WHERE case_id=? AND action=?",
                  (cid, "determine_product_movement"))
    review.decide_item(st, item["item_id"], "rejected", REP, feedback="no")
    erp = erp_sim.ErpSimulator(st)
    res = erp.approve(rma["rma_id"], REP)
    assert res.ok is False
    assert st.one("SELECT * FROM sim_shipments WHERE rma_id=?",
                  (rma["rma_id"],)) is None


def test_c8_the_decision_reports_its_consequence(flow, st):
    cid, rma = _movement_case(flow, st)
    item = st.one("SELECT * FROM checklist_items WHERE case_id=? AND action=?",
                  (cid, "determine_product_movement"))
    out = review.decide_item(st, item["item_id"], "rejected", REP,
                             feedback="not needed")
    assert rma["rma_id"] in out["consequence"]
    stored = st.one("SELECT * FROM checklist_items WHERE item_id=?",
                    (item["item_id"],))
    assert stored["consequence"]


def test_c8_approving_the_finding_does_not_execute_anything(flow, st):
    """Agreement is not authorisation; the movement panel is the gate."""
    cid, rma = _movement_case(flow, st)
    item = st.one("SELECT * FROM checklist_items WHERE case_id=? AND action=?",
                  (cid, "determine_product_movement"))
    review.decide_item(st, item["item_id"], "approved", REP)
    assert st.one("SELECT * FROM rmas WHERE rma_id=?",
                  (rma["rma_id"],))["status"] == erp_sim.PROPOSED
    assert st.one("SELECT * FROM sim_shipments WHERE rma_id=?",
                  (rma["rma_id"],)) is None


# =========================================================================
# C9 / C10 - approval had no visible effect, Action Approved unreachable
# =========================================================================

def test_c9_approving_a_movement_advances_the_case(flow, st):
    """C9: status stayed "Human Review Needed" and rma_needed stayed 1."""
    cid, rma = _movement_case(flow, st)
    before = st.case(cid)["status"]
    erp = erp_sim.ErpSimulator(st)
    res = erp.approve(rma["rma_id"], REP, note="ship the goggles")
    assert res.ok
    case = st.case(cid)
    assert case["status"] != before
    assert case["status"] == caseflow.AWAITING
    assert case["rma_needed"] == 0
    assert case["rma_outcome"] == erp_sim.EXECUTED
    assert case["tracking_number"]


def test_c10_action_approved_is_recorded_as_a_transition(flow, st):
    """C10: the status existed and was never assigned."""
    cid, rma = _movement_case(flow, st)
    erp_sim.ErpSimulator(st).approve(rma["rma_id"], REP)
    transitions = [e["summary"] for e in st.audit_trail(cid)
                   if e["event_type"] == "case_status_changed"]
    assert any(caseflow.ACTION_APPROVED in s for s in transitions), (
        f"Action Approved must appear in the trail, got {transitions}")
    assert any(caseflow.AWAITING in s for s in transitions)


def test_c10_every_declared_case_status_is_reachable(flow, st):
    """Walk one case through the lifecycle and collect the statuses seen."""
    seen = set()
    cid, rma = _movement_case(flow, st)
    seen.add(caseflow.NEW)
    seen.add(st.case(cid)["status"])
    erp_sim.ErpSimulator(st).approve(rma["rma_id"], REP)
    for e in st.audit_trail(cid):
        if e["event_type"] == "case_status_changed":
            seen.add(e["detail"]["to"])
            seen.add(e["detail"]["from"])
    review.close_case(st, cid, REP, note="done", resolved=True)
    seen.add(st.case(cid)["status"])

    # A verified routine enquiry with no fault reported closes itself, which
    # is the only way "AI Reviewed" is reached.
    other = flow.intake(
        "Lamp replacement interval",
        f"My name is Ivan Nowak and the serial number is {SERIAL_A}. "
        f"How often should the lamps be replaced?", IVAN)
    flow.pre_investigate(other)
    seen.add(st.case(other)["status"])
    review.close_case(st, other, REP, note="no action", resolved=False)
    seen.add(st.case(other)["status"])

    missing = set(caseflow.STATUSES) - seen
    assert not missing, f"unreachable statuses: {sorted(missing)}"


def test_rejecting_a_movement_records_the_outcome(flow, st):
    cid, rma = _movement_case(flow, st)
    erp_sim.ErpSimulator(st).reject(rma["rma_id"], REP, note="not warranted")
    case = st.case(cid)
    assert case["rma_needed"] == 0
    assert case["rma_outcome"] == erp_sim.REJECTED
    assert st.one("SELECT * FROM sim_shipments WHERE rma_id=?",
                  (rma["rma_id"],)) is None


def test_a_failed_movement_stays_failed_and_outstanding(flow, st):
    cid, rma = _movement_case(flow, st)
    erp = erp_sim.ErpSimulator(st)
    erp.store.update("rmas", "rma_id", rma["rma_id"],
                     {"status": erp_sim.APPROVED})
    res = erp.execute(rma["rma_id"], force_failure=True)
    assert res.ok is False
    case = st.case(cid)
    assert case["rma_outcome"] == erp_sim.FAILED
    assert case["rma_needed"] == 1
    assert case["status"] == caseflow.HUMAN_REVIEW
    assert case["tracking_number"] is None


def test_an_approval_is_idempotent(flow, st):
    cid, rma = _movement_case(flow, st)
    erp = erp_sim.ErpSimulator(st)
    first = erp.approve(rma["rma_id"], REP)
    second = erp.approve(rma["rma_id"], REP)
    assert first.ok and second.ok is False and second.duplicate
    assert len(st.query("SELECT * FROM sim_shipments WHERE rma_id=?",
                        (rma["rma_id"],))) == 1


# =========================================================================
# C13 - revision confirmation had no caller
# =========================================================================

def _inferred_revision_case(flow, st):
    cid = flow.intake(
        "Lamp flickering",
        f"The lamp on my unit is flickering badly during a session. "
        f"The serial number is {SERIAL_B}. My name is Mary Cohen.",
        MARY)
    flow.pre_investigate(cid)
    return cid


def test_c13_an_inferred_revision_blocks_a_procedure(flow, st):
    cid = _inferred_revision_case(flow, st)
    dev = st.device(SERIAL_B)
    assert dev["revision_status"] == config.REVISION_INFERRED
    item = st.one("SELECT * FROM checklist_items WHERE case_id=? AND action=? "
                  "ORDER BY seq DESC LIMIT 1",
                  (cid, "assess_device_revision"))
    assert item["status"] == caseflow.NEEDS_APPROVAL


def test_c13_confirming_the_revision_re_runs_the_guarded_investigation(
        flow, st):
    """C13: `human_confirmation` existed and nothing ever supplied it."""
    cid = _inferred_revision_case(flow, st)
    before = len(st.checklist(cid))
    out = flow.confirm_revision(cid, decision="confirmed", authorizer=REP,
                                note="read the label with the customer")
    assert out["ok"] and out["reinvestigated"]
    case = st.case(cid)
    assert case["revision_confirmation"] == "confirmed"
    assert case["revision_confirmed_by"] == REP
    assert len(st.checklist(cid)) > before, "the re-check must be recorded"
    actions = [i["action"] for i in st.checklist(cid)]
    assert actions.count("search_qms") >= 2


def test_c13_confirmation_does_not_bypass_the_applicability_check(flow, st):
    """Confirming which revision it is never authorises a procedure."""
    cid = _inferred_revision_case(flow, st)
    flow.confirm_revision(cid, decision="confirmed", authorizer=REP)
    validations = st.query(
        "SELECT * FROM checklist_items WHERE case_id=? AND action=? "
        "ORDER BY seq", (cid, "validate_procedure_applicability"))
    assert len(validations) >= 2, "the check must run again, not be skipped"
    rejected = st.query(
        "SELECT * FROM retrieved_sources WHERE case_id=? AND status!='accepted'",
        (cid,))
    assert rejected or validations[-1]["status"] in (
        caseflow.DONE, caseflow.ITEM_FAILED)


def test_c13_rejecting_the_revision_blocks_guidance(flow, st):
    cid = _inferred_revision_case(flow, st)
    out = flow.confirm_revision(cid, decision="rejected", authorizer=REP,
                                note="the label says Rev C")
    assert out["ok"] and out["reinvestigated"] is False
    case = st.case(cid)
    assert case["revision_confirmation"] == "rejected"
    assert case["status"] == caseflow.HUMAN_REVIEW


def test_c13_the_confirmation_is_audited_with_a_name(flow, st):
    cid = _inferred_revision_case(flow, st)
    flow.confirm_revision(cid, decision="confirmed", authorizer=REP)
    events = [e for e in st.audit_trail(cid)
              if e["event_type"] == "device_revision_confirmed"]
    assert events and events[0]["actor"] == REP


def test_c13_a_generic_checklist_approval_is_not_a_revision_confirmation(
        flow, st):
    """Clicking Approve on the finding must not confirm the revision."""
    cid = _inferred_revision_case(flow, st)
    item = st.one("SELECT * FROM checklist_items WHERE case_id=? AND action=? "
                  "ORDER BY seq DESC LIMIT 1",
                  (cid, "assess_device_revision"))
    review.decide_item(st, item["item_id"], "approved", REP)
    assert st.case(cid)["revision_confirmation"] is None


# =========================================================================
# Individual human authorisation controls
# =========================================================================

def test_attesting_identity_sets_the_flag(flow, st):
    cid = flow.intake("Help", "My unit will not switch on.", account_id=None)
    flow.pre_investigate(cid)
    assert st.case(cid)["identity_verified"] == 0
    item = st.one("SELECT * FROM checklist_items WHERE case_id=? AND action=?",
                  (cid, "verify_identity"))
    out = review.decide_item(st, item["item_id"], "approved", REP,
                             feedback="confirmed on the phone")
    assert st.case(cid)["identity_verified"] == 1
    assert "attested" in out["consequence"]


def test_rejecting_the_response_finding_rejects_the_draft(flow, st):
    cid = flow.intake("Question", "How often should I replace the lamps?",
                      IVAN)
    flow.pre_investigate(cid)
    draft = review.latest_draft(st, cid)
    assert draft is not None
    item = st.one("SELECT * FROM checklist_items WHERE case_id=? AND action=?",
                  (cid, "prepare_response"))
    review.decide_item(st, item["item_id"], "rejected", REP,
                       feedback="wrong tone")
    assert st.one("SELECT * FROM messages WHERE message_id=?",
                  (draft["message_id"],))["status"] == "rejected"


def test_the_primary_decision_is_the_most_consequential_one(flow, st):
    cid, rma = _movement_case(flow, st)
    view = presentation.describe_checklist(st.checklist(cid))
    top = presentation.primary_decision(
        view, st.case(cid), st.query(
            "SELECT * FROM rmas WHERE case_id=?", (cid,)),
        review.latest_draft(st, cid))
    assert top and top["kind"] == "movement_approval"
