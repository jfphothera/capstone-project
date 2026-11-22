"""Turning the 16-step investigative trace into something a person reads.

The trace itself is unchanged and still complete - this module does not
remove or merge any step. What it adds is the three things the trace on its
own does not say:

**What kind of step is this.** Six classes, after the workflow definition:

    ``A`` automatic investigation        the system did it, nobody approves it
    ``B`` informational finding          worth reading, no decision attached
    ``C`` human verification             a person has to attest something
    ``D`` human approval                 a person authorises a consequence
    ``E`` authorised execution           the consequence being carried out
    ``F`` follow-up, learning, closure   what happens afterwards

Only ``C`` and ``D`` are decisions. Offering Approve and Reject on all
sixteen steps - eleven of which are lookups - is what made it unclear which
approvals mattered (defect C7), so `offers_decision` is false for the rest.

**What the outcome actually was.** A failed lookup, a deliberate skip, a
valid rejection and an incomplete task are four different things and must not
all render as a green tick. `outcome` is one of a fixed vocabulary.

**Which of the three panels it belongs in.** "Investigation complete",
"Needs your attention", "Next steps".

Nothing here makes a decision or changes a record. It is a read model: give
it the stored rows and it returns labels.
"""

from __future__ import annotations

import json
from typing import Optional

from . import caseflow

# --- the six classes -------------------------------------------------------
AUTOMATIC = "A"          # automatic AI/system investigation
INFORMATIONAL = "B"      # informational finding or warning
HUMAN_VERIFY = "C"       # human verification or judgement
HUMAN_APPROVE = "D"      # human approval of a consequential action
EXECUTION = "E"          # authorised system execution
FOLLOW_UP = "F"          # follow-up, learning and closure

CLASS_LABEL = {
    AUTOMATIC: "Automatic investigation",
    INFORMATIONAL: "Finding",
    HUMAN_VERIFY: "Needs your verification",
    HUMAN_APPROVE: "Needs your approval",
    EXECUTION: "Authorised execution",
    FOLLOW_UP: "Follow-up",
}

# --- the three panels ------------------------------------------------------
PANEL_FINDINGS = "investigation_complete"
PANEL_ATTENTION = "needs_your_attention"
PANEL_NEXT = "next_steps"

# --- outcome vocabulary ----------------------------------------------------
COMPLETED = "Completed"
NO_CONCERN = "No concern found"
MATCH_FOUND = "Match found"
NO_MATCH = "No match"
INFO_MISSING = "Information missing"
UNCERTAIN = "Uncertain"
BLOCKED = "Blocked"
HUMAN_REQUIRED = "Human review required"
APPROVED = "Approved"
REJECTED = "Rejected"
EXECUTED = "Executed"
FAILED = "Failed"
NOT_APPLICABLE = "Not applicable"

# Outcomes that must not be drawn as a success.
NEGATIVE_OUTCOMES = frozenset(
    {NO_MATCH, INFO_MISSING, BLOCKED, HUMAN_REQUIRED, REJECTED, FAILED})
NEUTRAL_OUTCOMES = frozenset({NOT_APPLICABLE, UNCERTAIN})


# action -> (plain label, class, panel when nothing is outstanding)
_ACTIONS = {
    "read_message": ("Message reviewed", AUTOMATIC, PANEL_FINDINGS),
    "screen_adverse_event": ("Safety screening", INFORMATIONAL,
                             PANEL_FINDINGS),
    "classify_complaint": ("Complaint assessment", INFORMATIONAL,
                           PANEL_FINDINGS),
    "assess_priority": ("Priority assessment", INFORMATIONAL, PANEL_FINDINGS),
    "identify_customer": ("Customer information found", AUTOMATIC,
                          PANEL_FINDINGS),
    "verify_identity": ("Identity verification", HUMAN_VERIFY,
                        PANEL_FINDINGS),
    "check_erp_records": ("ERP customer match", AUTOMATIC, PANEL_FINDINGS),
    "assess_device_revision": ("Device and revision verification",
                               HUMAN_VERIFY, PANEL_FINDINGS),
    "review_case_history": ("Previous interactions", AUTOMATIC,
                            PANEL_FINDINGS),
    "search_qms": ("Applicable troubleshooting procedure", AUTOMATIC,
                   PANEL_FINDINGS),
    "validate_procedure_applicability": ("Document control check", AUTOMATIC,
                                         PANEL_FINDINGS),
    "evaluate_complaint": ("Complaint evaluation", INFORMATIONAL,
                           PANEL_FINDINGS),
    "evaluate_quality_signals": ("Quality observations", INFORMATIONAL,
                                 PANEL_FINDINGS),
    "determine_product_movement": ("Recommended resolution", HUMAN_APPROVE,
                                   PANEL_NEXT),
    "prepare_response": ("Customer response", HUMAN_APPROVE, PANEL_NEXT),
    "identify_missing_information": ("Information still needed",
                                     INFORMATIONAL, PANEL_FINDINGS),
}

