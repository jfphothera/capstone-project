"""The capstone agent: a bounded reasoning loop over a small set of tools.

    customer message
      -> safety screen                  (deterministic, runs first)
      -> ERP identification             (deterministic; uses conversation memory)
      -> intent routing                 (deterministic; picks relevant tools)
      -> reviewed-history recall        (advisory only, never a source)
      -> semantic QMS search            (real embeddings) + source validation
      -> draft / verify / correct       (LLM, then code-checked; Reflexion)
      -> final PASS/FAIL gate           (deterministic)
      -> escalation or approval request (simulated queues)
      -> ticket interaction recorded    (local JSONL)

Every trace step carries a `kind`. ReAct may request model tool selection and
verified prose; the enclosing LangGraph coordinates selective model planning,
advisory specialists and independent review. Recorded calls establish actual
model usage; authoritative facts and permissions remain deterministic.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import (
    cases,
    config,
    erp as erp_mod,
    generation,
    index as index_mod,
    memory as memory_mod,
    qms,
    react,
    tools as tools_mod,
    validation,
)

DETERMINISTIC = react.DETERMINISTIC
RETRIEVAL = react.RETRIEVAL
GENERATION = react.GENERATION

# The serial line is an ERP key, not a symptom description. It is stripped
# before the text is used as a retrieval query, so two messages that differ
# only by serial number produce the identical query.
_SERIAL_LINE = re.compile(r"^\s*serial\s*(number)?\s*[:#]\s*\S+\s*$",
                          re.IGNORECASE | re.MULTILINE)


def symptom_text(message: str) -> str:
    return _SERIAL_LINE.sub("", message or "").strip()


@dataclass
class Result:
    scenario_id: Optional[str]
    question: str
    outcome: str      # grounded | answered | abstained | escalated
    response: str
    generation_mode: str
    generation_engine: str
    verdict: validation.Verdict
    identity: erp_mod.IdentityResult
    selected_procedure: Optional[str]
    accepted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    retrieval_query: Optional[str] = None
    retrieval_queries: list = field(default_factory=list)
    filters: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    ticket_id: Optional[str] = None
    conversation_id: Optional[str] = None
    intent: dict = field(default_factory=dict)
    tool_calls: list = field(default_factory=list)
    grounding: Optional[dict] = None
    reflexion: list = field(default_factory=list)
    unproductive_attempts: int = 0
    case_id: Optional[str] = None
    case_priority: Optional[str] = None
    escalation_reason: Optional[str] = None
    rma_request_id: Optional[str] = None
    rma_status: Optional[str] = None
    memory_proposed_id: Optional[str] = None
    memory_used: list = field(default_factory=list)

    def as_dict(self):
        return {
            "scenario_id": self.scenario_id,
            "ticket_id": self.ticket_id,
            "conversation_id": self.conversation_id,
            "outcome": self.outcome,
            "intent": self.intent,
            "retrieval_query": self.retrieval_query,
            "retrieval_queries": self.retrieval_queries,
            "filters": self.filters,
            "device_identification": self.identity.as_dict(),
            "tool_calls": self.tool_calls,
            "retrieved": [v.as_dict() for v in self.accepted + self.rejected],
            "accepted_chunk_ids": [v.chunk_id for v in self.accepted],
            "rejected_chunk_ids": [v.chunk_id for v in self.rejected],
            "selected_procedure": self.selected_procedure,
            "validation": self.verdict.as_dict(),
            "grounding": self.grounding,
            "reflexion": self.reflexion,
            "unproductive_attempts": self.unproductive_attempts,
            "escalation": {"case_id": self.case_id,
                           "priority": self.case_priority,
                           "reason": self.escalation_reason},
            "approval": {"request_id": self.rma_request_id,
                         "status": self.rma_status},
            "memory": {"proposed": self.memory_proposed_id,
                       "recalled_used": self.memory_used},
            "generation_mode": self.generation_mode,
            "generation_engine": self.generation_engine,
            "response": self.response,
            "trace": [s.as_dict() for s in self.steps],
        }


class Agent:
    def __init__(self, prefer_llm: bool = True, verbose: bool = False,
                 escalations=None, approvals=None, reviewed_memory=None,
                 ticket_log_path: Optional[str] = None):
        self.erp = erp_mod.ERP()
        from .retrieval_profiles import ProfileIndex
        self.index = ProfileIndex(index_mod.VectorIndex.load_or_build(verbose=verbose))
        self.compatibility = qms.load_compatibility()
        self.prefer_llm = prefer_llm
        self.escalations = escalations or cases.EscalationQueue()
        self.approvals = approvals or cases.ApprovalQueue()
        self.reviewed_memory = reviewed_memory or memory_mod.ReviewedMemory()
        self.ticket_log_path = ticket_log_path or config.TICKET_LOG_PATH
        self.toolbox = tools_mod.Toolbox(
            self.erp, self.index, self.compatibility, self.reviewed_memory
        )

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def safety_hits(message: str) -> list:
        from . import triage
        screening = triage.assess_adverse_event(message)
        return screening.indicators + screening.hazard_indicators

    def new_conversation(self, account_id=None) -> memory_mod.Conversation:
        return memory_mod.Conversation(account_id=account_id)

    def _record_ticket(self, result: Result) -> str:
        ticket_id = (
            f"TKT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')[:17]}"
        )
        ident = result.identity.as_dict()
        row = {
            "ticket_id": ticket_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "scenario_id": result.scenario_id,
            "conversation_id": result.conversation_id,
            "customer": ident.get("customer_name"),
            "account_id": ident.get("account_id"),
            "serial_number": ident.get("serial_number"),
            "model": ident.get("model"),
            "revision": ident.get("revision"),
            "intent": result.intent,
            "outcome": result.outcome,
            "selected_procedure": result.selected_procedure,
            "accepted_chunk_ids": [v.chunk_id for v in result.accepted],
            "rejected_chunk_ids": [v.chunk_id for v in result.rejected],
            "validation": result.verdict.result,
            "validation_reason": result.verdict.reason,
            "grounding_verified": (result.grounding or {}).get("grounded"),
            "grounding_reason": (result.grounding or {}).get("reason"),
            "reflexion_attempts": len(result.reflexion),
            "unproductive_attempts": result.unproductive_attempts,
            "tool_calls": [t["tool"] for t in result.tool_calls],
            "case_id": result.case_id,
            "escalation_reason": result.escalation_reason,
            "rma_request_id": result.rma_request_id,
            "rma_status": result.rma_status,
            "generation_mode": result.generation_mode,
            "response": result.response,
        }
        os.makedirs(os.path.dirname(self.ticket_log_path), exist_ok=True)
        with open(self.ticket_log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return ticket_id

    def _finish(self, result: Result, conversation, loop) -> Result:
        result.steps = loop.steps
        result.unproductive_attempts = loop.unproductive
        result.reflexion = loop.reflexion_notes
        result.conversation_id = conversation.conversation_id
        result.ticket_id = self._record_ticket(result)
        conversation.record_turn(result.question, result)
        return result

    # -- the workflow ----------------------------------------------------

    def handle(self, message: str, account_id: Optional[str] = None,
               scenario_id: Optional[str] = None,
               conversation: Optional[memory_mod.Conversation] = None,
               verified_device: Optional[dict] = None,
               verified_customer_name: Optional[str] = None,
               ) -> Result:
        conv = conversation or self.new_conversation(account_id)
        if account_id and not conv.account_id:
            conv.account_id = account_id
        loop = react.ReasoningLoop(
            self.toolbox, prefer_llm=self.prefer_llm,
            escalations=self.escalations, approvals=self.approvals,
        )
        tool_calls = []

        def base(outcome, response, gen_mode, gen_engine, verdict, identity,
                 **kw):
            return Result(
                scenario_id=scenario_id, question=message, outcome=outcome,
                response=response, generation_mode=gen_mode,
                generation_engine=gen_engine, verdict=verdict,
                identity=identity, intent=intent, tool_calls=tool_calls, **kw
            )

        # A complaint always needs the troubleshooting path, even when it is
        # phrased as a delivery problem - there is an approved procedure for
        # a component missing from a delivery.
        from . import triage as triage_mod
        is_complaint = triage_mod.assess_complaint(message).is_complaint
        intent = tools_mod.classify_intent(message,
                                           is_complaint=is_complaint)

        # --- 1. safety, before anything else ---------------------------
        hits = self.safety_hits(message)
        loop._step(
            thought="Screen for safety or quality language before doing "
                    "anything else. A possible harm event is not a "
                    "troubleshooting problem.",
            action="safety.screen",
            observation=(f"{len(hits)} safety term(s) matched: {hits}"
                         if hits else "no safety terms matched"),
            detail={"matched_terms": hits},
        )

        # --- 2. identify the device ------------------------------------
        tr = self.toolbox.identify_device(message, conv.account_id,
                                          conv.confirmed_serial)
        tool_calls.append(tr.as_dict())
        identity = self._identity_from(message, conv)
        if verified_device is not None:
            # Operational SQLite is authoritative. The original checkpoint's
            # six-device JSON extract is not a second identity authority.
            row = verified_device
            device = erp_mod.Device(**{key: row.get(key) for key in (
                'serial_number', 'model', 'revision', 'status', 'install_date',
                'account_id', 'prescription_id', 'last_meter_recal')},
                customer_name=verified_customer_name or '')
            identity = erp_mod.IdentityResult(
                confirmed=True, device=device,
                reason='device supplied by current verified operational case context')
            tr = tools_mod.ToolResult(tool=tools_mod.IDENTIFY_DEVICE, productive=True,
                                      summary=identity.reason, data=identity.as_dict())
            tool_calls[-1] = tr.as_dict()
        loop._step(
            thought="Confirm exactly which unit this is. Model and revision "
                    "decide which procedures are even allowed.",
            action=tools_mod.IDENTIFY_DEVICE,
            observation=tr.summary,
            productive=tr.productive,
            detail=tr.data,
        )

        # Capture the symptom BEFORE the identification branch. Turn one of a
        # conversation often has the fault description but no serial; if the
        # symptom were only recorded on the confirmed path, the follow-up
        # that supplies the serial would have nothing left to search for.
        conv.note_symptom(symptom_text(message))

        if hits:
            return self._handle_safety(message, identity, hits, conv, loop,
                                       base)

        if not identity.confirmed:
            return self._handle_unidentified(message, identity, conv, loop,
                                             base)

        device = identity.device
        conv.remember_device(device)

        # --- 3. intent routing -----------------------------------------
        loop._step(
            thought="Pick tools for what was actually asked instead of "
                    "running everything.",
            action="route.classify_intent",
            observation=", ".join(k for k, v in intent.items() if v) or "none",
            detail=intent,
        )

        logistics_answer = None
        if intent["logistics"] or intent["order"]:
            o = self.toolbox.lookup_orders(device.account_id,
                                           device.serial_number)
            tool_calls.append(o.as_dict())
            loop._step(
                thought="The customer asked about an order or a delivery, so "
                        "look at the order record.",
                action=tools_mod.LOOKUP_ORDERS,
                observation=o.summary, productive=o.productive,
                detail={"orders": o.data.get("orders", [])},
            )
            s = self.toolbox.lookup_shipment(device.account_id,
                                             device.serial_number)
            tool_calls.append(s.as_dict())
            loop._step(
                thought="Check the shipment record for tracking and status.",
                action=tools_mod.LOOKUP_SHIPMENT,
                observation=s.summary, productive=s.productive,
                detail={"shipments": s.data.get("shipments", [])},
            )
            logistics_answer = (o, s)

        if not intent["troubleshooting"] and logistics_answer:
            return self._handle_logistics(message, identity, conv, loop, base,
                                          *logistics_answer)

        # --- 4. reviewed history (advisory only) ------------------------
        memory_note = ""
        memory_used = []
        mr = self.toolbox.recall_history(device, conv.accumulated_symptom()
                                         or message)
        tool_calls.append(mr.as_dict())
        loop._step(
            thought="Check whether a human-reviewed resolution already exists "
                    "for this unit. It is background only - it cannot supply "
                    "instructions and cannot override document control.",
            action=tools_mod.RECALL_HISTORY,
            observation=mr.summary, productive=mr.productive,
            detail=mr.data,
        )
        if mr.data.get("usable"):
            memory_used = mr.data["usable"]
            top = memory_used[0]
            memory_note = (
                f"A previous reviewed resolution on this same unit used "
                f"{top['procedure_id']}: {top['outcome_note']}"
            )

        # --- 5. retrieval ----------------------------------------------
        first_query = conv.accumulated_symptom() or message
        queries = [first_query]
        search = self.toolbox.search_qms(
            first_query, device,
            exclude_chunk_ids=conv.delivered_chunk_ids,
            prefer_procedures=conv.procedures_used,
        )
        tool_calls.append({"tool": search.tool, "productive": search.productive,
                           "summary": search.summary,
                           "data": {"query": first_query}})
        loop._step(
            thought="Search the controlled documents for this symptom, then "
                    "check every hit against the confirmed model and revision.",
            action=tools_mod.SEARCH_QMS,
            observation=search.summary,
            kind=RETRIEVAL, productive=search.productive,
            detail={k: v for k, v in search.data.items()
                    if k not in ("verdicts", "accepted_chunks")},
        )
        first_search = search

        if not search.productive and not search.data.get("procedure_exhausted"):
            loop._unproductive(
                thought="Nothing usable came back. The customer's wording may "
                        "be diluted by pleasantries; retry with just the "
                        "fault description.",
                action="retry.reformulate_query",
                observation="reformulating the search query",
                kind=RETRIEVAL,
                detail={"original_query": first_query},
            )
            retry_query = react.symptom_sentences(first_query)
            if retry_query and retry_query != first_query:
                queries.append(retry_query)
                search = self.toolbox.search_qms(
                    retry_query, device,
                    exclude_chunk_ids=conv.delivered_chunk_ids,
                    prefer_procedures=conv.procedures_used,
                )
                tool_calls.append(
                    {"tool": search.tool, "productive": search.productive,
                     "summary": search.summary,
                     "data": {"query": retry_query}})
                loop._step(
                    thought="Second retrieval attempt with the reformulated "
                            "query.",
                    action=tools_mod.SEARCH_QMS,
                    observation=search.summary,
                    kind=RETRIEVAL, productive=search.productive,
                    detail={k: v for k, v in search.data.items()
                            if k not in ("verdicts", "accepted_chunks")},
                )

        # Report the rejection evidence for the customer's own words.
        report_search = first_search if not search.productive else search
        verdicts = report_search.data["verdicts"]
        accepted_v = [v for v in verdicts if v.used]
        rejected_v = [v for v in verdicts if not v.used]
        filters = report_search.data["filters"]

        if not search.productive:
            return self._handle_no_source(
                message, identity, conv, loop, base, search, first_query,
                queries, accepted_v, rejected_v, filters,
                is_complaint=is_complaint,
            )

        accepted_chunks = search.data["accepted_chunks"]
        selected = search.data["selected_procedure"]

        # --- 6. draft, verify, correct ---------------------------------
        gen, report = loop.write_verified_reply(
            message, device, accepted_chunks, selected, memory_note
        )

        if gen is None:
            return self._handle_unverified(
                message, identity, conv, loop, base, report, selected,
                first_query, queries, accepted_v, rejected_v, filters,
            )

        # --- 7. final deterministic gate -------------------------------
        accepted_meta = [
            {"model": c.model, "applicable_revisions": c.applicable_revisions,
             "approval_status": c.approval_status}
            for c in accepted_chunks
        ]
        verdict = validation.validate_interaction(
            identity, accepted_meta, gen.text, selected
        )
        loop._step(
            thought="Final structured gate over the whole interaction.",
            action="validate.interaction",
            observation=f"{verdict.result} - {verdict.reason}",
            productive=verdict.passed,
            detail={"checks": verdict.checks},
        )

        conv.note_delivered([c.chunk_id for c in accepted_chunks])

        # Propose a resolution for human review. It is NOT usable until a
        # person approves it.
        proposed = self.reviewed_memory.propose(
            device, conv.accumulated_symptom() or message, selected,
            outcome_note=f"Grounded reply issued from {selected}.",
            source_ticket_id=None,
        )
        loop._step(
            thought="Offer this outcome for human review so it can become "
                    "reusable knowledge later. It is not recallable until "
                    "someone approves it.",
            action="memory.propose_resolution",
            observation=f"{proposed.memory_id} queued as "
                        f"{proposed.review_status}",
            detail={"memory_id": proposed.memory_id,
                    "review_status": proposed.review_status},
        )

        result = base(
            "grounded" if verdict.passed else "abstained", gen.text, gen.mode,
            gen.engine, verdict, identity,
            selected_procedure=selected, accepted=accepted_v,
            rejected=rejected_v, retrieval_query=first_query,
            retrieval_queries=queries, filters=filters,
            grounding=report.as_dict(), memory_used=memory_used,
            memory_proposed_id=proposed.memory_id,
        )
        return self._finish(result, conv, loop)

    # -- identity helper -------------------------------------------------

    def _identity_from(self, message, conv):
        probe = message or ""
        if conv.confirmed_serial and conv.confirmed_serial not in probe:
            probe = f"{probe}\nSerial number: {conv.confirmed_serial}"
        return erp_mod.identify_device(self.erp, probe, conv.account_id)

    # -- terminal branches -----------------------------------------------

    def _handle_safety(self, message, identity, hits, conv, loop, base):
        case = loop.escalate(
            cases.REASON_SAFETY,
            f"Possible safety or quality complaint. Matched: {', '.join(hits)}",
            identity, conv.conversation_id,
        )
        gen = generation.generate_abstention(
            identity.customer.customer_name if identity.customer else None,
            identity.device,
            reason=(
                "your message reports a possible safety issue "
                f"({', '.join(hits)}), and that has to be reviewed by a "
                "person rather than handled with a routine procedure."
            ),
            next_step=(
                f"I have raised this as a priority complaint record "
                f"({case.case_id}) with our safety team, and someone will "
                "contact you directly. Please stop using the unit and unplug "
                "it in the meantime."
            ),
        )
        verdict = validation.Verdict(
            "FAIL",
            "safety escalation: troubleshooting withheld pending human review",
            [{"check": "safety_screen", "result": "FAIL",
              "detail": f"matched {hits}"}],
        )
        result = base("escalated", gen.text, gen.mode, gen.engine, verdict,
                      identity, selected_procedure=None,
                      case_id=case.case_id, case_priority=case.priority,
                      escalation_reason=case.reason)
        return self._finish(result, conv, loop)

    def _handle_unidentified(self, message, identity, conv, loop, base):
        cands = ", ".join(
            f"{d.model} Rev {d.revision} ({d.serial_number})"
            for d in identity.candidates
        )
        loop._step(
            thought="Without a confirmed device I cannot know which procedures "
                    "apply, and guessing is the failure mode this system "
                    "exists to prevent. Ask rather than retrieve.",
            action="abstain.request_identification",
            observation="no retrieval attempted",
            detail={"candidates": [d.serial_number
                                   for d in identity.candidates]},
        )
        gen = generation.generate_abstention(
            identity.customer.customer_name if identity.customer else None,
            None,
            reason=f"{identity.reason}.",
            next_step=(
                "Could you send me the serial number from the label on the "
                "back of the unit? "
                + (f"Our records show these on your account: {cands}."
                   if cands else "")
            ).strip(),
        )
        conv.open_question = "awaiting serial number"
        verdict = validation.validate_interaction(identity, [], gen.text, None)
        result = base("abstained", gen.text, gen.mode, gen.engine, verdict,
                      identity, selected_procedure=None)
        return self._finish(result, conv, loop)

    def _handle_logistics(self, message, identity, conv, loop, base,
                          orders_tr, ship_tr):
        device = identity.device
        gen = generation.generate_logistics_answer(
            device.customer_name, device,
            orders_tr.data.get("orders", []),
            ship_tr.data.get("shipments", []),
        )
        loop._step(
            thought="This is an order status question. It is answered from "
                    "ERP and shipment records; the QMS corpus is irrelevant "
                    "and was not searched.",
            action="respond.logistics",
            observation=f"reply written by {gen.engine}",
            detail={"qms_searched": False},
        )
        verdict = validation.Verdict(
            "PASS",
            "order and shipment answer; no controlled procedure required",
            [{"check": "device_identity_confirmed", "result": "PASS",
              "detail": identity.reason},
             {"check": "qms_not_required", "result": "PASS",
              "detail": "logistics question answered from ERP records only"}],
        )
        result = base("answered", gen.text, gen.mode, gen.engine, verdict,
                      identity, selected_procedure=None)
        return self._finish(result, conv, loop)

    def _handle_no_source(self, message, identity, conv, loop, base, search,
                          first_query, queries, accepted_v, rejected_v,
                          filters, is_complaint=True):
        device = identity.device
        exhausted = search.data.get("procedure_exhausted")

        # No procedure matched, but nothing is broken either - the customer
        # asked a question. Raising a P3 engineering case for that would
        # bury the real faults, so answer plainly and leave it closed.
        if not is_complaint and not exhausted:
            loop._step(
                thought="No approved procedure matched, but no fault was "
                        "reported either. This is a question, not a case "
                        "for an engineer.",
                action="respond.no_procedure_needed",
                observation="answered without escalating",
                detail={"classification": "non-complaint enquiry"})
            gen = generation.generate_abstention(
                device.customer_name, device,
                reason=("this is not something our approved troubleshooting "
                        "procedures cover - they deal with faults rather "
                        "than general questions."),
                next_step=("If you tell me a bit more about what you need, "
                           "or if something is actually not working, I can "
                           "look again or put you in touch with the right "
                           "team."))
            verdict = validation.Verdict(
                "PASS",
                "no fault reported; no controlled procedure required",
                [{"check": "device_identity_confirmed", "result": "PASS",
                  "detail": identity.reason},
                 {"check": "procedure_not_required", "result": "PASS",
                  "detail": "the message reports no fault"}])
            result = base("answered", gen.text, gen.mode, gen.engine,
                          verdict, identity, selected_procedure=None,
                          accepted=accepted_v, rejected=rejected_v,
                          retrieval_query=first_query,
                          retrieval_queries=queries, filters=filters)
            return self._finish(result, conv, loop)

        rma_req = None
        if exhausted:
            case = loop.escalate(
                cases.REASON_LOOP_EXHAUSTED,
                f"Approved source sections for {device.model} Rev "
                f"{device.revision} have already been delivered. Field "
                f"completion has not been established.",
                identity, conv.conversation_id,
            )
            reason_text = (
                "I have no further approved guidance to add in this conversation. "
                "That does not establish that every physical check has been completed."
            )
            next_step = (
                f"I have raised {case.case_id} for a support engineer to review "
                "the checks and your observations before deciding the next action."
            )
            closing = ""
        else:
            case = loop.escalate(
                cases.REASON_NO_PROCEDURE,
                f"No approved procedure covers {device.model} Rev "
                f"{device.revision} for this symptom.",
                identity, conv.conversation_id,
            )
            reason_text = (
                "we do not have an approved procedure that covers "
                f"{device.model} revision {device.revision} for this symptom. "
                "The closest documents we hold are for a different model or "
                "revision, or have been withdrawn, and using one of those "
                "could give you the wrong instructions."
            )
            next_step = (
                f"I have passed this to a support engineer ({case.case_id}) "
                "who can work from the controlled documentation for your "
                "exact revision, and they will come back to you directly."
            )
            closing = ("I will not guess at steps from a procedure that does "
                       "not cover your device.")

        gen = generation.generate_abstention(
            device.customer_name, device, reason=reason_text,
            closing=closing, next_step=next_step,
        )
        verdict = validation.validate_interaction(identity, [], gen.text, None)
        result = base("abstained", gen.text, gen.mode, gen.engine, verdict,
                      identity, selected_procedure=None,
                      accepted=accepted_v, rejected=rejected_v,
                      retrieval_query=first_query, retrieval_queries=queries,
                      filters=filters, case_id=case.case_id,
                      case_priority=case.priority,
                      escalation_reason=case.reason,
                      rma_request_id=rma_req.request_id if rma_req else None,
                      rma_status=rma_req.status if rma_req else None)
        return self._finish(result, conv, loop)

    def _handle_unverified(self, message, identity, conv, loop, base, report,
                           selected, first_query, queries, accepted_v,
                           rejected_v, filters):
        device = identity.device
        case = loop.escalate(
            cases.REASON_UNVERIFIED,
            f"Drafted reply could not be verified against {selected}: "
            f"{report.reason if report else 'unknown'}",
            identity, conv.conversation_id,
        )
        gen = generation.generate_abstention(
            device.customer_name, device,
            reason=(
                "I was not able to produce an answer I could verify "
                "line-by-line against the approved procedure, and I will not "
                "send troubleshooting steps I cannot trace to a controlled "
                "document."
            ),
            next_step=(
                f"I have passed this to a support engineer ({case.case_id}) "
                "who will reply using the approved procedure directly."
            ),
        )
        verdict = validation.Verdict(
            "FAIL",
            f"response failed source verification: "
            f"{report.reason if report else 'unknown'}",
            [{"check": "response_grounded_in_source", "result": "FAIL",
              "detail": report.reason if report else "unknown"}],
        )
        result = base("escalated", gen.text, gen.mode, gen.engine, verdict,
                      identity, selected_procedure=selected,
                      accepted=accepted_v, rejected=rejected_v,
                      retrieval_query=first_query, retrieval_queries=queries,
                      filters=filters,
                      grounding=report.as_dict() if report else None,
                      case_id=case.case_id, case_priority=case.priority,
                      escalation_reason=case.reason)
        return self._finish(result, conv, loop)

    # -- comparison mode --------------------------------------------------

    def answer_without_retrieval(self, message: str,
                                 account_id: Optional[str] = None):
        """No QMS index is consulted at all. Used by the TEST 4 comparison."""
        identity = erp_mod.identify_device(self.erp, message, account_id)
        gen = generation.generate_ungrounded(
            message,
            identity.customer.customer_name if identity.customer else None,
            identity.device,
            prefer_llm=self.prefer_llm,
        )
        return gen, identity
