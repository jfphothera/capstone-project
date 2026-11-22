"""API integration tests.

These go through HTTP. The point is not that the handlers run - it is that
the *approval gates* are enforced at the API boundary, because a UI button is
not an approval control. A rejected movement must be unexecutable through the
API even if a browser offers the button anyway.

No inference worker is started: `start_workers=False` keeps the routes and
the rules identical while leaving the threads out of the test run.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from capstone_rag import (  # noqa: E402
    agent as agent_mod, caseflow, config, erp_sim, store as store_mod,
)
from capstone_rag.webapp import create_app  # noqa: E402

IVAN = "ACC-00010"
MARY = "ACC-00022"
SERIAL_A = "98148469"
SERIAL_B = "68177-DV5"
REP = "rep.test"


@pytest.fixture(scope="session")
def source_db():
    if not os.path.exists(config.DB_PATH):
        pytest.skip("run scripts/import_synthetic.py first")
    return config.DB_PATH


@pytest.fixture(scope="session")
def shared_agent():
    return agent_mod.Agent(prefer_llm=False)


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
    s.execute("UPDATE ticket_pool SET released=0")
    yield s
    s.close()


@pytest.fixture
def client(st, shared_agent):
    app = create_app(prefer_llm=False, store=st, agent=shared_agent,
                     representative=REP, start_workers=False)
    with TestClient(app) as c:
        yield c


def _make_case(client, subject, body, account_id=IVAN, investigate=True):
    r = client.post("/api/cases", json={"subject": subject, "body": body,
                                        "account_id": account_id})
    assert r.status_code == 201, r.text
    case_id = r.json()["case_id"]
    if investigate:
        from capstone_rag.webapp.runtime import get_runtime
        get_runtime().flow.pre_investigate(case_id)
    return case_id


def _movement_case(client):
    return _make_case(
        client, "Goggles missing from the box",
        f"My name is Ivan Nowak. My unit arrived but the goggles were missing from the box. "
        f"The serial number is {SERIAL_A}.")


# =========================================================================
# Health, reference and the inbox
# =========================================================================

def test_health_reports_one_worker_and_the_store(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["queue"]["maxsize"] >= 1
    assert body["store"]["customers"] == 300
    assert body["representative"] == REP


def test_reference_vocabularies_are_served_not_duplicated(client):
    ref = client.get("/api/reference").json()
    assert caseflow.ACTION_APPROVED in ref["statuses"]
    assert "Critical" in ref["priorities"]
    assert "No match" in ref["outcomes"]


def test_the_inbox_is_searchable_and_filterable(client):
    _make_case(client, "Lamp flickering", "The lamp flickers.",
               investigate=False)
    _make_case(client, "Where is my order", "Tracking please.",
               investigate=False)
    all_cases = client.get("/api/cases").json()
    assert all_cases["count"] == 2
    found = client.get("/api/cases", params={"q": "flicker"}).json()
    assert found["count"] == 1
    assert "Lamp" in found["cases"][0]["subject"]
    by_priority = client.get("/api/cases",
                             params={"priority": "Low"}).json()
    assert all(c["priority"] == "Low" for c in by_priority["cases"])


def test_the_inbox_row_carries_what_the_list_needs(client):
    cid = _movement_case(client)
    row = next(c for c in client.get("/api/cases").json()["cases"]
               if c["case_id"] == cid)
    for key in ("priority", "status", "customer_label", "preview",
                "is_complaint", "ai_state", "open_decisions"):
        assert key in row
    assert row["open_decisions"] >= 1


def test_an_unmatched_customer_is_labelled_as_such(client):
    _make_case(client, "Help please", "Nobody has got back to me.",
               account_id=None, investigate=False)
    row = client.get("/api/cases").json()["cases"][0]
    assert row["customer_label"] == "unmatched customer"


def test_critical_cases_are_visible_before_the_model_runs(client):
    cid = _make_case(client, "Burn from the unit",
                     "I was burned by the unit during a session.",
                     investigate=False)
    row = next(c for c in client.get("/api/cases").json()["cases"]
               if c["case_id"] == cid)
    assert row["priority"] == "Critical"
    assert row["ai_state"] == caseflow.QUEUED
    assert row["status"] == caseflow.HUMAN_REVIEW


# =========================================================================
# One case: the workspace payload
# =========================================================================

def test_the_workspace_arrives_in_one_request(client):
    cid = _movement_case(client)
    w = client.get(f"/api/cases/{cid}").json()
    for key in ("case", "customer", "checklist", "summary", "messages",
                "draft", "rmas", "shipments", "notifications", "history",
                "audit", "evidence", "primary_decision"):
        assert key in w, key
    assert w["messages"], "the original customer message must be there"
    assert w["draft"], "the proposed reply must be there"


def test_the_checklist_is_split_into_three_panels(client):
    cid = _movement_case(client)
    panels = client.get(f"/api/cases/{cid}").json()["checklist"]["panels"]
    assert set(panels) == {"investigation_complete", "needs_your_attention",
                           "next_steps"}
    assert panels["investigation_complete"], "findings must be summarised"
    labels = [d["label"] for d in
              client.get(f"/api/cases/{cid}").json()["checklist"]["all"]]
    assert "Message reviewed" in labels
    assert "Safety screening" in labels
    assert "read_message" not in labels, "no raw function names as labels"


def test_the_most_important_decision_is_identified(client):
    cid = _movement_case(client)
    top = client.get(f"/api/cases/{cid}").json()["primary_decision"]
    assert top["kind"] == "movement_approval"


def test_the_approval_states_its_consequences_before_the_click(client):
    cid = _movement_case(client)
    rma = client.get(f"/api/cases/{cid}").json()["rmas"][0]
    c = rma["consequences"]
    assert c["headline"] and c["erp_action"] and c["shipping"]
    assert "No RMA is finalised" in c["if_rejected"]


def test_a_missing_case_is_a_404(client):
    assert client.get("/api/cases/CASE-999999").status_code == 404


# =========================================================================
# The approval gates, through HTTP
# =========================================================================

def test_a_proposal_authorises_nothing(client, st):
    cid = _movement_case(client)
    rma = client.get(f"/api/cases/{cid}").json()["rmas"][0]
    assert rma["status"] == erp_sim.PROPOSED
    assert rma["shipment"] is None
    assert st.one("SELECT * FROM sim_shipments WHERE rma_id=?",
                  (rma["rma_id"],)) is None


def test_approving_a_movement_creates_the_linked_records(client, st):
    cid = _movement_case(client)
    rma_id = client.get(f"/api/cases/{cid}").json()["rmas"][0]["rma_id"]
    r = client.post(f"/api/rmas/{rma_id}/approve",
                    json={"authorizer": REP, "note": "send the goggles"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tracking_number"]
    w = client.get(f"/api/cases/{cid}").json()
    assert w["case"]["status"] == caseflow.AWAITING
    assert w["case"]["rma_needed"] is False
    assert w["case"]["rma_outcome"] == erp_sim.EXECUTED
    assert w["shipments"] and w["shipments"][0]["tracking_number"]
    assert w["rmas"][0]["call"]["status"] == erp_sim.EXECUTED
    assert any(e["event_type"] == "erp_movement_executed" for e in w["audit"])


def test_a_double_click_does_not_create_two_rmas(client, st):
    cid = _movement_case(client)
    rma_id = client.get(f"/api/cases/{cid}").json()["rmas"][0]["rma_id"]
    first = client.post(f"/api/rmas/{rma_id}/approve",
                        json={"authorizer": REP})
    second = client.post(f"/api/rmas/{rma_id}/approve",
                         json={"authorizer": REP})
    assert first.status_code == 200
    assert second.status_code == 409
    assert len(st.query("SELECT * FROM sim_shipments WHERE rma_id=?",
                        (rma_id,))) == 1


def test_a_rejected_movement_cannot_be_executed_through_the_api(client, st):
    cid = _movement_case(client)
    rma_id = client.get(f"/api/cases/{cid}").json()["rmas"][0]["rma_id"]
    assert client.post(f"/api/rmas/{rma_id}/reject",
                       json={"authorizer": REP,
                             "note": "customer found them"}).status_code == 200
    again = client.post(f"/api/rmas/{rma_id}/approve",
                        json={"authorizer": REP})
    assert again.status_code == 409
    assert st.one("SELECT * FROM sim_shipments WHERE rma_id=?",
                  (rma_id,)) is None


def test_rejecting_the_finding_blocks_the_movement_through_the_api(client, st):
    """Defect C8, enforced server-side."""
    cid = _movement_case(client)
    w = client.get(f"/api/cases/{cid}").json()
    rma_id = w["rmas"][0]["rma_id"]
    item = next(d for d in w["checklist"]["all"]
                if d["action"] == "determine_product_movement")
    r = client.post(f"/api/checklist/{item['item_id']}/decision",
                    json={"authorizer": REP, "decision": "rejected",
                          "feedback": "not needed"})
    assert r.status_code == 200, r.text
    assert rma_id in r.json()["consequence"]
    blocked = client.post(f"/api/rmas/{rma_id}/approve",
                          json={"authorizer": REP})
    assert blocked.status_code == 409


def test_the_api_refuses_a_decision_on_an_automatic_step(client):
    """Defect C7: a lookup is not a decision, whatever the UI renders."""
    cid = _movement_case(client)
    w = client.get(f"/api/cases/{cid}").json()
    item = next(d for d in w["checklist"]["all"]
                if d["action"] == "read_message")
    r = client.post(f"/api/checklist/{item['item_id']}/decision",
                    json={"authorizer": REP, "decision": "approved"})
    assert r.status_code == 400
    assert "audit" in r.json()["detail"]


def test_a_decision_needs_a_named_authorizer(client):
    cid = _movement_case(client)
    w = client.get(f"/api/cases/{cid}").json()
    item = next(d for d in w["checklist"]["all"] if d["offers_decision"])
    r = client.post(f"/api/checklist/{item['item_id']}/decision",
                    json={"decision": "approved"})
    assert r.status_code == 422


def test_a_second_decision_on_the_same_item_is_a_conflict(client):
    cid = _movement_case(client)
    w = client.get(f"/api/cases/{cid}").json()
    item = next(d for d in w["checklist"]["all"] if d["offers_decision"])
    client.post(f"/api/checklist/{item['item_id']}/decision",
                json={"authorizer": REP, "decision": "approved"})
    again = client.post(f"/api/checklist/{item['item_id']}/decision",
                        json={"authorizer": REP, "decision": "rejected"})
    assert again.status_code == 409


# =========================================================================
# Customer correspondence
# =========================================================================

def test_sending_a_reply_is_the_only_way_out(client, st):
    cid = _movement_case(client)
    draft = client.get(f"/api/cases/{cid}").json()["draft"]
    assert draft["status"] == "draft"
    r = client.post(f"/api/messages/{draft['message_id']}/send",
                    json={"authorizer": REP, "body": draft["body"]})
    assert r.status_code == 200
    sent = st.one("SELECT * FROM messages WHERE message_id=?",
                  (draft["message_id"],))
    assert sent["status"] == "sent"
    assert sent["approved_by"] == REP
    assert st.case(cid)["status"] == caseflow.AWAITING


def test_the_internal_note_never_reaches_the_customer(client, st):
    cid = _movement_case(client)
    w = client.get(f"/api/cases/{cid}").json()
    draft = w["draft"]
    assert draft["internal_note"], "the movement note should be separated out"
    assert "[For representative:" not in draft["body"]
    client.post(f"/api/messages/{draft['message_id']}/send",
                json={"authorizer": REP,
                      "body": draft["body"] + "\n[For representative: x]"})
    sent = st.one("SELECT * FROM messages WHERE message_id=?",
                  (draft["message_id"],))
    assert "[For representative:" not in sent["body"]


def test_resending_is_a_conflict_not_a_second_message(client):
    cid = _movement_case(client)
    draft = client.get(f"/api/cases/{cid}").json()["draft"]
    body = {"authorizer": REP, "body": draft["body"]}
    assert client.post(f"/api/messages/{draft['message_id']}/send",
                       json=body).status_code == 200
    assert client.post(f"/api/messages/{draft['message_id']}/send",
                       json=body).status_code == 409


def test_a_rejected_draft_is_kept_and_not_sent(client, st):
    cid = _movement_case(client)
    draft = client.get(f"/api/cases/{cid}").json()["draft"]
    r = client.post(f"/api/messages/{draft['message_id']}/reject",
                    json={"authorizer": REP, "note": "too vague"})
    assert r.status_code == 200
    row = st.one("SELECT * FROM messages WHERE message_id=?",
                 (draft["message_id"],))
    assert row["status"] == "rejected"
    assert row["body"], "the original text must still be in the record"


def test_regeneration_keeps_the_original_and_returns_a_draft(client, st):
    cid = _make_case(client, "Lamp replacement",
                     f"My name is Ivan Nowak, serial {SERIAL_A}. How often "
                     f"should the lamps be replaced?")
    draft = client.get(f"/api/cases/{cid}").json()["draft"]
    r = client.post(f"/api/messages/{draft['message_id']}/regenerate",
                    json={"authorizer": REP,
                          "feedback": "mention the service interval"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["message_id"] != draft["message_id"]
    new = st.one("SELECT * FROM messages WHERE message_id=?",
                 (out["message_id"],))
    assert new["status"] == "draft", "a revision is never auto-sent"
    rec = st.one("SELECT * FROM recommendations WHERE case_id=?", (cid,))
    assert rec["original_payload"], "the original must be preserved"
    assert rec["feedback"] == "mention the service interval"


# =========================================================================
# Revision confirmation (C13) through the API
# =========================================================================

def test_confirming_a_revision_rechecks_eligibility(client, st):
    cid = _make_case(
        client, "Lamp flickering",
        f"The lamp flickers during a session. Serial {SERIAL_B}. "
        f"My name is Mary Cohen.", account_id=MARY)
    w = client.get(f"/api/cases/{cid}").json()
    assert w["revision"]["needs_confirmation"] is True
    r = client.post(f"/api/cases/{cid}/revision-confirmation",
                    json={"authorizer": REP, "decision": "confirmed",
                          "note": "read the label with the customer"})
    assert r.status_code == 200, r.text
    assert r.json()["reinvestigated"] is True
    after = client.get(f"/api/cases/{cid}").json()
    assert after["revision"]["confirmation"] == "confirmed"
    assert after["revision"]["confirmed_by"] == REP
    actions = [d["action"] for d in after["checklist"]["all"]]
    assert actions.count("validate_procedure_applicability") >= 2


def test_rejecting_a_revision_keeps_the_case_with_a_person(client):
    cid = _make_case(
        client, "Lamp flickering",
        f"The lamp flickers. Serial {SERIAL_B}. My name is Mary Cohen.",
        account_id=MARY)
    r = client.post(f"/api/cases/{cid}/revision-confirmation",
                    json={"authorizer": REP, "decision": "rejected"})
    assert r.status_code == 200
    assert r.json()["reinvestigated"] is False
    assert client.get(f"/api/cases/{cid}").json()["case"]["status"] == (
        caseflow.HUMAN_REVIEW)


def test_a_revision_cannot_be_confirmed_without_a_device(client):
    cid = _make_case(client, "Hello", "Just a general question.",
                     account_id=None)
    r = client.post(f"/api/cases/{cid}/revision-confirmation",
                    json={"authorizer": REP, "decision": "confirmed"})
    assert r.status_code == 400


# =========================================================================
# Case lifecycle
# =========================================================================

def test_priority_override_needs_a_reason(client):
    cid = _movement_case(client)
    bad = client.post(f"/api/cases/{cid}/priority",
                      json={"authorizer": REP, "priority": "High",
                            "reason": ""})
    assert bad.status_code == 422
    ok = client.post(f"/api/cases/{cid}/priority",
                     json={"authorizer": REP, "priority": "High",
                           "reason": "customer is mid-treatment"})
    assert ok.status_code == 200
    assert ok.json()["case"]["priority"] == "High"


def test_assignment_is_recorded(client, st):
    cid = _movement_case(client)
    client.post(f"/api/cases/{cid}/assign",
                json={"authorizer": REP, "assignee": "rep.sam"})
    assert st.case(cid)["assigned_to"] == "rep.sam"


def test_closing_records_whether_it_was_resolved(client, st):
    cid = _movement_case(client)
    client.post(f"/api/cases/{cid}/close",
                json={"authorizer": REP, "resolved": False,
                      "resolution": "customer stopped replying"})
    case = st.case(cid)
    assert case["status"] == caseflow.CLOSED
    assert case["resolved"] == 0
    assert "stopped replying" in case["resolution"]


# =========================================================================
# Shipping and tracking
# =========================================================================

def test_tracking_advances_through_recorded_events(client):
    cid = _movement_case(client)
    rma_id = client.get(f"/api/cases/{cid}").json()["rmas"][0]["rma_id"]
    tracking = client.post(f"/api/rmas/{rma_id}/approve",
                           json={"authorizer": REP}).json()["tracking_number"]
    r = client.post(f"/api/shipments/{tracking}/advance",
                    json={"status": "in_transit", "location": "Riverton MN"})
    assert r.status_code == 200
    r = client.post(f"/api/shipments/{tracking}/advance",
                    json={"status": "delivered", "location": "Riverton MN"})
    assert r.status_code == 200
    shipment = client.get(f"/api/shipments/{tracking}").json()
    assert [e["status"] for e in shipment["events"]] == [
        "label_created", "in_transit", "delivered"]


def test_a_delivery_drafts_an_acknowledgement_that_still_needs_approval(
        client, st):
    cid = _movement_case(client)
    rma_id = client.get(f"/api/cases/{cid}").json()["rmas"][0]["rma_id"]
    tracking = client.post(f"/api/rmas/{rma_id}/approve",
                           json={"authorizer": REP}).json()["tracking_number"]
    client.post(f"/api/shipments/{tracking}/advance",
                json={"status": "in_transit"})
    out = client.post(f"/api/shipments/{tracking}/advance",
                      json={"status": "delivered"}).json()
    assert out["acknowledgement_draft"]
    row = st.one("SELECT * FROM messages WHERE message_id=?",
                 (out["acknowledgement_draft"],))
    assert row["status"] == "draft"


def test_the_sample_label_is_marked_invalid(client):
    cid = _movement_case(client)
    rma_id = client.get(f"/api/cases/{cid}").json()["rmas"][0]["rma_id"]
    tracking = client.post(f"/api/rmas/{rma_id}/approve",
                           json={"authorizer": REP}).json()["tracking_number"]
    text = client.get(f"/api/shipments/{tracking}/label").json()["text"]
    assert "NOT VALID FOR SHIPPING" in text


def test_a_failed_movement_can_be_retried_by_a_named_person(client, st):
    cid = _movement_case(client)
    rma_id = client.get(f"/api/cases/{cid}").json()["rmas"][0]["rma_id"]
    from capstone_rag.webapp.runtime import get_runtime
    # rc.19: a retry rests on a real recorded approval (not a status flip).
    assert get_runtime().erp.approve(rma_id, REP, "approved", force_failure=True).ok is False
    assert st.case(cid)["rma_outcome"] == erp_sim.FAILED
    r = client.post(f"/api/rmas/{rma_id}/retry",
                    json={"authorizer": REP, "note": "address corrected"})
    assert r.status_code == 200
    assert st.one("SELECT * FROM rmas WHERE rma_id=?",
                  (rma_id,))["status"] == erp_sim.EXECUTED
    assert st.one("SELECT 1 FROM approvals WHERE subject_id=? AND decision='retry_authorized'", (rma_id,))


def test_retry_cannot_manufacture_approval(client, st):
    cid = _movement_case(client)
    rma_id = client.get(f"/api/cases/{cid}").json()["rmas"][0]["rma_id"]
    st.update("rmas", "rma_id", rma_id, {"status": erp_sim.FAILED})  # failed without any recorded approval
    r = client.post(f"/api/rmas/{rma_id}/retry", json={"authorizer": REP, "note": "try anyway"})
    assert r.status_code == 409
    assert st.one("SELECT status FROM rmas WHERE rma_id=?", (rma_id,))["status"] == erp_sim.FAILED


# =========================================================================
# Chatbot over HTTP
# =========================================================================

def test_a_chat_session_can_be_resumed(client):
    cid = client.post("/api/chat/sessions").json()["conversation_id"]
    client.post(f"/api/chat/sessions/{cid}/messages",
                json={"text": "My name is Ivan Nowak"})
    resumed = client.get(f"/api/chat/sessions/{cid}").json()
    assert resumed["conversation_id"] == cid
    assert len(resumed["turns"]) >= 2


def test_the_chat_reply_reflects_the_message_just_sent(client):
    """Defect C6: the old sidebar always showed the previous turn."""
    cid = client.post("/api/chat/sessions").json()["conversation_id"]
    out = client.post(f"/api/chat/sessions/{cid}/messages",
                      json={"text": "My name is Ivan Nowak"}).json()
    assert out["identification"]["name"] is True
    assert out["turns"][-1]["role"] == "assistant"
    assert out["turns"][-2]["text"] == "My name is Ivan Nowak"


def test_the_customer_never_sees_internal_state(client):
    cid = client.post("/api/chat/sessions").json()["conversation_id"]
    out = client.post(f"/api/chat/sessions/{cid}/messages",
                      json={"text": "My name is Ivan Nowak"}).json()
    assert set(out["identification"]) == {"name", "date_of_birth",
                                          "serial_number", "verified"}
    assert all(isinstance(v, bool) for v in out["identification"].values())
    assert "claim" not in out and "account_id" not in out


def test_an_unidentifiable_chat_customer_appears_in_the_inbox(client):
    """Defect C4, end to end through the API."""
    cid = client.post("/api/chat/sessions").json()["conversation_id"]
    case_ref = None
    for _ in range(5):
        out = client.post(f"/api/chat/sessions/{cid}/messages",
                          json={"text": "hello"}).json()
        if out["handed_off"]:
            case_ref = out["case_reference"]
            break
    assert case_ref, "the customer must end up in the representative inbox"
    inbox = client.get("/api/cases").json()
    assert any(c["case_id"] == case_ref for c in inbox["cases"])


def test_a_chat_safety_report_becomes_a_critical_case(client, st):
    cid = client.post("/api/chat/sessions").json()["conversation_id"]
    out = client.post(
        f"/api/chat/sessions/{cid}/messages",
        json={"text": "The unit burned my arm during a session."}).json()
    assert out["handed_off"] is True
    case = st.case(out["case_reference"])
    assert case["priority"] == "Critical"
    assert case["status"] == caseflow.HUMAN_REVIEW


# =========================================================================
# Quality, trends, audit and operations
# =========================================================================

def test_the_quality_view_keeps_the_customers_own_words(client):
    _make_case(client, "Burn", "I was burned by the unit during a session.",
               investigate=False)
    q = client.get("/api/quality").json()
    assert q["groups"]["reported_injury"], q
    note = q["groups"]["reported_injury"][0]
    assert "burned" in (note["customer_statement"] or "")
    assert "no reportability" in q["mdr_note"].lower()


def test_an_adverse_event_determination_changes_the_case(client, st):
    _make_case(client, "Redness", "My skin is sore and red after treatment.",
               investigate=False)
    note = client.get("/api/notifications").json()["notifications"][0]
    r = client.post(f"/api/notifications/{note['notification_id']}"
                    f"/determination",
                    json={"authorizer": REP,
                          "determination": "confirmed adverse event",
                          "rationale": "Reported redness requires a simulated safety review."})
    assert r.status_code == 200
    case = st.case(note["case_id"])
    assert case["is_adverse_event"] == 1
    assert case["priority"] == "Critical"


def test_a_determination_cannot_be_made_twice(client):
    _make_case(client, "Redness", "My skin is sore after treatment.",
               investigate=False)
    note = client.get("/api/notifications").json()["notifications"][0]
    body = {"authorizer": REP, "determination": "not an adverse event",
            "rationale": "Representative reviewed the reported symptoms."}
    nid = note["notification_id"]
    assert client.post(f"/api/notifications/{nid}/determination",
                       json=body).status_code == 200
    assert client.post(f"/api/notifications/{nid}/determination",
                       json=body).status_code == 409


def test_analytics_separates_live_cases_from_replays(client):
    _movement_case(client)
    data = client.get("/api/analytics").json()
    assert data["scope"]["unique_cases"] >= 1
    assert data["scope"]["evaluation_case_rows"] == 0
    assert "denominator" in str(data).lower() or data["scope"]["note"]
    assert data["queue"]["maxsize"] >= 1
    assert data["queue"]["queued"] >= 0
    assert data["processing"]["completed"] >= 0
    assert "mean_seconds" in data["processing"]


def test_the_audit_feed_is_filterable(client):
    cid = _movement_case(client)
    everything = client.get("/api/audit").json()
    assert everything["events"]
    only = client.get("/api/audit",
                      params={"case_id": cid,
                              "event_type": "erp_movement"}).json()
    assert all(e["case_id"] == cid for e in only["events"])
    assert all("erp_movement" in e["event_type"] for e in only["events"])


def test_the_simulator_is_owned_by_the_application(client):
    before = client.get("/api/ops/queue").json()
    assert before["simulator"]["running"] is False
    client.post("/api/ops/arrivals/start")
    assert client.get("/api/ops/queue").json()["simulator"]["running"] is True
    client.post("/api/ops/arrivals/pause")
    assert client.get("/api/ops/queue").json()["simulator"]["running"] is False


def test_seeding_releases_ten_tickets_at_most_once_each(client, st):
    out = client.post("/api/ops/arrivals/seed", params={"n": 10}).json()
    assert len(out["released"]) == 10
    pools = st.query("SELECT pool_id FROM cases WHERE pool_id IS NOT NULL")
    assert len({p["pool_id"] for p in pools}) == 10


def test_reset_restores_the_pool_and_audits_the_removal(client, st):
    client.post("/api/ops/arrivals/seed", params={"n": 3})
    out = client.post("/api/ops/arrivals/reset").json()
    assert out["cases_removed"] == 3
    assert st.one("SELECT COUNT(*) c FROM ticket_pool WHERE released=1")["c"] \
        == 0
    assert st.query("SELECT * FROM audit_events WHERE event_type=?",
                    ("case_removed_by_reset",))


def test_deterministic_triage_is_available_without_the_model(client):
    r = client.post("/api/eval/triage",
                    json={"text": "There was a burning smell but nobody was "
                                  "hurt."})
    assert r.status_code == 200
    body = r.json()
    assert body["priority"] == "Critical"
    assert body["adverse_event"]["is_adverse_event"] is False
    assert body["adverse_event"]["hazard_indicators"]