# What approving or rejecting this step changes. Shown before the decision is
# taken, so the consequence is never a surprise. Steps absent from this map
# take no decision at all.
_CONSEQUENCES = {
    "verify_identity": {
        "approve": "Records that you have confirmed this customer's identity "
                   "yourself. Device-specific guidance becomes permitted.",
        "reject": "Leaves the identity unverified. No device-specific "
                  "guidance may be issued and the case stays with you.",
    },
    "assess_device_revision": {
        "approve": "Confirms which hardware revision this unit is, re-runs "
                   "the document search and the applicability check, and may "
                   "unlock a revision-specific procedure. It does not "
                   "authorise a procedure by itself.",
        "reject": "Records that the proposed revision is wrong. No "
                  "revision-specific instructions may be issued.",
    },
    "determine_product_movement": {
        "approve": "Agrees with the recommendation. The movement itself is "
                   "still authorised separately on the movement panel - "
                   "nothing ships from here.",
        "reject": "Cancels the proposed RMA. It can no longer be approved or "
                  "executed by anyone.",
    },
    "prepare_response": {
        "approve": "Marks the draft as reviewed. Sending it is a separate "
                   "action on the reply composer.",
        "reject": "Rejects the draft. Nothing is sent and the original is "
                  "kept in the record.",
    },
}


