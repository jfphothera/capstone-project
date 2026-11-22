"""End-to-end demonstrations of the Checkpoint 1.1 / 2.1 capabilities.

    python run_capstone_demo.py                 # all demonstrations
    python run_capstone_demo.py --demo 2
    python run_capstone_demo.py --llm           # use the local LLM (slow)

Checkpoint 3.1's retrieval demonstrations live in run_demo.py and are
unchanged. This script covers what was added afterwards: conversational
memory, sequential troubleshooting, the bounded reasoning loop, Reflexion
correction, reviewed memory, the escalation queue, return approval, and the
audit trail.

Everything is synthetic and local. Stores are written under data/_demo_run/
so the demonstration can be re-run from a clean slate.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from capstone_rag import (  # noqa: E402
    agent as agent_mod, audit as audit_mod, cases, config, erp as erp_mod,
    generation, memory as memory_mod,
)

RULE = "=" * 78
THIN = "-" * 78

SERIAL_A = "98148469"     # Ivan Nowak, P4800 Rev A
SERIAL_B = "68177-DV5"    # Mary Cohen, P4800 Rev B (no approved procedure)
SERIAL_SM = "38180-BM2"   # Peter Thomas, Series-M Rev C
ACC_IVAN, ACC_MARY, ACC_PETER = "ACC-00010", "ACC-00022", "ACC-00026"


def head(n, title):
    print(f"\n{RULE}\nDEMONSTRATION {n} - {title}\n{RULE}")


def show_turn(label, message, result, show_response=True):
    print(f"\n{THIN}\n{label}\n{THIN}")
    print("customer:")
    for line in message.splitlines():
        print(f"    {line}")
    ident = result.identity.as_dict()
    print(f"\n  device      : "
          + (f"{ident['model']} Rev {ident['revision']} "
             f"({ident['serial_number']})" if ident["confirmed"]
             else f"NOT CONFIRMED - {ident['reason']}"))
    print(f"  tools used  : "
          f"{', '.join(t['tool'] for t in result.tool_calls) or '-'}")
    print(f"  procedure   : {result.selected_procedure or '-'}")
    if result.accepted:
        print(f"  steps given : "
              f"{', '.join(v.chunk_id.split('#')[1] for v in result.accepted)}")
    print(f"  validation  : {result.verdict.result}")
    if result.grounding:
        print(f"  grounding   : {result.grounding['grounded']} "
              f"({result.grounding['checked_sentences']} sentence(s) checked)")
    print(f"  outcome     : {result.outcome}   "
          f"unproductive={result.unproductive_attempts}")
    if result.case_id:
        print(f"  escalation  : {result.case_id} [{result.case_priority}] "
              f"{result.escalation_reason}")
    if result.rma_request_id:
        print(f"  approval    : {result.rma_request_id} ({result.rma_status})")
    if show_response:
        print("\n  response:")
        for line in result.response.splitlines():
            print(f"    {line}")


def show_trace(result):
    print("\n  reasoning trace:")
    for s in result.steps:
        mark = " " if s.productive else "!"
        print(f"   {mark}{s.n:>2}. [{s.kind:<18}] {s.action}")
        print(f"        thought    : {s.thought[:92]}")
        print(f"        observation: {s.observation[:92]}")


# =========================================================================

def demo_1_conversation(ag, stores):
    head(1, "CONVERSATIONAL MEMORY AND SEQUENTIAL TROUBLESHOOTING")
    print("A customer reports a fault without a serial number, supplies it on\n"
          "the second message, then works through the procedure step by step.")

    conv = ag.new_conversation(ACC_IVAN)

    m1 = "Hi, my unit is completely dead. No lights at all and no fan noise."
    r1 = ag.handle(m1, conversation=conv)
    show_turn("TURN 1 - fault reported, no serial given", m1, r1)
    print(f"\n  >> conversation {conv.conversation_id}: device still unknown, "
          f"open question = {conv.open_question!r}")

    m2 = f"Sorry, the serial is {SERIAL_A}."
    r2 = ag.handle(m2, conversation=conv)
    show_turn("TURN 2 - serial supplied; symptom remembered from turn 1",
              m2, r2)
    print(f"\n  >> retrieval query was built from BOTH turns:")
    for line in (r2.retrieval_query or "").splitlines():
        print(f"       {line}")

    m3 = "I tried that and it is still completely dead."
    r3 = ag.handle(m3, conversation=conv)
    show_turn("TURN 3 - follow-up gets the NEXT steps, not a repeat", m3, r3)
    print(f"\n  >> steps already delivered: {len(conv.delivered_chunk_ids)}")

    turn = 3
    last = r3
    while turn < 10 and not last.rma_request_id:
        turn += 1
        last = ag.handle("Still no change, it is completely dead.",
                         conversation=conv)
    show_turn(f"TURN {turn} - every approved step exhausted", "Still no change.",
              last)
    show_trace(last)

    if last.rma_request_id:
        print(f"\n{THIN}\nHUMAN APPROVAL BOUNDARY\n{THIN}")
        req = stores["approvals"].get(last.rma_request_id)
        print(f"  request {req.request_id}")
        print(f"    status        : {req.status}")
        print(f"    is_authorised : {req.is_authorised}")
        print(f"    rma_number    : {req.rma_number}")
        print("    -> the agent prepared the return but CANNOT approve it")

        approved = stores["approvals"].approve(
            req.request_id, reviewer="svc.manager",
            note="Unit out of options, approve return.")
        print(f"\n  after a human approves:")
        print(f"    status        : {approved.status}")
        print(f"    is_authorised : {approved.is_authorised}")
        print(f"    rma_number    : {approved.rma_number}")
        print(f"    decided_by    : {approved.decided_by}")
    return conv


def demo_2_safety(ag, stores):
    head(2, "SAFETY ESCALATION AND THE HUMAN REVIEW QUEUE")
    print("A safety complaint must never be answered with a routine procedure.")

    m = (f"After my session I had skin redness, and there was a burning smell "
         f"from the unit.\nSerial number: {SERIAL_SM}")
    r = ag.handle(m, account_id=ACC_PETER)
    show_turn("Potential safety complaint", m, r)
    show_trace(r)

    q = stores["escalations"]
    case = q.get(r.case_id)
    print(f"\n{THIN}\nQUEUE LIFECYCLE\n{THIN}")
    print(f"  raised   : {case.case_id} [{case.priority}] {case.reason}")
    print(f"             status={case.status} assignee={case.assignee}")
    q.assign(case.case_id, "safety.reviewer")
    c = q.get(case.case_id)
    print(f"  assigned : status={c.status} assignee={c.assignee}")
    q.resolve(case.case_id,
              "Reviewed as a reportable thermal event; unit recalled.")
    c = q.get(case.case_id)
    print(f"  resolved : status={c.status}")
    print(f"             note={c.resolution_note}")

    print(f"\n  contrast - an ordinary unresolved issue:")
    r2 = ag.handle(f"The unit will not power on.\nSerial number: {SERIAL_B}",
                   account_id=ACC_MARY)
    c2 = q.get(r2.case_id)
    print(f"    {c2.case_id} [{c2.priority}] {c2.reason}")
    print(f"    safety case was {case.priority}, this one is {c2.priority}")


def demo_3_reviewed_memory(ag, stores):
    head(3, "REVIEWED LONG-TERM MEMORY")
    print("Only human-approved resolutions become reusable, and even then\n"
          "they are re-checked against document control at recall time.")

    mem = stores["reviewed_memory"]
    device = erp_mod.ERP().device_by_serial(SERIAL_A)

    r = ag.handle(f"The unit is completely dead.\nSerial number: {SERIAL_A}",
                  account_id=ACC_IVAN)
    print(f"\n  agent proposed memory {r.memory_proposed_id}")
    print(f"    pending review : {len(mem.pending())}")
    print(f"    approved       : {len(mem.approved())}")

    recalled = mem.recall(device, "unit is completely dead", ag.compatibility,
                          ag.toolbox.approved_procedures)
    print(f"\n  recall BEFORE review -> {len(recalled)} result(s)")
    print("    unreviewed memory is invisible to the agent")

    mem.approve(r.memory_proposed_id, reviewer="qa.reviewer")
    recalled = mem.recall(device, "unit is completely dead", ag.compatibility,
                          ag.toolbox.approved_procedures)
    print(f"\n  recall AFTER review  -> {len(recalled)} result(s)")
    for rc in recalled:
        print(f"    {rc.resolution.memory_id} sim={rc.similarity:.3f} "
              f"usable={rc.usable}")
        print(f"      {rc.reason}")

    print(f"\n{THIN}\nMEMORY CANNOT OVERRIDE DOCUMENT CONTROL\n{THIN}")
    stale = mem.propose(device, "unit is completely dead and will not start",
                        procedure_id="PROC-P4800-PWR-A-R1",
                        outcome_note="fixed using the withdrawn procedure")
    mem.approve(stale.memory_id, reviewer="qa.reviewer")
    print(f"  approved a memory citing PROC-P4800-PWR-A-R1 (a SUPERSEDED doc)")
    recalled = mem.recall(device, "unit is completely dead and will not start",
                          ag.compatibility, ag.toolbox.approved_procedures)
    for rc in recalled:
        print(f"    {rc.resolution.procedure_id:<22} usable={rc.usable}")
        print(f"      {rc.reason}")
    print("  -> a human-approved memory still cannot resurrect a withdrawn "
          "procedure")


def demo_4_reflexion(ag, stores, monkey_text=None):
    head(4, "RESPONSE VERIFICATION AND REFLEXION CORRECTION")
    print("A draft containing anything not traceable to the approved source\n"
          "is rejected by code, fed back as specific feedback, and corrected.")

    real = generation.generate_grounded
    state = {"n": 0}

    def first_draft_fabricates(*a, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return generation.Generation(
                text=("Hello Ivan Nowak,\n\n"
                      "Please replace the 3.15A fuse in the rear carrier and "
                      "measure the mains voltage at the inlet, it should read "
                      "240V.\n\nThis follows PROC-P4800-PWR-A.\n\n"
                      "Kind regards,\nCustomer Support"),
                mode="llm", engine="local LLM (stubbed to fabricate)")
        return real(*a, **kw)

    generation.generate_grounded = first_draft_fabricates
    try:
        m = f"The unit is completely dead.\nSerial number: {SERIAL_A}"
        r = ag.handle(m, account_id=ACC_IVAN)
    finally:
        generation.generate_grounded = real

    print(f"\n  first draft (deliberately fabricated):")
    print("    'Please replace the 3.15A fuse in the rear carrier and measure")
    print("     the mains voltage at the inlet, it should read 240V.'")

    print(f"\n  verification result:")
    for note in r.reflexion:
        print(f"    attempt {note['attempt']}: {note['failure'][:150]}")
        print(f"    feedback given back to the writer:")
        for line in note["feedback"].splitlines():
            print(f"      {line[:120]}")

    show_trace(r)
    print(f"\n  final outcome : {r.outcome}")
    print(f"  grounded      : {(r.grounding or {}).get('grounded')}")
    print(f"  fabrication reached the customer? "
          f"{'YES - BUG' if '3.15A' in r.response else 'no'}")
    print("\n  final response:")
    for line in r.response.splitlines():
        print(f"    {line}")


def demo_5_tools(ag, stores):
    head(5, "TOOL SELECTION")
    print("The agent picks tools from what was asked, not by running all of "
          "them.")

    for label, msg in [
        ("logistics question",
         f"Can you tell me where my order is and whether it has shipped?\n"
         f"Serial number: {SERIAL_A}"),
        ("troubleshooting question",
         f"The unit will not power on at all.\nSerial number: {SERIAL_A}"),
    ]:
        r = ag.handle(msg, account_id=ACC_IVAN)
        print(f"\n  {label}")
        print(f"    intent      : "
              f"{', '.join(k for k, v in r.intent.items() if v)}")
        print(f"    tools used  : "
              f"{', '.join(t['tool'] for t in r.tool_calls)}")
        print(f"    QMS searched: {r.retrieval_query is not None}")
        print(f"    outcome     : {r.outcome}")


def demo_6_audit(ag, stores):
    head(6, "AUDIT TRAIL")
    a = audit_mod.Auditor(
        ticket_log=config.TICKET_LOG_PATH,
        escalations=stores["escalations"], approvals=stores["approvals"],
        reviewed_memory=stores["reviewed_memory"])

    print(a.render_summary())

    tickets = a.tickets()
    grounded = [t for t in tickets if t["outcome"] == "grounded"]
    if grounded:
        print(f"\n{THIN}\nFULL DOSSIER FOR ONE INTERACTION\n{THIN}")
        print(a.render_ticket(grounded[0]["ticket_id"]))


# =========================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", type=int, help="run one demonstration (1-6)")
    ap.add_argument("--llm", action="store_true",
                    help="use the local LLM for generation (slow on CPU)")
    ap.add_argument("--keep", action="store_true",
                    help="keep previous demo stores instead of resetting")
    args = ap.parse_args()

    scratch = os.path.join(config.DATA_DIR, "_demo_run")
    if os.path.exists(scratch) and not args.keep:
        shutil.rmtree(scratch)
    os.makedirs(scratch, exist_ok=True)
    config.TICKET_LOG_PATH = os.path.join(scratch, "tickets.jsonl")

    stores = {
        "escalations": cases.EscalationQueue(os.path.join(scratch, "esc.jsonl")),
        "approvals": cases.ApprovalQueue(os.path.join(scratch, "rma.jsonl")),
        "reviewed_memory": memory_mod.ReviewedMemory(
            os.path.join(scratch, "mem.jsonl")),
    }

    print(RULE)
    print("CAPSTONE - AGENT CAPABILITY DEMONSTRATIONS")
    print(RULE)
    print(f"generation : {'local LLM' if args.llm else 'deterministic writer'}")
    print(f"stores     : {os.path.relpath(scratch, config.PROJECT_ROOT)}")
    ag = agent_mod.Agent(prefer_llm=args.llm, verbose=True, **stores)
    print(f"index      : {len(ag.index.chunks)} chunks, "
          f"{ag.index.matrix.shape[1]}-dim")

    demos = {
        1: demo_1_conversation, 2: demo_2_safety, 3: demo_3_reviewed_memory,
        4: demo_4_reflexion, 5: demo_5_tools, 6: demo_6_audit,
    }
    chosen = [args.demo] if args.demo else sorted(demos)
    for n in chosen:
        demos[n](ag, stores)

    print(f"\n{RULE}\nDONE\n{RULE}")


if __name__ == "__main__":
    main()
