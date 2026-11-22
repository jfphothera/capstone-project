"""Reproducible end-to-end demonstrations of the simulated operation.

    python run_operations_demo.py              # all demonstrations
    python run_operations_demo.py --demo 3
    python run_operations_demo.py --llm        # use the local model (slow)
    python run_operations_demo.py --keep       # keep the previous demo DB

Runs against its own database (data/demo_operations.db) so it can be re-run
from a clean slate without touching the live portal data. Source records are
copied from the imported operational database.

  1  Approved return      human approval -> ERP call + RMA -> sample label
  2  Rejected return      rejection recorded, no RMA or label created
  3  Outbound part        a part ships instead of the whole device
  4  Tracking inquiry     a real recorded shipment is retrieved and quoted
  5  Safety escalation    critical, troubleshooting stopped, QA notified
  6  Representative flow  pre-investigation, checklist, correction, approval
  7  Persistence          records survive a simulated restart
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from capstone_rag import (  # noqa: E402
    analytics, caseflow, config, erp_sim, movement, queueing, review,
    store as store_mod, triage,
)

RULE = "=" * 78
THIN = "-" * 78
REP = "rep.jordan"
DEMO_DB = os.path.join(config.DATA_DIR, "demo_operations.db")

IVAN, MARY, PETER = "ACC-00010", "ACC-00022", "ACC-00026"
SERIAL_A = "98148469"      # P4800 Rev A, verified revision
SERIAL_SM = "38180-BM2"    # Series-M Rev C, verified revision


def head(n, title):
    print(f"\n{RULE}\nDEMONSTRATION {n} - {title}\n{RULE}")


def show_case(st, case_id, label="case"):
    c = st.case(case_id)
    print(f"  {label}: {case_id}")
    print(f"    priority   : {c['priority']}  ({c['priority_reason'][:60]})")
    print(f"    status     : {c['status']}   ai={c['ai_state']}")
    print(f"    customer   : {c['customer_name']}  verified="
          f"{bool(c['identity_verified'])}")
    print(f"    device     : {c['model']} Rev {c['revision']}  "
          f"serial {c['serial_number']}")
    print(f"    complaint={bool(c['is_complaint'])}  "
          f"adverse_event={bool(c['is_adverse_event'])}"
          f"/{c['ae_certainty']}  rma_needed={bool(c['rma_needed'])}")


def show_links(st, case_id):
    calls = st.query("SELECT * FROM erp_calls WHERE case_id=?", (case_id,))
    rmas = st.query("SELECT * FROM rmas WHERE case_id=?", (case_id,))
    ships = st.query("SELECT * FROM sim_shipments WHERE case_id=?", (case_id,))
    print(f"\n  linked records")
    for c in calls:
        print(f"    ERP call  {c['call_id']}  {c['call_type']}  "
              f"status={c['status']}")
    for r in rmas:
        items = json.loads(r["items"] or "[]")
        print(f"    RMA       {r['rma_id']}  {r['movement_type']} "
              f"({r['direction']})  status={r['status']}  "
              f"approved_by={r['approved_by']}")
        for i in items:
            print(f"                 - {i.get('name')} "
                  f"[{i.get('part_number')}]")
    for s in ships:
        print(f"    Shipment  {s['shipment_id']}  {s['direction']}  "
              f"tracking={s['tracking_number']}  status={s['status']}")
    if not (calls or rmas or ships):
        print("    (none)")
    return calls, rmas, ships


def investigate(ops, case_id):
    ops.flow.pre_investigate(case_id)


def checklist_summary(st, case_id):
    print("\n  AI checklist")
    for i in st.checklist(case_id):
        mark = {"completed": "ok  ", "failed": "FAIL", "pending": "....",
                "requires_approval": "APPR", "not_applicable": "n/a "}[
            i["status"]]
        extra = f"   <- {i['missing_info'][:58]}" if i["missing_info"] else ""
        print(f"    [{mark}] {i['action']}{extra}")


# =========================================================================

def demo_1_approved_return(ops):
    head(1, "APPROVED RETURN - approval, ERP call, RMA, sample label")
    st = ops.store
    print("A customer has worked through every approved step and the unit is\n"
          "still faulty. The agent proposes a return; only a person can\n"
          "authorise it.")

    case_id = ops.flow.intake(
        "Unit still dead after all steps",
        "My name is Ivan Nowak and the serial is 98148469. I have been "
        "through every step your assistant gave me and the unit is still "
        "completely dead. This has happened again and again and it is "
        "still not fixed. I cannot use it for my treatments at all.",
        account_id=IVAN)
    investigate(ops, case_id)
    show_case(st, case_id)
    checklist_summary(st, case_id)

    # The exhausted path is reached by proposing the movement directly from
    # the rule set, as the representative would after reading the checklist.
    t = triage.triage(st.one(
        "SELECT body FROM messages WHERE case_id=? AND direction='inbound'",
        (case_id,))["body"])
    decision = movement.decide("the unit is still completely dead", t,
                               procedure_exhausted=True,
                               applicable_procedure="PROC-P4800-PWR-A",
                               identity_verified=True)
    print(f"\n  movement rule {decision.rule}: {decision.movement_type}")
    print(f"    {decision.reason}")
    prop = ops.erp.propose_movement(case_id, decision, account_id=IVAN,
                                    serial_number=SERIAL_A)
    rma_id = prop.rma["rma_id"]

    print(f"\n{THIN}\n  BEFORE APPROVAL\n{THIN}")
    show_links(st, case_id)
    print(f"    -> RMA {rma_id} is 'proposed'. No label exists. The agent "
          f"cannot approve it.")

    print(f"\n{THIN}\n  REPRESENTATIVE APPROVES\n{THIN}")
    res = ops.erp.approve(rma_id, REP, "All field steps exhausted; "
                                       "authorise return.")
    print(f"    approve() -> ok={res.ok} {res.reason}")
    show_links(st, case_id)

    ship = st.one("SELECT * FROM sim_shipments WHERE rma_id=?", (rma_id,))
    if ship and ship["label_path"] and os.path.exists(ship["label_path"]):
        print(f"\n  sample return label ({ship['label_path']}):")
        for line in open(ship["label_path"], encoding="utf-8").read().splitlines():
            print("    " + line)

    print(f"\n  duplicate protection: approving again ->")
    again = ops.erp.approve(rma_id, REP)
    print(f"    ok={again.ok} duplicate={again.duplicate} {again.reason}")
    return case_id


def demo_2_rejected_return(ops):
    head(2, "REJECTED RETURN - nothing is created")
    st = ops.store
    case_id = ops.flow.intake(
        "Please take this unit back",
        "This is Mary Cohen. I want to return my unit, serial 68177-DV5. "
        "It keeps failing and I have had enough.", account_id=MARY)
    investigate(ops, case_id)
    show_case(st, case_id)

    t = triage.triage("it keeps failing and is still not fixed")
    decision = movement.decide("the unit keeps failing", t,
                               procedure_exhausted=True,
                               applicable_procedure=None,
                               identity_verified=True)
    prop = ops.erp.propose_movement(case_id, decision, account_id=MARY,
                                    serial_number="68177-DV5")
    if not prop.ok:
        decision = movement.MovementDecision(
            movement.DEVICE_RETURN, movement.INBOUND,
            [{"part_number": "UNIT", "name": "Complete device"}],
            rule="MR-4", reason="customer requested return",
            requires_human_review=True)
        prop = ops.erp.propose_movement(case_id, decision, account_id=MARY,
                                        serial_number="68177-DV5")
    rma_id = prop.rma["rma_id"]
    print(f"\n  proposed {rma_id}")

    print(f"\n{THIN}\n  REPRESENTATIVE REJECTS\n{THIN}")
    res = ops.erp.reject(rma_id, REP,
                         "Unit is in warranty assessment; no return yet.")
    print(f"    reject() -> {res.reason}")
    show_links(st, case_id)

    ship = st.one("SELECT * FROM sim_shipments WHERE rma_id=?", (rma_id,))
    rma = st.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,))
    print(f"\n  verification")
    print(f"    RMA status            : {rma['status']}")
    print(f"    shipment rows created : {0 if ship is None else 1}")
    print(f"    label created         : {bool(ship)}")
    print(f"    -> nothing was finalized, and the rejection is on the record")
    for e in st.audit_trail(case_id):
        if "reject" in e["event_type"]:
            print(f"    audit: {e['event_type']} by {e['actor']} - "
                  f"{e['summary']}")
    return case_id


def demo_3_outbound_part(ops):
    head(3, "OUTBOUND PART - a part ships, the device stays put")
    st = ops.store
    print("An RMA is an inventory movement, not automatically a device\n"
          "return. Here the right answer is a pair of goggles.")

    case_id = ops.flow.intake(
        "Goggles missing from my delivery",
        "My name is Ivan Nowak, serial 98148469. The goggles that should "
        "have come with the device were missing from the box.",
        account_id=IVAN)
    investigate(ops, case_id)
    show_case(st, case_id)
    checklist_summary(st, case_id)

    rma = st.one("SELECT * FROM rmas WHERE case_id=? ORDER BY created_at "
                 "DESC", (case_id,))
    print(f"\n  the agent proposed: {rma['movement_type']} "
          f"({rma['direction']})")
    print(f"    reason: {rma['reason']}")
    print(f"    -> note this is NOT a device return")

    res = ops.erp.approve(rma["rma_id"], REP, "Send the goggles.")
    print(f"\n  representative approves -> ok={res.ok}")
    show_links(st, case_id)

    ship = st.one("SELECT * FROM sim_shipments WHERE rma_id=?",
                  (rma["rma_id"],))
    if ship:
        print(f"\n  sample outbound label ({ship['shipment_id']}):")
        for line in open(ship["label_path"],
                         encoding="utf-8").read().splitlines()[:22]:
            print("    " + line)
    return case_id, (ship or {}).get("tracking_number")


def demo_4_tracking(ops, tracking_number):
    head(4, "TRACKING INQUIRY - only recorded state is reported")
    st = ops.store
    if not tracking_number:
        print("  no shipment available from demonstration 3")
        return
    print(f"  looking up {tracking_number}")
    rec = ops.erp.track(tracking_number)
    print(f"    carrier   : {rec['carrier']}")
    print(f"    direction : {rec['direction']}")
    print(f"    status    : {rec['status']}")
    print(f"    contents  : "
          f"{[i.get('name') for i in rec['contents']]}")
    print(f"    ship to   : {rec['ship_to'].get('name')}, "
          f"{rec['ship_to'].get('city')}")
    print(f"    events    :")
    for e in rec["events"]:
        print(f"      {e['at'][:19]}  {e['status']:<14} {e['note']}")

    print(f"\n  advancing the simulated shipment")
    ops.erp.advance(tracking_number, erp_sim.IN_TRANSIT, "Regional hub",
                    "simulated scan")
    ops.erp.advance(tracking_number, erp_sim.DELIVERED, "Customer address",
                    "simulated delivery")
    rec = ops.erp.track(tracking_number)
    print(f"    status now: {rec['status']}")
    for e in rec["events"]:
        print(f"      {e['at'][:19]}  {e['status']:<14} {e['note']}")

    mid = review.acknowledge_delivery(st, rec["case_id"], tracking_number)
    if mid:
        msg = st.one("SELECT * FROM messages WHERE message_id=?", (mid,))
        print(f"\n  factual acknowledgement drafted from the recorded "
              f"delivery ({mid}, status={msg['status']}):")
        for line in msg["body"].splitlines():
            print("    " + line)
        print("\n    -> still a draft; the representative must approve it "
              "before it is sent")

    print(f"\n  a tracking number that does not exist:")
    print(f"    track('SIMFX0000000000') -> "
          f"{ops.erp.track('SIMFX0000000000')}")


def demo_5_safety(ops):
    head(5, "SAFETY ESCALATION - troubleshooting stops, QA is notified")
    st = ops.store
    case_id = ops.flow.intake(
        "Burn after treatment",
        "This is Peter Thomas, serial 38180-BM2. I have a burn on my arm "
        "after yesterday's session and there was a burning smell from the "
        "unit.", account_id=PETER)
    print("\n  at intake, before any model ran:")
    show_case(st, case_id)

    investigate(ops, case_id)
    print("\n  after the AI investigation:")
    show_case(st, case_id)
    checklist_summary(st, case_id)

    print(f"\n  QA notifications")
    for n in st.query("SELECT * FROM qa_notifications WHERE case_id=?",
                      (case_id,)):
        print(f"    {n['notification_id']}  kind={n['kind']}  "
              f"certainty={n['certainty']}  status={n['status']}")
        print(f"      {n['body']}")

    print(f"\n  simulated outbox")
    for m in st.query("SELECT * FROM messages WHERE case_id=? AND "
                      "channel='qa_notification'", (case_id,)):
        print(f"    {m['subject']} -> {m['status']}")

    srcs = st.query("SELECT COUNT(*) c FROM retrieved_sources WHERE case_id=?",
                    (case_id,))[0]["c"]
    print(f"\n  QMS passages retrieved: {srcs}  "
          f"(routine troubleshooting was not performed)")

    n = st.one("SELECT * FROM qa_notifications WHERE case_id=?", (case_id,))
    review.determine_adverse_event(
        st, n["notification_id"],
        "Confirmed reportable adverse event; unit quarantined.", REP)
    print(f"\n  representative determination recorded on "
          f"{n['notification_id']}")
    return case_id


def demo_6_representative_flow(ops):
    head(6, "REPRESENTATIVE WORKFLOW - checklist, correction, approval")
    st = ops.store
    case_id = ops.flow.intake(
        "P4800 will not power on",
        "My name is Ivan Nowak and the serial is 98148469. The unit is "
        "completely dead - no lights at all and no fan noise when I flip "
        "the switch.", account_id=IVAN)
    print("  1. ticket arrives and is triaged with no model involved")
    show_case(st, case_id)

    print("\n  2. the AI pre-investigates before the representative opens it")
    investigate(ops, case_id)
    checklist_summary(st, case_id)

    items = st.checklist(case_id)
    print("\n  3. the representative reviews individual findings")
    approve_me = next(i for i in items if i["action"] == "verify_identity")
    review.decide_item(st, approve_me["item_id"], "approved", REP)
    print(f"    approved  : {approve_me['action']}")

    reject_me = next(i for i in items
                     if i["action"] == "determine_product_movement")
    review.decide_item(st, reject_me["item_id"], "rejected", REP,
                       "Customer has not tried the interlock yet - do not "
                       "consider any movement.")
    print(f"    corrected : {reject_me['action']}")
    print(f"                'Customer has not tried the interlock yet'")

    print("\n  4. the representative rejects the draft and asks for a redo")
    draft = review.latest_draft(st, case_id)
    print(f"    original draft {draft['message_id']} "
          f"({len(draft['body'])} chars)")
    out = review.regenerate_reply(
        ops, case_id, draft["message_id"],
        "Too long. Lead with the interlock check, it is the most common "
        "cause.", REP)
    print(f"    {out['summary']}")

    print("\n  5. history is preserved, not overwritten")
    for r in st.query("SELECT * FROM recommendations WHERE case_id=?",
                      (case_id,)):
        print(f"    {r['rec_id']} kind={r['kind']} status={r['status']} "
              f"revisions={r['revision_count']}")
        print(f"      feedback: {r['feedback']}")
        print(f"      original kept: {bool(r['original_payload'])}  "
              f"revision kept: {bool(r['payload'])}")

    print("\n  6. the representative approves and sends")
    new_draft = review.latest_draft(st, case_id)
    sent = review.approve_and_send(st, case_id, new_draft["message_id"],
                                   new_draft["body"], REP)
    print(f"    sent -> {sent}")
    msg = st.one("SELECT * FROM messages WHERE message_id=?",
                 (new_draft["message_id"],))
    print(f"    message status={msg['status']} approved_by="
          f"{msg['approved_by']}")

    print("\n  7. resolve the case")
    review.close_case(st, case_id, REP, "Interlock was not seated.",
                      resolved=True)
    c = st.case(case_id)
    print(f"    status={c['status']} resolved={bool(c['resolved'])}")

    print("\n  8. the audit trail")
    for e in st.audit_trail(case_id):
        print(f"    {e['at'][11:19]}  {e['event_type']:<34} "
              f"{(e['actor'] or ''):<12} {(e['summary'] or '')[:46]}")
    return case_id


def demo_7_persistence(ops, case_ids):
    head(7, "PERSISTENCE - records survive a restart")
    path = ops.store.path
    before = ops.store.stats()
    print(f"  database: {path}")
    print(f"  before restart: {before}")

    ops.store.close()
    reopened = store_mod.Store(path)
    after = reopened.stats()
    print(f"  after  restart: {after}")
    print(f"  identical: {before == after}")

    cid = case_ids[0]
    c = reopened.case(cid)
    print(f"\n  {cid} after restart:")
    print(f"    status={c['status']} priority={c['priority']}")
    print(f"    checklist items : {len(reopened.checklist(cid))}")
    print(f"    audit events    : {len(reopened.audit_trail(cid))}")
    print(f"    messages        : {len(reopened.messages(cid))}")
    rmas = reopened.query("SELECT * FROM rmas WHERE case_id=?", (cid,))
    print(f"    RMAs            : "
          f"{[(r['rma_id'], r['status'], r['approved_by']) for r in rmas]}")
    return reopened


def show_trends(st):
    print(f"\n{RULE}\nTRENDS (live records only; evaluation replays excluded)"
          f"\n{RULE}")
    a = analytics.Analytics(st)
    rep = a.report()
    for section in ("safety", "complaints", "identification", "movements",
                    "shipping", "lifecycle", "escalation"):
        print(f"\n  {section}")
        for k, v in rep[section].items():
            print(f"    {k:<42} {v}")
    print(f"\n  proposed actions for the supervisor")
    for p in a.proposed_actions():
        print(f"    [{p['priority']}] {p['area']}: {p['action']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", type=int)
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    source_db = config.DB_PATH
    if not os.path.exists(source_db):
        sys.exit("Run scripts/import_synthetic.py first.")
    if os.path.exists(DEMO_DB) and not args.keep:
        os.remove(DEMO_DB)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(DEMO_DB + suffix):
                os.remove(DEMO_DB + suffix)
    if not os.path.exists(DEMO_DB):
        shutil.copy(source_db, DEMO_DB)

    st = store_mod.Store(DEMO_DB)
    # Keep only source data; the demonstrations create their own cases.
    if not args.keep:
        for t in ("shipment_events", "sim_shipments", "rmas", "erp_calls",
                  "approvals", "recommendations", "reasoning_attempts",
                  "retrieved_sources", "tool_calls", "checklist_items",
                  "qa_notifications", "conversations", "messages", "cases",
                  "improvement_proposals", "audit_events"):
            st.execute(f"DELETE FROM {t}")

    print(RULE)
    print("SIMULATED CUSTOMER-SERVICE OPERATION - END TO END")
    print(RULE)
    print(f"database   : {DEMO_DB}")
    print(f"generation : {'local LLM' if args.llm else 'deterministic writer'}")
    agent = caseflow.make_agent(prefer_llm=args.llm, verbose=True)
    ops = queueing.Operations.build(prefer_llm=args.llm, store=st, agent=agent)

    created = []
    demos = {1: demo_1_approved_return, 2: demo_2_rejected_return,
             5: demo_5_safety, 6: demo_6_representative_flow}
    tracking = None

    chosen = [args.demo] if args.demo else [1, 2, 3, 4, 5, 6, 7]
    for n in chosen:
        if n == 3:
            cid, tracking = demo_3_outbound_part(ops)
            created.append(cid)
        elif n == 4:
            demo_4_tracking(ops, tracking)
        elif n == 7:
            if created:
                st = demo_7_persistence(ops, created)
        elif n in demos:
            created.append(demos[n](ops))

    if not args.demo:
        show_trends(st)
    print(f"\n{RULE}\nDONE - {len(created)} cases created in {DEMO_DB}\n{RULE}")


if __name__ == "__main__":
    main()
