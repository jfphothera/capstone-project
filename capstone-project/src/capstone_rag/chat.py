"""The customer-facing chatbot's logic, kept out of the Streamlit view.

The conversation state is persisted to the database on every turn, so a
restart does not lose where the customer had got to, and the state is linked
to the customer, the device and (once handed over) the support case.

Handoff rules. The bot hands over, rather than continuing, when any of these
is true:

  * a safety or quality concern is raised
  * a return, replacement or any other consequential transaction is needed
  * identification is contradictory
  * no approved procedure covers the confirmed device
  * every approved step has been tried and the fault persists
  * the customer asks for a person

On handoff it opens a support case carrying the whole conversation, the
identification given, the sources retrieved, the steps already tried, the
safety flags and the reason.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

from . import (
    caseflow, config, identity as identity_mod, movement as movement_mod,
    store as store_mod, triage as triage_mod,
    activity, guidance, generation, response_policy,
)

OPENING = (
    "Hello, you have reached device support. I can help with routine "
    "questions and with approved troubleshooting steps for your unit.\n\n"
    "What is happening with your unit, and what model or serial number is "
    "on its label? I can use the applicable public user manual for basic help. "
    "If you need account information or an account-specific action, I will "
    "ask for the details needed to verify your identity."
)

ASK_FOR_PERSON = ("speak to a person", "speak to someone", "speak to a human",
                  "talk to a person", "talk to someone", "real person",
                  "human being", "representative", "manager", "agent please")

TRIED_ALREADY = ("already tried", "i tried", "ive tried", "i've tried",
                 "tried that", "done that", "did that", "already done",
                 "that didn't work", "that did not work", "no change",
                 "still not", "still dead", "still broken")

# An explicit statement that the customer cannot supply what was asked for.
# There is no point spending the rest of the budget on further questions.
CANNOT_PROVIDE = (
    "i cannot", "i can't", "i cant", "i don't have", "i dont have",
    "do not have", "don't know", "dont know", "do not know", "no idea",
    "not sure", "cannot find", "can't find", "cant find", "lost it",
    "not with me", "away from", "not at home", "unable to", "nothing on",
    "there is no label", "no label", "cannot read", "can't read",
    "worn off", "rubbed off",
)


@dataclass
class ChatSession:
    conversation_id: str
    claim: identity_mod.Claim = field(default_factory=identity_mod.Claim)
    account_id: Optional[str] = None
    customer_name: Optional[str] = None
    device: Optional[dict] = None
    verified: bool = False
    verification_points: list = field(default_factory=list)
    turns: list = field(default_factory=list)
    steps_tried: list = field(default_factory=list)
    delivered_chunk_ids: list = field(default_factory=list)
    procedures_used: list = field(default_factory=list)
    symptoms: list = field(default_factory=list)
    sources_shown: list = field(default_factory=list)
    safety_flags: list = field(default_factory=list)
    handed_off: bool = False
    handoff_case_id: Optional[str] = None
    handoff_reason: Optional[str] = None
    # Identification bookkeeping (defect C4 / C5).
    awaiting_points: list = field(default_factory=list)
    identification_attempts: int = 0
    corrections: list = field(default_factory=list)
    verification_history: list = field(default_factory=list)
    cannot_provide: bool = False

    def as_dict(self):
        d = dict(self.__dict__)
        d["claim"] = dict(self.claim.__dict__)
        return d

    @classmethod
    def from_dict(cls, d):
        claim = identity_mod.Claim(**(d.pop("claim", {}) or {}))
        s = cls(conversation_id=d.pop("conversation_id"), claim=claim)
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s


def _corrections_text(s: "ChatSession") -> str:
    """Readable correction history for the handed-over case body."""
    if not s.corrections:
        return "none"
    return "; ".join(
        f"{c['field']}: {c['from']!r} -> {c['to']!r}" for c in s.corrections)


@dataclass
class Reply:
    text: str
    sources: list = field(default_factory=list)
    handed_off: bool = False
    handoff_case_id: Optional[str] = None
    reason: str = ""
    generation_mode: str = 'template'
    public_context: dict = field(default_factory=dict)
    conversation_goal: str = ''


class ChatBot:
    def __init__(self, store, agent):
        self.store = store
        self.agent = agent
        self.flow = caseflow.CaseFlow(store, agent)

    # -- session persistence --------------------------------------------

    def new_session(self) -> ChatSession:
        cid = self.store.next_id("conversation", "CONV-{:06d}")
        # The opening message asks for identification, so the very first
        # reply is already an answer to an identity question and a bare name
        # or date in it should be read as one.
        s = ChatSession(
            conversation_id=cid,
            awaiting_points=list(identity_mod.VERIFICATION_POINTS))
        self._save(s)
        return s

    def load_session(self, conversation_id) -> Optional[ChatSession]:
        row = self.store.one(
            "SELECT * FROM conversations WHERE conversation_id=?",
            (conversation_id,))
        if not row:
            return None
        return ChatSession.from_dict(json.loads(row["state_json"]))

    def _save(self, s: ChatSession):
        now = store_mod.now()
        existing = self.store.one(
            "SELECT 1 FROM conversations WHERE conversation_id=?",
            (s.conversation_id,))
        row = {
            "conversation_id": s.conversation_id,
            "case_id": s.handoff_case_id, "run_id": None,
            "account_id": s.account_id, "channel": "chat",
            "created_at": now if not existing else None,
            "updated_at": now,
            "state_json": json.dumps(s.as_dict(), default=str),
        }
        if existing:
            row.pop("created_at")
            self.store.update("conversations", "conversation_id",
                              s.conversation_id, row)
        else:
            self.store.insert("conversations", row)

    # -- identification --------------------------------------------------

    def _absorb(self, s: ChatSession, text: str) -> dict:
        """Take any new identification, including an explicit correction.

        A detail supplied once is not final. When the customer gives a
        different value for a field they have already answered, the new value
        replaces the old one, the old one is kept in `corrections`, and the
        session drops back to unverified so the change is re-checked against
        the ERP rather than inheriting an earlier verified state.
        """
        known_serials = [row["serial_number"] for row in self.store.query(
            "SELECT serial_number FROM devices")]
        fresh = identity_mod.Claim.from_text(
            text, known_serials=known_serials, expecting=s.awaiting_points,
            known_names=[row['customer_name'] for row in self.store.query('SELECT customer_name FROM customers')])
        activity.emit('identity', 'Customer identity fields extracted', evidence={
            'received': fresh.__dict__, 'tentative_model': guidance.tentative_model(text)})
        signal = identity_mod.correction_signal(text)
        added, changed, cleared = [], [], []

        for fld in ("name", "dob", "serial", "phone", "email"):
            new, old = getattr(fresh, fld), getattr(s.claim, fld)
            if not new:
                continue
            if not old:
                setattr(s.claim, fld, new)
                added.append(fld)
            elif new != old:
                setattr(s.claim, fld, new)
                changed.append(fld)
                s.corrections.append(
                    {"at": store_mod.now(), "field": fld, "from": old,
                     "to": new, "stated_as_correction": signal["corrects"]})

        if fresh.serial:
            s.claim.serial_issue = None
            s.claim.serial_input = fresh.serial_input
        elif fresh.serial_issue:
            old = s.claim.serial or s.claim.serial_input
            s.claim.serial = None
            s.claim.serial_issue = fresh.serial_issue
            s.claim.serial_input = fresh.serial_input
            if "serial" not in added and "serial" not in changed:
                (changed if old else added).append("serial")
        s.claim.dob_issue = fresh.dob_issue if not fresh.dob else None

        # "Sorry, I gave you the wrong serial number" names the mistake
        # without supplying the replacement. Holding on to the wrong value
        # would keep reporting it as a contradiction forever.
        for fld in signal["fields"]:
            if fld in changed or fld in added:
                continue
            if getattr(s.claim, fld, None):
                s.corrections.append(
                    {"at": store_mod.now(), "field": fld,
                     "from": getattr(s.claim, fld), "to": None,
                     "stated_as_correction": True})
                setattr(s.claim, fld, None)
                if fld == "serial":
                    s.claim.serial_issue = None
                    s.claim.serial_input = None
                cleared.append(fld)

        if changed or cleared:
            s.account_id = None
            s.verified = False
            s.verification_points = []
            if "serial" in changed or "serial" in cleared:
                s.device = None
            self.store.audit(
                "chat_identification_corrected", actor="customer",
                case_id=s.handoff_case_id,
                summary=f"{', '.join(changed + cleared)} corrected; identity "
                        f"must be verified again",
                detail={"conversation_id": s.conversation_id,
                        "corrections": s.corrections[-3:]})

        return {"added": added, "changed": changed, "cleared": cleared,
                "correction_signal": signal}

    def _verify(self, s: ChatSession):
        res = identity_mod.verify(self.store, s.claim,
                                  account_id=s.account_id)
        s.account_id = res.account_id or s.account_id
        s.customer_name = res.customer_name or s.customer_name
        s.verified = res.verified
        s.verification_points = res.points_matched
        s.verification_history = (s.verification_history + [{
            "at": store_mod.now(), "verified": res.verified,
            "points_matched": res.points_matched,
            "points_failed": res.points_failed,
            "account_id": res.account_id, "reason": res.reason,
        }])[-10:]
        if res.serial_number:
            s.device = self.store.device(res.serial_number)
        elif res.account_id and res.verified and not s.device:
            owned = self.store.devices_for_account(res.account_id)
            if len(owned) == 1:
                s.device = owned[0]
        return res

    # -- the turn --------------------------------------------------------

    def respond(self, s: ChatSession, text: str) -> Reply:
        with activity.investigation(self.store, conversation_id=s.conversation_id,
                                    case_id=s.handoff_case_id, turn_id='TURN-' + uuid.uuid4().hex):
            activity.emit('message', 'Customer message received', evidence={'text': text})
            from .orchestration import ConversationCoordinator
            return ConversationCoordinator(self).run(s,text)

    def _respond(self, s: ChatSession, text: str) -> Reply:
        s.turns.append({"at": store_mod.now(), "role": "customer",
                        "text": text})
        points_before = list(s.verification_points)
        delta = self._absorb(s, text)
        low = text.lower()

        if s.handed_off:
            # A handoff is not a safety-screening exemption. Append to the
            # existing case rather than opening a second case or losing facts.
            t = triage_mod.triage(text)
            activity.emit('safety', 'Post-handoff safety screening', evidence=t.as_dict())
            self.store.insert('messages', {'message_id':self.store.next_id('message','CHAT-FOLLOWUP-{:08d}'),
                'case_id':s.handoff_case_id,'created_at':store_mod.now(),'direction':'inbound','channel':'chat',
                'author':'chat customer','body':text,'status':'received'})
            if triage_mod.requires_immediate_escalation(t):
                s.safety_flags.append(t.classification)
                self.flow._raise_quality_notification(s.handoff_case_id,t)
                self.store.update('cases','case_id',s.handoff_case_id,{'priority':'Critical',
                    'safety_hold':1,'is_adverse_event':int(t.adverse_event.is_adverse_event),'status':caseflow.HUMAN_REVIEW,
                    'priority_reason':t.priority_reason})
                return self._finish(s, Reply('Please stop using the unit and unplug it. I have added this safety concern to your existing support case for urgent review.'))
            reply = Reply(
                "Your case is with our support team now "
                f"({s.handoff_case_id}). They have everything from this "
                "conversation and will contact you directly. Is there "
                "anything else I can note for them?")
            return self._finish(s, reply)

        # 1. Safety first, every turn, before anything else.
        t = triage_mod.triage(text)
        activity.emit('safety', 'Safety screening and initial classification', evidence=t.as_dict())
        self._note_symptom(s, text, t)
        if triage_mod.requires_immediate_escalation(t):
            s.safety_flags.append(t.classification)
            return self._handoff(
                s, text,
                reason=f"safety: {t.priority_reason}",
                message=(
                    "Thank you for telling me, and I am sorry this has "
                    "happened. Because what you have described could be a "
                    "safety issue I am not going to work through any "
                    "troubleshooting with you. I have passed this straight "
                    "to our support team as a priority, and someone will "
                    "contact you directly.\n\nPlease stop using the unit and "
                    "unplug it in the meantime."))

        # 2. The customer asking for a person is always honoured.
        if any(p in low for p in ASK_FOR_PERSON):
            return self._handoff(
                s, text, reason="customer asked for a person",
                message=("Of course. I have passed this to our support team "
                         "with everything we have discussed, and someone "
                         "will come back to you."))

        # 3. Note what they have already tried, so we do not repeat it.
        if any(p in low for p in TRIED_ALREADY):
            s.steps_tried.append(text[:160])

        res = self._verify(s)
        activity.emit('verification', 'Identity verification evaluated',
                      'completed' if res.verified else 'pending', res.as_dict())
        if s.device:
            activity.emit('device', 'Exact equipment and revision evaluated', evidence={
                'device': s.device, 'revision': identity_mod.assess_revision(s.device).as_dict()})
        activity.emit('authorization', 'Information and troubleshooting scopes evaluated', evidence=guidance.authorization(
            s.verified, s.device, s.device and identity_mod.assess_revision(s.device).usable_for_troubleshooting))
        from . import support_workflow
        query = '\n'.join(s.symptoms) or text
        # Equipment identification is independent of account authorization.
        public_device = self.store.device(s.claim.serial) if s.claim.serial else s.device
        plan = support_workflow.investigate(query, store=self.store, agent=self.agent,
            claim=s.claim, verified=s.verified, device=public_device,
            delivered=s.delivered_chunk_ids, interface='chat')
        protected_request = bool(re.search(r'\btracking|shipment status|my order|refund|warranty|billing|refill code|unlock code|service history', query, re.I))
        if not protected_request and not res.contradictions:
            manual = plan.get('manual')
            if manual and manual['chunks'] and (not s.verified or
                    not public_device or not identity_mod.assess_revision(public_device).usable_for_troubleshooting
                    or re.search(r'user manual|user guide|basic operation', query, re.I)):
                return self._public_manual_reply(s, query, plan)
            if (public_device or plan.get('model')) and not guidance.specific_symptom(query):
                return self._finish(s, Reply(plan['question'],conversation_goal='clarify_symptom',
                    public_context={'model':plan.get('model'),'ordinary_public_help':True}))
            if plan.get('model') and plan['action']=='request_information':
                return self._finish(s, Reply(plan['question'],conversation_goal='clarify_observation',
                    public_context={'model':plan.get('model'),'ordinary_public_help':True}))
        if res.contradictions:
            return self._handoff(
                s, text, reason=f"identification contradictory: {res.reason}",
                message=("Some of the details do not match what I can see, "
                         "and I do not want to guess which account this is. "
                         "I have passed this to our support team to check "
                         "with you directly."))

        if not s.verified:
            if re.search(r'where.*(?:serial|label)|find.*(?:serial|label)', low):
                return self._finish(s, Reply(guidance.LABEL_GUIDANCE))
            return self._identification_turn(s, text, low, t, res, delta,
                                             points_before)

        s.awaiting_points = []
        s.identification_attempts = 0
        self._note_symptom(s, text, t)

        # 4. Logistics questions are answered from records.
        if t.classification == "order_status_enquiry" and s.account_id:
            return self._finish(s, Reply(self._order_answer(s)))

        # 5. Anything needing product movement goes to a person.
        decision = movement_mod.decide(
            text, t, applicable_procedure=None, device=s.device,
            identity_verified=s.verified)
        # MR-6 returns "none" when a procedure is still to try, but the
        # chatbot has not searched yet at this point, so only a movement
        # that is certain regardless of troubleshooting is handed over here.
        if decision.needs_transaction:
            return self._handoff(
                s, text,
                reason=f"product movement needed ({decision.movement_type}, "
                       f"rule {decision.rule})",
                message=("Thanks - from what you have described this needs "
                         "something sent out or brought back, and that has "
                         "to be arranged and approved by one of our team "
                         "rather than by me. I have passed it on with the "
                         "details, and they will confirm what happens "
                         "next."))

        if not s.device:
            s.awaiting_points = [identity_mod.POINT_SERIAL]
            return self._finish(s, Reply(
                ('Thank you for clarifying. I have updated the name. ' if delta['changed'] else '') +
                "I have the account details, but need to identify which unit this is about. "
                "Could you give me the serial number from the label on the "
                "back of the unit?"))

        # Clarification is not a procedure search or a failed recovery attempt.
        query = '\n'.join(s.symptoms) or text
        if guidance.administrative(query):
            return self._finish(s, Reply(
                'I can help clarify the next step. Is your question about obtaining a refill or unlock code, '
                'or about using the device? Please share the message you see, if there is one.'))
        if not guidance.specific_symptom(query):
            activity.emit('clarification', 'Symptom too broad for controlled troubleshooting; asking a diagnostic question')
            return self._finish(s, Reply('Thank you. ' + guidance.SYMPTOM_QUESTION))

        # 6. Revision provenance gates revision-specific guidance.
        rev = identity_mod.assess_revision(s.device)
        if t.complaint.is_complaint and not rev.usable_for_troubleshooting:
            return self._handoff(
                s, text,
                reason=f"device revision {rev.status}: {rev.reason}",
                message=("Before I give you any steps I need a colleague to "
                         "confirm the exact hardware revision of your unit - "
                         "our record is not definite, and the steps differ "
                         "between revisions. I have passed this on so they "
                         "can check and come back to you."))

        # 7. Grounded troubleshooting, or handoff.
        return self._troubleshoot(s, text, t)

    # -- identification ----------------------------------------------------

    def _identification_turn(self, s, text, low, t, res, delta,
                             points_before) -> Reply:
        """Ask again, or stop asking and fetch a person (defect C4).

        A customer is never left in the same question twice over. Progress is
        measured as *a new identifying detail or a new verification point*,
        not as "the customer replied", so a turn that adds nothing counts
        against a bounded budget. When the budget runs out - or when the
        customer says they cannot supply what was asked for, or the details
        read fine but match no record - the conversation is handed to a
        representative with everything collected so far, as a real case.
        """
        self._note_symptom(s, text, t)

        progressed = bool(delta["added"] or delta["changed"]
                          or len(res.points_matched) > len(points_before))
        asked_before = bool(s.awaiting_points)
        if not progressed:
            s.identification_attempts += 1
        if (asked_before and not progressed
                and any(p in low for p in CANNOT_PROVIDE)):
            s.cannot_provide = True

        supplied = sum(1 for f in ("name", "dob", "serial")
                       if getattr(s.claim, f))
        # Read correctly, matches nothing: more questions of the same kind
        # cannot help, so the budget here is shorter and the wording differs.
        unmatched = res.account_id is None and supplied >= 2
        limit = (config.MAX_UNMATCHED_IDENTIFICATION_ATTEMPTS if unmatched
                 else config.MAX_IDENTIFICATION_ATTEMPTS)

        if s.cannot_provide:
            return self._handoff(
                s, text,
                reason=("identification could not be established: the "
                        "customer cannot supply the details requested"),
                message=(
                    "That is not a problem, and I do not want to keep asking "
                    "you for something you do not have to hand. I have passed "
                    "this to a member of our support team together with "
                    "everything you have told me, and they can look it up "
                    "another way and come back to you."))

        if s.identification_attempts >= limit:
            if unmatched:
                reason = (f"identification unresolved: details supplied "
                          f"({supplied} of 3) match no record in the ERP")
                message = identity_mod.unmatched_message(res)
            else:
                reason = (f"identification unresolved after "
                          f"{s.identification_attempts} unproductive "
                          f"attempt(s); {res.reason}")
                message = (
                    "I have not been able to confirm the account from what I "
                    "have, and I would rather get a person involved than keep "
                    "asking you the same question. I have passed this to our "
                    "support team with the whole conversation, and someone "
                    "will come back to you.")
            return self._handoff(s, text, reason=reason, message=message)

        # The wording reflects how many *earlier* exchanges went nowhere, so
        # the first question does not open with "I still cannot find it".
        asked = max(s.identification_attempts - 1, 0)
        # Stored separately from the question: the question names one or two
        # points, but anything still usable is accepted if volunteered.
        # Received and verified are different states. Never ask for a received
        # value as though it were absent; a mismatch is a clarification instead.
        received = {'customer_name': s.claim.name, 'date_of_birth': s.claim.dob,
                    'serial_number': s.claim.serial}
        remaining = [p for p in (identity_mod.POINT_SERIAL, identity_mod.POINT_NAME,
                                 identity_mod.POINT_DOB)
                     if not received[p] and p in res.usable_points]
        s.awaiting_points = list(identity_mod.VERIFICATION_POINTS)
        if s.claim.dob_issue:
            ask = ('Could you please write your date of birth with the month spelled out, '
                   'for example 18 April 1954? I want to make sure I read the date correctly.')
        elif remaining:
            wanted = remaining[:2 if not supplied else 1]
            ask = 'Could you please provide ' + ' and '.join(
                identity_mod._FRIENDLY_POINT[p] for p in wanted) + '?'
        else:
            ask = ('I could not confirm a match from those details. Could you check the spelling '
                   'of the account name and the serial on the device label?')
        if delta['changed'] or delta['cleared']:
            prefix = 'Thank you for clarifying. I have updated the details. '
        elif delta['added']:
            labels = {'name': 'name', 'dob': 'date of birth', 'serial': 'serial number',
                      'phone': 'contact number', 'email': 'email address'}
            prefix = 'Thank you. I have received your ' + ' and '.join(labels[p] for p in delta['added']) + '. '
            if res.account_id is None and supplied >= 2:
                prefix += 'I have not confirmed a match yet. '
        elif not supplied:
            prefix = ("I'm sorry you're having trouble with your device. I'll be happy to help. "
                      if not any(turn['role'] == 'assistant' for turn in s.turns)
                      else 'To help identify the right equipment, ')
        else:
            prefix = 'To continue checking the details, '
        return self._finish(s, Reply(prefix + ask))

    # -- helpers ----------------------------------------------------------

    def _note_symptom(self, s, text, t):
        if (t.complaint.is_complaint or t.classification.startswith("complaint")
                or guidance.specific_symptom(text)
                or re.search(r'not working|isn.t working|isnt working|refill|unlock|guide|order|tracking', text, re.I)):
            if text not in s.symptoms:
                s.symptoms.append(text)

    def _order_answer(self, s) -> str:
        orders = self.store.query(
            "SELECT * FROM orders WHERE account_id=? ORDER BY order_date DESC",
            (s.account_id,))
        ships = self.store.query(
            "SELECT * FROM source_shipments WHERE account_id=? "
            "ORDER BY shipped_date DESC", (s.account_id,))
        if not orders and not ships:
            return ("I cannot see an open order on your account. If you were "
                    "expecting one, tell me roughly when it was placed and I "
                    "will pass it to the team to check.")
        parts = []
        if ships:
            sh = ships[0]
            parts.append(f"Your most recent shipment is {sh['tracking_number']} "
                         f"with {sh['carrier']}, showing as {sh['status']} "
                         f"({sh['last_scan']}).")
        if orders:
            o = orders[0]
            parts.append(f"It is against order {o['order_id']} for a "
                         f"{o['product_prescribed']}, currently "
                         f"{o['distributor_status']}.")
        return " ".join(parts)

    def _troubleshoot(self, s, text, t) -> Reply:
        query = "\n".join(s.symptoms) or text
        search = self.agent.toolbox.search_qms(
            query, self._device_view(s),
            exclude_chunk_ids=s.delivered_chunk_ids,
            # Without this the agent would not know it is partway through a
            # procedure, and would call it exhausted after the first batch
            # of steps instead of continuing to the next ones.
            prefer_procedures=s.procedures_used)
        activity.emit('retrieval', search.summary, evidence=search.data)

        if not search.productive:
            if search.data.get("procedure_exhausted"):
                return self._handoff(
                    s, text,
                    reason="all approved steps tried, fault persists",
                    message=("We have now been through every approved step I "
                             "have for your unit and it is still not right. "
                             "I have passed this to our support team to take "
                             "further. They will review the case and confirm "
                             "the appropriate next step; I cannot authorize "
                             "a repair, replacement or return."))
            return self._handoff(
                s, text,
                reason="no approved procedure covers this device and symptom",
                message=('Thank you for explaining the problem. Your equipment needs a closer review '
                         'before I can suggest the right steps. I have shared the details with '
                         'our support team so you can continue from here without repeating them.'))

        chunks = search.data["accepted_chunks"]
        procedure = search.data["selected_procedure"]
        gen, report = self._write(s, text, chunks, procedure)
        activity.emit('grounding', 'Controlled response validation',
                      'completed' if gen else 'rejected', evidence={
                          'report': report, 'generation_mode': gen.mode if gen else None})
        if gen is None:
            return self._handoff(
                s, text,
                reason=f"reply could not be verified against {procedure}",
                message=("I could not put together an answer I was able to "
                         "check line by line against the approved procedure, "
                         "and I will not send troubleshooting I cannot "
                         "stand behind. I have passed this to our support "
                         "team."))

        s.delivered_chunk_ids.extend(c.chunk_id for c in chunks)
        if procedure and procedure not in s.procedures_used:
            s.procedures_used.append(procedure)
        sources = [{"procedure_id": c.procedure_id,
                    "document_id": c.document_id,
                    "chunk_id": c.chunk_id,
                    "section": c.section} for c in chunks]
        s.sources_shown.extend(sources)
        return self._finish(s, Reply(gen.text, sources=sources, generation_mode=gen.mode))

    def _device_view(self, s):
        view = _DeviceView(s.device)
        view.customer_name = s.customer_name
        return view

    def _public_manual_reply(self, s, question, plan):
        from . import react, qms_policy, support_workflow
        manual = plan['manual']
        row = dict(plan.get('device') or {})
        row.setdefault('model', plan['model'] or 'general device support')
        device = _DeviceView(row)
        device.customer_name = s.claim.name or 'there'
        loop = react.ReasoningLoop(self.agent.toolbox, prefer_llm=self.agent.prefer_llm)
        gen, report = support_workflow.write_public_reply(self.agent, question, plan, s.claim.name)
        if gen is None:
            activity.emit('grounding', 'Public manual response failed grounding', 'rejected', report)
            return self._finish(s, Reply('I have located the user guide. Could you describe the exact indicator or message you see so I can focus the next check?'))
        disclosure = qms_policy.disclosure_check(gen.text, manual['chunks'], context=manual['context'])
        activity.emit('disclosure', 'Public manual response checked for access, use and disclosure',
                      'completed' if disclosure['passed'] else 'rejected', disclosure)
        if not disclosure['passed']:
            return self._finish(s, Reply('I am checking which instructions apply to the equipment. Could you confirm the model printed on its label?'))
        s.delivered_chunk_ids.extend(c.chunk_id for c in manual['chunks'] if c.chunk_id not in s.delivered_chunk_ids)
        sources = [{'document_id': c.document_id, 'procedure_id': c.procedure_id,
                    'section': c.section, 'chunk_id': c.chunk_id, 'scope': c.scope,
                    'model': c.model} for c in manual['chunks']]
        s.sources_shown.extend(sources)
        return self._finish(s, Reply(gen.text, sources=sources,
                                      generation_mode=gen.mode, public_context={**manual['context'],'ordinary_public_help':True}))

    def _write(self, s, text, chunks, procedure):
        from . import react
        loop = react.ReasoningLoop(self.agent.toolbox,
                                   prefer_llm=self.agent.prefer_llm)
        tried = ("The customer has already tried: "
                 + "; ".join(s.steps_tried)) if s.steps_tried else ""
        return loop.write_verified_reply(
            text, self._device_view(s), chunks, procedure, memory_note=tried)

    def _handoff(self, s, text, reason, message) -> Reply:
        """Open a support case carrying everything from the conversation."""
        transcript = "\n\n".join(
            f"[{t['role']}] {t['text']}" for t in s.turns)
        body = (
            f"Handed over from the customer chatbot.\n"
            f"Reason: {reason}\n\n"
            f"Identification supplied: name={s.claim.name!r} "
            f"dob={'yes' if s.claim.dob else 'no'} serial={s.claim.serial!r}\n"
            f"Verified: {s.verified} on {s.verification_points}\n"
            f"Identification attempts that added nothing: "
            f"{s.identification_attempts}\n"
            f"Corrections the customer made: {_corrections_text(s)}\n"
            f"Device: {(s.device or {}).get('model')} "
            f"Rev {(s.device or {}).get('revision')} "
            f"({(s.device or {}).get('revision_status')})\n"
            f"Steps the customer already tried: "
            f"{s.steps_tried or 'none recorded'}\n"
            f"Sources already shown: "
            f"{[x['procedure_id'] for x in s.sources_shown] or 'none'}\n"
            f"Safety flags: {s.safety_flags or 'none'}\n\n"
            f"--- conversation ---\n{transcript}")

        case_id = self.flow.intake(
            subject='[chat handoff] Customer support conversation',
            body=body, channel="chat", account_id=s.account_id,
            author=s.customer_name or "chat customer",
            screening_text='\n'.join(turn['text'] for turn in s.turns if turn['role'] == 'customer'))
        s.handed_off = True
        s.handoff_case_id = case_id
        s.handoff_reason = reason
        activity.link_case(case_id)
        activity.emit('escalation', 'Conversation handed to representative', 'awaiting_human_review',
                      {'reason': reason, 'case_id': case_id})

        self.store.update("conversations", "conversation_id",
                          s.conversation_id, {"case_id": case_id})
        self.store.audit("chat_handoff", actor="chatbot", case_id=case_id,
                         summary=reason,
                         detail={"conversation_id": s.conversation_id,
                                 "verified": s.verified,
                                 "steps_tried": s.steps_tried})
        reply = Reply(message, handed_off=True, handoff_case_id=case_id,
                      reason=reason)
        return self._finish(s, reply)

    def _compose_dialogue(self, s, reply, facts):
        from .grounding import DEVICE_ACTION_TERMS
        needed=[term for term in ('serial number','date of birth','name on the account') if term in reply.text.lower()]
        goal=reply.conversation_goal or ('explain recorded handoff' if s.handed_off else
            'request missing identification details' if needed else 'acknowledge received information and ask the next useful question')
        context={'conversation':s.turns[-6:],'customer_report':s.claim.__dict__,
            'identity_verified':s.verified,'equipment_model':reply.public_context.get('model') or (s.device or {}).get('model'),
            'ordinary_public_help':facts['ordinary_public_help'],'goal':goal,'requested_information':needed,
            'diagnostic_dimensions':['whether the display or power indicator illuminates','whether the lamps start','any display message']
                if goal in ('clarify_symptom','clarify_observation') else [],
            'handoff_reason':s.handoff_reason,'case_reference':s.handoff_case_id}
        system=('Compose a concise customer-service chat reply from the structured investigation context. '
            'Acknowledge the latest information naturally. Ask only the next useful question. '
            'For a diagnostic goal, ask about one specific listed diagnostic dimension, not a broad repeat of the complaint. '
            'Ordinary public help does not require an account name or date of birth. '
            'Do not add operating instructions, diagnoses, private account facts or approval promises. '
            'No repeated greeting or fixed opening. Only the finished reply, under 100 words.')
        for attempt in range(2):
            try:
                candidate=generation.compose_customer(system,json.dumps(context))
            except Exception as exc:
                activity.emit('fallback','Conversational composition failed; using the safe next-action fallback',evidence={'error':str(exc)})
                return None
            verdict=response_policy.validate(candidate,**facts)
            patterns={'serial number':r'\bserial\b','date of birth':r'date of birth|birthdate|\bdob\b','name on the account':r'account.{0,15}name|name.{0,20}account|full name'}
            if not candidate or len(candidate.split())>120 or any(not re.search(patterns[term],candidate,re.I) for term in needed):
                verdict['failures'].append({'fact':'retain the required next information request'})
            if goal in ('clarify_symptom','clarify_observation') and not re.search(r'power|turn on|display|lamp|indicator|message',candidate,re.I):
                verdict['failures'].append({'fact':'ask a concrete diagnostic observation about the display, power indicator or lamps'})
            added=[term for term in DEVICE_ACTION_TERMS if re.search(r'\b'+re.escape(term),candidate,re.I) and term.lower() not in reply.text.lower()]
            if added:
                verdict['failures'].append({'fact':'no ungrounded operating actions in an information request','conflicting_text':added})
            verdict['passed']=not verdict['failures']
            activity.emit('response_validation','Conversational composition checked','completed' if verdict['passed'] else 'rejected',
                {**verdict,'attempt':attempt+1})
            if verdict['passed']:
                return candidate
            context['correction_feedback']=verdict['failures']
            activity.emit('reflexion','Requesting bounded conversational correction','retrying',{'attempt':attempt+1,'feedback':verdict['failures']})
        return None

    def _finish(self, s, reply: Reply) -> Reply:
        facts = dict(verified=s.verified, name=s.customer_name if s.verified else None,
                     supplied_name=s.claim.name, serial=s.claim.serial,
                     device=s.device, revision_confirmed=bool(s.device and identity_mod.assess_revision(s.device).usable_for_troubleshooting),
                     ordinary_public_help=bool(reply.public_context.get('ordinary_public_help')))
        mode = reply.generation_mode
        # The model composes from typed facts and the investigation's next goal.
        # Existing deterministic text is a fallback, not a rewrite prompt.
        if not reply.sources and self.agent.prefer_llm and not s.safety_flags:
            try:
                if generation.llm_available():
                    candidate = self._compose_dialogue(s,reply,facts)
                    if candidate:
                        reply.text, mode = candidate, 'llm'
            except Exception as exc:
                activity.emit('fallback', 'Using decided response after local generation failure', 'completed', {'error': str(exc)})
        verdict = response_policy.validate(reply.text, **facts)
        from . import qms, qms_policy
        source_ids = {source.get('chunk_id') for source in reply.sources}
        grounding_sources = [c for c in self.agent.toolbox.index.chunks if c.chunk_id in source_ids]
        disclosure = qms_policy.disclosure_check(reply.text, grounding_sources,
            context=reply.public_context or {'model': (s.device or {}).get('model'),
                'revision': (s.device or {}).get('revision'),
                'revision_context': identity_mod.revision_context(s.device) if s.device else {},
                'compatibility': self.agent.toolbox.compatibility})
        activity.emit('disclosure', 'Customer disclosure policy checked before release',
                      'completed' if disclosure['passed'] else 'rejected', disclosure)
        if not disclosure['passed']:
            verdict['passed'] = False
            verdict['failures'].extend(disclosure['failures'])
        activity.emit('consistency', 'Final customer response checked against received and verified facts',
                      'completed' if verdict['passed'] else 'rejected', verdict)
        if not verdict['passed']:
            activity.emit('draft_rejected', 'Contradictory response preserved', 'rejected', {'response': reply.text, 'validation': verdict})
            reply.text = ('Thank you for the information. I am checking the details you have shared '
                          'so I can give you the right next step.')
            mode = 'corrected-template'
        reply.text = generation.customer_text(reply.text)
        reply.text = re.sub(r'\n\s*(?:Kind regards|Best regards),?\s*\nCustomer Support\s*$', '',reply.text,flags=re.I).strip()
        if any(t['role']=='assistant' for t in s.turns):
            reply.text=re.sub(r'^(?:Hello|Hi)\b[^\n]*,\s*\n\s*','',reply.text,flags=re.I)
        reply.generation_mode = mode
        activity.emit('response', 'Final customer response selected', evidence={
            'text': reply.text, 'generation_mode': mode, 'model_response_used': mode == 'llm',
            'case_id': s.handoff_case_id, 'sources': reply.sources})
        s.turns.append({"at": store_mod.now(), "role": "assistant",
                        "text": reply.text})
        self._save(s)
        return reply


class _DeviceView:
    """Adapts a device row to the attribute access the tools expect."""

    def __init__(self, row):
        self._row = row or {}
        self.serial_number = self._row.get("serial_number")
        self.model = self._row.get("model")
        self.revision = self._row.get("revision")
        self.customer_name = None
        self.account_id = self._row.get("account_id")
        self.status = self._row.get("status")

    @property
    def model_revision(self):
        return f"{self.model}|{self.revision}"
