"""Measurable evaluation of the agent across four dimensions.

Each case declares what *should* happen. The runner executes the agent and
scores four things independently, because they fail independently:

  device_identification - did it resolve the right unit, or correctly refuse
                          to guess?
  document_selection    - did it ground on the right procedure, or correctly
                          select none?
  troubleshooting       - was the final outcome the right one?
  escalation            - was a case raised, with the right reason?

Reported as counts and accuracy per dimension, so a regression shows up as a
number rather than as a wall of prose. Cases that exercise failure are as
important as the ones that succeed - an agent that answers everything scores
badly here, because abstaining is the correct answer to several cases.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from . import cases as cases_mod

DIMENSIONS = ("device_identification", "document_selection",
              "troubleshooting", "escalation")


@dataclass
class EvalCase:
    case_id: str
    description: str
    message: str
    account_id: Optional[str] = None
    # Expectations. None means "not scored on this dimension".
    expect_identified: Optional[bool] = None
    expect_serial: Optional[str] = None
    expect_procedure: Optional[str] = "__unset__"   # None = expect no procedure
    expect_outcome: Optional[str] = None
    expect_escalation_reason: Optional[str] = None
    expect_no_escalation: bool = False
    expect_grounded: Optional[bool] = None
    expect_qms_searched: Optional[bool] = None


@dataclass
class CaseScore:
    case_id: str
    description: str
    results: dict = field(default_factory=dict)   # dimension -> True/False/None
    notes: list = field(default_factory=list)
    actual: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(v for v in self.results.values() if v is not None)

    def as_dict(self):
        return {"case_id": self.case_id, "description": self.description,
                "passed": self.passed, "results": self.results,
                "notes": self.notes, "actual": self.actual}


def default_cases() -> list:
    """The evaluation set. Deliberately includes cases that must fail safely."""
    A, B = "98148469", "68177-DV5"          # P4800 Rev A / Rev B
    HOME, SM = "66383977", "38180-BM2"      # Home-NB Rev C / Series-M Rev C
    IVAN, MARY, PETER = "ACC-00010", "ACC-00022", "ACC-00026"

    return [
        EvalCase(
            "EV-01", "Valid power fault, approved procedure exists",
            f"The unit is completely dead, no lights and no fan noise.\n"
            f"Serial number: {A}", IVAN,
            expect_identified=True, expect_serial=A,
            expect_procedure="PROC-P4800-PWR-A", expect_outcome="grounded",
            expect_no_escalation=True, expect_grounded=True,
            expect_qms_searched=True,
        ),
        EvalCase(
            "EV-02", "Same fault, revision with no approved procedure",
            f"The unit is completely dead, no lights and no fan noise.\n"
            f"Serial number: {B}", MARY,
            expect_identified=True, expect_serial=B,
            expect_procedure=None, expect_outcome="abstained",
            expect_escalation_reason=cases_mod.REASON_NO_PROCEDURE,
            expect_qms_searched=True,
        ),
        EvalCase(
            "EV-03", "Model with no procedures at all",
            f"It is making a rattling noise and the session stops early.\n"
            f"Serial number: {HOME}", IVAN,
            expect_identified=True, expect_serial=HOME,
            expect_procedure=None, expect_outcome="abstained",
            expect_escalation_reason=cases_mod.REASON_NO_PROCEDURE,
        ),
        EvalCase(
            "EV-04", "Safety complaint escalates before retrieval",
            f"There was a burning smell and the casing was too hot to touch.\n"
            f"Serial number: {SM}", PETER,
            expect_identified=True, expect_serial=SM,
            expect_procedure=None, expect_outcome="escalated",
            expect_escalation_reason=cases_mod.REASON_SAFETY,
            expect_qms_searched=False,
        ),
        EvalCase(
            "EV-05", "Ambiguous identity, two devices on the account",
            "My unit has stopped working, please help.", MARY,
            expect_identified=False, expect_procedure=None,
            expect_outcome="abstained", expect_qms_searched=False,
        ),
        EvalCase(
            # Every account in the synthetic cast owns two devices, so there
            # is no single-device case to test. This checks the refusal is
            # consistent across accounts, not a quirk of one record.
            "EV-06", "Second ambiguous account also refuses to guess",
            "My unit will not switch on at all.", PETER,
            expect_identified=False, expect_procedure=None,
            expect_outcome="abstained", expect_qms_searched=False,
        ),
        EvalCase(
            "EV-07", "Logistics question answered without the QMS",
            f"Can you confirm the shipping status of my order?\n"
            f"Serial number: {A}", IVAN,
            expect_identified=True, expect_serial=A,
            expect_procedure=None, expect_outcome="answered",
            expect_no_escalation=True, expect_qms_searched=False,
        ),
        EvalCase(
            "EV-08", "Unknown serial is not accepted as a device",
            "My unit is dead.\nSerial number: 11111111", IVAN,
            expect_identified=False, expect_procedure=None,
            expect_outcome="abstained", expect_qms_searched=False,
        ),
        EvalCase(
            "EV-09", "Lamp fault selects the lamp procedure, not the power one",
            f"The output looks dim and flickers during a session, but it "
            f"powers up fine.\nSerial number: {A}", IVAN,
            expect_identified=True, expect_serial=A,
            expect_procedure="PROC-P4800-LAMP-A", expect_outcome="grounded",
            expect_grounded=True, expect_no_escalation=True,
        ),
        EvalCase(
            "EV-10", "Safety wording wins even inside a routine fault report",
            f"The lamp will not come on and I can smell burning.\n"
            f"Serial number: {A}", IVAN,
            expect_identified=True, expect_outcome="escalated",
            expect_procedure=None,
            expect_escalation_reason=cases_mod.REASON_SAFETY,
            expect_qms_searched=False,
        ),
        # --- coverage added after the operational layer was built --------
        EvalCase(
            "EV-11", "Thermal fault on a covered second model",
            f"The unit shuts down part way through a session and the case "
            f"feels very warm.\nSerial number: {SM}", PETER,
            expect_identified=True, expect_serial=SM,
            expect_procedure="PROC-SERIESM-THERM", expect_outcome="grounded",
            expect_grounded=True, expect_no_escalation=True,
        ),
        EvalCase(
            "EV-12", "Missing component resolves to the general procedure",
            f"The goggles that should have come with the device were "
            f"missing from the box.\nSerial number: {A}", IVAN,
            expect_identified=True,
            expect_procedure="PROC-GEN-MISSING-ACCESSORY",
            expect_outcome="grounded", expect_grounded=True,
        ),
        EvalCase(
            "EV-13", "Superseded document is never the answer",
            f"The unit is completely dead and I want the old fuse "
            f"instructions.\nSerial number: {A}", IVAN,
            expect_identified=True,
            expect_procedure="PROC-P4800-PWR-A", expect_outcome="grounded",
            expect_grounded=True,
        ),
        EvalCase(
            "EV-14", "Timer fault on the Rev C unit, not the Rev A one",
            f"The session timer keeps resetting itself part way through.\n"
            f"Serial number: 52385502", MARY,
            expect_identified=True, expect_serial="52385502",
            expect_procedure="PROC-P4800-TIMER-C", expect_outcome="grounded",
            expect_grounded=True,
        ),
        EvalCase(
            "EV-15", "Purchase request is not a fault and needs no procedure",
            f"I would like to buy a spare pair of goggles. How much are "
            f"they?\nSerial number: {A}", IVAN,
            expect_identified=True, expect_procedure=None,
            expect_outcome="answered", expect_no_escalation=True,
        ),
        EvalCase(
            "EV-16", "Frustration without a fault stays low priority",
            "This is absolutely unacceptable and I am furious. When is my "
            f"order arriving?\nSerial number: {A}", IVAN,
            expect_identified=True, expect_outcome="answered",
            expect_procedure=None, expect_qms_searched=False,
            expect_no_escalation=True,
        ),
    ]


def score_case(case: EvalCase, result) -> CaseScore:
    s = CaseScore(case.case_id, case.description)
    ident = result.identity
    searched = result.retrieval_query is not None

    s.actual = {
        "identified": ident.confirmed,
        "serial": ident.device.serial_number if ident.device else None,
        "procedure": result.selected_procedure,
        "outcome": result.outcome,
        "escalation_reason": result.escalation_reason,
        "grounded": (result.grounding or {}).get("grounded"),
        "qms_searched": searched,
        "unproductive_attempts": result.unproductive_attempts,
        # How the reply was produced, so model output is never counted as
        # though it were deterministic output or an escalation.
        "generation_mode": result.generation_mode,
        "was_corrected": len(result.reflexion) > 0,
    }

    # -- device identification
    if case.expect_identified is not None:
        ok = ident.confirmed == case.expect_identified
        if ok and case.expect_serial:
            ok = (ident.device is not None
                  and ident.device.serial_number == case.expect_serial)
        s.results["device_identification"] = ok
        if not ok:
            s.notes.append(
                f"identification: expected confirmed="
                f"{case.expect_identified} serial={case.expect_serial}, got "
                f"confirmed={ident.confirmed} "
                f"serial={s.actual['serial']}"
            )

    # -- document selection
    if case.expect_procedure != "__unset__":
        ok = result.selected_procedure == case.expect_procedure
        s.results["document_selection"] = ok
        if not ok:
            s.notes.append(
                f"document: expected {case.expect_procedure}, got "
                f"{result.selected_procedure}")
    if case.expect_qms_searched is not None:
        ok = searched == case.expect_qms_searched
        prev = s.results.get("document_selection")
        s.results["document_selection"] = ok if prev is None else (prev and ok)
        if not ok:
            s.notes.append(
                f"retrieval: expected qms_searched="
                f"{case.expect_qms_searched}, got {searched}")

    # -- troubleshooting outcome
    if case.expect_outcome is not None:
        ok = result.outcome == case.expect_outcome
        if case.expect_grounded is not None:
            ok = ok and ((result.grounding or {}).get("grounded")
                         == case.expect_grounded)
        s.results["troubleshooting"] = ok
        if not ok:
            s.notes.append(
                f"outcome: expected {case.expect_outcome}"
                f"/grounded={case.expect_grounded}, got {result.outcome}"
                f"/grounded={s.actual['grounded']}")

    # -- escalation behaviour
    if case.expect_escalation_reason is not None:
        ok = result.escalation_reason == case.expect_escalation_reason
        s.results["escalation"] = ok
        if not ok:
            s.notes.append(
                f"escalation: expected {case.expect_escalation_reason}, got "
                f"{result.escalation_reason}")
    elif case.expect_no_escalation:
        ok = result.case_id is None
        s.results["escalation"] = ok
        if not ok:
            s.notes.append(
                f"escalation: expected none, got {result.escalation_reason}")

    return s


def run(agent, eval_cases=None, store=None, flow=None) -> dict:
    """Score the retrieval agent, and the operational workflow if given one.

    `store` and `flow` are optional. When supplied, a second suite runs the
    full case pipeline - intake, triage, verification, checklist, movement
    and the approval gate - so the report covers the operation and not only
    the retrieval agent.
    """
    eval_cases = eval_cases or default_cases()
    scores = []
    for case in eval_cases:
        result = agent.handle(case.message, account_id=case.account_id,
                              scenario_id=case.case_id)
        scores.append(score_case(case, result))

    per_dim = {}
    for dim in DIMENSIONS:
        judged = [s for s in scores if s.results.get(dim) is not None]
        correct = [s for s in judged if s.results[dim]]
        per_dim[dim] = {
            "scored": len(judged),
            "correct": len(correct),
            "accuracy": (round(len(correct) / len(judged), 3)
                         if judged else None),
            "failed_cases": [s.case_id for s in judged if not s.results[dim]],
        }

    # How the replies were actually produced. Reported separately so a
    # deterministic fallback or an escalation is never counted as a
    # model-generated answer.
    mix = {"model_generated": 0, "model_corrected": 0,
           "deterministic_template": 0, "deterministic_abstention": 0,
           "escalated": 0}
    for s in scores:
        a = s.actual
        if a.get("outcome") == "escalated":
            mix["escalated"] += 1
        elif a.get("generation_mode") == "llm":
            mix["model_corrected" if a.get("was_corrected")
                else "model_generated"] += 1
        elif a.get("generation_mode") == "deterministic-abstention":
            mix["deterministic_abstention"] += 1
        else:
            mix["deterministic_template"] += 1

    report = {
        "cases": len(scores),
        "cases_passed": sum(1 for s in scores if s.passed),
        "per_dimension": per_dim,
        "response_mix": mix,
        "details": [s.as_dict() for s in scores],
    }
    if store is not None and flow is not None:
        report["operational"] = run_operational(store, flow)
    return report


# --- operational workflow suite -------------------------------------------

OPERATIONAL_CASES = [
    {
        "id": "OP-01",
        "description": "Two-point verification succeeds from the email",
        "subject": "Unit dead",
        "body": ("My name is Ivan Nowak and the serial is 98148469. The "
                 "unit is completely dead, no lights and no fan."),
        "account_id": "ACC-00010",
        "expect": {"verified": True, "movement": "none",
                   "status": "AI Reviewed"},
    },
    {
        "id": "OP-02",
        "description": "One point only: the agent asks, it does not guess",
        "subject": "Unit dead",
        "body": "The unit is completely dead. Serial number: 98148469",
        "account_id": "ACC-00010",
        "expect": {"verified": False, "status": "Human Review Needed"},
    },
    {
        "id": "OP-03",
        "description": "Missing component proposes a part, not a return",
        "subject": "Goggles missing",
        "body": ("My name is Ivan Nowak and the serial is 98148469. The "
                 "goggles that should have come with the device were "
                 "missing from the box."),
        "account_id": "ACC-00010",
        "expect": {"verified": True, "movement": "part_outbound",
                   "rma_status": "proposed"},
    },
    {
        "id": "OP-04",
        "description": "Safety stops troubleshooting and notifies QA",
        "subject": "Burn",
        "body": ("My name is Peter Thomas and the serial is 38180-BM2. I "
                 "have a burn on my arm after a session."),
        "account_id": "ACC-00026",
        "expect": {"priority": "Critical", "qa_notification": True,
                   "retrieved_sources": 0,
                   "status": "Human Review Needed"},
    },
    {
        "id": "OP-05",
        "description": "Unverified revision blocks revision-specific steps",
        "subject": "Unit dead",
        "body": ("My name is Mary Cohen and the serial is 68177-DV5. The "
                 "unit is completely dead."),
        "account_id": "ACC-00022",
        "expect": {"verified": True, "status": "Human Review Needed",
                   "revision_blocked": True},
    },
]


def run_operational(store, flow) -> dict:
    """Drive whole cases through the pipeline and score the outcome."""
    from . import erp_sim

    results, passed = [], 0
    for spec in OPERATIONAL_CASES:
        case_id = flow.intake(spec["subject"], spec["body"],
                              spec["account_id"], run_id="EVAL")
        flow.pre_investigate(case_id, run_id="EVAL")
        case = store.case(case_id)
        checklist = store.checklist(case_id)
        rma = store.one("SELECT * FROM rmas WHERE case_id=?", (case_id,))
        notes = store.query(
            "SELECT * FROM qa_notifications WHERE case_id=?", (case_id,))
        srcs = store.one(
            "SELECT COUNT(*) c FROM retrieved_sources WHERE case_id=?",
            (case_id,))["c"]
        rev_item = next((i for i in checklist
                         if i["action"] == "assess_device_revision"), None)

        actual = {
            "verified": bool(case["identity_verified"]),
            "status": case["status"],
            "priority": case["priority"],
            "movement": rma["movement_type"] if rma else "none",
            "rma_status": rma["status"] if rma else None,
            "qa_notification": bool(notes),
            "retrieved_sources": srcs,
            "revision_blocked": bool(
                rev_item and rev_item["status"] == "requires_approval"),
            "checklist_items": len(checklist),
        }
        problems = [f"{k}: expected {v!r}, got {actual.get(k)!r}"
                    for k, v in spec["expect"].items()
                    if actual.get(k) != v]
        ok = not problems
        passed += ok
        results.append({"case_id": spec["id"],
                        "description": spec["description"],
                        "passed": ok, "notes": problems, "actual": actual})

    # The approval gate, exercised end to end on a real proposal.
    gate = {"proposal_authorises_nothing": None,
            "approval_creates_transaction": None,
            "rejection_creates_nothing": None}
    erp = erp_sim.ErpSimulator(store)
    proposed = store.query(
        "SELECT * FROM rmas WHERE run_id='EVAL' AND status='proposed'")
    if proposed:
        r = proposed[0]
        gate["proposal_authorises_nothing"] = (
            store.one("SELECT COUNT(*) c FROM sim_shipments WHERE rma_id=?",
                      (r["rma_id"],))["c"] == 0)
        res = erp.approve(r["rma_id"], "eval.reviewer", "evaluation",
                          run_id="EVAL")
        gate["approval_creates_transaction"] = bool(
            res.ok and res.shipment and res.shipment["tracking_number"])
    if len(proposed) > 1:
        r2 = proposed[1]
        erp.reject(r2["rma_id"], "eval.reviewer", "evaluation",
                   run_id="EVAL")
        gate["rejection_creates_nothing"] = (
            store.one("SELECT COUNT(*) c FROM sim_shipments WHERE rma_id=?",
                      (r2["rma_id"],))["c"] == 0)

    return {
        "cases": len(results),
        "cases_passed": passed,
        "accuracy": round(passed / len(results), 3) if results else None,
        "approval_gate": gate,
        "details": results,
        "note": ("these cases are written with run_id='EVAL' so they never "
                 "appear in live business counts"),
    }


def render(report: dict) -> str:
    out = ["EVALUATION", "=" * 74,
           f"cases: {report['cases_passed']}/{report['cases']} fully correct",
           "",
           f"{'dimension':<24}{'correct':>9}{'scored':>8}{'accuracy':>10}  failures",
           "-" * 74]
    for dim, d in report["per_dimension"].items():
        acc = "-" if d["accuracy"] is None else f"{d['accuracy']:.0%}"
        out.append(f"{dim:<24}{d['correct']:>9}{d['scored']:>8}{acc:>10}  "
                   f"{', '.join(d['failed_cases']) or '-'}")
    out.append("")
    out.append("how the replies were produced (denominator = "
               f"{report['cases']} cases)")
    for k, v in report["response_mix"].items():
        out.append(f"  {k:<28} {v}")
    out.append("")
    out.append(f"{'case':<8}{'result':<8}description")
    out.append("-" * 74)
    for d in report["details"]:
        out.append(f"{d['case_id']:<8}{'PASS' if d['passed'] else 'FAIL':<8}"
                   f"{d['description']}")
        for n in d["notes"]:
            out.append(f"          ! {n}")

    op = report.get("operational")
    if op:
        out += ["", "OPERATIONAL WORKFLOW", "=" * 74,
                f"cases: {op['cases_passed']}/{op['cases']} correct"
                f"  ({op['accuracy']:.0%})" if op["accuracy"] is not None
                else "", op["note"], ""]
        for d in op["details"]:
            out.append(f"{d['case_id']:<8}"
                       f"{'PASS' if d['passed'] else 'FAIL':<8}"
                       f"{d['description']}")
            for n in d["notes"]:
                out.append(f"          ! {n}")
        out.append("")
        out.append("human approval gate")
        for k, v in op["approval_gate"].items():
            out.append(f"  {k:<36} {v}")
    return "\n".join(out)


def to_json(report: dict) -> str:
    return json.dumps(report, indent=2, default=str)
