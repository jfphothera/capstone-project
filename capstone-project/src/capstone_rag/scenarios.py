"""Repeatable synthetic customer-service scenarios and local evaluation.

The operational ticket pool is the source material, but evaluation never
releases those tickets and never writes to the demonstration database.  The
builder deterministically turns the 1,000 pooled tickets into labelled cases
with independent identity/email dimensions, follow-up turns, held-out rows,
and explicit expected outcomes.  The fast runner uses only local rules and a
temporary SQLite backup.  The real-model runner is deliberately opt-in and
sequential.
"""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import config, identity, qms, triage
from .store import Store


DATASET_DIR = os.path.join(config.DATA_DIR, "evaluation")
DATASET_PATH = os.path.join(DATASET_DIR, "scenarios.jsonl")
VALIDATION_PATH = os.path.join(DATASET_DIR, "validation.json")


ARCHETYPE_EXPECTATIONS = {
    "device_failed": ("complaint_malfunction", True, "troubleshoot"),
    "rma_broken_lamp": ("complaint_malfunction", True, "part_movement"),
    "missing_item": ("complaint_missing_component", True, "part_movement"),
    "report_of_erythema": ("adverse_event_suspected", False, "qa_review"),
    "order_status_shipping": ("order_status_enquiry", False, "track_order"),
    "refund": ("general_enquiry", False, "human_disposition"),
    "general_question": ("general_enquiry", False, "answer_or_request_detail"),
    "refill_code": ("general_enquiry", False, "answer_or_request_detail"),
    "training": ("general_enquiry", False, "answer_or_request_detail"),
    "it_helpdesk": ("general_enquiry", False, "human_disposition"),
    "phone_ai_silent": ("general_enquiry", False, "request_information"),
    "distributor_hcp_prospect": ("general_enquiry", False,
                                  "human_disposition"),
}

SAFETY_VARIANTS = (
    ("confirmed", "I have a painful burn and blistering on my arm."),
    ("confirmed", "The session caused swelling and visible skin damage."),
    ("suspected", "I have redness and irritation after the session."),
    ("hazard", "There was smoke and an unusual electrical odor, but no one "
               "was injured."),
    ("negative", "There was no smoke, no overheating, and no injury; I only "
                 "need a copy of the user guide."),
)


def _label_text(text, fallback):
    """Human-authored ground-truth vocabulary, separate from ``triage``."""
    low = (text or "").lower()
    harm_text = re.sub(
        r"\b(?:no (?:one )?was|not|wasn't|was not)\s+"
        r"(?:injured|burned|burnt|in pain|swollen)\b", " ", low)
    confirmed = ("painful burn", "blistering", "skin damage", "swelling",
                 "burn on", "injured", "injury")
    suspected = ("redness", "erythema", "irritation", "rash", "reaction")
    hazards = ("burning smell", "smoke", "sparks", "unusual electrical odor",
               "unusual electrical odour", "too hot to touch", "overheating")
    # Explicit local negatives are used by the false-positive test cases.
    negative = ("no smoke" in low and "no overheating" in low and
                "no injury" in low)
    if not negative and any(x in harm_text for x in confirmed):
        return "adverse_event_confirmed", "confirmed"
    if not negative and any(x in low for x in suspected):
        return "adverse_event_suspected", "suspected"
    if not negative and any(x in low for x in hazards):
        return "device_safety_hazard", "hazard"
    if any(x in low for x in ("missing from", "not in the box",
                              "wasn't included", "was not included")):
        return "complaint_missing_component", "none"
    if any(x in low for x in ("not working", "stopped working", "broken",
                              "failed", "no power", "completely dead",
                              "lamp appears to be out", "screen shows a code",
                              "return or repair", "flickering", "rattling")):
        return "complaint_malfunction", "none"
    if any(x in low for x in ("where is my order", "shipped", "shipping",
                              "delivery", "tracking", "hasn't arrived")):
        return "order_status_enquiry", "none"
    return fallback, "none"


