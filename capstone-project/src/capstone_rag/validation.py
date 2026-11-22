"""Deterministic validation of retrieved sources and the final response.

Every check here is exact structured comparison - string and set membership
against the ERP record and the compatibility mapping. There is no model in
this module, and no check is delegated to one. A retrieved chunk can be the
single most semantically relevant passage in the corpus and still be rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Document statuses that may be used to generate customer-facing instructions.
CURRENT_STATUSES = ("Approved",)
CUSTOMER_SERVICE_AUDIENCE = "customer_service"


def expected_procedure_family(query: str) -> Optional[str]:
    """Return an explicit fault family, or ``None`` when wording is broad.

    Semantic search discovers candidates. This narrow deterministic check
    prevents a high-ranking lamp document from answering an explicit no-power
    report (and vice versa) without pretending to understand vague reports.
    """
    low = (query or "").lower()
    if (any(x in low for x in ("missing from", "not in the box",
                               "wasn't included", "was not included"))
            or ("missing" in low and any(part in low for part in
                ("goggles", "cord", "filter", "lamp", "manual", "accessory", "component")))):
        return "MISSING"
    if any(x in low for x in ("panel is noticeably dimmer",
                              "panels are noticeably", "flicker",
                              "output is uneven", "although the unit powers on",
                              "but the power indicator is on",
                              "while the supply light stays on")):
        return "LAMP"
    if any(x in low for x in ("overheat", "overheating", "too hot",
                              "excessive heat", "thermal shutdown")):
        return "THERM"
    if "timer" in low and any(x in low for x in ("reset", "restart")):
        return "TIMER"
    if any(x in low for x in ("completely dead", "no lights", "no fan",
                              "no power", "won't turn on",
                              "will not turn on", "does not power")):
        return "PWR"
    if any(x in low for x in ("screen shows a code", "code i don't recognize",
                              "code i do not recognize")):
        return "UNSUPPORTED_CODE"
    return None

# A document whose `model` is ALL applies to every model. Used only for
# genuinely model-independent procedures, such as the one covering a
# component missing from a delivery, which is about what was in the carton
# rather than about the hardware. It is still subject to every other check:
# revision applicability, approval status, and the compatibility mapping.
WILDCARD_MODEL = "ALL"


@dataclass
class ChunkVerdict:
    chunk_id: str
    procedure_id: str
    document_id: str
    accepted: bool
    similarity: float
    rank: int
    failures: list = field(default_factory=list)
    denial_codes: list = field(default_factory=list)
    # Set once the agent has picked which single procedure to ground on.
    # A chunk can pass every check and still not be used, either because the
    # agent grounded on a different procedure or because it fell beyond
    # ACCEPT_K. That is not a rejection and is reported separately.
    used: bool = False
    not_used_reason: str = ""
    # "similarity" (the chunk was in the semantic top-k) or "document_order"
    # (the chunk is the next section of a procedure already in progress, and
    # was taken from the document rather than from the search result).
    selected_by: str = "similarity"

    @property
    def status(self) -> str:
        if not self.accepted:
            return "REJECT"
        return "USED" if self.used else "NOT-USED"

    @property
    def reason(self) -> str:
        if not self.accepted:
            return "; ".join(self.failures)
        if self.used:
            if self.selected_by == "document_order":
                return ("next section of the procedure already in progress; "
                        "selected in document order, not by similarity")
            return "authorised for this device and used for grounding"
        return self.not_used_reason or "authorised but not selected"

    def as_dict(self):
        sim = self.similarity
        return {
            "chunk_id": self.chunk_id,
            "procedure_id": self.procedure_id,
            "document_id": self.document_id,
            "similarity": (None if sim != sim else round(sim, 4)),
            "rank": self.rank,
            "selected_by": self.selected_by,
            "status": self.status,
            "accepted": self.accepted,
            "used": self.used,
            "reason": self.reason,
            "denial_codes": self.denial_codes,
        }


@dataclass
class Verdict:
    result: str  # "PASS" or "FAIL"
    reason: str
    checks: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.result == "PASS"

    def as_dict(self):
        return {"result": self.result, "reason": self.reason, "checks": self.checks}


def validate_chunk(hit, device, compatibility, min_similarity: float,
                   audience: str = CUSTOMER_SERVICE_AUDIENCE,
                   best_similarity: Optional[float] = None,
                   max_score_gap: Optional[float] = None,
                   query: Optional[str] = None,
                   revision_context: Optional[dict] = None
                   ) -> ChunkVerdict:
    """Decide whether one retrieved chunk may be used for this device."""
    chunk = hit.chunk
    failures = []
    denial_codes = []

    if hit.score < min_similarity:
        failures.append(
            f"similarity {hit.score:.3f} below threshold {min_similarity:.2f}"
        )
        denial_codes.append("SIMILARITY_BELOW_THRESHOLD")

    if (best_similarity is not None and max_score_gap is not None
            and hit.score < best_similarity - max_score_gap):
        failures.append(
            f"similarity {hit.score:.3f} is more than {max_score_gap:.2f} "
            f"below best candidate {best_similarity:.3f}"
        )
        denial_codes.append("SIMILARITY_RELATIVE_GAP")

    if audience not in chunk.audiences:
        failures.append(
            f"document audience {'/'.join(chunk.audiences)} does not permit "
            f"{audience} use"
        )
        denial_codes.append("AUDIENCE_NOT_PERMITTED")

    from . import identity
    from .qms_policy import authorize
    revision_context = revision_context or identity.revision_context(device)
    policy = authorize(chunk, operation='revision_troubleshooting', recipient='customer',
                       context={'model': device.model, 'revision': device.revision,
                                'revision_context': revision_context,
                                'compatibility': compatibility})
    failures.extend(reason for reason in policy.denial_reasons if reason not in failures)
    denial_codes.extend(code for code in policy.denial_codes if code not in denial_codes)

    return ChunkVerdict(
        chunk_id=chunk.chunk_id,
        procedure_id=chunk.procedure_id,
        document_id=chunk.document_id,
        accepted=not failures,
        similarity=hit.score,
        rank=hit.rank,
        failures=failures,
        denial_codes=denial_codes,
    )


def validate_interaction(identity, accepted, response_text: str,
                         selected_procedure: Optional[str]) -> Verdict:
    """Final PASS/FAIL gate over the whole interaction."""
    checks = []

    def record(name, ok, detail):
        checks.append({"check": name, "result": "PASS" if ok else "FAIL",
                       "detail": detail})
        return ok

    ok = record(
        "device_identity_confirmed",
        identity.confirmed,
        identity.reason,
    )
    if not ok:
        return Verdict("FAIL", f"device identity not confirmed: {identity.reason}",
                       checks)

    device = identity.device

    if not accepted:
        record("applicable_source_available", False,
               "no retrieved chunk passed model, revision, status and "
               "authorisation checks")
        return Verdict(
            "FAIL",
            "no applicable approved procedure for "
            f"{device.model} Rev {device.revision}",
            checks,
        )
    record("applicable_source_available", True,
           f"{len(accepted)} chunk(s) accepted")

    models_ok = all(v["model"] in (device.model, WILDCARD_MODEL)
                    for v in accepted)
    record("source_matches_model", models_ok,
           f"all accepted sources are for model {device.model} or are "
           f"model-independent")

    revs_ok = all(device.revision in v["applicable_revisions"] for v in accepted)
    record("source_applies_to_revision", revs_ok,
           f"all accepted sources apply to revision {device.revision}")

    status_ok = all(v["approval_status"] in CURRENT_STATUSES for v in accepted)
    record("source_status_current", status_ok,
           "all accepted sources are Approved")

    cited = bool(selected_procedure) and selected_procedure in (response_text or "")
    record("response_cites_source", cited,
           f"response cites {selected_procedure}" if cited
           else f"response does not cite {selected_procedure!r}")

    failed = [c["check"] for c in checks if c["result"] == "FAIL"]
    if failed:
        return Verdict("FAIL", f"failed checks: {', '.join(failed)}", checks)
    return Verdict(
        "PASS",
        f"grounded on {selected_procedure} for {device.model} Rev "
        f"{device.revision}",
        checks,
    )
