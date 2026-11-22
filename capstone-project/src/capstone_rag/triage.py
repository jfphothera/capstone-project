"""Deterministic triage: complaint, adverse event, quality class, priority.

These are business-rule judgements, not model judgements. They run on every
inbound message before any language model is involved, so a safety report is
surfaced immediately even when the inference queue is saturated.

The definitions are the ones agreed for this capstone.

**Complaint** - a report of something preventing intended use of the device:
missing supplied components, malfunctioning or broken equipment, or a
misunderstanding that prevents proper operation. Asking to *buy* goggles is
not a complaint. Reporting that goggles expected with the device were
missing is a complaint.

**Adverse event** - a report suggesting injury or harm. Confirmed or
suspected adverse events are Critical and stop ordinary troubleshooting. If
the classification is uncertain the uncertainty is recorded and put to the
representative rather than resolved silently either way.

**Quality control discrepancy** - the product does not match its fictional
specification (a white power cord where the spec says black). That is a
discrepancy to investigate, not an established manufacturing defect.

**Quality assurance concern** - something about procedures or document
control, not about a physical unit.

Priority is Critical / High / Medium / Low. Ordinary customer frustration is
detected and recorded but deliberately does **not** raise priority on its
own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CRITICAL, HIGH, MEDIUM, LOW = "Critical", "High", "Medium", "Low"
PRIORITY_ORDER = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}

# --- signal vocabularies ---------------------------------------------------

# Harm to a person. These make an adverse event confirmed rather than merely
# suspected.
AE_HARM = (
    "burn", "burned", "burnt", "blister", "blistered", "injury", "injured",
    "wound", "scarring", "scarred", "bleeding", "electric shock",
    "shocked me", "went to hospital", "emergency room", "a&e", "urgent care",
    "swelling", "swollen", "infection", "pain", "painful", "skin damage",
)
# Possible harm. Real units do cause mild reactions, so these are reported as
# *suspected* and put to a person rather than auto-classified either way.
AE_POSSIBLE = (
    "redness", "erythema", "irritation", "irritated", "rash", "sore",
    "soreness", "stinging", "itching", "itchy", "reaction", "discomfort",
    "tender", "peeling",
)
# Device hazard without a stated injury. Still Critical: the next person to
# use it could be hurt.
DEVICE_HAZARD = (
    "smoke", "smoking", "burning smell", "burnt smell", "smell of burning",
    "sparks", "sparking", "caught fire", "fire", "melted", "melting",
    "scorch", "scorched", "charred", "too hot to touch", "overheat",
    "overheating", "red hot", "unusual odor", "unusual odour",
    "electrical odor", "electrical odour", "hot smell",
)

MALFUNCTION = (
    "not working", "won't work", "wont work", "will not work",
    "stopped working", "broken", "broke", "faulty", "fault", "failed",
    "failing", "fails", "keeps failing", "packed up", "given up",
    "failure", "will not power", "won't power", "wont power", "no power",
    "dead", "does not turn on", "doesn't turn on", "will not turn on",
    "won't turn on", "error", "malfunction", "stopped", "cuts out",
    "shuts down", "flickering", "dim", "no output", "not lighting",
    "will not start", "won't start", "unusable", "out of order",
    # Mechanical and session symptoms. Without these a report like
    # "rattling noise and the session stops early" reads as a general
    # question, and a real fault is answered instead of escalated.
    "rattling", "rattle", "grinding", "buzzing", "whining", "vibrating",
    "stops early", "stops part way", "cuts off", "shuts off",
    "will not finish", "won't finish", "does not finish", "keeps stopping",
    "overheating", "leaking", "smells", "stuck", "jammed", "cracked",
    "lamp is out", "lamp appears to be out", "lamp went out",
    "screen shows a code", "return or repair", "needs repair",
)
MISSING_COMPONENT = (
    "missing", "did not come with", "didn't come with", "was not included",
    "wasn't included", "not in the box", "never arrived", "never received",
    "nothing in the box", "short shipped", "left out", "absent from",
)
CANNOT_OPERATE = (
    "cannot work out how", "can't work out how", "don't know how to use",
    "do not know how to use", "cannot figure out", "can't figure out",
    "unable to use", "cannot use", "can't use", "no instructions",
    "instructions are unclear", "do not understand how",
)
PURCHASE_INTENT = (
    "would like to buy", "want to buy", "like to purchase", "want to order",
    "how much", "what is the price", "whats the price", "where can i get",
    "can i order", "place an order for", "quote for", "cost of a",
)
REPEATED_FAILURE = (
    "again", "second time", "third time", "fourth time", "keeps happening",
    "keeps failing", "still not fixed", "still not working", "same problem",
    "recurring", "every time", "repeatedly", "more than once",
    "happened before",
)
REPEATED_CONTACT = (
    "called three times", "called twice", "no one has got back",
    "no one has come back", "nobody has replied", "still waiting to hear",
    "chased", "third email", "second email", "left messages",
    "no response", "haven't heard back", "have not heard back",
)
CANNOT_TREAT = (
    "cannot use", "can't use", "unable to use", "missing treatment",
    "missed treatment", "missing my sessions", "cannot treat",
    "out of action", "completely dead", "no treatments",
)
FRUSTRATION = (
    "unacceptable", "ridiculous", "furious", "angry", "appalling",
    "disgusted", "fed up", "sick of", "disappointed", "frustrating",
    "frustrated", "terrible service", "worst",
)
QA_DOC_CONCERN = (
    "the manual says", "instructions say", "the guide says",
    "documentation says", "procedure says", "manual is wrong",
    "instructions are wrong", "contradicts", "out of date manual",
    "wrong instructions", "ifu",
)
SPEC_DISCREPANCY = (
    "different colour", "different color", "wrong colour", "wrong color",
    "white power cord", "black power cord", "does not match the spec",
    "doesn't match the spec", "not what was described", "different model",
    "different part number", "label is wrong", "wrong label",
    # The customer contrasting what arrived with what was specified.
    "spec says", "specification says", "manual shows", "manual says it is",
    "supposed to be", "meant to be", "should have been", "but it should be",
    "does not match the manual", "not the same as the",
)
ORDER_STATUS = (
    "tracking", "shipped", "shipping", "delivery", "delivered", "dispatch",
    "where is my order", "order status", "when will it arrive", "eta",
    "arriving", "arrive", "my order",
)


_TERM_CACHE = {}


def _term_re(terms):
    """Word-boundary matcher, longest term first.

    A plain substring test is wrong here and was caught doing real damage:
    "burning smell" contains "burn", so a device hazard with no injury was
    being classified as a *confirmed* adverse event. Leading-boundary
    matching also lets "burn" match "burns" without matching "sunburnt".
    """
    key = id(terms)
    if key not in _TERM_CACHE:
        ordered = sorted(terms, key=len, reverse=True)
        _TERM_CACHE[key] = re.compile(
            r"\b(?:" + "|".join(re.escape(t) + (r'(?!ing\b)' if t == 'burn' else '') for t in ordered) + r")",
            re.IGNORECASE)
    return _TERM_CACHE[key]


def _hits(text: str, terms) -> list:
    low = (text or "").lower()
    found = _term_re(terms).findall(low)
    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


# --- results ---------------------------------------------------------------

@dataclass
class AdverseEvent:
    is_adverse_event: bool
    certainty: str            # confirmed | suspected | none
    indicators: list = field(default_factory=list)
    hazard_indicators: list = field(default_factory=list)
    requires_human_determination: bool = False
    reason: str = ""

    def as_dict(self):
        return dict(self.__dict__)


@dataclass
class Complaint:
    is_complaint: bool
    category: str             # malfunction | missing_component |
                              # cannot_operate | not_a_complaint
    indicators: list = field(default_factory=list)
    reason: str = ""

    def as_dict(self):
        return dict(self.__dict__)


@dataclass
class QualitySignals:
    qc_discrepancy: bool = False
    qa_document_concern: bool = False
    indicators: list = field(default_factory=list)
    reason: str = ""

    def as_dict(self):
        return dict(self.__dict__)


@dataclass
class Triage:
    adverse_event: AdverseEvent
    complaint: Complaint
    quality: QualitySignals
    priority: str
    priority_reason: str
    signals: dict = field(default_factory=dict)
    classification: str = ""

    def as_dict(self):
        return {
            "adverse_event": self.adverse_event.as_dict(),
            "complaint": self.complaint.as_dict(),
            "quality": self.quality.as_dict(),
            "priority": self.priority,
            "priority_reason": self.priority_reason,
            "classification": self.classification,
            "signals": self.signals,
        }


# --- rules -----------------------------------------------------------------

def assess_adverse_event(text: str) -> AdverseEvent:
    # Do not turn an explicit negative statement ("no smoke or injury") into
    # a safety event merely because it contains the vocabulary we screen for.
    # The substitution is intentionally local: "no injury, but it smoked"
    # removes only injury and still retains the device hazard.
    screened = text or ""
    # Negation can govern a list: "no smoke or injury" denies both. Keep
    # contrasts such as "no injury, but it smoked" outside that scope.
    signals = (r'burn(?:ed|t|s)?|blisters?|injur(?:y|ies|ed)|pain|redness|irritation|'
               r'swelling|skin damage|smoke|smoking|sparks?|burning smell|unusual odou?r|overheating')
    screened = re.sub(r'\b(?:no|neither|without)\s+(?:any\s+)?(?:' + signals + r')\b'
                      r'(?:\s*(?:,\s*|\s+(?:or|and|nor)\s+)(?:' + signals + r')\b)*',
                      ' ', screened, flags=re.I)
    screened = re.sub(r'\b(?:nobody|no one)\s+(?:was |is |has been )?injured\b', ' ', screened, flags=re.I)
    # An explicitly attributed manual quotation is documentation context;
    # actual symptoms elsewhere in the message remain screened normally.
    screened = re.sub(r'''\b(?:the )?(?:manual|guide|instructions|leaflet)\s+(?:says?|states?|mentions?)\s*[:]?\s*["“][^"”]+["”]''',
                      ' ', screened, flags=re.I)
    negated = (
        r"no\s+(?:one\s+was\s+)?(?:burn(?:ed|t)?|burns?|blisters?|"
        r"injur(?:y|ies|ed)|pain|redness|irritation|swelling|skin damage)",
        r"(?:did not|didn't|was not|wasn't|is not|isn't)\s+"
        r"(?:burn(?:ed|t)?|injured|painful|irritated|swollen|overheating)",
        r"no\s+(?:smoke|smoking|sparks?|burning smell|unusual odou?r|"
        r"overheating)",
    )
    for pattern in negated:
        screened = re.sub(pattern, " ", screened, flags=re.IGNORECASE)

    hazard = _hits(screened, DEVICE_HAZARD)
    # Look for harm in the text with the hazard phrases removed. "There was
    # a burning smell" describes a hazard, not a person being burned, and
    # must not be read as an injury.
    residual = screened.lower()
    for h in hazard:
        residual = residual.replace(h, " ")
    harm = _hits(residual, AE_HARM)
    possible = _hits(residual, AE_POSSIBLE)

    if harm:
        return AdverseEvent(
            True, "confirmed", harm, hazard, False,
            f"report describes harm to a person ({', '.join(harm[:3])})")
    if possible and hazard:
        return AdverseEvent(
            True, "suspected", possible, hazard, True,
            f"report describes a possible reaction ({', '.join(possible[:2])}) "
            f"together with a device hazard ({', '.join(hazard[:2])})")
    if possible:
        return AdverseEvent(
            True, "suspected", possible, hazard, True,
            f"report mentions {', '.join(possible[:3])}, which may or may not "
            f"be an adverse reaction; a person must decide")
    if hazard:
        return AdverseEvent(
            False, "none", [], hazard, True,
            f"no injury reported, but the device hazard "
            f"({', '.join(hazard[:2])}) could cause one and must be reviewed")
    return AdverseEvent(False, "none", [], [], False,
                        "no injury, reaction or device hazard reported")


def assess_complaint(text: str) -> Complaint:
    malfunction = _hits(text, MALFUNCTION)
    missing = _hits(text, MISSING_COMPONENT)
    cannot = _hits(text, CANNOT_OPERATE)
    purchase = _hits(text, PURCHASE_INTENT)

    # Precedence matters. "The goggles were missing" is a complaint even if
    # the customer also asks how to buy a spare; "I would like to buy
    # goggles" on its own is not.
    if missing:
        return Complaint(True, "missing_component", missing,
                         "a component expected with the device is reported "
                         "missing, which prevents intended use")
    if malfunction:
        return Complaint(True, "malfunction", malfunction,
                         "the device is reported as malfunctioning or broken")
    if cannot:
        return Complaint(True, "cannot_operate", cannot,
                         "the customer cannot operate the device as intended")
    if purchase:
        return Complaint(False, "not_a_complaint", purchase,
                         "this is a request to purchase or price an item, "
                         "not a report of something preventing use")
    return Complaint(False, "not_a_complaint", [],
                     "nothing reported that prevents intended use")


def assess_quality(text: str) -> QualitySignals:
    spec = _hits(text, SPEC_DISCREPANCY)
    doc = _hits(text, QA_DOC_CONCERN)
    q = QualitySignals(bool(spec), bool(doc), spec + doc)
    parts = []
    if spec:
        parts.append(
            "the product as received may not match its specification; that is "
            "a discrepancy to investigate, not an established manufacturing "
            "defect - it could equally be a customer substitution")
    if doc:
        parts.append(
            "the customer refers to the documentation itself, which is a "
            "document-control question rather than a device fault")
    q.reason = "; ".join(parts) or "no quality signals"
    return q


def assess_priority(text: str, ae: AdverseEvent, complaint: Complaint,
                    history: dict = None) -> tuple:
    """Return (priority, reason). Frustration alone never raises priority."""
    history = history or {}
    low = (text or "").lower()

    if ae.is_adverse_event:
        return CRITICAL, (f"{ae.certainty} adverse event: {ae.reason}")
    if ae.hazard_indicators:
        return CRITICAL, (
            f"device safety hazard reported "
            f"({', '.join(ae.hazard_indicators[:2])}); no injury stated but "
            f"the hazard itself is critical")

    cannot_treat = _hits(text, CANNOT_TREAT)
    repeat_fail = _hits(text, REPEATED_FAILURE)
    repeat_contact = _hits(text, REPEATED_CONTACT)
    prior_cases = history.get("prior_open_cases", 0)

    if cannot_treat and complaint.is_complaint:
        return HIGH, ("the customer cannot use equipment they need: "
                      f"{', '.join(cannot_treat[:2])}")
    if repeat_fail and complaint.is_complaint:
        return HIGH, (f"repeated unresolved failure reported "
                      f"({', '.join(repeat_fail[:2])})")
    if repeat_contact:
        return HIGH, (f"documented repeated unsuccessful contact "
                      f"({', '.join(repeat_contact[:2])})")
    # Several open cases is a signal only when this message is itself about
    # a fault. A customer with open cases asking where their parcel is does
    # not become urgent because of the other cases.
    if prior_cases >= 2 and complaint.is_complaint:
        return HIGH, (f"{prior_cases} other cases are already open for this "
                      f"customer and this is a further fault report")

    if complaint.is_complaint:
        return MEDIUM, (f"complaint ({complaint.category}) that does not "
                        f"currently prevent all use")
    if _hits(text, SPEC_DISCREPANCY):
        return MEDIUM, ("possible product discrepancy against the "
                        "specification; needs investigation before any "
                        "conclusion about a defect")
    if _hits(text, QA_DOC_CONCERN):
        return MEDIUM, ("possible document-control concern raised about the "
                        "instructions themselves")
    if _hits(text, ORDER_STATUS):
        return LOW, "order or delivery status enquiry"
    return LOW, "general enquiry with no reported fault"


def triage(text: str, history: dict = None) -> Triage:
    ae = assess_adverse_event(text)
    comp = assess_complaint(text)
    qual = assess_quality(text)
    priority, reason = assess_priority(text, ae, comp, history)

    frustration = _hits(text, FRUSTRATION)

    if ae.is_adverse_event:
        classification = f"adverse_event_{ae.certainty}"
    elif ae.hazard_indicators:
        classification = "device_safety_hazard"
    elif qual.qa_document_concern:
        classification = "qa_document_concern"
    elif qual.qc_discrepancy:
        classification = "qc_discrepancy"
    elif comp.is_complaint:
        classification = f"complaint_{comp.category}"
    elif _hits(text, ORDER_STATUS):
        classification = "order_status_enquiry"
    else:
        classification = "general_enquiry"

    return Triage(
        adverse_event=ae, complaint=comp, quality=qual,
        priority=priority, priority_reason=reason,
        classification=classification,
        signals={
            "frustration_detected": frustration,
            "frustration_affected_priority": False,
            "repeated_failure": _hits(text, REPEATED_FAILURE),
            "repeated_contact": _hits(text, REPEATED_CONTACT),
            "order_status": _hits(text, ORDER_STATUS),
        },
    )


def requires_immediate_escalation(t: Triage) -> bool:
    """True when ordinary troubleshooting must stop right now."""
    return t.priority == CRITICAL
