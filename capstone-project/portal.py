"""Representative portal - the primary capstone demonstration interface.

    streamlit run portal.py

A customer-service workspace, not a test harness. The engineering view is
still available separately as `app.py`.

Layout
  left    the inbox, with arrival-simulator and queue controls
  centre  the selected case: the customer's email and the reply being drafted
  right   the AI's investigation as an actionable checklist

Everything the representative does here is recorded: approvals, rejections,
written corrections, priority overrides and sent messages all land in the
append-only audit trail with their authoriser.
"""

from __future__ import annotations

import json
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from capstone_rag import (  # noqa: E402
    caseflow, config, erp_sim, generation, queueing, review,
    store as store_mod, triage as triage_mod,
)

st.set_page_config(page_title="Support Workspace", layout="wide")

REP = "rep.jordan"          # the single simulated representative

PRIORITY_ICON = {"Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "⚪"}
STATUS_ICON = {
    caseflow.DONE: "✅", caseflow.ITEM_FAILED: "❌",
    caseflow.PENDING: "⏳", caseflow.NEEDS_APPROVAL: "🖊️",
    caseflow.NA: "➖",
}


@st.cache_resource(show_spinner="Loading agent, embedding model and index...")
def get_ops(prefer_llm: bool):
    ops = queueing.Operations.build(prefer_llm=prefer_llm)
    ops.queue.start()
    ops.queue.submit_pending()
    return ops


def _j(value, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


# =========================================================================
# sidebar: simulator + queue
# =========================================================================

def sidebar(ops):
    with st.sidebar:
        st.header("Operations")

        q = ops.queue.snapshot()
        s = ops.simulator.snapshot()

        c1, c2 = st.columns(2)
        c1.metric("Queued", q["queued"])
        c2.metric("Done", q["completed"])
        if q["current"]:
            st.info(f"Model working on {q['current']}")
        else:
            st.caption("Model idle")
        if q["failed"] or q["retriable"]:
            st.warning(f"{q['failed']} failed, {q['retriable']} retriable")
        if q["last_error"]:
            st.caption(f"last error: {q['last_error'][:90]}")
        st.caption(f"One inference worker, queue cap {q['maxsize']}. Intake "
                   f"and safety screening never wait on the model.")

        st.markdown("---")
        st.subheader("Ticket arrivals")
        st.caption(f"{s['pool_available']} synthetic tickets left in the pool")
        if s["running"]:
            st.success(f"Running - next in ~{s['next_due_in']}s")
            if st.button("Pause arrivals", width="stretch"):
                ops.simulator.pause()
                st.rerun()
        else:
            st.caption(f"Paused. Interval {s['interval'][0]}-"
                       f"{s['interval'][1]}s when running.")
            if st.button("Start arrivals", type="primary", width="stretch"):
                ops.simulator.start()
                st.rerun()

        c1, c2 = st.columns(2)
        if c1.button("Add 1 now", width="stretch"):
            cid = ops.simulator.release_one()
            st.toast(f"Released {cid}" if cid else "Pool empty")
            st.rerun()
        if c2.button("Seed 10", width="stretch"):
            ops.simulator.seed_inbox(10)
            st.rerun()

        with st.expander("Reset demonstration"):
            st.caption("Returns every pooled ticket and removes the cases "
                       "they created. The audit trail keeps a record of the "
                       "reset.")
            if st.button("Reset", width="stretch"):
                out = ops.simulator.reset()
                st.session_state.pop("case_id", None)
                st.toast(f"Reset: {out['cases_removed']} cases removed")
                st.rerun()

        st.markdown("---")
        st.caption(f"Representative: **{REP}** (single simulated user)")
        if st.button("Refresh", width="stretch"):
            st.rerun()


# =========================================================================
# inbox
# =========================================================================

def inbox(ops):
    st.subheader("Inbox")
    cases = ops.store.cases()
    if not cases:
        st.info("No cases yet. Use **Seed 10** or **Start arrivals**.")
        return None

    only_open = st.checkbox("Hide resolved and closed", value=True)
    if only_open:
        cases = [c for c in cases
                 if c["status"] not in (caseflow.RESOLVED, caseflow.CLOSED)]

    cases.sort(key=lambda c: (triage_mod.PRIORITY_ORDER.get(c["priority"], 9),
                              c["created_at"]))

    rows = []
    for c in cases:
        flags = []
        if c["is_adverse_event"]:
            flags.append(f"AE/{c['ae_certainty']}")
        if c["is_complaint"]:
            flags.append("complaint")
        if c["rma_needed"]:
            flags.append("RMA")
        rows.append({
            "": PRIORITY_ICON.get(c["priority"], ""),
            "case": c["case_id"],
            "priority": c["priority"],
            "customer": c["customer_name"] or c["account_id"] or "unknown",
            "subject": (c["subject"] or "")[:46],
            "status": c["status"],
            "flags": ", ".join(flags) or "-",
            "AI": c["ai_state"],
            "assigned": c["assigned_to"] or "-",
        })
    st.dataframe(rows, width="stretch", hide_index=True, height=300)

    ids = [c["case_id"] for c in cases]
    current = st.session_state.get("case_id")
    index = ids.index(current) if current in ids else 0
    chosen = st.selectbox("Open case", ids, index=index,
                          format_func=lambda i: (
                              f"{PRIORITY_ICON.get(ops.store.case(i)['priority'], '')} "
                              f"{i} - {(ops.store.case(i)['subject'] or '')[:40]}"))
    st.session_state["case_id"] = chosen
    return chosen


# =========================================================================
# conversation column
# =========================================================================

def conversation(ops, case):
    cid = case["case_id"]
    st.subheader(f"{PRIORITY_ICON.get(case['priority'], '')} {cid}")
    st.caption(case["subject"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Priority", case["priority"])
    c2.metric("Status", case["status"])
    c3.metric("Verified", "yes" if case["identity_verified"] else "no")
    c4.metric("Device", f"{case['model'] or '-'} "
                        f"{('Rev ' + case['revision']) if case['revision'] else ''}")

    if case["is_adverse_event"] or case["priority"] == triage_mod.CRITICAL:
        st.error(f"Critical: {case['priority_reason']}")

    with st.expander("Override priority"):
        new = st.selectbox("Priority", [triage_mod.CRITICAL, triage_mod.HIGH,
                                        triage_mod.MEDIUM, triage_mod.LOW],
                           index=[triage_mod.CRITICAL, triage_mod.HIGH,
                                  triage_mod.MEDIUM,
                                  triage_mod.LOW].index(case["priority"]),
                           key=f"prio-{cid}")
        why = st.text_input("Reason (recorded)", key=f"prio-why-{cid}")
        if st.button("Apply override", key=f"prio-go-{cid}"):
            if not why.strip():
                st.warning("A reason is required.")
            else:
                review.override_priority(ops.store, cid, new, why, REP)
                st.rerun()

    st.markdown("#### Conversation")
    for m in ops.store.messages(cid):
        if m["channel"] == "qa_notification":
            st.warning(f"**QA notification** {m['subject']}\n\n{m['body']}")
            continue
        who = ("Customer" if m["direction"] == "inbound"
               else f"Us ({m['author']})")
        with st.chat_message("user" if m["direction"] == "inbound"
                             else "assistant"):
            st.caption(f"{who} - {m['created_at'][:19]} - {m['status']}")
            st.write(m["body"])

    draft = review.latest_draft(ops.store, cid)
    if draft:
        st.markdown("#### Proposed reply (needs your approval to send)")
        if "[For representative:" in draft["body"]:
            st.info("This draft contains a note for you that will be removed "
                    "before sending.")
        edited = st.text_area("Reply", value=draft["body"], height=260,
                              key=f"draft-{draft['message_id']}")
        c1, c2, c3 = st.columns(3)
        if c1.button("Approve and send", type="primary",
                     key=f"send-{draft['message_id']}"):
            review.approve_and_send(ops.store, cid, draft["message_id"],
                                    edited, REP)
            st.rerun()
        if c2.button("Reject draft", key=f"rej-{draft['message_id']}"):
            review.reject_draft(ops.store, cid, draft["message_id"], REP)
            st.rerun()
        with c3.popover("Reject and ask the AI to redo it"):
            fb = st.text_area("What is wrong with it?",
                              key=f"fb-{draft['message_id']}")
            if st.button("Regenerate", key=f"regen-{draft['message_id']}"):
                if not fb.strip():
                    st.warning("Feedback is required.")
                else:
                    with st.spinner("Reconsidering..."):
                        out = review.regenerate_reply(
                            ops, cid, draft["message_id"], fb, REP)
                    st.toast(out["summary"])
                    st.rerun()


# =========================================================================
# checklist column
# =========================================================================

def checklist(ops, case):
    cid = case["case_id"]
    st.subheader("AI investigation")
    items = ops.store.checklist(cid)
    if not items:
        st.info("Not investigated yet. The case is "
                f"**{case['ai_state']}** in the inference queue.")
        if st.button("Investigate now", key=f"inv-{cid}"):
            ops.queue.submit(cid, case["priority"])
            st.rerun()
        return

    done = sum(1 for i in items if i["status"] == caseflow.DONE)
    st.caption(f"{done}/{len(items)} steps completed - "
               f"{sum(1 for i in items if i['status'] == caseflow.NEEDS_APPROVAL)}"
               f" awaiting you")

    for i in items:
        icon = STATUS_ICON.get(i["status"], "")
        decided = (f" - {i['approval_decision']} by {i['authorizer']}"
                   if i["approval_decision"] else "")
        with st.expander(f"{icon} {i['action']}{decided}", expanded=bool(
                i["status"] == caseflow.NEEDS_APPROVAL and not
                i["approval_decision"])):
            st.caption(f"{i['description']}  \nby **{i['actor']}** at "
                       f"{i['executed_at'][:19]} - status `{i['status']}`")
            tr = _j(i["tool_result"])
            if tr is not None:
                st.json(tr, expanded=False)
            ev = _j(i["evidence"])
            if ev:
                with st.container():
                    st.caption("Evidence")
                    st.json(ev, expanded=False)
            if i["missing_info"]:
                st.warning(f"Missing / uncertain: {i['missing_info']}")
            if i["feedback"]:
                st.info(f"Your correction: {i['feedback']}")

            if not i["approval_decision"]:
                c1, c2 = st.columns(2)
                if c1.button("Approve", key=f"ok-{i['item_id']}"):
                    review.decide_item(ops.store, i["item_id"], "approved",
                                       REP)
                    st.rerun()
                with c2.popover("Reject / correct"):
                    note = st.text_input("Correction",
                                         key=f"cor-{i['item_id']}")
                    if st.button("Record", key=f"corgo-{i['item_id']}"):
                        review.decide_item(ops.store, i["item_id"],
                                           "rejected", REP, note)
                        st.rerun()

    # --- product movement ------------------------------------------
    rmas = ops.store.query(
        "SELECT * FROM rmas WHERE case_id=? ORDER BY created_at", (cid,))
    if rmas:
        st.markdown("#### Product movement")
        for r in rmas:
            items_ = _j(r["items"], [])
            st.write(f"**{r['rma_id']}** - {r['movement_type']} "
                     f"({r['direction']}) - `{r['status']}`")
            st.caption(r["reason"])
            st.caption("Items: " + ", ".join(
                f"{i.get('name')} [{i.get('part_number')}]" for i in items_))
            if r["status"] == erp_sim.PROPOSED:
                st.warning("The agent cannot approve this. You must.")
                note = st.text_input("Decision note", key=f"rn-{r['rma_id']}")
                c1, c2 = st.columns(2)
                if c1.button("Approve movement", type="primary",
                             key=f"ra-{r['rma_id']}"):
                    res = ops.erp.approve(r["rma_id"], REP, note)
                    st.toast(res.reason or
                             f"Executed, tracking {res.as_dict().get('tracking_number')}")
                    st.rerun()
                if c2.button("Reject movement", key=f"rr-{r['rma_id']}"):
                    res = ops.erp.reject(r["rma_id"], REP, note)
                    st.toast(res.reason)
                    st.rerun()
            elif r["status"] == erp_sim.EXECUTED:
                ship = ops.store.one(
                    "SELECT * FROM sim_shipments WHERE rma_id=?",
                    (r["rma_id"],))
                if ship:
                    st.success(f"Executed - {ship['tracking_number']} "
                               f"({ship['status']})")
                    with st.expander("Simulated label"):
                        if ship["label_path"] and os.path.exists(
                                ship["label_path"]):
                            st.code(open(ship["label_path"],
                                         encoding="utf-8").read())
                    c1, c2 = st.columns(2)
                    if c1.button("Mark in transit", key=f"t1-{ship['shipment_id']}"):
                        ops.erp.advance(ship["tracking_number"],
                                        erp_sim.IN_TRANSIT, "Regional hub",
                                        "simulated scan")
                        st.rerun()
                    if c2.button("Mark delivered", key=f"t2-{ship['shipment_id']}"):
                        ops.erp.advance(ship["tracking_number"],
                                        erp_sim.DELIVERED, "Destination",
                                        "simulated delivery")
                        review.acknowledge_delivery(
                            ops.store, cid, ship["tracking_number"])
                        st.rerun()
            elif r["status"] == erp_sim.REJECTED:
                st.error("Rejected - no finalized RMA or shipping label was "
                         "created.")
            elif r["status"] == erp_sim.FAILED:
                st.error("Execution failed. See the audit trail.")

    # --- closure ---------------------------------------------------
    st.markdown("#### Close the case")
    c1, c2 = st.columns(2)
    res_note = st.text_input("Resolution note", key=f"resn-{cid}")
    if c1.button("Resolve", key=f"res-{cid}"):
        review.close_case(ops.store, cid, REP, res_note, resolved=True)
        st.rerun()
    if c2.button("Close unresolved", key=f"unres-{cid}"):
        review.close_case(ops.store, cid, REP, res_note or
                          "closed without resolution", resolved=False)
        st.rerun()

    with st.expander("Audit trail"):
        for e in ops.store.audit_trail(cid):
            st.caption(f"{e['at'][:19]}  **{e['event_type']}**  "
                       f"({e['actor']})  {e['summary']}")


# =========================================================================

def main():
    st.title("Customer Support Workspace")
    llm_up = generation.llm_available()
    prefer = st.session_state.setdefault("prefer_llm", llm_up)
    ops = get_ops(prefer)

    if not ops.store.one("SELECT 1 c FROM customers LIMIT 1"):
        st.error("No synthetic data loaded. Run:  "
                 "`python scripts/import_synthetic.py`")
        return

    sidebar(ops)

    left, right = st.columns([3, 2])
    with left:
        case_id = inbox(ops)
        if case_id:
            case = ops.store.case(case_id)
            st.markdown("---")
            conversation(ops, case)
    with right:
        if st.session_state.get("case_id"):
            checklist(ops, ops.store.case(st.session_state["case_id"]))


if __name__ == "__main__":
    main()