@dataclass
class Scenario:
    scenario_id: str
    source_pool_id: str
    source_ticket_id: Optional[int]
    split: str
    category: str
    subject: str
    initial_message: str
    follow_up_messages: list[str] = field(default_factory=list)
    sender_email: str = ""
    sender_email_in_erp: bool = False
    account_id: Optional[str] = None
    customer_name: Optional[str] = None
    device_serial_number: Optional[str] = None
    device_model: Optional[str] = None
    device_revision: Optional[str] = None
    expected_identity: str = "unmatched"
    initially_available: list[str] = field(default_factory=list)
    follow_up_required: list[str] = field(default_factory=list)
    expected_complaint: bool = False
    expected_classification: str = "general_enquiry"
    expected_safety: str = "none"
    expected_priority: str = "Low"
    expected_erp_match: bool = False
    expected_procedure: Optional[str] = None
    qms_required: bool = False
    qms_gap_reason: Optional[str] = None
    expected_next_action: str = "human_disposition"
    human_escalation_required: bool = False
    product_movement_appropriate: bool = False
    human_approval_required: bool = False
    expected_shipment: str = "none"
    variation: str = "baseline"

    def as_dict(self):
        return asdict(self)

    def summary(self):
        return {
            "scenario_id": self.scenario_id,
            "split": self.split,
            "category": self.category,
            "subject": self.subject,
            "expected_identity": self.expected_identity,
            "expected_classification": self.expected_classification,
            "expected_priority": self.expected_priority,
            "human_escalation_required": self.human_escalation_required,
            "variation": self.variation,
        }


def _now():
    return datetime.now(timezone.utc).isoformat()


def _connect_ro(path: str):
    con = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _known_procedure(model, revision, archetype, compatibility, message=""):
    """Label an applicable procedure from the described symptom.

    Ticket archetypes are broad routing labels, not procedure ground truth.
    In particular, ``rma_broken_lamp`` contains screen-code and shutdown
    reports, while ``device_failed`` often describes a dim panel. Procedure
    labels therefore come from explicit symptom language and stay ``None``
    when the message does not justify a customer-facing procedure.
    """
    allowed = compatibility.get(f"{model}|{revision}", [])
    if archetype == "missing_item":
        wanted = "MISSING"
    else:
        low = (message or "").lower()
        lamp = ("lamp", "panel is noticeably dimmer", "panels are noticeably",
                "flicker", "output is uneven", "lamp never turns on")
        direct_power = ("completely dead", "no lights", "no fan", "no power",
                        "won't turn on", "will not turn on", "does not power")
        thermal = ("overheat", "overheating", "too hot", "excessive heat",
                   "thermal shutdown")
        if any(term in low for term in lamp):
            wanted = "LAMP"
        elif model == "Series-M" and any(term in low for term in thermal):
            wanted = "THERM"
        elif (model == "P4800" and "timer" in low and
              any(term in low for term in ("reset", "restart"))):
            wanted = "TIMER"
        elif any(term in low for term in direct_power):
            wanted = "PWR"
        else:
            return None
    return next((p for p in allowed if wanted in p), None)


def _identity_text(customer, device, mode, scenario_id):
    """Return initial suffix, follow-ups, fields present, expected result."""
    name = customer["customer_name"]
    serial = device["serial_number"] if device else None
    dob = customer["date_of_birth"]
    suffix, follow, available, needed = [], [], [], []

    if mode in (1, 5, 8, 9):
        suffix.extend((f"My name is {name}.",
                       f"The serial number is {serial}."))
        available.extend(("customer_name", "serial_number"))
        status = "verified_initially"
    elif mode == 2:
        if dob:
            suffix.extend((f"It's {name}.", f"DOB: {dob}."))
            available.extend(("customer_name", "date_of_birth"))
        else:
            suffix.extend((f"It's {name}.", f"Serial: {serial}."))
            available.extend(("customer_name", "serial_number"))
        status = "verified_initially"
    elif mode == 3:
        suffix.append(f"My name is {name}.")
        follow.append(f"The serial number is {serial}.")
        available.append("customer_name")
        needed.append("serial_number")
        status = "verified_after_follow_up"
    elif mode == 4:
        suffix.append(f"Serial number: {serial}.")
        follow.append(f"My name is {name}.")
        available.append("serial_number")
        needed.append("customer_name")
        status = "verified_after_follow_up"
    elif mode == 6:
        bad = f"X{scenario_id[-6:]}"
        suffix.extend((f"My name is {name}.", f"Serial number: {bad}."))
        follow.append(f"Sorry, I gave you the wrong serial number. It is {serial}.")
        available.extend(("customer_name", "incorrect_serial_number"))
        needed.append("serial_number_correction")
        status = "verified_after_correction"
    else:  # mode 7: contact data locates, but never verifies, the record
        suffix.append(f"You can reach me at {customer['email']}.")
        follow.append(f"My name is {name}; the serial number is {serial}.")
        available.append("email_locator")
        needed.extend(("customer_name", "serial_number"))
        status = "verified_after_follow_up"
    return "\n\n".join(suffix), follow, available, needed, status


