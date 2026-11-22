"""Simulated human review queue and return-authorisation approvals.

Both stores are local JSONL. Nothing here contacts a real ticketing system, a
real carrier or a real returns process - the point is to show the *boundary*
between what the agent finishes on its own and what waits for a person.

Two separate concerns live here:

**EscalationQueue** - cases the agent could not close. A case has a priority,
a status and an assignee, so the demonstration can show a safety complaint
arriving as P1-unassigned, being picked up, and being resolved.

**ApprovalQueue** - consequential actions. The agent may *request* a return
authorisation; it can never grant one. The request sits at `pending_approval`
until a named human approves or rejects it, and only an approved request
carries an RMA number. `is_authorised()` is the single place that answers
"may this action proceed", and it answers no unless a human said yes.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import config

# Case status
OPEN = "open"
ASSIGNED = "assigned"
RESOLVED = "resolved"

# Approval status
PENDING_APPROVAL = "pending_approval"
APPROVED = "approved"
REJECTED = "rejected"

# Why a case was raised. Safety is kept distinct from ordinary unresolved
# support because the two are handled differently and reported separately.
REASON_SAFETY = "safety_or_quality_complaint"
REASON_NO_PROCEDURE = "no_applicable_procedure"
REASON_UNVERIFIED = "response_failed_verification"
REASON_UNIDENTIFIED = "device_not_identified"
REASON_LOOP_EXHAUSTED = "reasoning_attempts_exhausted"

PRIORITY = {
    REASON_SAFETY: "P1",
    REASON_UNVERIFIED: "P2",
    REASON_NO_PROCEDURE: "P3",
    REASON_LOOP_EXHAUSTED: "P3",
    REASON_UNIDENTIFIED: "P4",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path) -> list:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _append(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _rewrite(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


# --- escalation ------------------------------------------------------------

@dataclass
class Case:
    case_id: str
    created_at: str
    reason: str
    priority: str
    status: str
    summary: str
    ticket_id: Optional[str] = None
    conversation_id: Optional[str] = None
    account_id: Optional[str] = None
    customer_name: Optional[str] = None
    serial_number: Optional[str] = None
    model: Optional[str] = None
    revision: Optional[str] = None
    assignee: Optional[str] = None
    assigned_at: Optional[str] = None
    resolution_note: Optional[str] = None
    resolved_at: Optional[str] = None
    notes: list = field(default_factory=list)

    def as_dict(self):
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


class EscalationQueue:
    def __init__(self, path: Optional[str] = None):
        self.path = path or config.ESCALATION_PATH

    def all_cases(self) -> list:
        return [Case.from_dict(r) for r in _read(self.path)]

    def get(self, case_id) -> Optional[Case]:
        for c in self.all_cases():
            if c.case_id == case_id:
                return c
        return None

    def open_cases(self) -> list:
        return [c for c in self.all_cases() if c.status != RESOLVED]

    def raise_case(self, reason: str, summary: str, identity=None,
                   ticket_id=None, conversation_id=None) -> Case:
        device = getattr(identity, "device", None)
        customer = getattr(identity, "customer", None)
        case = Case(
            case_id=f"CASE-{uuid.uuid4().hex[:8].upper()}",
            created_at=_now(),
            reason=reason,
            priority=PRIORITY.get(reason, "P3"),
            status=OPEN,
            summary=summary,
            ticket_id=ticket_id,
            conversation_id=conversation_id,
            account_id=(device.account_id if device else
                        getattr(customer, "account_id", None)),
            customer_name=(device.customer_name if device else
                           getattr(customer, "customer_name", None)),
            serial_number=device.serial_number if device else None,
            model=device.model if device else None,
            revision=device.revision if device else None,
        )
        _append(self.path, case.as_dict())
        return case

    def _update(self, case_id, **changes) -> Optional[Case]:
        rows = _read(self.path)
        hit = None
        for r in rows:
            if r.get("case_id") == case_id:
                r.update(changes)
                hit = Case.from_dict(r)
        if hit:
            _rewrite(self.path, rows)
        return hit

    def assign(self, case_id: str, assignee: str) -> Optional[Case]:
        return self._update(case_id, assignee=assignee, assigned_at=_now(),
                            status=ASSIGNED)

    def resolve(self, case_id: str, note: str,
                resolved_by: Optional[str] = None) -> Optional[Case]:
        case = self.get(case_id)
        if case is None:
            return None
        return self._update(
            case_id, status=RESOLVED, resolution_note=note,
            resolved_at=_now(),
            assignee=case.assignee or resolved_by,
        )

    def add_note(self, case_id: str, note: str) -> Optional[Case]:
        case = self.get(case_id)
        if case is None:
            return None
        notes = list(case.notes) + [{"at": _now(), "note": note}]
        return self._update(case_id, notes=notes)


# --- consequential-action approval ----------------------------------------

@dataclass
class ApprovalRequest:
    request_id: str
    created_at: str
    action: str
    status: str
    justification: str
    ticket_id: Optional[str] = None
    case_id: Optional[str] = None
    account_id: Optional[str] = None
    customer_name: Optional[str] = None
    serial_number: Optional[str] = None
    model: Optional[str] = None
    revision: Optional[str] = None
    procedure_id: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    decision_note: Optional[str] = None
    rma_number: Optional[str] = None

    @property
    def is_authorised(self) -> bool:
        """The only place that says an action may proceed."""
        return self.status == APPROVED

    def as_dict(self):
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


class ApprovalQueue:
    """Return authorisations. The agent requests; only a human approves."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or config.RMA_PATH

    def all_requests(self) -> list:
        return [ApprovalRequest.from_dict(r) for r in _read(self.path)]

    def get(self, request_id) -> Optional[ApprovalRequest]:
        for r in self.all_requests():
            if r.request_id == request_id:
                return r
        return None

    def pending(self) -> list:
        return [r for r in self.all_requests() if r.status == PENDING_APPROVAL]

    def request_return_authorisation(self, device, justification: str,
                                     procedure_id=None, ticket_id=None,
                                     case_id=None) -> ApprovalRequest:
        """Create a PENDING request. This never authorises anything."""
        req = ApprovalRequest(
            request_id=f"RMAREQ-{uuid.uuid4().hex[:8].upper()}",
            created_at=_now(),
            action="return_authorisation",
            status=PENDING_APPROVAL,
            justification=justification,
            ticket_id=ticket_id,
            case_id=case_id,
            account_id=device.account_id if device else None,
            customer_name=device.customer_name if device else None,
            serial_number=device.serial_number if device else None,
            model=device.model if device else None,
            revision=device.revision if device else None,
            procedure_id=procedure_id,
        )
        _append(self.path, req.as_dict())
        return req

    def _decide(self, request_id, status, reviewer, note, rma=None):
        rows = _read(self.path)
        hit = None
        for r in rows:
            if r.get("request_id") == request_id:
                if r.get("status") != PENDING_APPROVAL:
                    # Already decided; a second decision is not accepted.
                    return None
                r["status"] = status
                r["decided_by"] = reviewer
                r["decided_at"] = _now()
                r["decision_note"] = note
                if rma:
                    r["rma_number"] = rma
                hit = ApprovalRequest.from_dict(r)
        if hit:
            _rewrite(self.path, rows)
        return hit

    def approve(self, request_id: str, reviewer: str,
                note: str = "") -> Optional[ApprovalRequest]:
        rma = f"RMA-{datetime.now(timezone.utc).strftime('%Y')}-" \
              f"{uuid.uuid4().hex[:6].upper()}"
        return self._decide(request_id, APPROVED, reviewer, note, rma)

    def reject(self, request_id: str, reviewer: str,
               note: str = "") -> Optional[ApprovalRequest]:
        return self._decide(request_id, REJECTED, reviewer, note)