def _loads(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def outcome_for(item: dict) -> str:
    """Translate a stored checklist row into one outcome word.

    A human decision, where one has been taken, outranks the automatic
    status: a representative who rejected a finding should see "Rejected",
    not the "requires approval" the system wrote before they got there.
    """
    decision = item.get("approval_decision")
    status = item.get("status")
    action = item.get("action")
    result = _loads(item.get("tool_result")) or {}

    if action == 'screen_adverse_event' and decision == 'determined':
        return COMPLETED

    if decision == "rejected":
        return REJECTED
    if decision == "approved":
        return APPROVED
    if decision == "confirmed":
        return APPROVED

    if status == caseflow.NA:
        return NOT_APPLICABLE
    if status == caseflow.PENDING:
        return BLOCKED
    if status == caseflow.NEEDS_APPROVAL:
        return HUMAN_REQUIRED
    if status == caseflow.ITEM_FAILED:
        # A lookup that found nothing is "No match"; a lookup that could not
        # run is "Failed". The distinction matters to whoever reads it.
        if action in ("identify_customer", "check_erp_records", "search_qms"):
            return NO_MATCH
        return FAILED

    # status == completed
    if action == "screen_adverse_event":
        if isinstance(result, dict):
            if result.get("requires_human_determination"):
                return UNCERTAIN
            if result.get("is_adverse_event"):
                return HUMAN_REQUIRED
        return NO_CONCERN
    if action == "classify_complaint" and isinstance(result, dict):
        return COMPLETED if result.get("is_complaint") else NO_CONCERN
    if action in ("identify_customer", "check_erp_records", "verify_identity"):
        return MATCH_FOUND
    if action == "identify_missing_information" and isinstance(result, dict):
        return (INFO_MISSING if result.get("items_with_gaps") else COMPLETED)
    if item.get("missing_info"):
        return INFO_MISSING
    return COMPLETED


def describe_item(item: dict) -> dict:
    """One checklist row as the representative-facing view of it."""
    action = item.get("action") or ""
    label, cls, panel = _ACTIONS.get(
        action, (action.replace("_", " ").capitalize(), AUTOMATIC,
                 PANEL_FINDINGS))
    outcome = outcome_for(item)
    decided = bool(item.get("approval_decision"))
    offers = (cls in (HUMAN_VERIFY, HUMAN_APPROVE) and not decided
              and outcome not in (NOT_APPLICABLE,))
    # A verification step that already matched needs no attestation.
    if cls == HUMAN_VERIFY and outcome in (MATCH_FOUND, COMPLETED):
        offers = False
    if action == 'prepare_response':
        offers = False  # the actual draft is approved/rejected in the composer
    if action == 'determine_product_movement' and (_loads(item.get('tool_result')) or {}).get('movement_type') == 'none':
        offers = False

    if offers or outcome in (BLOCKED, HUMAN_REQUIRED):
        panel = PANEL_ATTENTION
    elif outcome in (INFO_MISSING, UNCERTAIN) and cls != AUTOMATIC:
        panel = PANEL_ATTENTION

    return {
        "item_id": item.get("item_id"),
        "seq": item.get("seq"),
        "action": action,
        "label": label,
        "technical_name": action,
        "description": item.get("description"),
        "decision_class": cls,
        "decision_class_label": CLASS_LABEL[cls],
        "panel": panel,
        "outcome": outcome,
        "tone": ("negative" if outcome in NEGATIVE_OUTCOMES
                 else "neutral" if outcome in NEUTRAL_OUTCOMES
                 else "positive"),
        "offers_decision": offers,
        "consequences": _CONSEQUENCES.get(action) if offers else None,
        "status": item.get("status"),
        "actor": item.get("actor"),
        "at": item.get("executed_at"),
        "missing_info": item.get("missing_info"),
        "approval_decision": item.get("approval_decision"),
        "authorizer": item.get("authorizer"),
        "decided_at": item.get("decided_at"),
        "feedback": item.get("feedback"),
        "consequence": item.get("consequence"),
        "evidence": _loads(item.get("evidence")),
        "tool_result": _loads(item.get("tool_result")),
    }


def describe_checklist(items) -> dict:
    """The three panels, plus the full trace for technical review."""
    described = [describe_item(i) for i in items]
    latest = {d['action']:d for d in described}
    current = sorted(latest.values(), key=lambda d:d['seq'])
    for d in described:
        if latest[d['action']] is not d:
            d['offers_decision'] = False
    panels = {PANEL_FINDINGS: [], PANEL_ATTENTION: [], PANEL_NEXT: []}
    for d in current:
        panels[d["panel"]].append(d)
    return {
        "panels": panels,
        "all": described,
        'current':current,
        "open_decisions": sum(1 for d in current if d["offers_decision"]),
        "unresolved": sum(1 for d in current
                          if d["outcome"] in (BLOCKED, HUMAN_REQUIRED,
                                              INFO_MISSING)
                          and not d["approval_decision"]),
    }


def primary_decision(described: dict, case: dict, rmas=(),
                     draft: Optional[dict] = None) -> Optional[dict]:
    """The single most important thing still waiting on a person.

    Ordered by consequence, not by position in the checklist: a safety
    determination outranks an RMA, an RMA outranks a draft reply, and a draft
    reply outranks an informational query.
    """
    if case.get("is_adverse_event") or case.get("priority") == "Critical":
        for d in described.get('current',described['all']):
            if (d["action"] == "screen_adverse_event"
                    and d["outcome"] in (UNCERTAIN, HUMAN_REQUIRED)):
                return {"kind": "adverse_event_determination", "item": d}
    if not case.get('identity_verified'):
        for d in described['panels'][PANEL_ATTENTION]:
            if d['action']=='verify_identity' and d['offers_decision']:
                return {'kind':'verification','item':d}
    for r in rmas:
        if r.get("status") == "proposed":
            return {"kind": "movement_approval", "rma": r}
    for d in described["panels"][PANEL_ATTENTION]:
        if d["decision_class"] == HUMAN_VERIFY and d["offers_decision"]:
            return {"kind": "verification", "item": d}
    if draft and draft.get("status") == "draft":
        return {"kind": "reply_approval", "message": draft}
    for d in described["panels"][PANEL_ATTENTION]:
        if d["offers_decision"]:
            return {"kind": "finding_decision", "item": d}
    return None
