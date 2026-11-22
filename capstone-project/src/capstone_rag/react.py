"""Bounded ReAct-style reasoning loop with Reflexion-style self-correction.

Each iteration records a Thought (why this tool), an Action (the tool call)
and an Observation (what came back), so the whole decision path is readable
afterwards. The policy choosing the next action is deterministic Python, not a
model: the set of tools is small and closed, and the brief asks for
deterministic controls wherever they suffice. The language model is used for
one thing only - writing prose - and its output is verified afterwards by code.

The escalation budget counts **unproductive attempts**: a retrieval that
yields nothing usable, or a draft that fails grounding verification. Working
through three legitimate troubleshooting steps is not three attempts. When the
budget is spent, or the agent runs out of options, the case goes to a human.

Reflexion enters at the verification step. A draft that fails grounding is not
discarded silently - the specific unsupported sentences become written feedback
that is fed back into the next attempt, and the whole exchange stays in the
trace. If a second attempt still fails, the agent stops trying to write prose
and falls back to a deterministic writer that can only emit source text. If
even that fails, it escalates rather than sending something unverified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import (
    cases,
    config,
    generation,
    grounding,
    tools as tools_mod,
    validation,
)

DETERMINISTIC = "deterministic"
RETRIEVAL = "semantic-retrieval"
GENERATION = "generation"
VERIFICATION = "verification"

_SENT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class LoopStep:
    n: int
    thought: str
    action: str
    observation: str
    kind: str = DETERMINISTIC
    productive: bool = True
    detail: dict = field(default_factory=dict)

    # Aliases so existing trace renderers keep working unchanged.
    @property
    def name(self) -> str:
        return self.action

    @property
    def summary(self) -> str:
        return self.observation

    def as_dict(self):
        return {
            "n": self.n,
            "thought": self.thought,
            "action": self.action,
            "observation": self.observation,
            "kind": self.kind,
            "productive": self.productive,
            "detail": self.detail,
        }


def symptom_sentences(text: str) -> str:
    """Keep only the sentences that describe a fault.

    Used to reformulate a query after a retrieval miss. Courtesy and
    administrative lines dilute the embedding; dropping them is a cheap and
    deterministic second attempt.
    """
    kept = []
    for raw in _SENT.split(" ".join((text or "").split())):
        low = raw.lower()
        if any(t in low for t in tools_mod.TROUBLESHOOT_TERMS):
            kept.append(raw.strip())
    return " ".join(kept) if kept else (text or "").strip()


class ReasoningLoop:
    """One pass of the agent over one customer message."""

    def __init__(self, toolbox, prefer_llm=True, escalations=None,
                 approvals=None, max_unproductive=None):
        self.tools = toolbox
        self.prefer_llm = prefer_llm
        self.escalations = escalations or cases.EscalationQueue()
        self.approvals = approvals or cases.ApprovalQueue()
        self.max_unproductive = (config.MAX_UNPRODUCTIVE_ATTEMPTS
                                 if max_unproductive is None
                                 else max_unproductive)
        self.steps: list = []
        self.unproductive = 0
        self.reflexion_notes: list = []

    # -- trace helpers ---------------------------------------------------

    def _step(self, thought, action, observation, kind=DETERMINISTIC,
              productive=True, detail=None) -> LoopStep:
        step = LoopStep(
            n=len(self.steps) + 1, thought=thought, action=action,
            observation=observation, kind=kind, productive=productive,
            detail=detail or {},
        )
        self.steps.append(step)
        from . import activity
        activity.emit('tool' if kind != GENERATION else 'generation',
                      f'{action}: {observation}',
                      'completed' if productive else 'retrying',
                      {'action': action, 'kind': kind, 'observation': observation,
                       'evidence': detail or {}})
        return step

    def _unproductive(self, thought, action, observation, kind=DETERMINISTIC,
                      detail=None):
        self.unproductive += 1
        step = self._step(thought, action, observation, kind,
                          productive=False, detail=detail or {})
        step.detail["unproductive_attempts"] = self.unproductive
        step.detail["budget"] = self.max_unproductive
        return step

    @property
    def budget_spent(self) -> bool:
        return self.unproductive >= self.max_unproductive

    # -- verification + Reflexion ----------------------------------------

    def _verify(self, text, accepted_chunks, device, procedure, reported_text='', public_context=False):
        report = grounding.check_grounding(text, accepted_chunks, device,
                                           procedure, reported_text=reported_text)
        if public_context:
            from . import response_policy
            permission=response_policy.validate(text,ordinary_public_help=True)
            if not permission['passed']:
                report.grounded=False
                report.authorization_issues=permission['failures']
            report.context_issues=response_policy.conversation_consistency(text,reported_text)
            if report.context_issues:
                report.grounded=False
        self._step(
            thought="A drafted reply has to be checked against the source "
                    "before it can be sent. Verification is separate from "
                    "generation and does not ask the model to judge itself.",
            action="verify.grounding",
            observation=("verified: " + report.reason if report.grounded
                         else "REJECTED: " + report.reason),
            kind=VERIFICATION,
            productive=report.grounded,
            detail=report.as_dict(),
        )
        return report

    def write_verified_reply(self, message, device, accepted_chunks,
                             procedure, memory_note="", public_context=False):
        """Draft, verify, and correct. Returns (generation, report) or (None, report).

        Attempt 1 - the language model, unaided.
        Attempt 2 - the language model, given the specific sentences that
                    failed and told to fix them (Reflexion).
        Attempt 3 - the deterministic writer, which can only emit sentences
                    lifted from the accepted source and therefore cannot
                    fabricate.
        """
        gen = generation.generate_grounded(
            message, device.customer_name, device, accepted_chunks, procedure,
            prefer_llm=self.prefer_llm, extra_context=memory_note,
            public_context=public_context,
        )
        self._step(
            thought=f"An approved source is available ({procedure}). Draft a "
                    f"reply grounded in those extracts.",
            action="generate.draft",
            observation=f"draft written by {gen.engine}",
            kind=GENERATION if gen.mode == "llm" else DETERMINISTIC,
            detail={"mode": gen.mode, "engine": gen.engine,
                    "chunk_ids": [c.chunk_id for c in accepted_chunks]},
        )
        report = self._verify(gen.text, accepted_chunks, device, procedure, message, public_context)
        if report.grounded:
            return gen, report

        # --- Reflexion: feed the failure back in ------------------------
        feedback = report.feedback()
        self.reflexion_notes.append({
            "attempt": 1,
            "failure": report.reason,
            "feedback": feedback,
        })
        self._unproductive(
            thought="The draft contains material I cannot trace to the "
                    "approved source. I will not send it. Record what failed "
                    "and try again with that feedback.",
            action="reflexion.record_feedback",
            observation=f"{len(report.unsupported)} unsupported sentence(s), "
                        f"{len(report.identifier_issues)} identifier issue(s)",
            kind=VERIFICATION,
            detail={"feedback": feedback},
        )

        if self.prefer_llm and generation.llm_available() \
                and not self.budget_spent:
            gen2 = generation.generate_grounded(
                message, device.customer_name, device, accepted_chunks,
                procedure, prefer_llm=True, extra_context=memory_note,
                correction_feedback=feedback,
                public_context=public_context,
            )
            self._step(
                thought="Retry with the specific corrections required.",
                action="generate.redraft_with_feedback",
                observation=f"corrected draft written by {gen2.engine}",
                kind=GENERATION,
                detail={"mode": gen2.mode,
                        "feedback_given": feedback},
            )
            report2 = self._verify(gen2.text, accepted_chunks, device,
                                   procedure, message, public_context)
            if report2.grounded:
                return gen2, report2
            self.reflexion_notes.append({
                "attempt": 2,
                "failure": report2.reason,
                "feedback": report2.feedback(),
            })
            self._unproductive(
                thought="The corrected draft still fails verification. Stop "
                        "asking the model for prose.",
                action="reflexion.abandon_llm",
                observation="falling back to the deterministic writer",
                kind=VERIFICATION,
                detail={"failure": report2.reason},
            )

        if self.budget_spent:
            return None, report

        # --- deterministic fallback -------------------------------------
        gen3 = generation.generate_grounded(
            message, device.customer_name, device, accepted_chunks, procedure,
            prefer_llm=False, extra_context=memory_note,
            public_context=public_context,
        )
        self._step(
            thought="The deterministic writer only emits sentences taken from "
                    "the accepted extracts, so it cannot introduce anything "
                    "unsupported.",
            action="generate.deterministic_fallback",
            observation=f"reply written by {gen3.engine}",
            kind=DETERMINISTIC,
            detail={"mode": gen3.mode},
        )
        report3 = self._verify(gen3.text, accepted_chunks, device, procedure, message, public_context)
        if report3.grounded:
            return gen3, report3

        self._unproductive(
            thought="Even the deterministic fallback cannot be verified. "
                    "Something is wrong with the source material; a person "
                    "needs to look at this.",
            action="reflexion.fallback_failed",
            observation=report3.reason,
            kind=VERIFICATION,
            detail={"failure": report3.reason},
        )
        return None, report3

    # -- escalation ------------------------------------------------------

    def escalate(self, reason, summary, identity, conversation_id=None):
        case = self.escalations.raise_case(
            reason=reason, summary=summary, identity=identity,
            conversation_id=conversation_id,
        )
        self._step(
            thought="This cannot be closed safely by the agent.",
            action="escalate.raise_case",
            observation=f"{case.case_id} [{case.priority}] {reason} -> "
                        f"{case.status}",
            detail={"case_id": case.case_id, "priority": case.priority,
                    "reason": reason, "status": case.status},
        )
        return case

    def request_rma(self, device, justification, procedure_id=None,
                    case_id=None):
        """Request a return authorisation. Cannot grant one."""
        req = self.approvals.request_return_authorisation(
            device, justification, procedure_id=procedure_id, case_id=case_id,
        )
        self._step(
            thought="A return is a consequential action. I can prepare the "
                    "request but a person has to approve it before anything "
                    "is promised to the customer.",
            action="approval.request_return_authorisation",
            observation=f"{req.request_id} created with status {req.status}; "
                        f"no RMA number issued",
            detail={"request_id": req.request_id, "status": req.status,
                    "is_authorised": req.is_authorised},
        )
        return req
