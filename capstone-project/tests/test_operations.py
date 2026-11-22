"""Operational tests: cases, queue, ERP/RMA, shipping, chat, analytics.

Each test gets its own copy of the imported database, so nothing here
touches the live portal data or depends on another test's leftovers. The
agent runs with prefer_llm=False for determinism.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from capstone_rag import (  # noqa: E402
    agent as agent_mod, analytics, caseflow, chat, config, erp_sim,
    movement, queueing, review, store as store_mod, triage,
)

IVAN, MARY, PETER = "ACC-00010", "ACC-00022", "ACC-00026"
SERIAL_A = "98148469"       # P4800 Rev A, revision verified
SERIAL_B = "68177-DV5"      # P4800 Rev B, revision inferred
SERIAL_SM = "38180-BM2"     # Series-M Rev C, revision verified
SERIAL_HOME = "66383977"    # Home-NB, no QMS coverage at all
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
def ops(st, shared_agent):
    f = caseflow.CaseFlow(st, shared_agent)
    q = queueing.InferenceQueue(f)
    sim = queueing.ArrivalSimulator(f, q, min_seconds=1, max_seconds=2, seed=3)
    return queueing.Operations(store=st, flow=f, queue=q, simulator=sim,
                               erp=f.erp)


def make_case(flow, subject, body, account_id=IVAN, investigate=True):
    cid = flow.intake(subject, body, account_id=account_id)
    if investigate:
        flow.pre_investigate(cid)
    return cid


# =========================================================================
# Intake, triage and the checklist
# =========================================================================

def test_intake_is_fast_and_uses_no_model(flow, st):
    t0 = time.time()
    cid = flow.intake("Unit dead", "The unit is completely dead.", IVAN)
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"intake took {elapsed:.2f}s; it must not block"
    c = st.case(cid)
    assert c["priority"] and c["classification"]
    assert c["ai_state"] == caseflow.QUEUED


def test_critical_is_flagged_at_intake_before_any_investigation(flow, st):
    cid = flow.intake(
        "Burn", "I have a burn on my arm after a session. Ivan Nowak, "
                f"serial {SERIAL_A}.", IVAN)
    c = st.case(cid)
    assert c["priority"] == triage.CRITICAL
    assert c["status"] == caseflow.HUMAN_REVIEW
    assert c["ai_state"] == caseflow.QUEUED, "flagged without the model"
    assert st.query("SELECT * FROM qa_notifications WHERE case_id=?", (cid,))


def test_investigation_creates_a_full_checklist(flow, st):
    cid = make_case(flow, "Unit dead",
                    f"My name is Ivan Nowak and the serial is {SERIAL_A}. "
                    f"The unit is completely dead, no lights or fan.")
    items = st.checklist(cid)
    actions = [i["action"] for i in items]
    for required in ("read_message", "screen_adverse_event",
                     "classify_complaint", "assess_priority",
                     "identify_customer", "verify_identity",
                     "check_erp_records", "assess_device_revision",
                     "review_case_history", "search_qms",
                     "validate_procedure_applicability",
                     "evaluate_complaint", "determine_product_movement",
                     "prepare_response", "identify_missing_information"):
        assert required in actions, f"checklist missing {required}"
    for i in items:
        assert i["actor"] in (caseflow.AI, caseflow.REP)
        assert i["status"] in (caseflow.DONE, caseflow.ITEM_FAILED,
                               caseflow.PENDING, caseflow.NEEDS_APPROVAL,
                               caseflow.NA)
        assert i["executed_at"]


def test_failed_lookup_is_recorded_not_invented(flow, st):
    cid = make_case(flow, "Help", "Something is wrong with my machine.",
                    account_id=MARY)
    items = {i["action"]: i for i in st.checklist(cid)}
    erp = items["check_erp_records"]
    assert erp["status"] == caseflow.ITEM_FAILED
    assert erp["missing_info"]


def test_safety_case_skips_retrieval_entirely(flow, st):
    cid = make_case(flow, "Burning smell",
                    f"Peter Thomas, serial {SERIAL_SM}. There was a burning "
                    f"smell and the casing was too hot to touch.",
                    account_id=PETER)
    items = {i["action"]: i for i in st.checklist(cid)}
    assert items["search_qms"]["status"] == caseflow.NA
    assert st.one("SELECT COUNT(*) c FROM retrieved_sources WHERE case_id=?",
                  (cid,))["c"] == 0


def test_uncertain_adverse_event_is_retained_for_human_determination(flow, st):
    cid = flow.intake("Redness",
                      f"Ivan Nowak, serial {SERIAL_A}. I had some mild "
                      f"redness after my session.", IVAN)
    note = st.one("SELECT * FROM qa_notifications WHERE case_id=?", (cid,))
    assert note is not None, "an uncertain AE must not be silently discarded"
    assert note["kind"] == "possible_adverse_event"
    assert note["status"] == "open"

    review.determine_adverse_event(st, note["notification_id"],
                                   "not an adverse event", REP,
                                   rationale="Not device related.")
    after = st.one("SELECT * FROM qa_notifications WHERE notification_id=?",
                   (note["notification_id"],))
    assert after["determination"] and after["determined_by"] == REP
    assert after['determination_note'] == 'Not device related.'
    assert after['body'] == note['body']


def test_inferred_revision_blocks_revision_specific_guidance(flow, st):
    cid = make_case(flow, "Unit dead",
                    f"My name is Mary Cohen and the serial is {SERIAL_B}. "
                    f"The unit is completely dead.", account_id=MARY)
    items = {i["action"]: i for i in st.checklist(cid)}
    assert items["assess_device_revision"]["status"] == caseflow.NEEDS_APPROVAL
    assert items["search_qms"]["status"] == caseflow.PENDING
    assert st.case(cid)["status"] == caseflow.HUMAN_REVIEW


# =========================================================================
# Priority override
# =========================================================================

def test_priority_override_is_recorded_with_a_reason(flow, st):
    cid = make_case(flow, "Question", "Just a general question.",
                    investigate=False)
    before = st.case(cid)["priority"]
    review.override_priority(st, cid, triage.HIGH, "VIP account", REP)
    after = st.case(cid)
    assert after["priority"] == triage.HIGH
    assert after["priority_overridden_by"] == REP
    assert "VIP account" in after["priority_reason"]
    events = [e for e in st.audit_trail(cid)
              if e["event_type"] == "priority_overridden"]
    assert events and events[0]["detail"]["from"] == before


# =========================================================================
# Product movement rules
# =========================================================================

def test_missing_component_ships_a_part_not_the_device():
    t = triage.triage("The goggles were missing from the box.")
    d = movement.decide("The goggles were missing from the box.", t,
                        identity_verified=True)
    assert d.movement_type == movement.PART_OUTBOUND
    assert d.direction == movement.OUTBOUND
    assert any("goggles" in i["name"].lower() for i in d.items)
    assert d.rule == "MR-2"


def test_safety_event_returns_the_whole_device_for_quarantine():
    t = triage.triage("I got a burn during my session.")
    d = movement.decide("I got a burn during my session.", t,
                        identity_verified=True)
    assert d.movement_type == movement.DEVICE_RETURN
    assert d.direction == movement.INBOUND
    assert d.requires_human_review is True
    assert d.rule == "MR-1"


def test_exhausted_procedure_returns_the_device():
    t = triage.triage("The unit is still completely dead.")
    d = movement.decide("still completely dead", t, procedure_exhausted=True,
                        applicable_procedure="PROC-P4800-PWR-A",
                        identity_verified=True)
    assert d.movement_type == movement.DEVICE_RETURN
    assert d.rule == "MR-4"


def test_fault_with_steps_remaining_moves_nothing():
    t = triage.triage("The unit will not power on.")
    d = movement.decide("The unit will not power on.", t,
                        applicable_procedure="PROC-P4800-PWR-A",
                        identity_verified=True)
    assert d.movement_type == movement.NONE
    assert d.needs_transaction is False
    assert d.rule == "MR-6"


def test_fault_with_no_procedure_goes_to_a_person():
    t = triage.triage("The unit will not power on.")
    d = movement.decide("The unit will not power on.", t,
                        applicable_procedure=None, identity_verified=True)
    assert d.movement_type == movement.HUMAN_REVIEW
    assert d.requires_human_review is True


def test_a_replaceable_part_is_preferred_over_returning_the_device():
    """MR-3 must beat MR-4: ship the lamp, do not recall the chassis."""
    t = triage.triage("The lamp has failed and the unit is still dead.")
    d = movement.decide("the lamp has failed", t, procedure_exhausted=True,
                        applicable_procedure="PROC-P4800-LAMP-A",
                        identity_verified=True)
    assert d.movement_type == movement.PART_OUTBOUND
    assert d.rule == "MR-3"


def test_avoidable_full_return_is_detectable():
    """A whole-device return raised on a case that only described a part."""
    t = triage.triage("The lamp has failed and the unit is still dead.")
    d = movement.decide("the lamp has failed", t, procedure_exhausted=True,
                        applicable_procedure=None, identity_verified=True)
    assert d.movement_type == movement.DEVICE_RETURN
    assert d.rule == "MR-4"
    assert movement.avoidable_full_return(d, "the lamp has failed") is True


def test_safety_returns_are_never_counted_as_avoidable():
    t = triage.triage("I got a burn and the lamp has failed.")
    d = movement.decide("I got a burn and the lamp has failed", t,
                        identity_verified=True)
    assert d.rule == "MR-1"
    assert movement.avoidable_full_return(d, "the lamp has failed") is False


# =========================================================================
# ERP transactions, approval and idempotency
# =========================================================================

def _propose(ops, cid, kind=movement.DEVICE_RETURN):
    # Transaction fixtures represent an already verified customer context.
    from capstone_rag import identity
    verified=identity.verify(ops.store,identity.Claim(name='Ivan Nowak',serial=SERIAL_A),account_id=IVAN)
    assert verified.verified
    ops.store.update('cases','case_id',cid,{'identity_verified':1,'account_id':IVAN,'serial_number':SERIAL_A,
        'verification_points':json.dumps(verified.points_matched)})
    d = movement.MovementDecision(
        kind, movement.INBOUND if kind == movement.DEVICE_RETURN
        else movement.OUTBOUND,
        [{"part_number": "UNIT", "name": "Complete device"}]
        if kind == movement.DEVICE_RETURN
        else [{"part_number": "ACC-GOG-01", "name": "Protective goggles"}],
        rule="TEST", reason="test movement")
    return ops.erp.propose_movement(cid, d, account_id=IVAN,
                                    serial_number=SERIAL_A)


def test_proposal_authorises_nothing(ops, st):
    cid = ops.flow.intake("x", "unit dead", IVAN)
    res = _propose(ops, cid)
    assert res.rma["status"] == erp_sim.PROPOSED
    assert st.one("SELECT COUNT(*) c FROM sim_shipments")["c"] == 0


def test_approval_creates_linked_call_rma_shipment_and_label(ops, st):
    cid = ops.flow.intake("x", "unit dead", IVAN)
    rma_id = _propose(ops, cid).rma["rma_id"]
    res = ops.erp.approve(rma_id, REP, "authorised")
    assert res.ok
    call = st.one("SELECT * FROM erp_calls WHERE call_id=?",
                  (res.call["call_id"],))
    rma = st.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,))
    ship = st.one("SELECT * FROM sim_shipments WHERE rma_id=?", (rma_id,))
    assert call["status"] == erp_sim.EXECUTED
    assert rma["status"] == erp_sim.EXECUTED and rma["approved_by"] == REP
    assert ship and ship["tracking_number"].startswith("SIMFX")
    assert rma["call_id"] == call["call_id"]
    assert ship["case_id"] == cid
    assert os.path.exists(ship["label_path"])
    label = open(ship["label_path"], encoding="utf-8").read()
    assert "NOT VALID FOR SHIPPING" in label
    assert rma_id in label and ship["tracking_number"] in label


def test_rejection_creates_no_rma_or_label(ops, st):
    cid = ops.flow.intake("x", "unit dead", IVAN)
    rma_id = _propose(ops, cid).rma["rma_id"]
    res = ops.erp.reject(rma_id, REP, "not warranted")
    assert res.ok
    assert st.one("SELECT * FROM rmas WHERE rma_id=?",
                  (rma_id,))["status"] == erp_sim.REJECTED
    assert st.one("SELECT COUNT(*) c FROM sim_shipments")["c"] == 0
    assert not any(f.startswith(rma_id)
                   for f in os.listdir(config.LABEL_DIR)) \
        if os.path.isdir(config.LABEL_DIR) else True


def test_a_decision_cannot_be_applied_twice(ops):
    cid = ops.flow.intake("x", "unit dead", IVAN)
    rma_id = _propose(ops, cid).rma["rma_id"]
    assert ops.erp.approve(rma_id, REP).ok
    again = ops.erp.approve(rma_id, REP)
    assert again.ok is False and again.duplicate is True


def test_repeated_proposals_do_not_duplicate_the_transaction(ops, st):
    cid = ops.flow.intake("x", "unit dead", IVAN)
    first = _propose(ops, cid)
    second = _propose(ops, cid)
    assert second.duplicate is True
    assert second.rma["rma_id"] == first.rma["rma_id"]
    assert st.one("SELECT COUNT(*) c FROM rmas")["c"] == 1


def test_repeated_execution_does_not_duplicate_the_shipment(ops, st):
    cid = ops.flow.intake("x", "unit dead", IVAN)
    rma_id = _propose(ops, cid).rma["rma_id"]
    ops.erp.approve(rma_id, REP)
    again = ops.erp.execute(rma_id)
    assert again.duplicate is True
    assert st.one("SELECT COUNT(*) c FROM sim_shipments")["c"] == 1


def test_execution_requires_approval_first(ops):
    cid = ops.flow.intake("x", "unit dead", IVAN)
    rma_id = _propose(ops, cid).rma["rma_id"]
    res = ops.erp.execute(rma_id)
    assert res.ok is False
    assert "only an approved movement" in res.reason


def test_failed_shipping_operation_is_recorded(ops, st):
    cid = ops.flow.intake("x", "goggles missing", IVAN)
    rma_id = _propose(ops, cid, movement.PART_OUTBOUND).rma["rma_id"]
    st.update("rmas", "rma_id", rma_id, {"status": erp_sim.APPROVED})
    res = ops.erp.execute(rma_id, force_failure=True)
    assert res.ok is False
    assert st.one("SELECT * FROM rmas WHERE rma_id=?",
                  (rma_id,))["status"] == erp_sim.FAILED
    assert st.one("SELECT COUNT(*) c FROM sim_shipments")["c"] == 0
    assert any(e["event_type"] == "erp_movement_failed"
               for e in st.audit_trail(cid))


def test_outbound_without_an_address_fails_rather_than_pretending(ops, st):
    st.update("customers", "account_id", IVAN, {"street": None})
    cid = ops.flow.intake("x", "goggles missing", IVAN)
    rma_id = _propose(ops, cid, movement.PART_OUTBOUND).rma["rma_id"]
    res = ops.erp.approve(rma_id, REP)
    assert res.ok is False
    assert "address" in res.reason


# =========================================================================
# Shipping and tracking
# =========================================================================

def test_tracking_returns_only_recorded_state(ops, st):
    cid = ops.flow.intake("x", "goggles missing", IVAN)
    rma_id = _propose(ops, cid, movement.PART_OUTBOUND).rma["rma_id"]
    res = ops.erp.approve(rma_id, REP)
    tn = res.shipment["tracking_number"]

    rec = ops.erp.track(tn)
    assert rec["status"] == erp_sim.LABEL_CREATED
    assert rec["direction"] == movement.OUTBOUND
    assert len(rec["events"]) == 1
    assert ops.erp.track("SIMFX9999999999") is None


def test_tracking_status_changes_are_event_sourced(ops):
    cid = ops.flow.intake("x", "goggles missing", IVAN)
    rma_id = _propose(ops, cid, movement.PART_OUTBOUND).rma["rma_id"]
    tn = ops.erp.approve(rma_id, REP).shipment["tracking_number"]
    ops.erp.advance(tn, erp_sim.IN_TRANSIT, "Hub", "scan")
    rec = ops.erp.advance(tn, erp_sim.DELIVERED, "Door", "left in porch")
    assert rec["status"] == erp_sim.DELIVERED
    assert [e["status"] for e in rec["events"]] == [
        erp_sim.LABEL_CREATED, erp_sim.IN_TRANSIT, erp_sim.DELIVERED]


def test_delivery_acknowledgement_needs_a_recorded_delivery(ops, st):
    cid = ops.flow.intake("x", "unit dead", IVAN)
    rma_id = _propose(ops, cid).rma["rma_id"]
    tn = ops.erp.approve(rma_id, REP).shipment["tracking_number"]

    assert review.acknowledge_delivery(st, cid, tn) == "", (
        "must not acknowledge a delivery that has not happened")

    ops.erp.advance(tn, erp_sim.DELIVERED, "Facility", "booked in")
    mid = review.acknowledge_delivery(st, cid, tn)
    assert mid
    msg = st.one("SELECT * FROM messages WHERE message_id=?", (mid,))
    assert msg["status"] == "draft", "must still need approval to send"
    assert tn in msg["body"]
    assert 'carrier records delivery' in msg['body']
    assert 'receiving team still needs to confirm' in msg['body']


# =========================================================================
# Representative review workflow
# =========================================================================

def test_items_are_approved_individually_not_wholesale(flow, st):
    cid = make_case(flow, "Unit dead",
                    f"My name is Ivan Nowak and the serial is {SERIAL_A}. "
                    f"The unit is completely dead.")
    items = st.checklist(cid)
    target = items[0]
    review.decide_item(st, target["item_id"], "approved", REP)
    after = st.checklist(cid)
    decided = [i for i in after if i["approval_decision"]]
    assert len(decided) == 1, "approving one item must not approve the rest"


def test_item_correction_is_recorded_with_its_feedback(flow, st):
    cid = make_case(flow, "Unit dead",
                    f"My name is Ivan Nowak and the serial is {SERIAL_A}. "
                    f"The unit is completely dead.")
    item = st.checklist(cid)[0]
    review.decide_item(st, item["item_id"], "rejected", REP, "wrong device")
    row = st.one("SELECT * FROM checklist_items WHERE item_id=?",
                 (item["item_id"],))
    assert row["approval_decision"] == "rejected"
    assert row["feedback"] == "wrong device"
    assert row["authorizer"] == REP
    assert st.one("SELECT * FROM approvals WHERE subject_id=?",
                  (item["item_id"],))


def test_a_decided_item_cannot_be_decided_again(flow, st):
    cid = make_case(flow, "Unit dead", f"Ivan Nowak, serial {SERIAL_A}, "
                                       f"unit completely dead.")
    item = st.checklist(cid)[0]
    review.decide_item(st, item["item_id"], "approved", REP)
    second = review.decide_item(st, item["item_id"], "rejected", REP)
    assert second["ok"] is False and second["duplicate"] is True


def test_regeneration_keeps_original_feedback_and_revision(ops, st):
    cid = make_case(ops.flow, "Unit dead",
                    f"My name is Ivan Nowak and the serial is {SERIAL_A}. "
                    f"The unit is completely dead.")
    draft = review.latest_draft(st, cid)
    original_body = draft["body"]
    out = review.regenerate_reply(ops, cid, draft["message_id"],
                                  "Too long, lead with the interlock.", REP)
    assert out["ok"]
    rec = st.one("SELECT * FROM recommendations WHERE case_id=?", (cid,))
    assert rec["original_payload"] == original_body
    assert rec["feedback"] == "Too long, lead with the interlock."
    assert rec["payload"]
    old = st.one("SELECT * FROM messages WHERE message_id=?",
                 (draft["message_id"],))
    assert old["status"] == "rejected", "the original must be kept, rejected"
    assert review.latest_draft(st, cid)["message_id"] != draft["message_id"]


def test_sending_requires_explicit_approval_and_strips_internal_notes(
        flow, st):
    cid = make_case(flow, "Goggles missing",
                    f"My name is Ivan Nowak and the serial is {SERIAL_A}. "
                    f"The goggles were missing from the box.")
    draft = review.latest_draft(st, cid)
    assert draft["status"] == "draft"
    assert "[For representative:" in draft["body"]

    review.approve_and_send(st, cid, draft["message_id"], draft["body"], REP)
    sent = st.one("SELECT * FROM messages WHERE message_id=?",
                  (draft["message_id"],))
    assert sent["status"] == "sent" and sent["approved_by"] == REP
    assert "[For representative:" not in sent["body"]


def test_a_sent_message_cannot_be_sent_twice(flow, st):
    cid = make_case(flow, "Unit dead", f"Ivan Nowak, serial {SERIAL_A}, "
                                       f"completely dead.")
    draft = review.latest_draft(st, cid)
    review.approve_and_send(st, cid, draft["message_id"], draft["body"], REP)
    again = review.approve_and_send(st, cid, draft["message_id"], "x", REP)
    assert again["ok"] is False and again.get("duplicate")


def test_repeated_corrections_raise_a_reviewable_proposal(flow, st):
    for n in range(4):
        cid = make_case(flow, f"Unit dead {n}",
                        f"My name is Ivan Nowak and the serial is "
                        f"{SERIAL_A}. The unit is completely dead.")
        item = next(i for i in st.checklist(cid)
                    if i["action"] == "assess_priority")
        review.decide_item(st, item["item_id"], "rejected", REP,
                           "priority should be higher")
    props = st.query("SELECT * FROM improvement_proposals")
    assert props, "repeated corrections should raise a proposal"
    assert props[0]["review_status"] == "pending_review", (
        "a proposal must not take effect without review")


# =========================================================================
# Queue behaviour
# =========================================================================

def test_critical_is_served_before_queued_low_priority_work(ops, st):
    for _ in range(5):
        cid = ops.flow.intake("Question", "Just a general question.", MARY)
        ops.queue.submit(cid, st.case(cid)["priority"])
    crit = ops.flow.intake("Burn", f"Ivan Nowak serial {SERIAL_A}. I have a "
                                   f"burn on my arm.", IVAN)
    ops.queue.submit(crit, st.case(crit)["priority"])
    order = [c for _, _, c in sorted(ops.queue._heap)]
    assert order[0] == crit


def test_critical_is_visible_without_waiting_for_the_model(ops, st):
    for _ in range(5):
        cid = ops.flow.intake("Question", "General question.", MARY)
        ops.queue.submit(cid, st.case(cid)["priority"])
    crit = ops.flow.intake("Burn", f"Ivan Nowak serial {SERIAL_A}. I have a "
                                   f"burn on my arm.", IVAN)
    c = st.case(crit)
    assert c["priority"] == triage.CRITICAL
    assert c["status"] == caseflow.HUMAN_REVIEW
    assert c["ai_state"] == caseflow.QUEUED
    assert st.query("SELECT * FROM qa_notifications WHERE case_id=?",
                    (crit,))


def test_a_case_is_not_queued_twice(ops, st):
    cid = ops.flow.intake("Question", "General question.", MARY)
    assert ops.queue.submit(cid) is True
    assert ops.queue.submit(cid) is False


def test_queue_at_capacity_marks_work_retriable_not_lost(st, shared_agent):
    f = caseflow.CaseFlow(st, shared_agent)
    q = queueing.InferenceQueue(f, maxsize=2)
    ids = [f.intake("Q", "General question.", MARY) for _ in range(4)]
    for cid in ids:
        q.submit(cid)
    states = [st.case(c)["ai_state"] for c in ids]
    assert states.count(caseflow.RETRIABLE) == 2
    assert q.stats.dropped_full == 2

    # Nothing was dropped: once the worker frees capacity the overflow can
    # be picked up again.
    q._next()
    q._next()
    assert q.submit_pending() >= 1, "retriable work must be recoverable"


def test_arrival_simulator_never_reuses_a_ticket(ops, st):
    first = ops.simulator.seed_inbox(5)
    pool_ids = [st.case(c)["pool_id"] for c in first]
    assert len(set(pool_ids)) == 5
    more = ops.simulator.seed_inbox(5)
    assert not (set(st.case(c)["pool_id"] for c in more) & set(pool_ids))


def test_reset_restores_the_pool_and_removes_simulated_cases(ops, st):
    ops.simulator.seed_inbox(4)
    assert len(st.cases()) == 4
    out = ops.simulator.reset()
    assert out["cases_removed"] == 4
    assert len(st.cases()) == 0
    assert st.one("SELECT COUNT(*) c FROM ticket_pool WHERE released=1")["c"] == 0
    assert any(e["event_type"] == "case_removed_by_reset"
               for e in st.query("SELECT * FROM audit_events")), (
        "the audit trail must record the removal")


def test_worker_processes_the_queue(ops, st):
    ids = ops.simulator.seed_inbox(3)
    for cid in ids:
        ops.queue.submit(cid, st.case(cid)["priority"])
    ops.queue.start()
    deadline = time.time() + 120
    while time.time() < deadline:
        s = ops.queue.snapshot()
        if s["queued"] == 0 and s["current"] is None:
            break
        time.sleep(0.5)
    ops.queue.stop()
    assert ops.queue.stats.completed == 3
    for cid in ids:
        assert st.case(cid)["ai_state"] == caseflow.COMPLETED


# =========================================================================
# Chatbot
# =========================================================================

@pytest.fixture
def bot(st, shared_agent):
    return chat.ChatBot(st, shared_agent)


def test_chat_asks_for_identification_without_repeating_itself(bot):
    s = bot.new_session()
    r1 = bot.respond(s, "My unit is completely dead.")
    assert "serial" in r1.text.lower() or "name" in r1.text.lower()
    r2 = bot.respond(s, "My name is Ivan Nowak.")
    assert "name on the account" not in r2.text, (
        "it should not ask again for the name it was just given")


def test_chat_gives_grounded_steps_once_verified(bot):
    s = bot.new_session()
    bot.respond(s, "My unit is completely dead, no lights and no fan.")
    bot.respond(s, "My name is Ivan Nowak.")
    r = bot.respond(s, f"The serial is {SERIAL_A}.")
    assert s.verified is True
    assert r.sources, "a grounded reply must cite its sources"
    assert all(x["procedure_id"] == "PROC-P4800-PWR-A" for x in r.sources)


def test_chat_advances_through_steps_without_repeating(bot):
    s = bot.new_session()
    bot.respond(s, "My unit is completely dead, no lights and no fan.")
    bot.respond(s, "My name is Ivan Nowak.")
    r1 = bot.respond(s, f"The serial is {SERIAL_A}.")
    first = {x["section"] for x in r1.sources}
    r2 = bot.respond(s, "I tried that, still completely dead.")
    second = {x["section"] for x in r2.sources}
    assert second and not (first & second)


def test_chat_hands_off_on_safety_without_troubleshooting(bot, st):
    s = bot.new_session()
    r = bot.respond(s, f"I am Ivan Nowak, serial {SERIAL_A}. I got a burn "
                       f"on my arm during my session.")
    assert r.handed_off and r.handoff_case_id
    assert not r.sources
    case = st.case(r.handoff_case_id)
    assert case["priority"] == triage.CRITICAL
    assert case["channel"] == "chat"


def test_chat_cannot_create_a_transaction_and_hands_off(bot, st):
    s = bot.new_session()
    r = bot.respond(s, f"Hello, I'm Ivan Nowak, serial {SERIAL_A}. The "
                       f"goggles that should have come with the device were "
                       f"missing from the box.")
    assert r.handed_off
    assert "product movement" in r.reason
    assert st.one("SELECT COUNT(*) c FROM rmas")["c"] == 0, (
        "the chatbot must never create an RMA")


def test_chat_handoff_carries_the_whole_conversation(bot, st):
    s = bot.new_session()
    bot.respond(s, "My unit is completely dead.")
    bot.respond(s, "My name is Ivan Nowak.")
    bot.respond(s, f"The serial is {SERIAL_A}.")
    r = bot.respond(s, "I want to speak to a person please.")
    assert r.handed_off
    body = st.one("SELECT body FROM messages WHERE case_id=? AND "
                  "direction='inbound'", (r.handoff_case_id,))["body"]
    assert "Ivan Nowak" in body
    assert SERIAL_A in body
    assert "conversation" in body.lower()
    assert "PROC-P4800-PWR-A" in body, "sources shown must be carried over"


def test_chat_state_survives_a_restart(bot, st):
    s = bot.new_session()
    bot.respond(s, "My unit is completely dead.")
    bot.respond(s, f"My name is Ivan Nowak, serial {SERIAL_A}.")
    reloaded = bot.load_session(s.conversation_id)
    assert reloaded is not None
    assert reloaded.verified == s.verified
    assert reloaded.claim.serial == SERIAL_A
    assert len(reloaded.turns) == len(s.turns)


def test_chat_will_not_guess_a_contradictory_identity(bot):
    s = bot.new_session()
    r = bot.respond(s, f"My name is Mary Cohen and the serial is {SERIAL_A}.")
    assert r.handed_off
    assert "contradict" in r.reason


# =========================================================================
# Analytics and replay isolation
# =========================================================================

def test_analytics_counts_live_cases(flow, st):
    make_case(flow, "Unit dead", f"Ivan Nowak serial {SERIAL_A}, unit "
                                 f"completely dead.")
    flow.intake("Burn", f"Ivan Nowak serial {SERIAL_A}. I have a burn.", IVAN)
    a = analytics.Analytics(st)
    rep = a.report()
    assert rep["lifecycle"]["total"] == 2
    assert rep["safety"]["adverse_events_confirmed"] == 1
    assert rep["complaints"]["complaints"] >= 1


def test_evaluation_replays_do_not_inflate_live_counts(flow, st):
    flow.intake("Unit dead", "The unit is completely dead.", IVAN)
    for _ in range(5):
        flow.intake("Unit dead", "The unit is completely dead.", IVAN,
                    run_id="EVAL-RUN-1")

    live = analytics.Analytics(st).report()
    assert live["lifecycle"]["total"] == 1, (
        "replayed cases must not appear in live business counts")

    replay = analytics.Analytics(st, run_id="EVAL-RUN-1").report()
    assert replay["lifecycle"]["total"] == 5

    both = analytics.Analytics(st, include_eval=True).report()
    assert both["lifecycle"]["total"] == 6


def test_unresolved_closure_stays_visible_in_reporting(flow, st):
    cid = make_case(flow, "Unit dead", f"Ivan Nowak serial {SERIAL_A}, "
                                       f"completely dead.")
    review.close_case(st, cid, REP, "customer stopped responding",
                      resolved=False)
    rep = analytics.Analytics(st).report()
    assert rep["lifecycle"]["closed_without_resolution"] == 1
    assert cid in rep["lifecycle"]["closed_unresolved_ids"]
    assert rep["lifecycle"]["resolved"] == 0


def test_failures_by_model_reports_a_denominator(flow, st):
    make_case(flow, "Unit dead", f"Ivan Nowak serial {SERIAL_A}, unit "
                                 f"completely dead.")
    rep = analytics.Analytics(st).report()
    by_model = rep["failures_by_model"]
    assert "P4800" in by_model
    entry = by_model["P4800"]
    assert entry["devices_on_file"] and entry["devices_on_file"] > 1
    assert entry["complaints_per_device"] is not None
    assert "not a field failure rate" in entry["note"]


def test_proposed_actions_surface_real_findings(flow, st):
    flow.intake("Burn", f"Ivan Nowak serial {SERIAL_A}. I have a burn.", IVAN)
    actions = analytics.Analytics(st).proposed_actions()
    assert any(a["area"] == "adverse events" for a in actions)


# =========================================================================
# Persistence and audit
# =========================================================================

def test_everything_survives_a_restart(ops, st):
    cid = make_case(ops.flow, "Goggles missing",
                    f"My name is Ivan Nowak and the serial is {SERIAL_A}. "
                    f"The goggles were missing from the box.")
    rma = st.one("SELECT * FROM rmas WHERE case_id=?", (cid,))
    ops.erp.approve(rma["rma_id"], REP, "send them")
    before = st.stats()
    path = st.path
    st.close()

    reopened = store_mod.Store(path)
    assert reopened.stats() == before
    assert reopened.case(cid)["status"]
    assert len(reopened.checklist(cid)) > 10
    assert reopened.one("SELECT * FROM rmas WHERE rma_id=?",
                        (rma["rma_id"],))["approved_by"] == REP
    assert reopened.one("SELECT * FROM sim_shipments WHERE rma_id=?",
                        (rma["rma_id"],))
    assert len(reopened.audit_trail(cid)) >= 3
    reopened.close()


def test_audit_trail_is_append_only_through_a_correction(flow, st):
    cid = make_case(flow, "Unit dead", f"Ivan Nowak serial {SERIAL_A}, "
                                       f"completely dead.")
    before = len(st.audit_trail(cid))
    item = st.checklist(cid)[0]
    review.decide_item(st, item["item_id"], "rejected", REP, "wrong")
    review.close_case(st, cid, REP, "done", resolved=True)
    after = st.audit_trail(cid)
    assert len(after) > before
    kinds = [e["event_type"] for e in after]
    assert "checklist_item_rejected" in kinds
    assert "case_closed" in kinds
    assert kinds.index("case_created") == 0, "history was reordered or lost"
