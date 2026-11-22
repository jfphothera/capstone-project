"""Read-only views over everything the agent recorded.

The ticket log, the escalation queue, the approval queue and the reviewed
memory are four append-only JSONL files. This module joins them so a reviewer
can answer the questions a final demonstration needs to answer:

  - what has this customer asked us, and what did we say?
  - which tools did the agent choose, and why?
  - what did retrieval return, and what was rejected, and on what grounds?
  - did the response pass validation and source verification?
  - was anything corrected after a failed draft?
  - who escalated what, to whom, and what happened to it?
  - what consequential actions were requested, and who approved them?

Nothing here writes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import cases, config, memory as memory_mod


def _read_jsonl(path) -> list:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


@dataclass
class AuditView:
    tickets: list = field(default_factory=list)
    cases: list = field(default_factory=list)
    approvals: list = field(default_factory=list)
    memories: list = field(default_factory=list)


class Auditor:
    def __init__(self, ticket_log=None, escalations=None, approvals=None,
                 reviewed_memory=None):
        self.ticket_log = ticket_log or config.TICKET_LOG_PATH
        self.escalations = escalations or cases.EscalationQueue()
        self.approvals = approvals or cases.ApprovalQueue()
        self.memory = reviewed_memory or memory_mod.ReviewedMemory()

    # -- raw ------------------------------------------------------------

    def tickets(self) -> list:
        return _read_jsonl(self.ticket_log)

    def load(self) -> AuditView:
        return AuditView(
            tickets=self.tickets(),
            cases=[c.as_dict() for c in self.escalations.all_cases()],
            approvals=[a.as_dict() for a in self.approvals.all_requests()],
            memories=[m.as_dict() for m in self.memory._all()],
        )

    # -- queries ---------------------------------------------------------

    def customer_history(self, account_id=None, serial_number=None,
                         conversation_id=None) -> list:
        """Every interaction for a customer, device or conversation."""
        rows = self.tickets()
        if account_id:
            rows = [r for r in rows if r.get("account_id") == account_id]
        if serial_number:
            rows = [r for r in rows
                    if r.get("serial_number") == serial_number]
        if conversation_id:
            rows = [r for r in rows
                    if r.get("conversation_id") == conversation_id]
        return rows

    def ticket(self, ticket_id) -> dict:
        for r in self.tickets():
            if r.get("ticket_id") == ticket_id:
                return r
        return {}

    def case_for_ticket(self, ticket_id) -> dict:
        for c in self.escalations.all_cases():
            if c.ticket_id == ticket_id:
                return c.as_dict()
        return {}

    def summary(self) -> dict:
        """Counts a reviewer can sanity-check the demonstration against."""
        tickets = self.tickets()
        all_cases = self.escalations.all_cases()
        approvals = self.approvals.all_requests()
        mems = self.memory._all()

        def count(rows, key):
            out = {}
            for r in rows:
                out[r.get(key)] = out.get(r.get(key), 0) + 1
            return out

        return {
            "tickets": len(tickets),
            "by_outcome": count(tickets, "outcome"),
            "by_validation": count(tickets, "validation"),
            "grounding_verified": sum(
                1 for t in tickets if t.get("grounding_verified") is True),
            "grounding_failed": sum(
                1 for t in tickets if t.get("grounding_verified") is False),
            "reflexion_corrections": sum(
                t.get("reflexion_attempts") or 0 for t in tickets),
            "cases": len(all_cases),
            "cases_by_reason": {
                r: sum(1 for c in all_cases if c.reason == r)
                for r in {c.reason for c in all_cases}
            },
            "cases_by_status": {
                s: sum(1 for c in all_cases if c.status == s)
                for s in {c.status for c in all_cases}
            },
            "approvals": len(approvals),
            "approvals_by_status": {
                s: sum(1 for a in approvals if a.status == s)
                for s in {a.status for a in approvals}
            },
            "memories": len(mems),
            "memories_by_review": {
                s: sum(1 for m in mems if m.review_status == s)
                for s in {m.review_status for m in mems}
            },
        }

    # -- rendering -------------------------------------------------------

    def render_ticket(self, ticket_id: str) -> str:
        """A readable dossier for one interaction."""
        t = self.ticket(ticket_id)
        if not t:
            return f"no ticket {ticket_id}"
        out = [f"TICKET {t['ticket_id']}   recorded {t.get('recorded_at')}",
               "=" * 74]
        out.append(f"conversation : {t.get('conversation_id')}")
        out.append(f"customer     : {t.get('customer')} ({t.get('account_id')})")
        out.append(f"device       : {t.get('model')} Rev {t.get('revision')} "
                   f"serial {t.get('serial_number')}")
        out.append(f"intent       : {t.get('intent')}")
        out.append(f"tools used   : {', '.join(t.get('tool_calls') or []) or '-'}")
        out.append(f"outcome      : {t.get('outcome')}")
        out.append(f"procedure    : {t.get('selected_procedure') or '-'}")
        out.append(f"accepted     : {t.get('accepted_chunk_ids') or '-'}")
        out.append(f"rejected     : {len(t.get('rejected_chunk_ids') or [])} "
                   f"chunk(s)")
        out.append(f"validation   : {t.get('validation')} - "
                   f"{t.get('validation_reason')}")
        out.append(f"grounding    : {t.get('grounding_verified')} - "
                   f"{t.get('grounding_reason')}")
        out.append(f"corrections  : {t.get('reflexion_attempts')} reflexion "
                   f"attempt(s), {t.get('unproductive_attempts')} unproductive")

        case = self.case_for_ticket(ticket_id)
        if t.get("case_id") or case:
            cid = t.get("case_id") or case.get("case_id")
            c = self.escalations.get(cid)
            if c:
                out.append(f"escalation   : {c.case_id} [{c.priority}] "
                           f"{c.reason} status={c.status} "
                           f"assignee={c.assignee or '-'}")
                if c.resolution_note:
                    out.append(f"  resolved   : {c.resolution_note}")

        if t.get("rma_request_id"):
            r = self.approvals.get(t["rma_request_id"])
            if r:
                out.append(f"approval     : {r.request_id} {r.status} "
                           f"authorised={r.is_authorised} "
                           f"rma={r.rma_number or '-'} "
                           f"decided_by={r.decided_by or '-'}")
        out.append("-" * 74)
        out.append("response:")
        for line in (t.get("response") or "").splitlines():
            out.append("  " + line)
        return "\n".join(out)

    def render_summary(self) -> str:
        s = self.summary()
        out = ["AUDIT SUMMARY", "=" * 74]
        for k, v in s.items():
            out.append(f"  {k:<24} {v}")
        return "\n".join(out)
