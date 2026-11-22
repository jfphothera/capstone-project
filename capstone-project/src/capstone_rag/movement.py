"""Fictional business rules for choosing a product movement.

An RMA here is an **ERP inventory-movement transaction**. It is not a synonym
for "send the whole device back". Most faults that need anything physical
need a part, not a unit: a mains cord, a lamp module, an intake filter, a
pair of goggles. Bringing a working chassis back to the facility because a
consumable failed is the expensive wrong answer, and the trends view counts
how often it nearly happened.

Movement types
--------------
  none                        nothing physical needs to move
  part_outbound               send a part or accessory to the customer
  device_replacement_outbound send a replacement unit out
  device_return               the unit comes back to the facility
  human_review                the rules cannot decide; a person must

Every decision carries its rule id and the evidence it used, and anything
uncertain resolves to human_review rather than to a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

NONE = "none"
PART_OUTBOUND = "part_outbound"
DEVICE_REPLACEMENT = "device_replacement_outbound"
DEVICE_RETURN = "device_return"
HUMAN_REVIEW = "human_review"

INBOUND, OUTBOUND = "inbound", "outbound"

# Fictional catalogue of customer-replaceable parts and accessories.
PART_CATALOGUE = {
    "goggles": {"part_number": "ACC-GOG-01", "name": "Protective goggles",
                "customer_replaceable": True},
    "power cord": {"part_number": "ACC-CRD-02", "name": "Mains power cord",
                   "customer_replaceable": True},
    "mains cord": {"part_number": "ACC-CRD-02", "name": "Mains power cord",
                   "customer_replaceable": True},
    "cord": {"part_number": "ACC-CRD-02", "name": "Mains power cord",
             "customer_replaceable": True},
    "lamp": {"part_number": "CON-LMP-10", "name": "Lamp module",
             "customer_replaceable": True},
    "lamp module": {"part_number": "CON-LMP-10", "name": "Lamp module",
                    "customer_replaceable": True},
    "filter": {"part_number": "CON-FLT-04", "name": "Intake filter",
               "customer_replaceable": True},
    "intake filter": {"part_number": "CON-FLT-04", "name": "Intake filter",
                      "customer_replaceable": True},
    "fuse": {"part_number": "CON-FUS-03", "name": "Fuse carrier",
             "customer_replaceable": True},
    "fuse carrier": {"part_number": "CON-FUS-03", "name": "Fuse carrier",
                     "customer_replaceable": True},
    "manual": {"part_number": "DOC-IFU-01", "name": "Instructions for use",
               "customer_replaceable": True},
    "treatment head": {"part_number": "ASY-HED-20", "name": "Treatment head",
                       "customer_replaceable": False},
}

_PART_RE = re.compile(
    r"\b(?:" + "|".join(sorted((re.escape(k) for k in PART_CATALOGUE),
                               key=len, reverse=True)) + r")",
    re.IGNORECASE)


@dataclass
class MovementDecision:
    movement_type: str
    direction: str = ""
    items: list = field(default_factory=list)
    rule: str = ""
    reason: str = ""
    requires_human_review: bool = True
    evidence: dict = field(default_factory=dict)

    @property
    def needs_transaction(self) -> bool:
        return self.movement_type in (PART_OUTBOUND, DEVICE_REPLACEMENT,
                                      DEVICE_RETURN)

    def as_dict(self):
        return {
            "movement_type": self.movement_type,
            "direction": self.direction,
            "items": self.items,
            "rule": self.rule,
            "reason": self.reason,
            "requires_human_review": self.requires_human_review,
            "evidence": self.evidence,
        }


def mentioned_parts(text: str) -> list:
    found, seen = [], set()
    for m in _PART_RE.findall(text or ""):
        key = m.lower()
        entry = PART_CATALOGUE.get(key)
        if entry and entry["part_number"] not in seen:
            seen.add(entry["part_number"])
            found.append({**entry, "matched_on": key})
    return found


def decide(text: str, triage_result, *, procedure_exhausted: bool = False,
           applicable_procedure: str = None, device: dict = None,
           identity_verified: bool = False) -> MovementDecision:
    """Choose a movement under rules MR-1 .. MR-7."""
    parts = mentioned_parts(text)
    ae = triage_result.adverse_event
    comp = triage_result.complaint

    # MR-1  A safety event or device hazard: the unit is quarantined and
    # comes back for investigation. Never decided by the agent alone.
    if ae.is_adverse_event or ae.hazard_indicators:
        return MovementDecision(
            DEVICE_RETURN, INBOUND,
            [{"part_number": "UNIT", "name": "Complete device",
              "reason": "quarantine for safety investigation"}],
            rule="MR-1",
            reason=("a safety event or device hazard was reported, so the "
                    "unit is quarantined and returned for investigation "
                    "rather than repaired in the field"),
            requires_human_review=True,
            evidence={"adverse_event": ae.certainty,
                      "hazard": ae.hazard_indicators})

    # MR-2  A component that should have shipped is missing. Send the part;
    # nothing needs to come back.
    damaged_replaceable = [p for p in parts if p.get('customer_replaceable')]
    if damaged_replaceable and re.search(r'\b(broken|cracked|damaged)\b', text, re.I) and not re.search(r'\b(smoke|sparking|exposed wires|shattered device)\b', text, re.I):
        return MovementDecision(PART_OUTBOUND, OUTBOUND, damaged_replaceable, rule='MR-COMPONENT-DAMAGE',
            reason='Reported damage identifies a customer-replaceable component; keep the complete device with the customer pending named part-shipment authorization',
            evidence={'parts': [p['name'] for p in damaged_replaceable], 'procedure': 'PROC-OPS-MOVEMENT'})
    if re.search(r'\b(device exchange|exchange the device|replacement device|replace the whole device)\b', text, re.I):
        return MovementDecision(DEVICE_REPLACEMENT, OUTBOUND,
            [{'part_number': 'UNIT', 'name': 'Replacement device'}], rule='MR-EXCHANGE',
            reason='Propose a linked return and separately authorized replacement; dispatch requires verified return receipt',
            evidence={'procedure': 'PROC-OPS-MOVEMENT', 'receiving_required': True})
    if comp.category == "missing_component":
        items = parts or [{"part_number": "UNKNOWN",
                           "name": "unidentified missing component",
                           "customer_replaceable": True}]
        unknown = any(i["part_number"] == "UNKNOWN" for i in items)
        return MovementDecision(
            PART_OUTBOUND, OUTBOUND, items, rule="MR-2",
            reason=("a component expected with the device is missing, so the "
                    "part is shipped out; the device itself stays with the "
                    "customer"),
            requires_human_review=(unknown or not identity_verified),
            evidence={"parts_detected": [i["name"] for i in items],
                      "identity_verified": identity_verified})

    # MR-3  A customer-replaceable part has failed and an approved procedure
    # covers it. Send the part.
    replaceable = [p for p in parts if p.get("customer_replaceable")]
    if comp.category == "malfunction" and replaceable and applicable_procedure and procedure_exhausted:
        return MovementDecision(
            PART_OUTBOUND, OUTBOUND, replaceable, rule="MR-3",
            reason=(f"the fault points at a customer-replaceable part and "
                    f"{applicable_procedure} covers it, so a part shipment "
                    f"resolves this without returning the unit"),
            requires_human_review=not identity_verified,
            evidence={"procedure": applicable_procedure,
                      "parts": [p["name"] for p in replaceable]})

    # MR-4  Every approved step has been tried and the unit is still faulty.
    # Only now does the whole device move.
    if procedure_exhausted:
        return MovementDecision(
            DEVICE_RETURN, INBOUND,
            [{"part_number": "UNIT", "name": "Complete device",
              "reason": "all approved field steps exhausted"}],
            rule="MR-4",
            reason=("every approved troubleshooting step for this model and "
                    "revision has been completed and the fault persists"),
            requires_human_review=True,
            evidence={"procedure_exhausted": True})

    # MR-5  A fault with no applicable approved procedure. The agent has no
    # basis to choose a movement at all.
    if comp.is_complaint and not applicable_procedure:
        return MovementDecision(
            HUMAN_REVIEW, "", [], rule="MR-5",
            reason=("a fault is reported but no approved procedure covers "
                    "this model and revision, so there is no basis to "
                    "choose a movement; an engineer decides"),
            requires_human_review=True,
            evidence={"applicable_procedure": None})

    # MR-6  A fault with a procedure still to work through. Troubleshoot
    # first; moving product now would be premature.
    if comp.is_complaint and applicable_procedure:
        return MovementDecision(
            NONE, "", [], rule="MR-6",
            reason=(f"{applicable_procedure} has steps still to try, so no "
                    f"product movement is warranted yet"),
            requires_human_review=False,
            evidence={"procedure": applicable_procedure})

    # MR-7  Nothing reported that needs anything physical.
    return MovementDecision(
        NONE, "", [], rule="MR-7",
        reason="nothing reported that requires a product movement",
        requires_human_review=False,
        evidence={"classification": triage_result.classification})


def avoidable_full_return(decision: MovementDecision, text: str) -> bool:
    """Would a part shipment have done instead of a whole-device return?

    Used by the trends view. A return raised under MR-4 while the customer
    was only ever describing a replaceable part is the pattern worth seeing.
    """
    if decision.movement_type != DEVICE_RETURN:
        return False
    if decision.rule == "MR-1":
        return False        # safety returns are never 'avoidable'
    return bool([p for p in mentioned_parts(text)
                 if p.get("customer_replaceable")])
