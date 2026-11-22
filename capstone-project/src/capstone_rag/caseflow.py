"""Case intake and AI pre-investigation.

Two clearly separated phases, because they have different latency budgets.

**intake()** runs on arrival and touches no language model. It triages the
message, creates the case, flags safety, sets priority and persists
everything. It takes milliseconds, so a Critical case is visible in the
inbox immediately even when the inference queue is backed up.

**pre_investigate()** is the slow phase and runs on the single inference
worker. It verifies identity, consults the ERP, searches the QMS, validates
applicability, decides whether product needs to move, drafts a reply and
writes the checklist the representative reviews.

Nothing here invents a finding. When a lookup fails the checklist item says
`failed` and records what was missing; it does not fill the gap with a guess.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from . import (
    config,
    erp_sim,
    identity as identity_mod,
    movement as movement_mod,
    store as store_mod,
    triage as triage_mod,
)

# Case states
NEW = "New"
AI_REVIEWED = "AI Reviewed"
HUMAN_REVIEW = "Human Review Needed"
ACTION_APPROVED = "Action Approved"
AWAITING = "Awaiting Customer/Shipment"
RESOLVED = "Resolved"
CLOSED = "Closed"

# AI processing states
QUEUED, PROCESSING, COMPLETED, FAILED, RETRIABLE = (
    "queued", "processing", "completed", "failed", "retriable")

# Checklist item statuses
DONE, ITEM_FAILED, PENDING, NEEDS_APPROVAL, NA = (
    "completed", "failed", "pending", "requires_approval", "not_applicable")

AI, REP = "ai", "representative"

# The order a case normally moves through. Used only to describe a
# transition in the audit trail, never to forbid one.
STATUSES = (NEW, AI_REVIEWED, HUMAN_REVIEW, ACTION_APPROVED, AWAITING,
            RESOLVED, CLOSED)


def set_status(store, case_id: str, status: str, actor: str, why: str,
               run_id=None) -> dict:
    """Move a case to a new status and record the move.

    Every status change goes through here so that "what did my approval
    actually do?" has an answer in the audit trail: the old status, the new
    one, who caused it and why. A no-op is still reported, so a caller can
    tell the difference between "already there" and "changed".
    """
    case = store.case(case_id)
    if case is None:
        return {"ok": False, "reason": f"no case {case_id}"}
    before = case["status"]
    if before == status:
        return {"ok": True, "changed": False, "status": status}
    store.update("cases", "case_id", case_id,
                 {"status": status, "updated_at": store_mod.now()})
    store.audit("case_status_changed", actor=actor, case_id=case_id,
                summary=f"{before} -> {status}: {why}",
                detail={"from": before, "to": status, "reason": why},
                run_id=run_id or case.get("run_id"))
    return {"ok": True, "changed": True, "from": before, "status": status}


@dataclass
class ChecklistBuilder:
    store: object
    case_id: str
    run_id: Optional[str] = None
    _seq: int = 0
    items: list = field(default_factory=list)

    def __post_init__(self):
        # A second investigation pass - after a representative confirms a
        # device revision, say - appends to the trace rather than replacing
        # it, so the earlier finding and the later one are both auditable.
        if self._seq == 0:
            row = self.store.one(
                "SELECT MAX(seq) m FROM checklist_items WHERE case_id=?",
                (self.case_id,))
            self._seq = (row or {}).get("m") or 0

    def add(self, action, description, status, actor=AI, tool_result=None,
            evidence=None, missing_info=None):
        self._seq += 1
        item_id = f"{self.case_id}-CL{self._seq:02d}"
        row = {
            "item_id": item_id, "case_id": self.case_id, "run_id": self.run_id,
            "seq": self._seq, "action": action, "description": description,
            "actor": actor, "status": status,
            "tool_result": json.dumps(tool_result, default=str)
            if tool_result is not None else None,
            "evidence": json.dumps(evidence, default=str)
            if evidence is not None else None,
            "missing_info": missing_info,
            "approval_decision": None, "authorizer": None, "decided_at": None,
            "feedback": None, "executed_at": store_mod.now(),
        }
        self.store.insert("checklist_items", row)
        from . import activity
        activity.emit(action, description, {NEEDS_APPROVAL: 'awaiting_human_review',
                      PENDING: 'pending', ITEM_FAILED: 'failed', NA: 'not_applicable'}.get(status, 'completed'),
                      {'status': status, 'tool_result': tool_result, 'evidence': evidence,
                       'missing_info': missing_info})
        self.items.append(row)
        return row


def hand_off_failure(store, case_id, reason, error=None):
    """Make an incomplete investigation a visible, actionable human handoff.

    Sets Human Review Needed and records the reason and error. Nothing else
    changes: no message is sent, no movement is approved or executed, pending
    drafts and proposals stay pending, so a retry cannot duplicate anything.
    """
    try:
        if store.case(case_id) is None:
            return False
        store.update('cases', 'case_id', case_id, {'status': HUMAN_REVIEW, 'ai_state': COMPLETED, 'updated_at': store_mod.now()})
        store.audit('automatic_human_handoff', actor='ai', case_id=case_id,
                    summary=f'{reason}; routed to a person',
                    detail={'reason': reason, 'error': str(error)[:500] if error else None,
                            'error_class': type(error).__name__ if error else None,
                            'consequential_actions_taken': False})
        return True
    except Exception:
        return False


def make_agent(prefer_llm: bool = True, verbose: bool = False, store=None):
    """An Agent whose own JSONL bookkeeping is kept out of the way.

    The standalone Agent keeps its own escalation and approval queues, which
    predate the SQLite store and are still used by the Checkpoint 3.1
    demonstrations. Inside the operational system the SQLite `cases`,
    `erp_calls` and `rmas` tables are the single source of truth, so the
    Agent's internal files are pointed at a scratch directory to avoid two
    competing records of the same escalation.
    """
    from . import agent as agent_mod, cases as cases_mod, memory as memory_mod

    # Tie legacy agent files to the selected DB so --db really isolates a demo.
    scratch = os.path.join(os.path.dirname(os.path.abspath(store.path)),
                           os.path.basename(store.path) + '.agent') if store is not None else os.path.join(config.DATA_DIR, '_agent_internal')
    os.makedirs(scratch, exist_ok=True)
    return agent_mod.Agent(
        prefer_llm=prefer_llm, verbose=verbose,
        escalations=cases_mod.EscalationQueue(
            os.path.join(scratch, "escalations.jsonl")),
        approvals=cases_mod.ApprovalQueue(
            os.path.join(scratch, "rma_requests.jsonl")),
        reviewed_memory=memory_mod.ReviewedMemory(
            os.path.join(scratch, "resolutions.jsonl")),
        ticket_log_path=os.path.join(scratch, 'tickets.jsonl'),
    )


class CaseFlow:
    def __init__(self, store=None, agent=None):
        self.store = store or store_mod.get_store()
        self.agent = agent          # lazily required; only pre_investigate
        self.erp = erp_sim.ErpSimulator(self.store)

    # ------------------------------------------------------------ intake

    def _field_completion(self, case_id, procedure):
        if not procedure:
            return False
        for item in self.store.checklist(case_id):
            evidence = json.loads(item.get('tool_result') or '{}')
            if (item['action'] == 'verify_field_completion' and item.get('approval_decision') == 'confirmed'
                    and item.get('authorizer') and evidence.get('procedure_id') == procedure
                    and evidence.get('completed') is True and evidence.get('customer_report')):
                return True
        return False

    def intake(self, subject: str, body: str, account_id=None, *,
               channel="email", pool_id=None, author=None,
               run_id=None, screening_text=None) -> str:
        """Fast, deterministic, model-free. Returns the new case id."""
        now = store_mod.now()
        case_id = self.store.new_case_id()

        history = {"prior_open_cases": len(self.store.query(
            "SELECT 1 FROM cases WHERE account_id=? AND run_id IS ? "
            "AND status NOT IN (?,?)",
            (account_id, run_id, RESOLVED, CLOSED)))} if account_id else {}

        t = triage_mod.triage(screening_text if screening_text is not None else f"{subject}\n{body}", history)
        from . import support_workflow
        operational = support_workflow.consult_operational(
            screening_text if screening_text is not None else f'{subject}\n{body}')

        self.store.insert("cases", {
            "case_id": case_id, "run_id": run_id, "created_at": now,
            "updated_at": now, "closed_at": None, "channel": channel,
            "status": NEW, "priority": t.priority,
            "priority_reason": t.priority_reason,
            "priority_overridden_by": None, "subject": subject,
            "account_id": account_id, "customer_name": None,
            "serial_number": None, "model": None, "revision": None,
            "identity_verified": 0, "verification_points": None,
            "is_complaint": 1 if t.complaint.is_complaint else 0,
            "complaint_reason": t.complaint.reason,
            "is_adverse_event": 1 if t.adverse_event.is_adverse_event else 0,
            "ae_certainty": t.adverse_event.certainty,
            "classification": t.classification,
            "rma_needed": 0, "tracking_number": None, "tracking_status": None,
            "ai_state": QUEUED, "assigned_to": None, "resolution": None,
            "resolved": None, "pool_id": pool_id,
        })
        self.store.insert("messages", {
            "message_id": f"{case_id}-M01", "case_id": case_id,
            "run_id": run_id, "created_at": now, "direction": "inbound",
            "channel": channel, "author": author, "subject": subject,
            "body": body, "status": "received",
            "approved_by": None, "approved_at": None,
        })
        self.store.audit("case_created", actor="intake", case_id=case_id,
                         summary=f"{t.priority} - {t.classification}",
                         detail=t.as_dict(), run_id=run_id)
        self.store.audit('operational_qms_consulted', actor='intake', case_id=case_id,
                         summary='Controlled intake, complaint and safety procedures establish the workflow',
                         detail=operational['evidence'], run_id=run_id)
        if operational['missing']:
            self.store.audit('qms_rule_conflict', actor='validator', case_id=case_id,
                summary='Required controlled workflow support is missing; preserve safety hold and route affected actions for review',
                detail=operational['evidence'], run_id=run_id)

        # Safety is raised at intake, before any model runs, so a Critical
        # case cannot be hidden behind a slow inference queue.
        if triage_mod.requires_immediate_escalation(t):
            self._raise_quality_notification(case_id, t, run_id, procedure_support=operational['evidence'])
            self.store.update("cases", "case_id", case_id,
                              {"status": HUMAN_REVIEW, "safety_hold":1,"updated_at": now})
            self.store.audit(
                "safety_flagged_at_intake", actor="intake", case_id=case_id,
                summary="Critical at intake; routine troubleshooting stopped",
                detail={"priority_reason": t.priority_reason}, run_id=run_id)
        if pool_id:
            self.store.update("ticket_pool", "pool_id", pool_id,
                              {"released": 1})
        return case_id

    def _raise_quality_notification(self, case_id, t, run_id=None, procedure_support=None):
        ae = t.adverse_event
        if ae.is_adverse_event:
            kind = ("adverse_event" if ae.certainty == "confirmed"
                    else "possible_adverse_event")
            body = (f"{ae.certainty.title()} adverse event reported. "
                    f"{ae.reason}. Routine troubleshooting has been stopped "
                    f"pending review.")
        elif ae.hazard_indicators:
            kind = "possible_adverse_event"
            body = (f"Device safety hazard reported "
                    f"({', '.join(ae.hazard_indicators[:3])}) with no injury "
                    f"stated. Retained as a possible adverse event pending "
                    f"review rather than discarded.")
        elif t.quality.qc_discrepancy:
            kind = "qc_discrepancy"
            body = t.quality.reason
        elif t.quality.qa_document_concern:
            kind = "qa_document_concern"
            body = t.quality.reason
        else:
            return None

        nid = self.store.next_id("qa_notification", "QN-{:06d}")
        self.store.insert("qa_notifications", {
            "notification_id": nid, "case_id": case_id, "run_id": run_id,
            "created_at": store_mod.now(), "kind": kind,
            "certainty": ae.certainty, "body": body, "status": "open",
            "determination": None, "determined_by": None,
            "determined_at": None,
        })
        self.store.insert("messages", {
            "message_id": f"{case_id}-QN-{nid}", "case_id": case_id,
            "run_id": run_id, "created_at": store_mod.now(),
            "direction": "outbound", "channel": "qa_notification",
            "author": "system", "subject": f"[{kind}] {nid}",
            "body": body, "status": "sent",
            "approved_by": None, "approved_at": None,
        })
        self.store.audit("qa_notification_raised", actor="intake",
                         case_id=case_id, summary=f"{nid} {kind}",
                         detail={"body": body, 'supporting_controlled_procedures': procedure_support,
                                 'notification_procedure': 'PROC-QA-SAFETY',
                                 'human_determination_required': True}, run_id=run_id)
        return nid

    # -------------------------------------------------- pre-investigation

    def pre_investigate(self, case_id: str, run_id=None, resume=False) -> dict:
        from . import activity, operational_profile
        case = self.store.one('SELECT status FROM cases WHERE case_id=?', (case_id,))
        if case and case['status'] == 'Closed':
            # rc.27: a closed case never runs a stale queued pass (no late draft or overwrite after closure).
            self.store.audit('investigation_skipped_closed', case_id=case_id,
                             summary='Closed case: queued investigation pass not run', detail={'resume': resume})
            return {'skipped': 'case_closed'}
        try:
            with activity.investigation(self.store, case_id=case_id):
                from .orchestration import CaseOrchestrator
                # rc.27: the operational policy pinned in this case's snapshot (C1 for cases created under the
                # ticketing profile) is used on every pass; older cases keep the default orchestrator.
                trigger = 'human_resume' if resume else ('customer_followup' if self.store.one(
                    "SELECT 1 FROM messages WHERE case_id=? AND direction='inbound' AND message_id LIKE '%-SIM-IN-%'", (case_id,)) else 'initial')
                policy = operational_profile.orchestrator_policy(self.store, case_id, trigger)
                if policy:
                    activity.emit('methodology', 'Operational method ' + str(policy.get('methodology_id')) + ' pinned for this pass',
                                  evidence={'trigger': trigger, 'policy': policy})
                return CaseOrchestrator(self, policy).run(case_id, resume=resume)
        except Exception as exc:
            # A model/tool failure that stops the investigation is a visible
            # human handoff, never a case left silently in "New".
            hand_off_failure(self.store, case_id, 'Investigation could not complete', exc)
            raise

    def _document_without_reply(self,case,finding,msg):
        from .coordination import CaseRepository
        from . import activity
        repo=CaseRepository(self.store);cid=case['case_id']
        repo.note(cid,finding['outbound_message']['reason'],'intake_specialist',kind='intake',
            evidence=finding['evidence'],command_id='intake-note:'+msg['message_id'])
        if finding['contact_task']:
            key='contact-task:'+msg['message_id']
            self.store.execute('INSERT OR IGNORE INTO contact_tasks (task_id,case_id,kind,status,endpoint,evidence,command_id) VALUES(?,?,?,?,?,?,?)',
                (key,cid,finding['contact_task'],'needs_endpoint_review',None,json.dumps(finding['evidence']),key))
        self.store.update('cases','case_id',cid,{'ai_state':COMPLETED,'status':HUMAN_REVIEW if finding['contact_task'] else AI_REVIEWED})
        activity.emit('interpretation','No customer reply selected; internal documentation recorded','completed',finding)
        self.store.audit('no_reply_decision',actor='intake_specialist',case_id=cid,summary=finding['outbound_message']['reason'],detail=finding)
        return {'case_id':cid,'status':self.store.case(cid)['status'],'communication':finding['outbound_message'],
                'movement':{'movement_type':'none'},'checklist':[],'interpretation':finding}

    def _pre_investigate(self, case_id: str, run_id=None, planning=None,
                         customer_claim=None, qms_evidence=None) -> dict:
        """The slow phase. Requires an Agent for retrieval and drafting."""
        if self.agent is None:
            raise RuntimeError("pre_investigate needs an Agent")

        case = self.store.case(case_id)
        if case is None:
            raise ValueError(f"no case {case_id}")
        run_id = run_id or case.get("run_id")
        msg = self.store.one(
            "SELECT * FROM messages WHERE case_id=? AND direction='inbound' "
            "ORDER BY created_at DESC LIMIT 1", (case_id,))
        inbound_history = [m['body'] for m in self.store.messages(case_id) if m['direction'] == 'inbound']
        current_request = (msg or {}).get('body') or ''
        from .interpretation import interpret
        intake_finding=interpret(current_request,case.get('channel','unknown'),(msg or {}).get('message_id'))
        if intake_finding['outbound_message']['decision']!='draft_for_review':
            return self._document_without_reply(case,intake_finding,msg)
        text = f"{case['subject']}\n" + '\n'.join(inbound_history)

        conversation = self.store.one('SELECT state_json FROM conversations WHERE case_id=?', (case_id,))
        chat_state = json.loads(conversation['state_json']) if conversation else None
        if chat_state:
            text = '\n'.join(turn['text'] for turn in chat_state.get('turns', []) if turn['role'] == 'customer')

        self.store.update("cases", "case_id", case_id,
                          {"ai_state": PROCESSING,
                           "updated_at": store_mod.now()})
        cl = ChecklistBuilder(self.store, case_id, run_id)
        t = triage_mod.triage(current_request if len(inbound_history) > 1 else text)
        if len(inbound_history) > 1:
            unresolved_safety = self.store.one("SELECT notification_id FROM qa_notifications WHERE case_id=? AND status='open'", (case_id,))
            # The stored safety hold is authoritative across follow-ups: a benign
            # follow-up, a request to continue troubleshooting, or a request to
            # ignore the complaint keeps the whole conversation on the safety
            # path. Only a recorded human determination clears safety_hold.
            if unresolved_safety or case.get('safety_hold'):
                t = triage_mod.triage(text)
        if case.get('safety_hold') and not triage_mod.requires_immediate_escalation(t):
            # Persisted hold, no recorded human clearance: stay on the safety path
            # (no ordinary troubleshooting, no device guidance, no movements).
            t.priority = triage_mod.CRITICAL
            t.priority_reason = 'Safety hold persists from an earlier report on this case; only a recorded human determination can clear it.'
        from . import guidance

        cl.add("read_message", "Read the incoming support request", DONE,
               tool_result={"characters": len(text),
                            "channel": case["channel"]})
        cl.add("screen_adverse_event",
               "Screen for adverse events and safety concerns",
               DONE, tool_result=t.adverse_event.as_dict(),
               evidence={"indicators": t.adverse_event.indicators,
                         "hazards": t.adverse_event.hazard_indicators},
               missing_info=("a person must determine whether this is an "
                             "adverse event"
                             if t.adverse_event.requires_human_determination
                             else None))
        cl.add("classify_complaint", "Classify whether this is a complaint",
               DONE, tool_result=t.complaint.as_dict())
        cl.add("assess_priority", "Assess priority", DONE,
               tool_result={"priority": t.priority,
                            "reason": t.priority_reason,
                            "frustration_ignored":
                                t.signals["frustration_detected"]})

        # --- identify and verify the customer --------------------------
        # Only what the customer actually supplied counts as a verification
        # point. The account the mail arrived on is a locator, not a point -
        # filling the name in from that record and then matching it against
        # the same record would verify nothing.
        known_serials = [row["serial_number"] for row in self.store.query(
            "SELECT serial_number FROM devices")]
        claim = identity_mod.Claim.from_text(
            text, known_serials=known_serials,
            known_names=[row['customer_name'] for row in self.store.query('SELECT customer_name FROM customers')])
        if customer_claim is not None:claim=identity_mod.Claim(**customer_claim)
        if chat_state:
            claim = identity_mod.Claim(**chat_state.get('claim', {}))
        ver = identity_mod.verify(self.store, claim,
                                  account_id=case.get("account_id"))
        cl.add("identify_customer", "Identify the customer from the message",
               DONE if ver.account_id else ITEM_FAILED,
               tool_result={"account_id": ver.account_id,
                            "customer_name": ver.customer_name,
                            "candidates": ver.candidates},
               missing_info=None if ver.account_id else ver.reason)
        cl.add("verify_identity",
               "Verify identity on two points (rule IV-1)",
               DONE if ver.verified else NEEDS_APPROVAL,
               tool_result=ver.as_dict(),
               evidence={"points_matched": ver.points_matched,
                         "locators_used": ver.locators_used},
               missing_info=None if ver.verified else ver.reason)

        device = None
        if ver.serial_number:
            device = self.store.device(ver.serial_number)
        elif ver.account_id and ver.verified:
            owned = self.store.devices_for_account(ver.account_id)
            device = owned[0] if len(owned) == 1 else None

        cl.add("check_erp_records",
               "Consult ERP for account, device and order records",
               DONE if device else ITEM_FAILED,
               tool_result={"device": device,
                            "orders": self.store.query(
                                "SELECT * FROM orders WHERE account_id=?",
                                (ver.account_id,)) if ver.account_id else []},
               missing_info=None if device else
               "no single device could be resolved from the ERP")

        rev = identity_mod.assess_revision(device) if device else None
        from . import support_workflow
        active_request = current_request if len(inbound_history) > 1 else text
        support_plan = support_workflow.investigate(active_request, store=self.store, agent=self.agent,
            claim=claim, verified=ver.verified, device=device, interface='ticket',
            authorized_evidence=qms_evidence)
        cl.add('consult_operational_procedures', 'Consult controlled operational QMS and select investigation tools',
               DONE if not support_plan['operational']['missing'] else NEEDS_APPROVAL,
               tool_result={'action': support_plan['action'], 'mode': support_plan['selection_mode']},
               evidence=support_plan['operational']['evidence'])
        cl.add("assess_device_revision",
               "Establish the hardware revision and how far it is trusted "
               "(rule RV-1)",
               (DONE if rev and rev.usable_for_troubleshooting
                else NEEDS_APPROVAL if rev else NA),
               tool_result=rev.as_dict() if rev else None,
               evidence={"status": rev.status, "basis": rev.basis}
               if rev else None,
               missing_info=(None if (rev and rev.usable_for_troubleshooting)
                             else (rev.reason if rev else
                                   "no device confirmed")))

        # --- case history ----------------------------------------------
        history = self.store.query(
            "SELECT case_id, created_at, status, classification, resolution "
            "FROM cases WHERE account_id=? AND case_id!=? AND run_id IS ? "
            "ORDER BY created_at DESC LIMIT 5",
            (ver.account_id, case_id, run_id)) if ver.account_id else []
        cl.add("review_case_history", "Review earlier cases for this customer",
               DONE, tool_result={"prior_cases": history})

        # --- QMS -------------------------------------------------------
        agent_result = None
        applicable_procedure = None
        exhausted = False
        # The revision gate exists to stop revision-specific *instructions*
        # going out on an unverified inference. It has no bearing on an order
        # status question, so it is only applied when a fault actually needs
        # a procedure.
        needs_procedure = (t.complaint.is_complaint
                           and not triage_mod.requires_immediate_escalation(t))
        revision_blocks = bool(needs_procedure and device and rev
                               and not rev.usable_for_troubleshooting)

        if triage_mod.requires_immediate_escalation(t):
            cl.add("search_qms",
                   "Search approved device documentation", NA,
                   missing_info=("skipped: a safety concern is unresolved, so "
                                 "routine troubleshooting must not continue"))
            cl.add("validate_procedure_applicability",
                   "Validate document applicability", NA,
                   missing_info="skipped for the same reason")
        elif device and ver.verified and not revision_blocks and not guidance.administrative(text):
            # The orchestration QMS specialist has already selected and
            # authorized an immutable evidence package.  Consume it here;
            # performing another agent search can select additional chunks
            # under a different ranking and break provenance continuity.
            contracted = bool(qms_evidence and support_plan.get('manual')
                              and support_plan['manual'].get('chunks'))
            if contracted:
                applicable_procedure = support_plan['manual'].get('procedure')
                exhausted = self._field_completion(case_id, applicable_procedure)
                self._persist_qms_contract(case_id, support_plan['manual'], run_id)
                chunk_ids = [c.chunk_id for c in support_plan['manual']['chunks']]
                cl.add("search_qms", "Search approved device documentation", DONE,
                       tool_result={"query": qms_evidence.get('retrieval_query'),
                                    "accepted": chunk_ids, "rejected": 0})
                cl.add("validate_procedure_applicability",
                       "Validate model, revision, status and authorisation", DONE,
                       tool_result=support_plan['manual'].get('contract'),
                       evidence={"evidence_contract_reused": True,
                                 "source_chunk_ids": chunk_ids})
            else:
                agent_result = self.agent.handle(
                    active_request, account_id=ver.account_id, scenario_id=case_id,
                    verified_device=device, verified_customer_name=ver.customer_name)
                applicable_procedure = agent_result.selected_procedure
                # Model/tool exhaustion is investigation failure, never completion
                # of physical checks. Only a recorded field-completion review counts.
                exhausted = self._field_completion(case_id, applicable_procedure)
                self._persist_agent_evidence(case_id, agent_result, run_id)
                # The agent routes by intent, so an order-status question never
                # reaches the QMS at all. That is correct behaviour, not a
                # failed lookup, and the checklist must not call it a failure.
                searched = agent_result.retrieval_query is not None
                if not searched:
                    skip = (f"not applicable: this was routed as "
                            f"{', '.join(k for k, v in agent_result.intent.items() if v)}"
                            f", which is answered from ERP records")
                    cl.add("search_qms", "Search approved device documentation",
                           NA, tool_result={"intent": agent_result.intent},
                           missing_info=skip)
                    cl.add("validate_procedure_applicability",
                           "Validate document applicability", NA,
                           missing_info=skip)
                else:
                    cl.add("search_qms", "Search approved device documentation",
                           DONE if agent_result.accepted else ITEM_FAILED,
                           tool_result={"query": agent_result.retrieval_query,
                                        "accepted": [v.chunk_id for v in
                                                     agent_result.accepted],
                                        "rejected": len(agent_result.rejected)},
                           missing_info=(None if agent_result.accepted else
                                         "no approved procedure covers this "
                                         "device and symptom"))
                    cl.add("validate_procedure_applicability",
                           "Validate model, revision, status and authorisation",
                           DONE if agent_result.accepted else ITEM_FAILED,
                           tool_result=agent_result.filters,
                           evidence={"rejected": [v.as_dict() for v in
                                                  agent_result.rejected][:8]})
        else:
            reason = (f"the hardware revision is {rev.status} rather than "
                      f"verified, so revision-specific procedures cannot be "
                      f"authorised until someone confirms it"
                      if revision_blocks else "no device could be confirmed")
            cl.add("search_qms", "Search approved device documentation",
                   PENDING, missing_info=reason)
            cl.add("validate_procedure_applicability",
                   "Validate document applicability", PENDING,
                   missing_info=reason)

        # --- complaint / AE evaluation ---------------------------------
        cl.add("evaluate_complaint", "Evaluate as a customer-service complaint",
               DONE, tool_result={"is_complaint": t.complaint.is_complaint,
                                  "category": t.complaint.category,
                                  "reason": t.complaint.reason})
        cl.add("evaluate_quality_signals",
               "Separate QC product discrepancy from QA document concern",
               DONE if (t.quality.qc_discrepancy or t.quality.qa_document_concern)
               else NA,
               tool_result=t.quality.as_dict())

        # --- product movement -------------------------------------------
        decision = movement_mod.decide(
            text, t, procedure_exhausted=exhausted,
            applicable_procedure=applicable_procedure, device=device,
            identity_verified=ver.verified)
        if planning and planning.get('mode')=='tree_of_thought' and planning.get('selected_action') in ('clarify','human_review') and decision.needs_transaction:
            decision=movement_mod.MovementDecision(movement_mod.HUMAN_REVIEW,rule='TOT-PREREQUISITES',
                reason='Selected plan requires clarification or human review before an inventory proposal')
        if planning:
            expected={'part_shipment':movement_mod.PART_OUTBOUND,
                      'device_return':movement_mod.DEVICE_RETURN,
                      'replacement':movement_mod.DEVICE_REPLACEMENT}.get(
                          planning.get('selected_action'))
            if expected and decision.movement_type!=expected:
                decision=movement_mod.MovementDecision(
                    movement_mod.HUMAN_REVIEW,rule='PLAN-TRANSITION-MISMATCH',
                    reason='Planner-selected disposition and deterministic movement rules disagree; no operation was proposed',
                    evidence={'selected_action':planning.get('selected_action'),
                              'deterministic_movement':decision.as_dict()})
        decision.evidence['controlled_workflow_support'] = support_plan['operational']['evidence']
        decision.evidence['agent_selected_tool'] = support_plan['action']
        if decision.needs_transaction and ('propose_movement' not in support_plan['operational']['actions']
                                          or support_plan['operational']['missing']):
            self.store.audit('qms_rule_conflict', actor='validator', case_id=case_id,
                             summary='Inventory proposal blocked: controlled procedural support is missing or conflicts with fixed approval rules',
                             detail=support_plan['operational']['evidence'])
            decision = movement_mod.MovementDecision(movement_mod.HUMAN_REVIEW,
                rule='QMS-CONTROL', reason='Controlled movement support requires document-owner review')
        cl.add("determine_product_movement",
               "Decide whether any product needs to move, and what",
               NEEDS_APPROVAL if decision.needs_transaction else DONE,
               tool_result=decision.as_dict(),
               evidence={"rule": decision.rule,
                         "avoidable_full_return":
                             movement_mod.avoidable_full_return(decision, text)},
               missing_info=(decision.reason
                             if decision.movement_type ==
                             movement_mod.HUMAN_REVIEW else None))

        proposal = None
        self.store.update('cases', 'case_id', case_id, {
            'identity_verified': int(ver.verified), 'customer_name': ver.customer_name,
            'account_id': ver.account_id or case.get('account_id'),
            'serial_number': (device or {}).get('serial_number'),
            'model': (device or {}).get('model'), 'revision': (device or {}).get('revision')})
        self.erp.ensure_service_call(case_id, account_id=ver.account_id,
                                     serial_number=(device or {}).get('serial_number'), run_id=run_id)
        if decision.needs_transaction:
            proposal = self.erp.propose_movement(
                case_id, decision, account_id=ver.account_id,
                serial_number=(device or {}).get("serial_number"),
                run_id=run_id)

        # --- draft the reply --------------------------------------------
        # Save current facts before constructing/validating any customer draft.
        self.store.update('cases', 'case_id', case_id, {
            'identity_verified': int(ver.verified), 'customer_name': ver.customer_name,
            'account_id': ver.account_id or case.get('account_id'),
            'serial_number': (device or {}).get('serial_number'),
            'model': (device or {}).get('model'), 'revision': (device or {}).get('revision')})
        draft = self._draft_reply(case_id, case, t, ver, agent_result,
                                  decision, run_id, support_plan=support_plan, claim=claim)
        composition = json.loads((self.store.one('SELECT metadata FROM messages WHERE message_id=?',(draft,)) or {}).get('metadata') or '{}')
        cl.add("prepare_response", "Prepare a customer response for review",
               NEEDS_APPROVAL, tool_result={"message_id": draft},
               evidence={'grounded':composition.get('grounded'),'source':composition.get('procedure_id'),
                         'statement_authority':composition.get('statement_authority'),
                         'generation_mode':composition.get('generation_mode')})

        missing = [i["action"] for i in cl.items if i["missing_info"]]
        cl.add("identify_missing_information",
               "List what is still unknown", DONE,
               tool_result={"items_with_gaps": missing})

        # Human review is for cases the agent genuinely cannot close, not for
        # every case. A routine enquiry with no fault reported does not need
        # an engineer just because no QMS procedure applied to it.
        needs_human, why = False, ""
        if triage_mod.requires_immediate_escalation(t):
            needs_human, why = True, "critical safety classification"
        elif not ver.verified:
            needs_human, why = True, "customer identity not verified"
        elif decision.needs_transaction:
            needs_human, why = True, "a product movement needs approval"
        elif revision_blocks:
            needs_human, why = True, (
                f"device revision is {rev.status}, not verified, and this "
                f"case needs revision-specific guidance")
        elif t.complaint.is_complaint and not applicable_procedure:
            needs_human, why = True, ("a fault was reported but no approved "
                                      "procedure covers it")
        status = HUMAN_REVIEW if needs_human else AI_REVIEWED
        if needs_human:
            self.store.audit("human_review_required", actor="ai",
                             case_id=case_id, summary=why, run_id=run_id)
        self.store.update("cases", "case_id", case_id, {
            "ai_state": COMPLETED, "status": status,
            "updated_at": store_mod.now(),
            "customer_name": ver.customer_name,
            "account_id": ver.account_id or case.get("account_id"),
            "serial_number": (device or {}).get("serial_number"),
            "model": (device or {}).get("model"),
            "revision": (device or {}).get("revision"),
            "identity_verified": 1 if ver.verified else 0,
            "verification_points": json.dumps(ver.points_matched),
        })
        self.store.audit("ai_pre_investigation_completed", actor="ai",
                         case_id=case_id,
                         summary=f"{len(cl.items)} checklist items; status "
                                 f"{status}",
                         detail={"movement": decision.as_dict(),
                                 "verified": ver.verified}, run_id=run_id)
        self.erp.ensure_service_call(case_id, run_id=run_id)
        # rc.25: the evidence this pass actually authorized for the draft, so
        # a validator that did not run the QMS specialist (direct adapter B)
        # validates against the same frozen package instead of an empty one.
        manual = (support_plan or {}).get('manual') or {}
        authorized = ({'sources': [c.chunk_id for c in manual.get('chunks') or []],
                       'evidence_contract': manual.get('contract') or {},
                       'authorized_procedures': manual.get('procedures') or [],
                       'origin': 'upstream_contract' if qms_evidence else 'pre_investigation_manual_search'}
                      if manual.get('chunks') else None)
        return {"case_id": case_id, "status": status,
                "checklist": cl.items, "movement": decision.as_dict(),
                "proposal": proposal.as_dict() if proposal else None,
                "verification": ver.as_dict(),
                "triage": t.as_dict(), "authorized_evidence": authorized}

    # ----------------------------------------------- revision confirmation

    def confirm_revision(self, case_id: str, *, decision: str,
                         authorizer: str, note: str = "",
                         run_id=None) -> dict:
        from . import activity
        with activity.investigation(self.store, case_id=case_id):
            activity.emit('revision_decision', 'Representative recorded a revision decision',
                          evidence={'decision': decision, 'authorizer': authorizer, 'note': note})
            return self._confirm_revision(case_id, decision=decision, authorizer=authorizer,
                                          note=note, run_id=run_id)

    def _confirm_revision(self, case_id: str, *, decision: str,
                          authorizer: str, note: str = '', run_id=None) -> dict:
        """Record a representative's decision on an inferred revision.

        This is the missing half of rule RV-1 (defect C13). `assess_revision`
        has always accepted a human confirmation; nothing ever supplied one,
        so a case blocked on an inferred revision could not be unblocked.

        A confirmation re-opens exactly the part of the investigation the
        block stopped - the QMS search, the deterministic applicability check
        and the reply that depends on them. Nothing else is re-run, and the
        applicability check itself is untouched: confirming the revision says
        *which* revision the unit is, it does not authorise a procedure for
        it. If no approved procedure covers the confirmed revision the case
        still comes back needing a person.
        """
        if decision not in ("confirmed", "rejected"):
            return {"ok": False, "reason": "decision must be confirmed or "
                                           "rejected"}
        case = self.store.case(case_id)
        if case is None:
            return {"ok": False, "reason": f"no case {case_id}"}
        device = (self.store.device(case.get("serial_number"))
                  if case.get("serial_number") else None)
        if device is None:
            return {"ok": False, "reason": ("no device is confirmed on this "
                                            "case, so there is no revision "
                                            "to confirm")}

        now = store_mod.now()
        run_id = run_id or case.get("run_id")
        self.store.update("cases", "case_id", case_id, {
            "revision_confirmation": decision,
            "revision_confirmed_by": authorizer,
            "revision_confirmed_at": now, "updated_at": now})

        item = self.store.one(
            "SELECT * FROM checklist_items WHERE case_id=? AND action=? "
            "ORDER BY seq DESC LIMIT 1",
            (case_id, "assess_device_revision"))
        if item:
            self.store.update("checklist_items", "item_id", item["item_id"], {
                "approval_decision": decision, "authorizer": authorizer,
                "decided_at": now, "feedback": note or None,
                "consequence": (
                    f"revision {device.get('revision')} confirmed by "
                    f"{authorizer}; document eligibility re-checked"
                    if decision == "confirmed" else
                    f"revision rejected by {authorizer}; no revision-specific "
                    f"guidance may be issued")})
        self.store.insert("approvals", {
            "approval_id": self.store.next_id("approval", "APV-{:06d}"),
            "case_id": case_id, "run_id": run_id, "at": now,
            "subject_type": "device_revision",
            "subject_id": device.get("serial_number"),
            "decision": decision, "authorizer": authorizer, "note": note})
        self.store.audit(
            f"device_revision_{decision}", actor=authorizer, case_id=case_id,
            summary=(f"{device.get('serial_number')} revision "
                     f"{device.get('revision')} {decision} by {authorizer}"),
            detail={"revision": device.get("revision"),
                    "revision_status": device.get("revision_status"),
                    "basis": device.get("revision_basis"), "note": note},
            run_id=run_id)

        if decision == "rejected":
            set_status(self.store, case_id, HUMAN_REVIEW, authorizer,
                       "the proposed device revision was rejected; the unit's "
                       "revision has to be established another way",
                       run_id=run_id)
            return {"ok": True, "decision": decision, "reinvestigated": False,
                    "summary": ("Recorded. No revision-specific procedure can "
                                "be authorised until the revision is "
                                "established.")}

        rev = identity_mod.assess_revision(device,
                                           human_confirmation=authorizer)
        result = self._reinvestigate_procedure(case, device, rev, authorizer,
                                               run_id)
        result.update({"ok": True, "decision": decision,
                       "reinvestigated": True})
        return result

    def _reinvestigate_procedure(self, case, device, rev, authorizer,
                                 run_id) -> dict:
        """Re-run the guarded part of the investigation, and only that."""
        if self.agent is None:
            raise RuntimeError("re-investigation needs an Agent")
        case_id = case["case_id"]
        msg = self.store.one(
            "SELECT * FROM messages WHERE case_id=? AND direction='inbound' "
            "ORDER BY created_at LIMIT 1", (case_id,))
        text = f"{case['subject']}\n{(msg or {}).get('body') or ''}"
        t = triage_mod.triage(text)
        cl = ChecklistBuilder(self.store, case_id, run_id)

        cl.add("assess_device_revision",
               "Re-establish the hardware revision after human confirmation "
               "(rule RV-1)", DONE, actor=REP,
               tool_result=rev.as_dict(),
               evidence={"confirmed_by": authorizer,
                         "status_before": device.get("revision_status")})

        agent_result = self.agent.handle(text,
                                         account_id=case.get("account_id"),
                                         scenario_id=case_id, verified_device=device,
                                         verified_customer_name=case.get('customer_name'))
        self._persist_agent_evidence(case_id, agent_result, run_id)
        applicable_procedure = agent_result.selected_procedure
        searched = agent_result.retrieval_query is not None

        if not searched:
            skip = ("not applicable: this request is answered from ERP "
                    "records, not from device documentation")
            cl.add("search_qms", "Search approved device documentation", NA,
                   tool_result={"intent": agent_result.intent},
                   missing_info=skip)
            cl.add("validate_procedure_applicability",
                   "Validate document applicability", NA, missing_info=skip)
        else:
            cl.add("search_qms",
                   "Search approved device documentation for the confirmed "
                   "revision",
                   DONE if agent_result.accepted else ITEM_FAILED,
                   tool_result={"query": agent_result.retrieval_query,
                                "accepted": [v.chunk_id for v in
                                             agent_result.accepted],
                                "rejected": len(agent_result.rejected)},
                   missing_info=(None if agent_result.accepted else
                                 "no approved procedure covers this device "
                                 "and symptom even with the revision "
                                 "confirmed"))
            cl.add("validate_procedure_applicability",
                   "Validate model, revision, status and authorisation",
                   DONE if agent_result.accepted else ITEM_FAILED,
                   tool_result=agent_result.filters,
                   evidence={"rejected": [v.as_dict() for v in
                                          agent_result.rejected][:8]})

        exhausted = self._field_completion(case_id, applicable_procedure)
        decision = movement_mod.decide(
            text, t, procedure_exhausted=exhausted,
            applicable_procedure=applicable_procedure, device=device,
            identity_verified=bool(case.get("identity_verified")))
        proposal = None
        if decision.needs_transaction:
            cl.add("determine_product_movement",
                   "Decide whether any product needs to move, and what",
                   NEEDS_APPROVAL, tool_result=decision.as_dict(),
                   evidence={"rule": decision.rule})
            proposal = self.erp.propose_movement(
                case_id, decision, account_id=case.get("account_id"),
                serial_number=device.get("serial_number"), run_id=run_id)

        ver = identity_mod.VerificationResult(
            verified=bool(case.get("identity_verified")),
            account_id=case.get("account_id"),
            customer_name=case.get("customer_name"))
        draft = self._draft_reply(case_id, case, t, ver, agent_result,
                                  decision, run_id)
        cl.add("prepare_response",
               "Prepare a customer response for review", NEEDS_APPROVAL,
               tool_result={"message_id": draft},
               evidence={"grounded": (agent_result.grounding or {}).get(
                   "grounded"), "source": applicable_procedure})

        if decision.needs_transaction:
            why = "a product movement needs approval"
        elif t.complaint.is_complaint and not applicable_procedure:
            why = ("the revision is confirmed but no approved procedure "
                   "covers this fault for it")
        else:
            why = ""
        status = HUMAN_REVIEW if why else AI_REVIEWED
        set_status(self.store, case_id, status, authorizer,
                   why or "revision confirmed and an approved procedure now "
                          "applies", run_id=run_id)
        self.store.update("cases", "case_id", case_id,
                          {"revision": device.get("revision"),
                           "ai_state": COMPLETED,
                           "updated_at": store_mod.now()})
        return {"status": status, "procedure": applicable_procedure,
                "message_id": draft,
                "proposal": proposal.as_dict() if proposal else None,
                "checklist": cl.items,
                "summary": (
                    f"Revision confirmed. Document eligibility was re-checked "
                    f"and " + (f"{applicable_procedure} now applies."
                               if applicable_procedure else
                               "still no approved procedure covers this "
                               "fault for the confirmed revision."))}

    # ------------------------------------------------------------ helpers

    def _persist_agent_evidence(self, case_id, result, run_id):
        from . import activity
        activity.emit('agent_result', 'Investigation response and decision evidence', evidence=result.as_dict())
        now = store_mod.now()
        for v in result.accepted + result.rejected:
            d = v.as_dict()
            self.store.insert("retrieved_sources", {
                "case_id": case_id, "run_id": run_id, "at": now,
                "chunk_id": d["chunk_id"], "document_id": d["document_id"],
                "procedure_id": d["procedure_id"],
                "similarity": d["similarity"], "rank": d["rank"],
                "selected_by": d.get("selected_by"), "status": d["status"],
                "reason": d["reason"]})
        for s in result.steps:
            self.store.insert("reasoning_attempts", {
                "case_id": case_id, "run_id": run_id, "at": now,
                "seq": s.n, "kind": s.kind, "action": s.action,
                "thought": s.thought, "observation": s.observation,
                "productive": 1 if s.productive else 0})
        for tc in result.tool_calls:
            self.store.insert("tool_calls", {
                "case_id": case_id, "run_id": run_id, "at": now,
                "tool": tc["tool"], "productive": 1 if tc["productive"] else 0,
                "summary": tc["summary"],
                "detail": json.dumps(tc.get("data"), default=str)[:4000]})

    def _persist_qms_contract(self, case_id, manual, run_id):
        """Persist the exact upstream-authorized evidence without re-searching."""
        candidate_by_id = {
            row['chunk_id']: row
            for row in (manual.get('evidence') or {}).get('candidates', [])
        }
        now = store_mod.now()
        for rank, chunk in enumerate(manual.get('chunks', []), 1):
            candidate = candidate_by_id.get(chunk.chunk_id, {})
            self.store.insert("retrieved_sources", {
                "case_id": case_id, "run_id": run_id, "at": now,
                "chunk_id": chunk.chunk_id, "document_id": chunk.document_id,
                "procedure_id": chunk.procedure_id,
                "similarity": candidate.get('score'), "rank": rank,
                "selected_by": "qms_evidence_contract", "status": "USED",
                "reason": "authorized upstream and reused by operations"})

    def _draft_reply(self, case_id, case, t, ver, agent_result, decision,
                     run_id, support_plan=None, claim=None) -> str:
        latest = self.store.one("SELECT * FROM messages WHERE case_id=? AND direction='inbound' ORDER BY created_at DESC LIMIT 1", (case_id,)) or {}
        resolved_response, resolved_mode = None, 'template'
        if latest.get('parent_message_id') and not triage_mod.requires_immediate_escalation(t) and re.search(r'\b(?:works now|working now|problem is resolved|issue is resolved|now powers on)\b', latest.get('body',''), re.I):
            resolved_response = 'Thank you for letting us know the issue is resolved. If anything changes, please get back in touch.'
            if self.agent.prefer_llm:
                from . import generation, response_policy
                from .grounding import DEVICE_ACTION_TERMS
                try:
                    candidate=generation.compose_customer('Acknowledge the customer-reported successful result warmly. Do not prescribe further troubleshooting, claim an account action, or close the case automatically. Under 60 words.',
                        json.dumps({'customer_report':latest['body'],'intent':'acknowledge reported resolution','case_resolution':'requires representative decision'}))
                    if response_policy.validate_case(self.store,case_id,candidate)['passed'] and not any(re.search(r'\b'+re.escape(term),candidate,re.I) for term in DEVICE_ACTION_TERMS):
                        resolved_response,resolved_mode=candidate,'llm'
                except (ValueError, OSError, TimeoutError):
                    pass
        if latest.get('parent_message_id') and ver.verified and not triage_mod.requires_immediate_escalation(t):
            shipment = self.store.one('SELECT tracking_number FROM sim_shipments WHERE case_id=? ORDER BY created_at DESC LIMIT 1', (case_id,))
            if shipment and re.search(r'label|packag|return|tracking|pickup|ship|drop.off', latest.get('body',''), re.I):
                from . import activity
                from .operations_workflow import shipping_draft
                activity.emit('shipping_lookup','Recorded shipment retrieved for the customer follow-up',evidence=shipment)
                mid=shipping_draft(self.store,shipment['tracking_number'],'followup:'+latest['message_id'])
                activity.emit('response','Shipping follow-up draft prepared from the recorded shipment',evidence={'message_id':mid,'generation_mode':'recorded_facts','model_response_used':False,'sent':False})
                return mid
        public_generation = None
        if (not resolved_response and support_plan and support_plan.get('manual') and support_plan['manual']['chunks']
                and not triage_mod.requires_immediate_escalation(t)
                and (not ver.verified or agent_result is None or agent_result.outcome not in ('grounded', 'answered'))):
            from . import support_workflow
            inbound_question = self.store.one('SELECT body FROM messages WHERE case_id=? AND direction=? ORDER BY created_at DESC LIMIT 1', (case_id, 'inbound')) or {}
            public_generation, public_report = support_workflow.write_public_reply(self.agent,
                inbound_question.get('body', ''), support_plan, claim.name if claim else None)
        if triage_mod.requires_immediate_escalation(t):
            body = (
                f"Hello {ver.customer_name or 'there'},\n\n"
                "Thank you for telling us about this. Because your message "
                "describes a possible safety issue, I have stopped routine "
                "troubleshooting and passed it straight to our quality team "
                "for review. Please stop using the unit and unplug it.\n\n"
                "Someone will contact you directly.\n\n"
                "Kind regards,\nCustomer Support")
        elif resolved_response:
            body = resolved_response
        elif public_generation is not None:
            body = public_generation.text
        elif not ver.verified:
            body = (support_plan['question'] if support_plan and support_plan.get('model')
                    and not support_plan.get('requires_account_verification') else identity_mod.request_message(ver))
            body = (f"Hello {(claim.name if claim else None) or 'there'},\n\n"
                    f"Thank you for getting in touch. {body}\n\n"
                    f"Kind regards,\nCustomer Support")
        elif agent_result is not None:
            body = agent_result.response
        else:
            from . import guidance
            inbound = self.store.one('SELECT body FROM messages WHERE case_id=? AND direction=? ORDER BY created_at LIMIT 1', (case_id, 'inbound')) or {}
            if guidance.administrative(case['subject'] + '\n' + inbound.get('body', '')):
                body = (f"Hello {ver.customer_name or 'there'},\n\n"
                        'Thank you for your question. I have the account details you provided. '
                        'Could you tell us which part of the refill, code, or account process you need help with? '
                        'A member of our team will review the request before any account changes are made.\n\n'
                        'Kind regards,\nCustomer Support')
            else:
                body = (f"Hello {ver.customer_name or 'there'},\n\n"
                    "Thank you for getting in touch. I need a colleague to "
                    "confirm some details about your unit before I can give "
                    "you device-specific guidance, and they will come back to "
                    "you.\n\nKind regards,\nCustomer Support")

        from . import response_policy, activity
        facts = response_policy.case_facts(self.store, case_id)
        verdict = response_policy.validate_case(self.store, case_id, body)
        activity.emit('consistency', 'Proposed email checked against current case facts',
                      'completed' if verdict['passed'] else 'rejected', verdict)
        if not verdict['passed']:
            self.store.audit('draft_consistency_rejected', actor='validator', case_id=case_id,
                             summary='Contradictory draft blocked before representative approval',
                             detail={'original_draft': body, 'validation': verdict, 'facts': facts})
            body = (f"Hello {ver.customer_name if ver.verified else 'there'},\n\n"
                    'Thank you for the details you have provided. '
                    'I am checking the information for your request, and a colleague will review '
                    'the next step with you.\n\nKind regards,\nCustomer Support')

        if decision.needs_transaction and not resolved_response and self.store.one("SELECT 1 FROM rmas WHERE case_id=? AND status='proposed'",(case_id,)):
            body += ("\n\n[For representative: a product movement has been "
                     "proposed and needs your approval before anything is "
                     "promised to the customer.]")

        from .generation import customer_text
        body = customer_text(body)
        mode = resolved_mode if resolved_response else public_generation.mode if public_generation else agent_result.generation_mode if agent_result else 'template'
        if not verdict['passed'] or triage_mod.requires_immediate_escalation(t):
            mode = 'corrected-template' if not verdict['passed'] else 'safety-template'

        mid = f"{case_id}-DRAFT-{self.store.next_id('draft', '{:05d}')}"
        source_ids = ([c.chunk_id for c in support_plan['manual']['chunks']] if public_generation else
                      [v.chunk_id for v in agent_result.accepted if getattr(v,'used',False)] if agent_result else [])
        if resolved_response or not verdict['passed'] or triage_mod.requires_immediate_escalation(t):
            source_ids=[]
        authority='customer_report' if resolved_response else 'approved_qms' if source_ids else 'case_facts'
        source_procedure=(support_plan['manual']['procedure'] if public_generation else agent_result.selected_procedure if agent_result and source_ids else None)
        source_grounded=(public_report.grounded if public_generation and source_ids else (agent_result.grounding or {}).get('grounded') if agent_result and source_ids else None)
        self.store.insert("messages", {
            "message_id": mid, "case_id": case_id, "run_id": run_id,
            "created_at": store_mod.now(), "direction": "outbound",
            "channel": case["channel"], "author": "ai",
            "subject": f"Re: {case['subject']}", "body": body,
            "status": "draft", "approved_by": None, "approved_at": None,
            'metadata':json.dumps({'kind':'agent_response','generation_mode':mode,'source_chunk_ids':source_ids,
                'statement_authority':authority,'procedure_id':source_procedure,'grounded':source_grounded})})
        activity.emit('response', 'Customer email draft prepared; awaiting representative authorization', evidence={
            'message_id': mid, 'generation_mode': mode, 'model_response_used': mode == 'llm',
            'text': body.split('[For representative:')[0].rstrip(), 'sent': False})
        self.store.audit("response_drafted", actor="ai", case_id=case_id,
                         summary=f"draft {mid}", run_id=run_id)
        return mid
