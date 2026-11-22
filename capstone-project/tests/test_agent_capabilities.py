"""Tests for the Checkpoint 1.1 / 2.1 capabilities.

Every test uses isolated store files via tmp_path, so the demonstration logs
in data/ are never touched by a test run. The agent runs with prefer_llm=False
so results are deterministic and fast; the LLM path is exercised by
run_capstone_demo.py.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from capstone_rag import (  # noqa: E402
    agent as agent_mod,
    audit as audit_mod,
    cases,
    config,
    erp as erp_mod,
    generation,
    grounding,
    memory as memory_mod,
    react,
    tools as tools_mod,
)

SERIAL_A = "98148469"      # Ivan Nowak, P4800 Rev A - has approved procedure
SERIAL_B = "68177-DV5"     # Mary Cohen, P4800 Rev B - deliberate coverage gap
SERIAL_HOME = "66383977"   # Ivan Nowak, Home-NB    - no procedures at all
SERIAL_SM = "38180-BM2"    # Peter Thomas, Series-M
ACC_IVAN = "ACC-00010"
ACC_MARY = "ACC-00022"


@pytest.fixture
def stores(tmp_path):
    return {
        "escalations": cases.EscalationQueue(str(tmp_path / "esc.jsonl")),
        "approvals": cases.ApprovalQueue(str(tmp_path / "rma.jsonl")),
        "reviewed_memory": memory_mod.ReviewedMemory(str(tmp_path / "mem.jsonl")),
    }


@pytest.fixture
def ag(stores, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TICKET_LOG_PATH",
                        str(tmp_path / "tickets.jsonl"))
    return agent_mod.Agent(prefer_llm=False, **stores)


# =========================================================================
# Customer and device handling
# =========================================================================

def test_erp_returns_account_device_and_order_information():
    e = erp_mod.ERP()
    dev = e.device_by_serial(SERIAL_A)
    assert dev.model == "P4800" and dev.revision == "A"
    assert e.orders_for_account(dev.account_id)
    assert e.shipments_for_account(dev.account_id)
    assert e.customer_by_account(dev.account_id).customer_name == "Ivan Nowak"


def test_ambiguous_identification_is_not_guessed(ag):
    r = ag.handle("My unit stopped working.", account_id=ACC_MARY)
    assert r.identity.confirmed is False
    assert r.outcome == "abstained"
    assert r.retrieval_query is None
    assert "serial number" in r.response.lower()


# =========================================================================
# Short-term conversational memory
# =========================================================================

def test_serial_supplied_in_a_later_turn_resolves_the_device(ag):
    conv = ag.new_conversation(ACC_MARY)

    t1 = ag.handle("My unit is completely dead, no lights at all.",
                   conversation=conv)
    assert t1.identity.confirmed is False
    assert conv.open_question == "awaiting serial number"

    t2 = ag.handle(f"Sorry - the serial is {SERIAL_A}.", conversation=conv)
    assert t2.identity.confirmed is True
    assert t2.identity.device.serial_number == SERIAL_A


def test_symptom_from_the_first_turn_is_still_used_for_retrieval(ag):
    """Turn 2 says only the serial. Retrieval must still search the fault."""
    conv = ag.new_conversation(ACC_IVAN)
    ag.handle("My unit is completely dead - no lights, no fan noise at all.",
              conversation=conv)
    t2 = ag.handle(f"The serial is {SERIAL_A}.", conversation=conv)

    assert "no lights" in (t2.retrieval_query or ""), (
        "the original symptom was lost between turns"
    )
    assert t2.outcome == "grounded"
    assert t2.selected_procedure == "PROC-P4800-PWR-A"


def test_conversation_remembers_the_device_without_repeating_the_serial(ag):
    conv = ag.new_conversation(ACC_IVAN)
    ag.handle(f"Unit is dead. Serial number: {SERIAL_A}", conversation=conv)
    assert conv.confirmed_serial == SERIAL_A

    t2 = ag.handle("It is still dead after trying that.", conversation=conv)
    assert t2.identity.confirmed is True
    assert t2.identity.device.serial_number == SERIAL_A


# =========================================================================
# Sequential troubleshooting
# =========================================================================

def test_follow_up_turn_gives_the_next_step_not_a_repeat(ag):
    conv = ag.new_conversation(ACC_IVAN)
    t1 = ag.handle(
        f"The unit is completely dead, no lights or fan. Serial: {SERIAL_A}",
        conversation=conv)
    assert t1.outcome == "grounded"
    first_chunks = {v.chunk_id for v in t1.accepted}
    assert first_chunks

    t2 = ag.handle("I tried that and it is still completely dead.",
                   conversation=conv)
    second_chunks = {v.chunk_id for v in t2.accepted}

    assert second_chunks, "follow-up produced no steps at all"
    assert not (second_chunks & first_chunks), (
        "the same steps were served twice instead of advancing"
    )


def test_delivered_sections_escalate_without_claiming_field_completion(ag, stores):
    conv = ag.new_conversation(ACC_IVAN)
    last = None
    # P4800 Rev A has two authorised procedures totalling 12 sections, so the
    # agent legitimately has several turns of real steps before it runs out.
    for _ in range(12):
        last = ag.handle(
            f"Still completely dead, no lights, no fan. Serial: {SERIAL_A}",
            conversation=conv)
        if last.escalation_reason == cases.REASON_LOOP_EXHAUSTED:
            break

    assert last.escalation_reason == cases.REASON_LOOP_EXHAUSTED
    assert not last.rma_request_id, "delivered guidance cannot establish field completion"
    assert last.case_id
    assert 'does not establish' in last.response.lower()


# =========================================================================
# Tool selection
# =========================================================================

def test_intent_routing_distinguishes_logistics_from_troubleshooting():
    assert tools_mod.classify_intent(
        "Where is my order, has it shipped yet?")["logistics"]
    assert tools_mod.classify_intent(
        "The lamp will not power on")["troubleshooting"]
    assert not tools_mod.classify_intent(
        "Can you confirm the tracking number for my order?"
    )["troubleshooting"]


def test_logistics_question_does_not_search_the_qms(ag):
    r = ag.handle(
        f"Can you confirm the shipping status of my order? Serial: {SERIAL_A}",
        account_id=ACC_IVAN)
    assert r.outcome == "answered"
    assert r.retrieval_query is None, "QMS was searched for a shipping question"
    used = [t["tool"] for t in r.tool_calls]
    assert tools_mod.LOOKUP_SHIPMENT in used
    assert tools_mod.SEARCH_QMS not in used


def test_troubleshooting_question_does_not_call_the_shipment_tool(ag):
    r = ag.handle(f"The unit will not power on. Serial: {SERIAL_A}",
                  account_id=ACC_IVAN)
    used = [t["tool"] for t in r.tool_calls]
    assert tools_mod.SEARCH_QMS in used
    assert tools_mod.LOOKUP_SHIPMENT not in used


# =========================================================================
# Bounded ReAct loop
# =========================================================================

def test_trace_records_thought_action_observation(ag):
    r = ag.handle(f"The unit will not power on. Serial: {SERIAL_A}",
                  account_id=ACC_IVAN)
    assert len(r.steps) >= 5
    for s in r.steps:
        assert s.thought and s.action and s.observation
    actions = [s.action for s in r.steps]
    assert actions[0] == "safety.screen"
    assert tools_mod.IDENTIFY_DEVICE in actions
    assert "verify.grounding" in actions


def test_legitimate_steps_do_not_consume_the_escalation_budget(ag):
    r = ag.handle(f"The unit will not power on. Serial: {SERIAL_A}",
                  account_id=ACC_IVAN)
    assert r.outcome == "grounded"
    assert r.unproductive_attempts == 0, (
        "successful troubleshooting must not count against the budget"
    )


def test_failed_retrieval_counts_as_unproductive_and_escalates(ag):
    r = ag.handle(f"The unit will not power on. Serial: {SERIAL_B}",
                  account_id=ACC_MARY)
    assert r.outcome == "abstained"
    assert r.unproductive_attempts >= 1
    assert r.case_id
    assert r.escalation_reason == cases.REASON_NO_PROCEDURE


def test_loop_escalates_after_three_unproductive_attempts(ag, monkeypatch,
                                                          stores):
    """Force every draft to fail verification and check the budget holds."""
    def always_fabricate(*a, **kw):
        return generation.Generation(
            text=("Hello there,\n\nPlease bridge the fuse holder with wire "
                  "and measure the voltage at 240V.\n\nKind regards"),
            mode="template", engine="test stub")

    monkeypatch.setattr(generation, "generate_grounded", always_fabricate)

    r = ag.handle(f"The unit will not power on. Serial: {SERIAL_A}",
                  account_id=ACC_IVAN)

    assert r.outcome == "escalated"
    assert r.escalation_reason == cases.REASON_UNVERIFIED
    assert r.unproductive_attempts <= config.MAX_UNPRODUCTIVE_ATTEMPTS
    assert "bridge the fuse" not in r.response, (
        "unverified content reached the customer"
    )
    assert stores["escalations"].get(r.case_id) is not None


def test_query_is_reformulated_after_a_retrieval_miss(ag):
    r = ag.handle(f"The unit will not power on. Serial: {SERIAL_B}",
                  account_id=ACC_MARY)
    assert len(r.retrieval_queries) >= 2, "no second retrieval attempt was made"
    assert any(s.action == "retry.reformulate_query" for s in r.steps)


# =========================================================================
# Reflexion
# =========================================================================

def test_reflexion_records_feedback_when_a_draft_fails(ag, monkeypatch):
    calls = {"n": 0}
    real = generation.generate_grounded

    def first_draft_bad(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return generation.Generation(
                text=("Hello there,\n\nPlease replace the 3.15A fuse in the "
                      "rear carrier.\n\nKind regards"),
                mode="template", engine="test stub")
        return real(*a, **kw)

    monkeypatch.setattr(generation, "generate_grounded", first_draft_bad)

    r = ag.handle(f"The unit will not power on. Serial: {SERIAL_A}",
                  account_id=ACC_IVAN)

    assert r.reflexion, "no reflexion feedback was recorded"
    assert "fuse" in r.reflexion[0]["feedback"]
    assert any(s.action == "reflexion.record_feedback" for s in r.steps)
    # It recovered via the deterministic fallback rather than escalating.
    assert r.outcome == "grounded"
    assert "3.15A fuse" not in r.response


# =========================================================================
# Reviewed long-term memory
# =========================================================================

def test_proposed_memory_is_not_recallable_until_approved(ag, stores):
    mem = stores["reviewed_memory"]
    r = ag.handle(f"The unit will not power on. Serial: {SERIAL_A}",
                  account_id=ACC_IVAN)
    assert r.memory_proposed_id
    assert mem.pending()
    assert not mem.approved()

    device = erp_mod.ERP().device_by_serial(SERIAL_A)
    recalled = mem.recall(device, "unit will not power on",
                          ag.compatibility, ag.toolbox.approved_procedures)
    assert recalled == [], "unreviewed memory was recallable"


def test_approved_memory_is_recalled_and_surfaced(ag, stores):
    mem = stores["reviewed_memory"]
    r1 = ag.handle(f"The unit will not power on. Serial: {SERIAL_A}",
                   account_id=ACC_IVAN)
    mem.approve(r1.memory_proposed_id, reviewer="qa.reviewer")

    r2 = ag.handle(f"The unit will not power on again. Serial: {SERIAL_A}",
                   account_id=ACC_IVAN)
    assert r2.memory_used, "approved memory was not recalled"
    assert r2.memory_used[0]["usable"] is True


def test_memory_cannot_resurrect_an_unauthorised_procedure(ag, stores):
    """A memory whose procedure is no longer authorised must be discarded."""
    mem = stores["reviewed_memory"]
    device = erp_mod.ERP().device_by_serial(SERIAL_A)

    res = mem.propose(device, "unit will not power on at all",
                      procedure_id="PROC-P4800-PWR-A-R1",  # superseded doc
                      outcome_note="resolved by the withdrawn procedure")
    mem.approve(res.memory_id, reviewer="qa.reviewer")

    recalled = mem.recall(device, "unit will not power on at all",
                          ag.compatibility, ag.toolbox.approved_procedures)
    assert recalled, "memory was not even considered"
    assert all(not r.usable for r in recalled)
    assert "no longer authorised" in recalled[0].reason


def test_memory_does_not_override_document_control(ag, stores):
    """An approved memory for a Rev A fix must not help a Rev B device."""
    mem = stores["reviewed_memory"]
    dev_a = erp_mod.ERP().device_by_serial(SERIAL_A)
    res = mem.propose(dev_a, "unit completely dead no lights",
                      procedure_id="PROC-P4800-PWR-A",
                      outcome_note="reseating the head fixed it")
    mem.approve(res.memory_id, reviewer="qa.reviewer")

    r = ag.handle(f"Unit completely dead no lights. Serial: {SERIAL_B}",
                  account_id=ACC_MARY)
    assert r.outcome == "abstained"
    assert r.selected_procedure is None
    assert "PROC-P4800-PWR-A" not in r.response


# =========================================================================
# Escalation and human oversight
# =========================================================================

def test_safety_complaint_is_separated_from_ordinary_unresolved(ag, stores):
    safety = ag.handle(
        f"There was a burning smell from the unit. Serial: {SERIAL_SM}",
        account_id="ACC-00026")
    ordinary = ag.handle(f"The unit will not power on. Serial: {SERIAL_B}",
                         account_id=ACC_MARY)

    assert safety.outcome == "escalated"
    assert safety.escalation_reason == cases.REASON_SAFETY
    assert safety.case_priority == "P1"
    assert safety.retrieval_query is None, "retrieval ran despite a safety flag"

    assert ordinary.escalation_reason == cases.REASON_NO_PROCEDURE
    assert ordinary.case_priority == "P3"
    assert safety.case_priority != ordinary.case_priority


def test_escalated_case_can_be_assigned_and_resolved(ag, stores):
    r = ag.handle(f"The unit will not power on. Serial: {SERIAL_HOME}",
                  account_id=ACC_IVAN)
    q = stores["escalations"]
    case = q.get(r.case_id)
    assert case.status == cases.OPEN and case.assignee is None

    q.assign(case.case_id, "eng.taylor")
    assert q.get(case.case_id).status == cases.ASSIGNED
    assert q.get(case.case_id).assignee == "eng.taylor"

    q.resolve(case.case_id, "Authored a Home-NB procedure; customer advised.")
    done = q.get(case.case_id)
    assert done.status == cases.RESOLVED
    assert done.resolution_note


# =========================================================================
# Human approval for consequential actions
# =========================================================================

def test_agent_cannot_authorise_its_own_return(stores):
    q = stores["approvals"]
    device = erp_mod.ERP().device_by_serial(SERIAL_A)
    req = q.request_return_authorisation(device, "all steps exhausted")

    assert req.status == cases.PENDING_APPROVAL
    assert req.is_authorised is False
    assert req.rma_number is None


def test_approval_issues_an_rma_number(stores):
    q = stores["approvals"]
    device = erp_mod.ERP().device_by_serial(SERIAL_A)
    req = q.request_return_authorisation(device, "all steps exhausted")

    approved = q.approve(req.request_id, "svc.manager", "agreed")
    assert approved.is_authorised is True
    assert approved.rma_number and approved.rma_number.startswith("RMA-")
    assert approved.decided_by == "svc.manager"


def test_rejected_request_never_becomes_authorised(stores):
    q = stores["approvals"]
    device = erp_mod.ERP().device_by_serial(SERIAL_A)
    req = q.request_return_authorisation(device, "all steps exhausted")

    rejected = q.reject(req.request_id, "svc.manager", "still in warranty check")
    assert rejected.status == cases.REJECTED
    assert rejected.is_authorised is False
    assert rejected.rma_number is None


def test_a_decided_request_cannot_be_decided_again(stores):
    q = stores["approvals"]
    device = erp_mod.ERP().device_by_serial(SERIAL_A)
    req = q.request_return_authorisation(device, "all steps exhausted")
    q.reject(req.request_id, "svc.manager", "no")

    assert q.approve(req.request_id, "someone.else") is None
    assert q.get(req.request_id).status == cases.REJECTED


# =========================================================================
# Auditability
# =========================================================================

def test_audit_reconstructs_a_full_interaction(ag, stores, tmp_path):
    r = ag.handle(f"The unit will not power on. Serial: {SERIAL_A}",
                  account_id=ACC_IVAN)
    a = audit_mod.Auditor(
        ticket_log=config.TICKET_LOG_PATH,
        escalations=stores["escalations"], approvals=stores["approvals"],
        reviewed_memory=stores["reviewed_memory"])

    t = a.ticket(r.ticket_id)
    assert t["selected_procedure"] == "PROC-P4800-PWR-A"
    assert t["accepted_chunk_ids"]
    assert t["rejected_chunk_ids"]
    assert t["validation"] == "PASS"
    assert t["grounding_verified"] is True
    assert t["tool_calls"]

    rendered = a.render_ticket(r.ticket_id)
    assert "PROC-P4800-PWR-A" in rendered
    assert "validation" in rendered


def test_audit_shows_customer_history_across_turns(ag, stores):
    conv = ag.new_conversation(ACC_IVAN)
    ag.handle(f"Unit is dead. Serial: {SERIAL_A}", conversation=conv)
    ag.handle("Still dead after trying that.", conversation=conv)

    a = audit_mod.Auditor(
        ticket_log=config.TICKET_LOG_PATH,
        escalations=stores["escalations"], approvals=stores["approvals"],
        reviewed_memory=stores["reviewed_memory"])
    hist = a.customer_history(conversation_id=conv.conversation_id)
    assert len(hist) == 2


def test_audit_summary_counts_outcomes(ag, stores):
    ag.handle(f"Will not power on. Serial: {SERIAL_A}", account_id=ACC_IVAN)
    ag.handle(f"Will not power on. Serial: {SERIAL_B}", account_id=ACC_MARY)
    ag.handle(f"Burning smell. Serial: {SERIAL_SM}", account_id="ACC-00026")

    a = audit_mod.Auditor(
        ticket_log=config.TICKET_LOG_PATH,
        escalations=stores["escalations"], approvals=stores["approvals"],
        reviewed_memory=stores["reviewed_memory"])
    s = a.summary()
    assert s["tickets"] == 3
    assert s["by_outcome"].get("grounded") == 1
    assert s["cases"] >= 2
    assert cases.REASON_SAFETY in s["cases_by_reason"]
