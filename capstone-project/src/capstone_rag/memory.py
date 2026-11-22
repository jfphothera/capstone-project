"""Two kinds of memory, with very different trust levels.

**Short-term (Conversation)** is scratch state for one support conversation.
It holds the confirmed device and the symptom text accumulated so far, so a
customer who gives their serial number in a second message does not have to
repeat the fault description. It is in-process and disappears when the
conversation ends.

**Long-term (ReviewedMemory)** is a durable store of past resolutions, and it
is deliberately hard to write to. A resolution enters as `pending_review` and
is invisible to the agent until a human approves it. Even then it is re-checked
against the live compatibility mapping at recall time, because a procedure that
was authorised when the memory was written may since have been withdrawn or
de-authorised for that revision.

The rule that matters: **recalled memory never supplies troubleshooting
content.** It can tell the agent and the reviewer that this unit had a similar
fault before and what procedure resolved it. The instructions themselves still
have to come from a QMS chunk that passes validation on this turn. A memory
entry cannot promote an unauthorised procedure, and cannot resurrect a
superseded one.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from . import config, embeddings

PENDING = "pending_review"
APPROVED = "approved"
REJECTED = "rejected"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- short-term ------------------------------------------------------------

@dataclass
class Conversation:
    """Working memory for a single support conversation."""

    conversation_id: str = field(
        default_factory=lambda: f"CONV-{uuid.uuid4().hex[:8].upper()}"
    )
    account_id: Optional[str] = None
    confirmed_serial: Optional[str] = None
    confirmed_model: Optional[str] = None
    confirmed_revision: Optional[str] = None
    symptom_history: list = field(default_factory=list)
    turns: list = field(default_factory=list)
    open_question: Optional[str] = None
    # Procedures already walked through with this customer, so a follow-up
    # gets the next step rather than the same one again.
    procedures_used: list = field(default_factory=list)
    # Individual procedure sections already sent, which is what makes
    # sequential troubleshooting across turns possible.
    delivered_chunk_ids: list = field(default_factory=list)

    def note_delivered(self, chunk_ids):
        for cid in chunk_ids:
            if cid not in self.delivered_chunk_ids:
                self.delivered_chunk_ids.append(cid)

    def note_symptom(self, text: str):
        text = (text or "").strip()
        if text and text not in self.symptom_history:
            self.symptom_history.append(text)

    def accumulated_symptom(self) -> str:
        """Every symptom description so far, oldest first.

        Used as the retrieval query, so a follow-up message of "it's still
        dead" still searches against the original fault description.
        """
        return "\n".join(self.symptom_history)

    def remember_device(self, device):
        if device is not None:
            self.confirmed_serial = device.serial_number
            self.confirmed_model = device.model
            self.confirmed_revision = device.revision

    def record_turn(self, message: str, result):
        self.turns.append({
            "turn": len(self.turns) + 1,
            "at": _now(),
            "message": message,
            "outcome": getattr(result, "outcome", None),
            "ticket_id": getattr(result, "ticket_id", None),
            "procedure": getattr(result, "selected_procedure", None),
        })
        proc = getattr(result, "selected_procedure", None)
        if proc and proc not in self.procedures_used:
            self.procedures_used.append(proc)

    def as_dict(self):
        return {
            "conversation_id": self.conversation_id,
            "account_id": self.account_id,
            "confirmed_serial": self.confirmed_serial,
            "confirmed_model": self.confirmed_model,
            "confirmed_revision": self.confirmed_revision,
            "turn_count": len(self.turns),
            "symptom_history": self.symptom_history,
            "procedures_used": self.procedures_used,
            "delivered_chunk_ids": self.delivered_chunk_ids,
            "open_question": self.open_question,
            "turns": self.turns,
        }


# --- long-term -------------------------------------------------------------

@dataclass
class Resolution:
    memory_id: str
    created_at: str
    account_id: Optional[str]
    serial_number: Optional[str]
    model: Optional[str]
    revision: Optional[str]
    symptom: str
    procedure_id: Optional[str]
    outcome_note: str
    review_status: str = PENDING
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    source_ticket_id: Optional[str] = None

    def as_dict(self):
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


@dataclass
class Recalled:
    resolution: Resolution
    similarity: float
    usable: bool
    reason: str

    def as_dict(self):
        return {
            "memory_id": self.resolution.memory_id,
            "symptom": self.resolution.symptom[:120],
            "procedure_id": self.resolution.procedure_id,
            "outcome_note": self.resolution.outcome_note,
            "reviewed_by": self.resolution.reviewed_by,
            "similarity": round(self.similarity, 4),
            "usable": self.usable,
            "reason": self.reason,
        }


class ReviewedMemory:
    """Append-only store of past resolutions, gated on human review."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or config.MEMORY_PATH

    # -- persistence ----------------------------------------------------

    def _all(self) -> list:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    out.append(Resolution.from_dict(json.loads(line)))
        return out

    def _rewrite(self, rows):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r.as_dict()) + "\n")

    # -- writing --------------------------------------------------------

    def propose(self, device, symptom: str, procedure_id: Optional[str],
                outcome_note: str, account_id=None,
                source_ticket_id=None) -> Resolution:
        """Record a candidate resolution. Not recallable until approved."""
        res = Resolution(
            memory_id=f"MEM-{uuid.uuid4().hex[:8].upper()}",
            created_at=_now(),
            account_id=account_id or (device.account_id if device else None),
            serial_number=device.serial_number if device else None,
            model=device.model if device else None,
            revision=device.revision if device else None,
            symptom=(symptom or "").strip(),
            procedure_id=procedure_id,
            outcome_note=outcome_note,
            review_status=PENDING,
            source_ticket_id=source_ticket_id,
        )
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(res.as_dict()) + "\n")
        return res

    def _set_status(self, memory_id, status, reviewer) -> Optional[Resolution]:
        rows = self._all()
        hit = None
        for r in rows:
            if r.memory_id == memory_id:
                r.review_status = status
                r.reviewed_by = reviewer
                r.reviewed_at = _now()
                hit = r
        if hit:
            self._rewrite(rows)
        return hit

    def approve(self, memory_id: str, reviewer: str) -> Optional[Resolution]:
        return self._set_status(memory_id, APPROVED, reviewer)

    def reject(self, memory_id: str, reviewer: str) -> Optional[Resolution]:
        return self._set_status(memory_id, REJECTED, reviewer)

    # -- reading --------------------------------------------------------

    def pending(self) -> list:
        return [r for r in self._all() if r.review_status == PENDING]

    def approved(self) -> list:
        return [r for r in self._all() if r.review_status == APPROVED]

    def recall(self, device, query: str, compatibility: dict,
               approved_procedures: set, k: int = 3,
               min_similarity: float = 0.45) -> list:
        """Find past resolutions for this device that are still valid today.

        Four gates, in order. An entry must be human-approved, must be for the
        same model and revision, must be semantically close to the current
        symptom, and its procedure must STILL be authorised and current. The
        last gate is the important one: it stops a stale memory from
        reintroducing a procedure that has since been withdrawn.
        """
        if device is None:
            return []

        candidates = [
            r for r in self.approved()
            if r.model == device.model and r.revision == device.revision
        ]
        if not candidates:
            return []

        vecs = embeddings.embed([query] + [c.symptom for c in candidates])
        sims = vecs[1:] @ vecs[0]
        order = np.argsort(-sims)[:k]

        out = []
        for i in order:
            r = candidates[int(i)]
            sim = float(sims[int(i)])
            if sim < min_similarity:
                continue

            authorised = compatibility.get(device.model_revision, [])
            if not r.procedure_id:
                usable, reason = False, "memory records no procedure"
            elif r.procedure_id not in authorised:
                usable, reason = False, (
                    f"procedure {r.procedure_id} is no longer authorised for "
                    f"{device.model_revision}; memory discarded"
                )
            elif r.procedure_id not in approved_procedures:
                usable, reason = False, (
                    f"procedure {r.procedure_id} is no longer an approved "
                    f"current document; memory discarded"
                )
            else:
                usable, reason = True, (
                    f"reviewed by {r.reviewed_by}; procedure still authorised "
                    f"and current"
                )
            out.append(Recalled(resolution=r, similarity=sim, usable=usable,
                                reason=reason))
        return out
