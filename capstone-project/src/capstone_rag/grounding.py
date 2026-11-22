"""Verify that a generated reply actually came from its approved source.

The Checkpoint 3.1 validator proved a reply *cites* an authorised procedure. It
did not prove the language model added nothing of its own. This module closes
that gap with two independent checks, both of which run after generation and
neither of which asks a model to judge its own work:

1. **Sentence support.** Every sentence in the reply that gives an instruction
   or makes a device-specific claim must be traceable to a sentence in the
   accepted source extracts. Support is established either by embedding
   similarity (so honest paraphrase survives) or by content-word recall (so a
   near-verbatim lift survives even if the encoder scores it oddly). A sentence
   that satisfies neither is reported as unsupported.

2. **Identifier transcription.** Serial numbers, procedure IDs and document IDs
   in the reply are compared character-for-character against the ERP record and
   the accepted sources. This is exact string work, not similarity, because a
   serial that is one character wrong is wrong.

The second check exists because it caught a real defect: Qwen3 4B rendered the
serial 98148469 as "981484:69" in a captured demonstration run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from . import config, embeddings

# Words carrying no topical content, ignored when measuring overlap.
STOPWORDS = frozenset("""
a an and are as at be been before both but by can could did do does doing done
for from had has have having he her his i if in into is it its me my no nor not
of on or our out over own she should so than that the their them then there
these they this those to too under until up was we were what when where which
while who will with would you your yours am being here how il ll re ve s t
""".split())

# A sentence is worth checking if it tells the customer to do something.
INSTRUCTION_MARKERS = (
    "please", "could you", "can you", "ask ", "verify", "check", "confirm",
    "unplug", "plug", "reseat", "remove", "replace", "ensure", "make sure",
    "do not", "don't", "avoid", "never", "switch", "turn ", "press", "push",
    "slide", "inspect", "clean", "allow", "move ", "retry", "try ", "disconnect",
    "reconnect", "leave", "report", "stop ", "let me know", "send ", "read ",
)

# Sentences that are pure courtesy carry no device claim and are not checked.
BOILERPLATE_PATTERNS = (
    r"^\s*(hello|hi|dear|good morning|good afternoon)\b",
    r"^\s*(kind regards|best regards|regards|thanks|thank you|many thanks)\b",
    r"^\s*customer support\s*$",
    r"^\s*(thank you for (reaching out|getting in touch|contacting))",
    r"^\s*(thanks for (reaching out|getting in touch|contacting))",
    r"^\s*(i am sorry|i'm sorry|sorry to hear)",
    r"^\s*(this follows|reference procedure|source)\b",
    r"^\s*let me know (the results|if|how)",
)
_BOILERPLATE_RE = [re.compile(p, re.IGNORECASE) for p in BOILERPLATE_PATTERNS]

# Statements about the support process rather than about the device: what the
# agent has looked up, what it will do next, who will be in touch. These are
# not traceable to a QMS extract because they are not claims about the
# hardware - the device facts inside them (serial, model, revision, procedure
# id) are checked separately and exactly by check_identifiers().
PROCESS_PATTERNS = (
    r'\bi can help with the public user manual\b',
    r'\bthese instructions apply across its supported variants\b',
    r'\bthey do not require access to your private account records\b',
    r"\bi (have |'ve )?(confirmed|checked|looked at|pulled up)\b.*"
    r"\b(account|record|system|file)\b",
    r"\bi (am|'m) working from\b",
    r"\bi (have |'ve )?(passed|escalated|raised|prepared|logged|opened)\b",
    r"\b(someone|a support engineer|our .* team|they) will (contact|come back"
    r"|reply|be in touch|look)\b",
    r"\bi (will|'ll) (raise|escalate|pass|arrange|check|come back)\b",
    r"\bi (cannot|can't|will not|won't) (give|guess|promise|invent)\b",
    r"\breply (here|to this)\b",
    r"\blet me know\b",
    r"\bi have looked up\b",
    r"\bthis follows\b",
)
_PROCESS_RE = [re.compile(p, re.IGNORECASE) for p in PROCESS_PATTERNS]

# ...but a sentence is only process talk if it does NOT also tell the customer
# to do something physical to the device. Without this guard, a model could
# smuggle a fabricated instruction in behind "I will" or "let me know".
DEVICE_ACTION_TERMS = (
    "unplug", "plug in", "reseat", "remove", "replace", "open the", "bridge",
    "measure", "press", "push", "slide", "clean", "bypass", "fuse", "interlock",
    "filter tray", "aperture", "lamp hour", "voltage", "probe", "screw",
    "panel", "enclosure", "cord", "outlet", "switch off", "power button",
    "hold the", "disconnect", "reconnect", "latch", "intake", "vent",
)


# Leading word boundary only, so "cord" matches "cords" but NOT "record" and
# "panel" matches "panels". A plain substring test produced exactly that bug:
# "on your account record" was read as mentioning a mains cord.
_DEVICE_ACTION_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in DEVICE_ACTION_TERMS) + r")",
    re.IGNORECASE,
)


def is_process_statement(sentence: str) -> bool:
    if _DEVICE_ACTION_RE.search(sentence):
        return False
    return any(p.search(sentence) for p in _PROCESS_RE)

# Split only on real sentence ends. NOT on ":" (it opens a list) and NOT on a
# bare newline (both the QMS documents and the model's output are hard-wrapped,
# so a newline usually sits mid-sentence).
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")
_BULLET = re.compile(r"^[-*•]\s+")
_WORD = re.compile(r"[a-z0-9']+")

# Identifier shapes we police.
_PROC_RE = re.compile(r"\bPROC-[A-Z0-9\-]+\b")
_DOC_RE = re.compile(r"\bQMS-DOC-[0-9]+\b")
# Serial-shaped: a run of 5-10 digits, optionally with the -LLN suffix the
# synthetic ERP uses. Matched loosely on purpose so a mangled serial still
# trips the check.
_SERIALISH_RE = re.compile(r"\b\d{5,10}(?:[-:][A-Za-z]{2}\d)?\b")


@dataclass
class SentenceCheck:
    sentence: str
    supported: bool
    best_similarity: float
    best_recall: float
    best_source: str = ""
    # "sentence" when the whole sentence matched, "clause" when the sentence
    # had to be broken up and one of its clauses failed.
    level: str = "sentence"
    failing_clause: str = ""
    authority: str = 'customer_qms'

    def as_dict(self):
        return {
            "sentence": self.sentence,
            "supported": self.supported,
            "best_similarity": round(self.best_similarity, 4),
            "best_recall": round(self.best_recall, 3),
            "best_source": self.best_source,
            "level": self.level,
            "failing_clause": self.failing_clause,
            'authority': self.authority,
        }


@dataclass
class GroundingReport:
    grounded: bool
    sentence_checks: list = field(default_factory=list)
    identifier_issues: list = field(default_factory=list)
    checked: int = 0
    skipped: int = 0
    authorization_issues: list = field(default_factory=list)
    context_issues: list = field(default_factory=list)

    @property
    def unsupported(self) -> list:
        return [c for c in self.sentence_checks if not c.supported]

    @property
    def reason(self) -> str:
        if self.grounded:
            return (f"all {self.checked} instruction sentence(s) traced to the "
                    f"accepted source; identifiers match")
        parts = []
        if self.authorization_issues:
            parts.append('response authorization: '+str(self.authorization_issues))
        if self.context_issues:
            parts.append('conversation consistency: '+str(self.context_issues))
        if self.unsupported:
            parts.append(
                f"{len(self.unsupported)} unsupported sentence(s): "
                + "; ".join(f"{c.sentence[:70]!r} (best sim "
                            f"{c.best_similarity:.2f})"
                            for c in self.unsupported[:3])
            )
        if self.identifier_issues:
            parts.append("identifier problems: " + "; ".join(
                self.identifier_issues[:3]))
        return " | ".join(parts)

    def feedback(self) -> str:
        """A correction note the generator can act on (Reflexion input)."""
        lines = []
        for c in self.unsupported:
            if c.level == "clause" and c.failing_clause:
                lines.append(
                    f"- This part of a sentence is not in the approved "
                    f"source, remove it and keep the rest: "
                    f"\"{c.failing_clause}\"  (from: \"{c.sentence}\")"
                )
            else:
                lines.append(
                    f"- Remove or rewrite this sentence, it is not in the "
                    f"approved source: \"{c.sentence}\""
                )
        for issue in self.identifier_issues:
            lines.append(f"- Fix this identifier error: {issue}")
        for issue in self.authorization_issues:
            lines.append(f'- Honor the response authority restriction: {issue}')
        for issue in self.context_issues:
            lines.append(f'- Use the received customer information: {issue}')
        return "\n".join(lines)

    def as_dict(self):
        return {
            "grounded": self.grounded,
            "reason": self.reason,
            "checked_sentences": self.checked,
            "skipped_sentences": self.skipped,
            "unsupported_count": len(self.unsupported),
            "identifier_issues": self.identifier_issues,
            'authorization_issues':self.authorization_issues,
            'context_issues':self.context_issues,
            "sentence_checks": [c.as_dict() for c in self.sentence_checks],
        }


# --- helpers ---------------------------------------------------------------

def _blocks(text: str) -> list:
    """Unwrap hard-wrapped text into logical blocks.

    A block ends at a blank line or at the start of a new bullet. Without this
    step a sentence wrapped across two lines is split into two fragments, and
    fragments look unsupported no matter how faithful the reply is.
    """
    blocks, cur = [], []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            if cur:
                blocks.append(" ".join(cur))
                cur = []
            continue
        if _BULLET.match(stripped) and cur:
            blocks.append(" ".join(cur))
            cur = []
        cur.append(_BULLET.sub("", stripped))
    if cur:
        blocks.append(" ".join(cur))
    return blocks


def split_sentences(text: str) -> list:
    out = []
    for block in _blocks(text):
        for raw in _SENT_SPLIT.split(block):
            s = " ".join(raw.split()).strip()
            if len(s) > 2:
                out.append(s)
    return out


def content_words(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower())
            if w not in STOPWORDS and len(w) > 2}


def is_boilerplate(sentence: str) -> bool:
    return any(p.search(sentence) for p in _BOILERPLATE_RE)


def is_checkable(sentence: str, source_vocab: set) -> bool:
    """Only police sentences that instruct or make a device-specific claim."""
    if is_boilerplate(sentence) or is_process_statement(sentence):
        return False
    low = sentence.lower()
    if any(m in low for m in INSTRUCTION_MARKERS):
        return True
    # A claim about the device: shares at least two topical words with the
    # source material.
    return len(content_words(sentence) & source_vocab) >= 2


# Clause boundaries. A sentence that mixes an authorised instruction with an
# invented one ("reseat the mains cord AND replace the 3.15A fuse") scores
# well as a whole, because most of it is faithful. Splitting on coordinating
# conjunctions and semicolons exposes the invented half.
_IMPERATIVES = (
    "check", "verify", "confirm", "replace", "remove", "reseat", "unplug",
    "measure", "clean", "inspect", "press", "hold", "slide", "push", "open",
    "bridge", "disconnect", "reconnect", "set", "turn", "leave", "retry",
    "run", "adjust", "tighten", "loosen", "swap",
)
_VERB_ALT = "|".join(_IMPERATIVES)

_CLAUSE_SPLIT = re.compile(
    "|".join([
        r";",                                   # explicit clause break
        r",\s+(?:and|then|also|plus|but)\s+",   # ", and ...", ", then ..."
        r"\s+and\s+then\s+",
        r"\s+and\s+also\s+",
        # "... and <imperative> ..." - a second instruction joined by "and"
        rf"\s+and\s+(?=(?:please\s+)?(?:{_VERB_ALT})\b)",
    ]),
    re.IGNORECASE,
)

MIN_CLAUSE_WORDS = 4


def split_clauses(sentence: str) -> list:
    """Break a sentence at coordinating boundaries.

    Only used when the whole sentence passed, to check that each half would
    pass on its own. Very short fragments are ignored - they carry no
    instruction and only add noise.
    """
    parts = [p.strip(" ,;") for p in _CLAUSE_SPLIT.split(sentence or "")]
    clauses = [p for p in parts if p and len(p.split()) >= MIN_CLAUSE_WORDS]
    return clauses if len(clauses) > 1 else []


def recall(sentence: str, source_sentence: str) -> float:
    cw = content_words(sentence)
    if not cw:
        return 1.0
    return len(cw & content_words(source_sentence)) / len(cw)


# --- identifier checking ---------------------------------------------------

def check_identifiers(response: str, device, selected_procedure,
                      accepted_chunks, reported_text='') -> list:
    """Exact-match every identifier in the reply. No similarity involved."""
    issues = []

    allowed_procs = {selected_procedure} if selected_procedure else set()
    allowed_procs |= {c.procedure_id for c in accepted_chunks}
    for found in set(_PROC_RE.findall(response or "")):
        if found not in allowed_procs:
            issues.append(
                f"procedure id {found!r} is not the authorised procedure "
                f"({sorted(allowed_procs) or 'none'})"
            )

    allowed_docs = {c.document_id for c in accepted_chunks}
    for found in set(_DOC_RE.findall(response or "")):
        if found not in allowed_docs:
            issues.append(
                f"document id {found!r} is not among the accepted sources "
                f"({sorted(allowed_docs) or 'none'})"
            )

    if device is not None:
        serial = device.serial_number
        # Any number in the reply that is shaped like a serial must BE the
        # serial. This is what catches a mis-transcribed digit or a stray
        # colon, which similarity scoring would never notice.
        for found in set(_SERIALISH_RE.findall(response or "")):
            if found == serial:
                continue
            if serial is None and found in set(_SERIALISH_RE.findall(reported_text)):
                continue
            # Numbers that genuinely appear in the source text are fine.
            if any(found in c.text for c in accepted_chunks):
                continue
            issues.append(
                f"{found!r} looks like a serial number but the confirmed "
                f"serial is {serial!r}"
            )
        # If the reply talks about a serial at all, it must get it right.
        if serial and re.search(r"serial", response or "", re.IGNORECASE) \
                and serial not in (response or ""):
            issues.append(
                f"reply mentions a serial number but does not contain the "
                f"confirmed serial {serial!r}"
            )

    return issues


# --- the main check --------------------------------------------------------

def is_supported(sim: float, rec: float) -> bool:
    """Two-signal support rule. See the commentary in config.py."""
    if sim >= config.GROUNDING_STRONG_SIM:
        return True
    if sim >= config.GROUNDING_MIN_SIM and rec >= config.GROUNDING_MIN_RECALL:
        return True
    return rec >= config.GROUNDING_STRONG_RECALL


def reported_fact_support(sentence, reported_text):
    """A factual acknowledgment may use customer evidence, never instructions.

    Require an acknowledgment form, exact content words and matching polarity.
    A whole sentence containing an imperative, causal diagnosis or operating
    action cannot enter this path, even if the customer supplied those words.
    """
    if not reported_text or _DEVICE_ACTION_RE.search(sentence):
        return False
    if not re.match(r"^(?:you (?:said|reported|mentioned)|i (?:see|understand)|your (?:device|unit|[A-Z0-9-]+))\b", sentence, re.I):
        return False
    if re.search(r'\b(?:please|should|must|need to|because|therefore|caused|means that|try|check|do not|read|note whether)\b', sentence, re.I):
        return False
    neutral = {'said', 'reported', 'mentioned', 'see', 'understand', 'device', 'unit', 'serial'}
    equivalents={'powers':'power','lamps':'lamp','stays':'','stay':'','remain':'','remains':'','still':''}
    def normalized(value):
        return {equivalents.get(word,word) for word in content_words(value)-neutral if equivalents.get(word,word)}
    words = normalized(sentence)
    if not words or not words <= normalized(reported_text):
        return False
    negative = lambda t: bool(re.search(r"\b(?:no|not|never|cannot|can't|isn't|doesn't|won't|off|dark)\b", t, re.I))
    return any(words <= normalized(part) and negative(sentence) == negative(part)
               for part in split_sentences(reported_text))


def verified_record_support(sentence, device):
    """Only canonical equipment identifiers and record acknowledgments.

    This establishes factual support; the caller's independent identity and
    disclosure gates still decide whether a protected record may be used.
    """
    if device is None or not getattr(device, 'account_id', None) or _DEVICE_ACTION_RE.search(sentence):
        return False
    if not re.match(r"^(?:i (?:have )?(?:confirmed|verified)|your (?:device|unit|[A-Z0-9-]+))\b", sentence, re.I):
        return False
    words = content_words(sentence)
    allowed = content_words('confirmed verified device unit model revision serial number account record matches')
    for key in ('model', 'revision', 'serial_number', 'customer_name', 'account_id'):
        allowed |= content_words(str(getattr(device, key, '') or ''))
    return bool(words) and words <= allowed


def diagnostic_question(sentence):
    """Request an observation; never authorize a physical operation."""
    if not sentence.rstrip().endswith('?') or _DEVICE_ACTION_RE.search(sentence):
        return False
    if re.search(r'\b(?:disassemble|dismantle|override|modify|adjust|test treatment|operate|restart|reset)\b',sentence,re.I):
        return False
    return bool(re.match(r'^(?:(?:could|can) you (?:tell|describe|confirm|share|report)|(?:what|which) (?:indicator|message|items|symptom|model|serial|observation))\b',sentence,re.I))


def observation_request(sentence):
    if _DEVICE_ACTION_RE.search(sentence) or re.search(r'\b(?:reset|restart|operate|adjust|modify|disassemble)\b',sentence,re.I):
        return False
    return bool(re.match(r'^If you (?:see|notice) (?:a|an|any) (?:message|code|indicator),? (?:please )?(?:share|report|describe) it (?:here|to us|with support)[.!]?$',sentence,re.I))


def check_grounding(response: str, accepted_chunks, device=None,
                    selected_procedure=None, reported_text='') -> GroundingReport:
    """Verify the reply against the accepted source extracts."""
    identifier_issues = check_identifiers(
        response, device, selected_procedure, accepted_chunks, reported_text=reported_text
    )

    source_sentences = []
    for c in accepted_chunks:
        # The section heading and document title are part of the source. A
        # reply that says "next we'll check the mains supply" is restating the
        # heading "Step 1 - Mains supply and cord check", which is legitimate.
        source_sentences.append(f"{c.section}.")
        source_sentences.append(f"{c.title}.")
        source_sentences.extend(split_sentences(c.text))
    source_vocab = set()
    for s in source_sentences:
        source_vocab |= content_words(s)

    candidates = split_sentences(response)
    fact_checks = [SentenceCheck(s, True, 0.0, 1.0, 'customer message', authority='customer_report')
                   for s in candidates if reported_fact_support(s, reported_text)]
    fact_checks += [SentenceCheck(s, True, 0.0, 1.0, 'verified equipment record', authority='verified_erp')
                    for s in candidates if verified_record_support(s, device) and not reported_fact_support(s, reported_text)]
    fact_checks += [SentenceCheck(s,True,0.0,1.0,'non-operating observation request',authority='diagnostic_question')
                    for s in candidates if diagnostic_question(s) or observation_request(s)]
    checkable = [s for s in candidates if is_checkable(s, source_vocab) and not reported_fact_support(s, reported_text)
                 and not verified_record_support(s, device) and not diagnostic_question(s) and not observation_request(s)]
    skipped = len(candidates) - len(checkable)

    if not checkable:
        return GroundingReport(
            grounded=not identifier_issues,
            sentence_checks=fact_checks,
            identifier_issues=identifier_issues,
            checked=0,
            skipped=skipped,
        )

    if not source_sentences:
        return GroundingReport(
            grounded=False,
            sentence_checks=[
                SentenceCheck(s, False, 0.0, 0.0, "") for s in checkable
            ],
            identifier_issues=identifier_issues,
            checked=len(checkable),
            skipped=skipped,
        )

    # Every sentence, plus the clauses of every sentence, in one encode.
    clause_map = {}          # sentence index -> list of clause strings
    clause_texts = []
    for i, sentence in enumerate(checkable):
        cl = split_clauses(sentence)
        if cl:
            clause_map[i] = cl
            clause_texts.extend(cl)

    vecs = embeddings.embed(checkable + clause_texts + source_sentences)
    resp_vecs = vecs[:len(checkable)]
    clause_vecs = vecs[len(checkable):len(checkable) + len(clause_texts)]
    src_vecs = vecs[len(checkable) + len(clause_texts):]
    sims = resp_vecs @ src_vecs.T
    clause_sims = clause_vecs @ src_vecs.T if len(clause_texts) else None

    clause_index = {}
    pos = 0
    for i, cl in clause_map.items():
        clause_index[i] = list(range(pos, pos + len(cl)))
        pos += len(cl)

    def score(row, text):
        best_i = int(np.argmax(row))
        best_sim = float(row[best_i])
        # Recall is measured against the best few sources, not only the best
        # by cosine, because a lifted sentence can split across two source
        # sentences.
        top = np.argsort(-row)[:3]
        best_rec = max(recall(text, source_sentences[j]) for j in top)
        return best_sim, best_rec, best_i

    checks = list(fact_checks)
    for i, sentence in enumerate(checkable):
        best_sim, best_rec, best_i = score(sims[i], sentence)
        supported = is_supported(best_sim, best_rec)
        level, failing = "sentence", ""

        # A sentence can pass as a whole while carrying an invented clause,
        # because the faithful majority drags the score up. Re-check each
        # clause on its own.
        if supported and i in clause_index:
            for text, ci in zip(clause_map[i], clause_index[i]):
                c_sim, c_rec, _ = score(clause_sims[ci], text)
                if not is_supported(c_sim, c_rec):
                    supported = False
                    level, failing = "clause", text
                    best_sim, best_rec = c_sim, c_rec
                    break

        checks.append(SentenceCheck(
            sentence=sentence,
            supported=supported,
            best_similarity=best_sim,
            best_recall=best_rec,
            best_source=source_sentences[best_i][:120],
            level=level,
            failing_clause=failing,
        ))

    grounded = all(c.supported for c in checks) and not identifier_issues
    return GroundingReport(
        grounded=grounded,
        sentence_checks=checks,
        identifier_issues=identifier_issues,
        checked=len(checks),
        skipped=skipped,
    )