def _strip_existing_identity(text, customer=None):
    """Remove identity injected into the operational pool before re-labelling.

    The pool importer intentionally adds a realistic identity mix. Evaluation
    needs its own controlled mix, so retaining those lines would give some
    rows three points while their labels say one.
    """
    value = text or ""
    value = re.sub(r"(?im)^\s*(?:serial(?: number)?|date of birth|dob)\s*[:=-]"
                   r"\s*[^\r\n]+\r?$", "", value)
    value = re.sub(r"(?im)^\s*[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\s*$", "", value)
    if customer and customer.get("customer_name"):
        value = re.sub(re.escape(customer["customer_name"]), "Customer",
                       value, flags=re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def build(*, db_path: Optional[str] = None, count: int = 1000,
          seed: int = 20261026, output_path: Optional[str] = DATASET_PATH,
          persist: bool = True) -> tuple[list[Scenario], dict]:
    """Derive labelled cases from the existing synthetic pool.

    Exact distributions are intentional: every tenth row is genuinely
    unmatched; the following row uses an ERP e-mail.  These dimensions do not
    overlap, so missing e-mail never becomes the reason a case is unmatched.
    """
    db_path = db_path or config.DB_PATH
    con = _connect_ro(db_path)
    tickets = [dict(r) for r in con.execute(
        "SELECT * FROM ticket_pool ORDER BY pool_id LIMIT ?", (count,))]
    customers = {r["account_id"]: dict(r) for r in con.execute(
        "SELECT * FROM customers")}
    devices = defaultdict(list)
    for row in con.execute("SELECT * FROM devices ORDER BY serial_number"):
        devices[row["account_id"]].append(dict(row))
    con.close()
    if len(tickets) < count:
        raise ValueError(f"requested {count} scenarios but only "
                         f"{len(tickets)} pooled tickets exist")

    compatibility = qms.load_compatibility()
    device_accounts = sorted(a for a in customers if devices.get(a))
    rng = random.Random(seed)
    rows = []
    for i, ticket in enumerate(tickets):
        sid = f"EVAL-{i + 1:04d}"
        unmatched = i % 10 == 0
        email_in_erp = i % 10 == 1
        original_customer = customers.get(ticket.get("account_id"))
        source_customer = original_customer
        owned = devices.get(ticket.get("account_id"), [])
        if not owned and not unmatched:
            # Some synthetic identities legitimately own no device. Preserve
            # that in the ERP, but bind this *evaluation scenario* to another
            # fictional customer/device so the requested 90% matchable rate
            # measures verification rather than inventory prevalence.
            fallback_account = device_accounts[i % len(device_accounts)]
            source_customer = customers[fallback_account]
            owned = devices[fallback_account]
        device = next((d for d in owned if d["serial_number"] ==
                       ticket.get("device_serial_number")), None)
        device = device or (owned[0] if owned else None)
        category = ticket.get("archetype") or "general_question"
        classification, qms_required, next_action = ARCHETYPE_EXPECTATIONS.get(
            category, ARCHETYPE_EXPECTATIONS["general_question"])
        body = _strip_existing_identity(ticket.get("body") or "",
                                        original_customer)
        follow = []
        available = []
        needed = []
        variation = "baseline"

        # Expand the sparse safety source category without changing the source
        # ticket pool. Five rows per 100 cover confirmed, suspected, hazard,
        # and explicit negative language with stable denominators.
        safety_kind = None
        if i % 100 in range(20, 25):
            safety_kind, sentence = SAFETY_VARIANTS[i % 100 - 20]
            body = body.rstrip() + "\n\n" + sentence
            variation = f"safety_{safety_kind}"
            if safety_kind == "confirmed":
                classification = "adverse_event_confirmed"
            elif safety_kind == "suspected":
                classification = "adverse_event_suspected"
            elif safety_kind == "hazard":
                classification = "device_safety_hazard"
            qms_required = False
            next_action = "qa_review" if safety_kind != "negative" else next_action

        if unmatched:
            fake_name = f"Unlisted Customer {i + 1}"
            fake_serial = f"ZZ{i + 1:08d}"
            body = body.rstrip() + (f"\n\nMy name is {fake_name}.\n"
                                    f"Serial number: {fake_serial}.")
            sender = f"unlisted.{i + 1}@outside.example.invalid"
            expected_identity = "unmatched"
            account_id = customer_name = serial = model = revision = None
            available = ["customer_name", "serial_number"]
            expected_match = False
        else:
            if not source_customer or not device:
                raise ValueError(f"{ticket['pool_id']} lacks a linked customer/device")
            suffix, follow, available, needed, expected_identity = _identity_text(
                source_customer, device, i % 10, sid)
            body = body.rstrip() + "\n\n" + suffix
            sender = (source_customer["email"] if email_in_erp else
                      f"sender.{i + 1}@outside.example.invalid")
            account_id = source_customer["account_id"]
            customer_name = source_customer["customer_name"]
            serial = device["serial_number"]
            model = device["model"]
            revision = device["revision"]
            expected_match = True

        label_fallback = ("general_enquiry"
                          if category == "order_status_shipping"
                          else classification)
        labelled_classification, labelled_safety = _label_text(
            body, label_fallback)
        classification = labelled_classification

        if safety_kind == "confirmed" or labelled_safety == "confirmed":
            expected_safety, expected_priority = "confirmed", "Critical"
        elif (safety_kind == "suspected" or labelled_safety == "suspected" or
              category == "report_of_erythema"):
            expected_safety, expected_priority = "suspected", "Critical"
            classification = "adverse_event_suspected"
        elif safety_kind == "hazard" or labelled_safety == "hazard":
            expected_safety, expected_priority = "hazard", "Critical"
        else:
            expected_safety = "none"
            # Derive priority from the documented category policy, not by
            # calling the classifier that the runner is meant to evaluate.
            expected_priority = ("Medium" if classification.startswith("complaint_")
                                 else "Low")
            if any(x in body.lower() for x in ("cannot use", "completely dead",
                                               "unable to use")) and \
                    classification.startswith("complaint_"):
                expected_priority = "High"

        qms_required = classification.startswith("complaint_")
        potential_procedure = _known_procedure(
            model, revision, category, compatibility, body)
        procedure = (potential_procedure if expected_match and
                     expected_safety == "none" else None)
        gap_reason = None
        if qms_required and procedure is None:
            low_body = body.lower()
            if not expected_match:
                gap_reason = "identity_unverified_blocks_device_instructions"
            elif expected_safety != "none":
                gap_reason = "safety_hold_blocks_ordinary_troubleshooting"
            elif model == "Home-NB":
                gap_reason = "unsupported_device_no_fictional_manual"
            elif model == "P4800" and revision == "B":
                gap_reason = "unsupported_revision_deliberate_negative_case"
            elif (model == "Series-M" and
                  any(x in low_body for x in ("screen shows a code", "code i don't"))):
                gap_reason = "service_only_no_customer_facing_procedure"
            elif any(x in low_body for x in ("screen shows a code", "code i don't")):
                gap_reason = "missing_customer_facing_error_code_manual"
            elif any(x in low_body for x in ("shuts off", "stopped working",
                                              "not working")):
                gap_reason = "symptom_too_broad_for_safe_customer_procedure"
            else:
                gap_reason = "no_explicit_supported_symptom_in_message"
        movement = (category in ("missing_item", "rma_broken_lamp") and
                    expected_safety == "none")
        escalation = (not expected_match or expected_safety != "none" or
                      (qms_required and procedure is None) or movement or
                      expected_identity == "verified_after_correction")

        rows.append(Scenario(
            scenario_id=sid, source_pool_id=ticket["pool_id"],
            source_ticket_id=ticket.get("source_ticket_id"),
            split="held_out" if i % 5 == 0 else "development",
            category=category, subject=ticket.get("subject") or category,
            initial_message=body, follow_up_messages=follow,
            sender_email=sender, sender_email_in_erp=email_in_erp,
            account_id=account_id, customer_name=customer_name,
            device_serial_number=serial, device_model=model,
            device_revision=revision, expected_identity=expected_identity,
            initially_available=available, follow_up_required=needed,
            expected_complaint=classification.startswith("complaint_"),
            expected_classification=classification,
            expected_safety=expected_safety,
            expected_priority=expected_priority,
            expected_erp_match=expected_match,
            expected_procedure=procedure, qms_required=qms_required,
            qms_gap_reason=gap_reason,
            expected_next_action=next_action,
            human_escalation_required=escalation,
            product_movement_appropriate=movement,
            human_approval_required=movement or expected_safety != "none",
            expected_shipment=("part_after_approval" if movement else "none"),
            variation=variation,
        ))

    # Seed is recorded and used for stable future extensions. Shuffling is not
    # performed because scenario ids intentionally retain source-pool order.
    rng.random()
    report = validate(rows, seed=seed)
    if persist and output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row.as_dict(), sort_keys=True) + "\n")
        validation_path = os.path.join(os.path.dirname(output_path),
                                       "validation.json")
        with open(validation_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")
    return rows, report


def validate(rows: list[Scenario], *, seed: int = 20261026) -> dict:
    ids = [r.scenario_id for r in rows]
    source_ids = [r.source_pool_id for r in rows]
    matched = sum(r.expected_erp_match for r in rows)
    email = sum(r.sender_email_in_erp for r in rows)
    follow = sum(bool(r.follow_up_required) for r in rows)
    qms_needed = [r for r in rows if r.qms_required]
    qms_covered = sum(bool(r.expected_procedure) for r in qms_needed)
    problems = []
    if len(ids) != len(set(ids)):
        problems.append("duplicate scenario_id")
    if len(source_ids) != len(set(source_ids)):
        problems.append("duplicate source_pool_id")
    for r in rows:
        if r.expected_erp_match and not all((r.account_id,
                                             r.device_serial_number)):
            problems.append(f"{r.scenario_id}: matchable row lacks linkage")
        if not r.expected_erp_match and r.account_id is not None:
            problems.append(f"{r.scenario_id}: unmatched row has account_id")
    n = len(rows)
    return {
        "generated_at": _now(), "seed": seed, "total": n,
        "split": dict(Counter(r.split for r in rows)),
        "categories": dict(Counter(r.category for r in rows)),
        "variations": dict(Counter(r.variation for r in rows)),
        "erp_match": {"count": matched,
                      "percent": round(100 * matched / n, 2) if n else 0},
        "genuinely_unmatched": n - matched,
        "sender_email_in_erp": {
            "count": email,
            "percent": round(100 * email / n, 2) if n else 0},
        "sender_email_absent_from_erp": n - email,
        "requires_follow_up": follow,
        "qms": {"required": len(qms_needed), "covered": qms_covered,
                "deliberate_gaps": len(qms_needed) - qms_covered},
        "referential_integrity": "PASS" if not problems else "FAIL",
        "problems": problems,
    }


def load(*, category: Optional[str] = None,
         path: str = DATASET_PATH) -> list[Scenario]:
    if not os.path.exists(path):
        rows, _ = build(persist=False)
    else:
        with open(path, encoding="utf-8") as fh:
            rows = [Scenario(**json.loads(line)) for line in fh if line.strip()]
    return [r for r in rows if not category or r.category == category]


def categories(path: str = DATASET_PATH):
    return sorted({r.category for r in load(path=path)})


def get(scenario_id: str, path: str = DATASET_PATH):
    return next((r for r in load(path=path)
                 if r.scenario_id == scenario_id), None)


def _merge_claim(current, fresh, correction=False):
    for name in ("name", "dob", "serial", "phone", "email"):
        value = getattr(fresh, name)
        if value and (correction or not getattr(current, name)):
            setattr(current, name, value)
    if fresh.serial:
        current.serial_issue = None
        current.serial_input = fresh.serial_input
    elif fresh.serial_issue and (correction or not current.serial):
        current.serial = None
        current.serial_issue = fresh.serial_issue
        current.serial_input = fresh.serial_input


def _confusion(expected, actual):
    tp = sum(e and a for e, a in zip(expected, actual))
    tn = sum(not e and not a for e, a in zip(expected, actual))
    fp = sum(not e and a for e, a in zip(expected, actual))
    fn = sum(e and not a for e, a in zip(expected, actual))
    return {"tp": tp, "tn": tn, "fp": fp, "fn": fn,
            "accuracy": round((tp + tn) / len(expected), 4) if expected else None}


def _sqlite_backup(source, destination):
    src = sqlite3.connect(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def run(*, scenario_ids=None, category=None, split=None, limit=1000, seed=7,
        out_dir=None, agent=None, prefer_llm=False, mode="fast",
        failure_category=None, db_path=None, dataset_path=None):
    """Run a filtered evaluation and export JSON plus readable Markdown.

    ``mode='real'`` is opt-in and sequential. The API deliberately invokes
    the default fast mode, so opening a browser can never start a large model
    batch.
    """
    rows = load(category=category, path=dataset_path or DATASET_PATH)
    if split:
        rows = [row for row in rows if row.split == split]
    if scenario_ids:
        wanted = set(scenario_ids)
        rows = [r for r in rows if r.scenario_id in wanted]
    random.Random(seed).shuffle(rows)
    rows = rows[:limit]
    if mode == "real":
        return _run_real(rows, out_dir=out_dir, agent=agent,
                         prefer_llm=prefer_llm, seed=seed)

    db_path = db_path or config.DB_PATH
    with tempfile.TemporaryDirectory(prefix="capstone-eval-") as td:
        isolated = os.path.join(td, "evaluation.db")
        _sqlite_backup(db_path, isolated)
        store = Store(isolated)
        try:
            result = _run_fast(rows, store)
        finally:
            store.close()
    result.update({"run_id": f"FAST-{uuid.uuid4().hex[:10].upper()}",
                   "mode": "fast_deterministic", "seed": seed,
                   "started_at": _now(),
                   "not_evaluated": [
                       "language-model response quality",
                       "model response grounding and Reflexion correction",
                       "model latency and fallback usage",
                   ]})
    if failure_category:
        result["details"] = [d for d in result["details"]
                             if failure_category in d["failures"]]
    return _export(result, out_dir)


def _run_fast(rows, store):
    details = []
    expected_safety, actual_safety = [], []
    expected_complaint, actual_complaint = [], []
    expected_match, actual_match = [], []
    per_category = defaultdict(lambda: {"total": 0, "passed": 0})
    compatibility = qms.load_compatibility()
    known_serials = [r["serial_number"] for r in store.query(
        "SELECT serial_number FROM devices")]

    for row in rows:
        t = triage.triage(row.initial_message)
        claim = identity.Claim.from_text(row.initial_message,
                                         known_serials=known_serials)
        ver = identity.verify(store, claim)
        for follow in row.follow_up_messages:
            fresh = identity.Claim.from_text(
                follow, known_serials=known_serials,
                expecting=ver.missing or identity.VERIFICATION_POINTS)
            correction = "wrong" in follow.lower() or "sorry" in follow.lower()
            _merge_claim(claim, fresh, correction=correction)
            ver = identity.verify(store, claim)

        qms_actual = None
        if row.qms_required and ver.account_id and row.device_model:
            qms_actual = _known_procedure(row.device_model,
                                          row.device_revision, row.category,
                                          compatibility,
                                          row.initial_message)
        safety_actual = ("confirmed" if t.adverse_event.certainty == "confirmed"
                         else "suspected" if t.adverse_event.certainty == "suspected"
                         else "hazard" if t.adverse_event.hazard_indicators
                         else "none")
        failures = []
        if bool(ver.verified) != row.expected_erp_match:
            failures.append("identity_verification")
        if t.classification != row.expected_classification:
            failures.append("classification")
        if t.priority != row.expected_priority:
            failures.append("priority")
        if safety_actual != row.expected_safety:
            failures.append("safety_detection")
        if row.qms_required and qms_actual != row.expected_procedure:
            failures.append("qms_applicability")

        expected_safety.append(row.expected_safety != "none")
        actual_safety.append(safety_actual != "none")
        expected_complaint.append(row.expected_complaint)
        actual_complaint.append(t.complaint.is_complaint)
        expected_match.append(row.expected_erp_match)
        actual_match.append(ver.verified)
        per_category[row.category]["total"] += 1
        per_category[row.category]["passed"] += not failures
        def grade(ok=None, reason="", severity="ordinary"):
            return {"status": ("NOT_EVALUATED" if ok is None else
                               "PASS" if ok else "FAIL"),
                    "severity": severity, "reason": reason}

        serial_ok = (None if (not row.expected_erp_match or
                              not claim.serial) else
                     ver.serial_number == row.device_serial_number)
        procedure_ok = (None if not row.qms_required else
                        qms_actual == row.expected_procedure)
        details.append({
            "scenario_id": row.scenario_id, "category": row.category,
            "split": row.split, "passed": not failures,
            "failures": failures,
            "expected": {"identity": row.expected_identity,
                         "classification": row.expected_classification,
                         "priority": row.expected_priority,
                         "safety": row.expected_safety,
                         "procedure": row.expected_procedure},
            "actual": {"identity_verified": ver.verified,
                       "identity_reason": ver.reason,
                       "classification": t.classification,
                       "priority": t.priority, "safety": safety_actual,
                       "procedure": qms_actual},
            "grades": {
                "identity_and_device": grade(
                    bool(ver.verified) == row.expected_erp_match,
                    ver.reason, "critical"),
                "serial_canonicalization": grade(
                    serial_ok, "canonical ERP serial comparison", "critical"),
                "complaint_classification": grade(
                    t.classification == row.expected_classification,
                    f"expected {row.expected_classification}; got {t.classification}"),
                "safety_detection": grade(
                    safety_actual == row.expected_safety,
                    f"expected {row.expected_safety}; got {safety_actual}",
                    "critical"),
                "priority": grade(
                    t.priority == row.expected_priority,
                    f"expected {row.expected_priority}; got {t.priority}"),
                "procedure_selection": grade(
                    procedure_ok,
                    f"expected {row.expected_procedure}; got {qms_actual}",
                    "critical"),
                "document_applicability": grade(
                    procedure_ok, "model/revision compatibility result",
                    "critical"),
                "troubleshooting_sequence": grade(
                    None, "requires agent execution"),
                "escalation_appropriateness": grade(
                    None, "requires end-to-end workflow execution"),
                "product_movement": grade(
                    None, "requires movement decision execution", "critical"),
                "human_approval_enforcement": grade(
                    None, "covered by workflow/API tests", "critical"),
                "courtesy": grade(None, "human or optional external rubric"),
                "clarity": grade(None, "human or optional external rubric"),
                "relevance_actionability": grade(
                    None, "requires generated response"),
                "repetition": grade(None, "requires generated response"),
                "unauthorized_promises": grade(
                    None, "requires generated response", "critical"),
                "grounding": grade(None, "requires generated response",
                                   "critical"),
            },
        })

    grade_summary = defaultdict(Counter)
    for detail in details:
        for dimension, outcome in detail["grades"].items():
            grade_summary[dimension][outcome["status"]] += 1
    return {
        "cases": len(rows), "passed": sum(d["passed"] for d in details),
        "failed": sum(not d["passed"] for d in details),
        "confusion_matrices": {
            "safety_event": _confusion(expected_safety, actual_safety),
            "complaint": _confusion(expected_complaint, actual_complaint),
            "identity_verified": _confusion(expected_match, actual_match),
        },
        "by_category": dict(per_category),
        "grade_summary": {name: dict(counts)
                          for name, counts in grade_summary.items()},
        "details": details,
    }


def _run_real(rows, *, out_dir, agent, prefer_llm, seed):
    if len(rows) > 100:
        raise ValueError("real-model mode is limited to 100 sequential cases")
    # Never reuse the operational agent's writable queues.  A real-model
    # evaluation may be slow, but it must still be a replay rather than a
    # source of representative cases, approvals, memory, or audit tickets.
    from .agent import Agent
    from .cases import ApprovalQueue, EscalationQueue
    from .memory import ReviewedMemory

    details = []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="capstone-real-eval-") as td:
        eval_agent = Agent(
            prefer_llm=prefer_llm,
            escalations=EscalationQueue(os.path.join(td, "escalations.jsonl")),
            approvals=ApprovalQueue(os.path.join(td, "approvals.jsonl")),
            reviewed_memory=ReviewedMemory(os.path.join(td, "memory.jsonl")),
            ticket_log_path=os.path.join(td, "tickets.jsonl"),
        )
        for row in rows:
            t0 = time.perf_counter()
            result = eval_agent.handle(
                row.initial_message, account_id=row.account_id,
                scenario_id=row.scenario_id,
            )
            payload = result.as_dict()
            details.append({
                "scenario_id": row.scenario_id,
                "split": row.split,
                "category": row.category,
                "input_conversation": [row.initial_message,
                                       *row.follow_up_messages],
                "expected": row.as_dict(),
                "latency_seconds": round(time.perf_counter() - t0, 3),
                "outcome": payload.get("outcome"),
                "generation_mode": payload.get("generation_mode"),
                "grounding": payload.get("grounding"),
                "reflexion": payload.get("reflexion"),
                "escalation": payload.get("escalation"),
                "validation": payload.get("validation"),
                "identity": payload.get("device_identification"),
                "selected_procedure": payload.get("selected_procedure"),
                "retrieval_candidates": payload.get("retrieved"),
                "accepted_sources": [
                    item for item in payload.get("retrieved", [])
                    if item.get("accepted")],
                "rejected_sources": [
                    item for item in payload.get("retrieved", [])
                    if not item.get("accepted")],
                "reasoning_trace": payload.get("trace"),
                "model_configuration": {
                    "prefer_llm": prefer_llm,
                    "configured_model": config.OLLAMA_MODEL,
                },
                "subjective_rubric": {
                    "courtesy": "NOT_REVIEWED",
                    "clarity": "NOT_REVIEWED",
                    "relevance_actionability": "NOT_REVIEWED",
                    "repetition": "NOT_REVIEWED",
                    "unauthorized_promises": "NOT_REVIEWED",
                    "review_note": ("Complete by a human or supplementary "
                                    "independent evaluator; the agent is not "
                                    "its own sole grader."),
                },
                "response": result.response,
            })
    report = {
        "run_id": f"REAL-{uuid.uuid4().hex[:10].upper()}",
        "mode": "real_model_sequential", "seed": seed, "cases": len(rows),
        "started_at": _now(),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "details": details,
    }
    return _export(report, out_dir)


def _export(report, out_dir):
    if not out_dir:
        return report
    os.makedirs(out_dir, exist_ok=True)
    stem = report["run_id"].lower()
    json_path = os.path.join(out_dir, f"{stem}.json")
    md_path = os.path.join(out_dir, f"{stem}.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    report["exports"] = {"json": json_path, "markdown": md_path}
    return report


def render_markdown(report):
    lines = [f"# Evaluation {report['run_id']}", "",
             f"- Mode: {report['mode']}",
             f"- Cases: {report.get('cases', 0)}"]
    if "passed" in report:
        lines += [f"- Passed: {report['passed']}",
                  f"- Failed: {report['failed']}", "",
                  "## Confusion matrices", "",
                  "| Dimension | TP | TN | FP | FN | Accuracy |",
                  "|---|---:|---:|---:|---:|---:|"]
        for name, m in report["confusion_matrices"].items():
            lines.append(f"| {name} | {m['tp']} | {m['tn']} | {m['fp']} | "
                         f"{m['fn']} | {m['accuracy']} |")
        lines += ["", "## Failures", ""]
        failed = [d for d in report["details"] if not d["passed"]]
        lines += ([f"- **{d['scenario_id']}**: {', '.join(d['failures'])}"
                   for d in failed] or ["None."])
    else:
        lines += [f"- Elapsed seconds: {report.get('elapsed_seconds', '-')}"]
    lines.append("")
    return "\n".join(lines)


def compare(before_path, after_path):
    with open(before_path, encoding="utf-8") as fh:
        before = json.load(fh)
    with open(after_path, encoding="utf-8") as fh:
        after = json.load(fh)
    if before.get("cases") != after.get("cases"):
        raise ValueError("runs use different denominators")
    return {
        "denominator": before.get("cases"),
        "before": {"run_id": before.get("run_id"),
                   "passed": before.get("passed")},
        "after": {"run_id": after.get("run_id"),
                  "passed": after.get("passed")},
        "change": ((after.get("passed") or 0) - (before.get("passed") or 0)),
    }
