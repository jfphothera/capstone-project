"""The tools the reasoning loop can choose between.

Every tool is ordinary deterministic Python over the local synthetic stores,
except `search_qms`, which runs the real embedding-based retrieval. None of
them reaches a live system.

Each returns a ToolResult whose `productive` flag drives the loop's escalation
budget. Productive means "this call produced information the agent did not
already have". A lookup that comes back empty, or a retrieval where every
candidate is rejected, is unproductive, and three of those end the loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import config, erp as erp_mod, identity as identity_mod, qms_policy, validation

# Tool names, used in the trace and in routing.
IDENTIFY_DEVICE = "erp.identify_device"
LOOKUP_ORDERS = "erp.lookup_orders"
LOOKUP_SHIPMENT = "erp.lookup_shipment"
SEARCH_QMS = "qms.semantic_search"
RECALL_HISTORY = "memory.recall_resolutions"


@dataclass
class ToolResult:
    tool: str
    productive: bool
    summary: str
    data: dict = field(default_factory=dict)

    def as_dict(self):
        return {"tool": self.tool, "productive": self.productive,
                "summary": self.summary, "data": self.data}


class Toolbox:
    def __init__(self, erp, index, compatibility, reviewed_memory=None):
        self.erp = erp
        self.index = index
        self.compatibility = compatibility
        self.memory = reviewed_memory
        # Procedures whose documents are currently Approved. Used to re-check
        # a recalled memory against today's corpus.
        self.approved_procedures = {
            c.procedure_id for c in index.chunks
            if c.approval_status in validation.CURRENT_STATUSES
        }

    # -- ERP ------------------------------------------------------------

    def identify_device(self, message: str, account_id=None,
                        known_serial=None) -> ToolResult:
        """Resolve exactly one device, or say honestly that it cannot.

        `known_serial` comes from short-term memory: a serial confirmed
        earlier in this conversation. It is appended to the text the extractor
        sees, so a follow-up message that omits the serial still resolves.
        """
        probe = message or ""
        if known_serial and known_serial not in probe:
            probe = f"{probe}\nSerial number: {known_serial}"

        identity = erp_mod.identify_device(self.erp, probe, account_id)
        return ToolResult(
            tool=IDENTIFY_DEVICE,
            productive=identity.confirmed,
            summary=(
                f"confirmed {identity.device.model} Rev "
                f"{identity.device.revision} ({identity.device.serial_number})"
                if identity.confirmed
                else f"not confirmed - {identity.reason}"
            ),
            data={**identity.as_dict(),
                  "used_remembered_serial": bool(
                      known_serial and known_serial not in (message or ""))},
        )

    def lookup_orders(self, account_id=None, serial=None) -> ToolResult:
        rows = []
        if serial:
            rows = self.erp.orders_for_serial(serial)
        if not rows and account_id:
            rows = self.erp.orders_for_account(account_id)
        from . import activity
        store = activity.active_store()
        if store:
            rows = store.query('SELECT * FROM orders WHERE account_id=? ORDER BY order_date DESC', (account_id,))
        return ToolResult(
            tool=LOOKUP_ORDERS,
            productive=bool(rows),
            summary=(f"{len(rows)} order record(s) found" if rows
                     else "no order records for this customer or device"),
            data={"orders": rows},
        )

    def lookup_shipment(self, account_id=None, serial=None,
                        order_id=None) -> ToolResult:
        rows = []
        if order_id:
            rows = [s for s in self.erp.shipments if s["order_id"] == order_id]
        if not rows and serial:
            rows = self.erp.shipments_for_serial(serial)
        if not rows and account_id:
            rows = self.erp.shipments_for_account(account_id)
        from . import activity
        store = activity.active_store()
        if store:
            rows = store.query('SELECT * FROM source_shipments WHERE account_id=? ORDER BY shipped_date DESC', (account_id,))
        summary = "no shipment records found"
        if rows:
            first = rows[0]
            summary = (f"{len(rows)} shipment(s); most recent "
                       f"{first['tracking_number']} via {first['carrier']} - "
                       f"{first['status']}")
        return ToolResult(
            tool=LOOKUP_SHIPMENT,
            productive=bool(rows),
            summary=summary,
            data={"shipments": rows},
        )

    # -- QMS ------------------------------------------------------------

    @staticmethod
    def _section_index(chunk_id: str) -> int:
        """Order within the parent document, from the '#S<n>-' in the id."""
        m = re.search(r"#S(\d+)", chunk_id or "")
        return int(m.group(1)) if m else 9999

    def _undelivered_by_procedure(self, authorised, exclude_chunk_ids,
                                  exclude_procedures) -> dict:
        """Every still-unused section of each authorised procedure.

        Built from the whole index rather than the search results, because a
        later step of the right procedure may score poorly against the
        customer's wording and still be the correct next thing to do.
        """
        out = {}
        for c in self.index.chunks:
            if c.procedure_id not in authorised:
                continue
            if c.procedure_id in exclude_procedures:
                continue
            if c.approval_status not in validation.CURRENT_STATUSES:
                continue
            if validation.CUSTOMER_SERVICE_AUDIENCE not in c.audiences:
                continue
            if c.chunk_id in exclude_chunk_ids:
                continue
            out.setdefault(c.procedure_id, []).append(c)
        return out

    def search_qms(self, query: str, device, exclude_procedures=(),
                   exclude_chunk_ids=(), prefer_procedures=(),
                   search_k=None) -> ToolResult:
        """Semantic retrieval, then deterministic validation of each hit.

        Search is deliberately unfiltered so that wrong-revision and withdrawn
        documents are visibly considered and visibly rejected, rather than
        silently never appearing.

        `exclude_chunk_ids` carries the steps already given to this customer
        earlier in the conversation, so a follow-up gets the *next* step of the
        procedure instead of a repeat of the first.
        """
        exclude_chunk_ids = set(exclude_chunk_ids)
        revision_context = identity_mod.revision_context(device)
        # Widen the sweep when earlier steps are excluded, otherwise the
        # already-delivered chunks crowd out the ones we still need.
        k = search_k or (config.SEARCH_K + 2 * len(exclude_chunk_ids))
        hits = [hit for hit in self.index.search_procedures(
            query, k=len(self.index.chunks))
            if hit.chunk.scope == 'exact_model_revision'][:k]
        best_similarity = hits[0].score if hits else None
        verdicts = [
            validation.validate_chunk(h, device, self.compatibility,
                                      getattr(self.index,'minimum_similarity',config.MIN_SIMILARITY),
                                      best_similarity=best_similarity,
                                      max_score_gap=
                                      config.MAX_RELATIVE_SCORE_GAP,
                                      query=query,
                                      revision_context=revision_context)
            for h in hits
        ]
        by_chunk = {h.chunk.chunk_id: h.chunk for h in hits}

        passing = [v for v in verdicts if v.accepted
                   and v.procedure_id not in exclude_procedures
                   and v.chunk_id not in exclude_chunk_ids]
        preferred_family = validation.expected_procedure_family(query)
        if preferred_family and preferred_family != "UNSUPPORTED_CODE":
            passing.sort(key=lambda v: (
                preferred_family not in v.procedure_id, v.rank))

        # Which procedure to ground on. Semantic rank picks it, except that a
        # procedure already started with this customer wins, so a multi-turn
        # session finishes one procedure before starting another instead of
        # alternating between them.
        authorised = self.compatibility.get(device.model_revision, [])
        undelivered = self._undelivered_by_procedure(
            authorised, exclude_chunk_ids, exclude_procedures)

        selected = None
        for proc in prefer_procedures:
            if undelivered.get(proc):
                selected = proc
                break
        if selected is None:
            for v in passing:
                if undelivered.get(v.procedure_id):
                    selected = v.procedure_id
                    break
        if selected is None and passing:
            selected = passing[0].procedure_id

        # Retrieval decides WHICH procedure applies. The controlled document
        # decides the order its steps are performed in, so once a procedure
        # is chosen the agent walks the whole document in section order rather
        # than serving whichever fragments happened to rank highest. Without
        # this a customer was given "symptom confirmation, step 1, step 4" and
        # never saw steps 2 and 3.
        ordered = sorted(undelivered.get(selected, []),
                         key=lambda c: self._section_index(c.chunk_id))
        accepted_chunks = ordered[:config.ACCEPT_K]
        chosen_ids = {c.chunk_id for c in accepted_chunks}

        # A section chosen by document order may not have been in the
        # similarity top-k at all. It still has to appear in the evidence, so
        # synthesise a verdict for it rather than let it be used invisibly.
        seen = {v.chunk_id for v in verdicts}
        for c in accepted_chunks:
            if c.chunk_id in seen:
                continue
            verdicts.append(validation.ChunkVerdict(
                chunk_id=c.chunk_id,
                procedure_id=c.procedure_id,
                document_id=c.document_id,
                accepted=True,
                similarity=float("nan"),
                # Never appeared in the semantic ranking, so it sorts after
                # everything that did rather than ahead of it.
                rank=9999,
                selected_by="document_order",
            ))

        for v in verdicts:
            if not v.accepted:
                continue
            if v.chunk_id in chosen_ids:
                v.used = True
            elif v.chunk_id in exclude_chunk_ids:
                v.not_used_reason = (
                    "already given to this customer earlier in the "
                    "conversation; moving on to the next step"
                )
            elif v.procedure_id in exclude_procedures:
                v.not_used_reason = (
                    f"{v.procedure_id} already used earlier in this "
                    "conversation; looking for the next step"
                )
            elif v.procedure_id != selected:
                v.not_used_reason = (
                    f"agent grounded on {selected}; mixing procedures is not "
                    "permitted"
                )
            else:
                v.not_used_reason = (
                    f"beyond accept_k={config.ACCEPT_K} for this turn; will "
                    "be offered next"
                )

        filters = {
            "model": device.model,
            "revision": device.revision,
            "revision_context": revision_context,
            "approval_status_in": list(validation.CURRENT_STATUSES),
            "required_audience": validation.CUSTOMER_SERVICE_AUDIENCE,
            "authorised_procedures": self.compatibility.get(
                device.model_revision, []),
            "min_similarity": config.MIN_SIMILARITY,
            "max_relative_score_gap": config.MAX_RELATIVE_SCORE_GAP,
            "excluded_procedures": list(exclude_procedures),
            "excluded_chunks": sorted(exclude_chunk_ids),
        }
        # Exhausted means the troubleshooting path we were on has run out:
        # steps have already been given, the procedures we were working
        # through have nothing left, and no other procedure matched this
        # symptom. It is NOT "every authorised procedure has been consumed" -
        # a device can have an authorised procedure for an unrelated problem
        # (a missing accessory, say) that will never be relevant to this
        # fault, and waiting for it to be delivered would mean the return
        # was never proposed.
        in_progress_remaining = any(undelivered.get(p)
                                    for p in prefer_procedures)
        exhausted = (bool(authorised) and bool(exclude_chunk_ids)
                     and not accepted_chunks and not in_progress_remaining)

        return ToolResult(
            tool=SEARCH_QMS,
            productive=bool(accepted_chunks),
            summary=(
                f"{len(accepted_chunks)} chunk(s) accepted from {selected}"
                if accepted_chunks else
                "every applicable step has already been given to this "
                "customer; the procedure is exhausted" if exhausted else
                f"all {len(verdicts)} candidates rejected for "
                f"{device.model} Rev {device.revision}"
            ),
            data={
                "query": query,
                "filters": filters,
                "selected_procedure": selected,
                "procedure_exhausted": exhausted,
                "verdicts": verdicts,
                "accepted_chunks": accepted_chunks,
                "general_chunks": [
                    h.chunk for h in self.index.search(query, k=len(self.index.chunks))
                    if h.chunk.scope in ('public_general', 'model_family', 'model_independent')
                    and qms_policy.authorize(
                        h.chunk, operation='customer_answer', recipient='customer',
                        context={'model': device.model, 'revision': device.revision,
                                 'revision_context': revision_context,
                                 'compatibility': self.compatibility}).allowed
                    and h.score >= getattr(self.index, 'public_manual_similarity', .12)
                ][:config.ACCEPT_K],
                "candidates": [
                    {"rank": h.rank, "chunk_id": h.chunk.chunk_id,
                     "procedure_id": h.chunk.procedure_id,
                     "model": h.chunk.model,
                     "revisions": h.chunk.applicable_revisions,
                     "status": h.chunk.approval_status,
                     "audiences": h.chunk.audiences,
                     "similarity": round(h.score, 4)}
                    for h in hits
                ],
            },
        )

    # -- memory ----------------------------------------------------------

    def recall_history(self, device, query: str) -> ToolResult:
        """Look for a human-approved past resolution on this exact device."""
        if self.memory is None or device is None:
            return ToolResult(RECALL_HISTORY, False,
                              "no reviewed memory store configured")
        recalled = self.memory.recall(
            device, query, self.compatibility, self.approved_procedures
        )
        usable = [r for r in recalled if r.usable]
        discarded = [r for r in recalled if not r.usable]
        return ToolResult(
            tool=RECALL_HISTORY,
            productive=bool(usable),
            summary=(
                f"{len(usable)} reviewed resolution(s) still valid"
                + (f", {len(discarded)} discarded as stale" if discarded else "")
                if recalled else "no reviewed history for this device"
            ),
            data={"recalled": [r.as_dict() for r in recalled],
                  "usable": [r.as_dict() for r in usable],
                  "discarded": [r.as_dict() for r in discarded]},
        )


# --- intent routing --------------------------------------------------------
# Deterministic keyword routing, so the loop picks tools relevant to what was
# actually asked rather than running everything. Kept as plain Python on
# purpose: the brief asks for deterministic controls where they suffice, and
# intent here is a small closed set.

LOGISTICS_TERMS = (
    "ship", "shipped", "shipping", "shipment", "track", "tracking", "deliver",
    "delivery", "delivered", "dispatch", "courier", "parcel", "arrive",
    "arrived", "order status", "where is my", "when will", "eta",
)
ORDER_TERMS = (
    "order", "invoice", "purchase", "backorder", "reorder", "prescription",
    "buy", "price", "cost", "how much", "quote", "spare",
)
TROUBLESHOOT_TERMS = (
    "not working", "won't", "wont", "will not", "broken", "fault", "error",
    "fail", "failed", "dead", "power", "lamp", "noise", "hot", "stops",
    "stopped", "reset", "flicker", "dim", "shut", "problem", "issue",
    "trouble", "repair", "fix", "light", "screen", "timer", "rattl",
)


MISSING_TERMS = (
    "missing", "did not come with", "didn't come with", "was not included",
    "wasn't included", "not in the box", "never arrived", "short shipped",
)


def classify_intent(message: str, is_complaint: bool = False) -> dict:
    """Which concerns does this message raise? Several can be true at once.

    `is_complaint` comes from triage. A missing-component complaint mentions
    a delivery, so a purely lexical reading routes it as logistics and the
    QMS is never consulted - but there IS an approved procedure for a
    component missing from a delivery, so a complaint always counts as
    needing the troubleshooting path as well.
    """
    low = (message or "").lower()
    logistics = any(t in low for t in LOGISTICS_TERMS)
    order = any(t in low for t in ORDER_TERMS)
    trouble = (any(t in low for t in TROUBLESHOOT_TERMS)
               or any(t in low for t in MISSING_TERMS)
               or is_complaint)
    # A message that is clearly neither still gets troubleshooting as the
    # default, because that is what a support agent is for.
    if not (logistics or order or trouble):
        trouble = True
    return {"logistics": logistics, "order": order, "troubleshooting": trouble}
