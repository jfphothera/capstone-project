"""Two-point customer and equipment verification.

Fictional business rule IV-1
----------------------------
Before the agent gives device-specific recommendations it must verify the
customer using **any two** of:

  * customer name
  * date of birth
  * exact device serial number

A serial number is matched to one exact canonical ERP value. Harmless case,
space and hyphen presentation differences may be normalised only when they
resolve to one unambiguous canonical value. It is never fuzzy-matched, never
partially matched, and a near-miss is reported rather than quietly corrected.

Phone number, e-mail address and postal details are *locators*. They narrow
the candidate set and can resolve a name discrepancy (a married name, a
practice name versus a contact name), but on their own they never satisfy
the two-point requirement.

Where information is missing or contradictory the result says so. It never
guesses.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from dataclasses import dataclass, field
from typing import Optional

from . import config

POINT_NAME = "customer_name"
POINT_DOB = "date_of_birth"
POINT_SERIAL = "serial_number"
VERIFICATION_POINTS = (POINT_NAME, POINT_DOB, POINT_SERIAL)
REQUIRED_POINTS = 2

# Fictional formats only. They deliberately do not encode model or revision.
_SERIAL_NUMERIC = r"(?:\d[\s-]*){7}\d"
_SERIAL_SUFFIXED = r"\d{5}[\s-]*[A-Z][\s-]*[A-Z][\s-]*\d"
SERIAL_RE = re.compile(
    r"(?<![A-Z0-9])(" + _SERIAL_SUFFIXED + r"|" + _SERIAL_NUMERIC
    + r")(?![A-Z0-9])", re.IGNORECASE)
DOB_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")
PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
# "nov 11 2001", "November 11, 2001"  and  "11 Nov 2001", "11 November 2001"
MONTH_DOB_RE = re.compile(
    r"\b(?:(?P<mon1>" + _MONTH_ALT + r")\.?\s+(?P<day1>\d{1,2})(?:st|nd|rd|th)?"
    r"\s*,?\s*(?P<year1>\d{4})"
    r"|(?P<day2>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon2>" + _MONTH_ALT + r")\.?"
    r"\s*,?\s*(?P<year2>\d{4}))\b", re.IGNORECASE)

ANY_DOB_RE = re.compile(
    "(?:" + DOB_RE.pattern + ")|(?:" + MONTH_DOB_RE.pattern + ")",
    re.IGNORECASE)


def normalise_phone(value) -> str:
    return re.sub(r"\D", "", value or "")


def normalise_name(value) -> str:
    import unicodedata
    value=unicodedata.normalize('NFC',str(value or '')).lower()
    return ' '.join(''.join(c for c in value if c.isalpha() or c.isspace()).split())


@dataclass(frozen=True)
class SerialResolution:
    """Result of applying the fictional serial presentation rules."""

    canonical: Optional[str]
    status: str
    matches: tuple[str, ...] = ()


def _serial_shape(value: str) -> Optional[str]:
    """Return the canonical fictional shape, without consulting the ERP."""
    compact = re.sub(r"[\s-]", "", (value or "").strip()).upper()
    if re.fullmatch(r"\d{8}", compact):
        return compact
    if re.fullmatch(r"\d{5}[A-Z]{2}\d", compact):
        return f"{compact[:5]}-{compact[5:]}"
    return None


def resolve_serial(value: str, known_serials=()) -> SerialResolution:
    """Resolve presentation variants to exactly one canonical ERP serial.

    Only spaces, hyphens and letter case are normalised. Character
    substitution, partial matching and edit-distance correction are never
    attempted. An exact stored value wins; otherwise a normalised key must
    identify one and only one stored value.
    """
    raw = (value or "").strip().rstrip(".,;:")
    known = tuple(str(s) for s in (known_serials or ()))
    if raw in known:
        return SerialResolution(raw, "matched", (raw,))

    shaped = _serial_shape(raw)
    if shaped is None:
        return SerialResolution(None, "malformed")
    if not known:
        return SerialResolution(shaped, "canonical")

    matches = tuple(s for s in known if _serial_shape(s) == shaped)
    if len(matches) == 1:
        return SerialResolution(matches[0], "matched", matches)
    if len(matches) > 1:
        return SerialResolution(None, "ambiguous", matches)
    return SerialResolution(shaped, "unknown")


def normalise_dob(value) -> Optional[str]:
    """Return ISO yyyy-mm-dd, or None if it cannot be read confidently."""
    if not value:
        return None
    v = value.strip().rstrip(".,")
    def valid(year, month, day):
        try:
            parsed = date(int(year), int(month), int(day))
            return parsed.isoformat() if 1900 <= parsed.year <= date.today().year else None
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return valid(*v.split("-"))
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", v)
    if m:
        a, b, c = m.groups()
        year = int(c) + (1900 if len(c) == 2 and int(c) > 30 else
                         2000 if len(c) == 2 else 0)
        # Ambiguous between US and ISO ordering; only accept when the first
        # field cannot be a month, otherwise refuse rather than guess.
        if 1 <= int(a) <= 12 and 1 <= int(b) <= 12 and int(a) != int(b):
            return None
        if int(a) > 12:
            return valid(year, b, a)
        return valid(year, a, b)
    # Month names are unambiguous in either order, so both are accepted.
    m = MONTH_DOB_RE.fullmatch(v)
    if m:
        g = m.groupdict()
        mon = (g["mon1"] or g["mon2"] or "").lower().rstrip(".")
        day = int(g["day1"] or g["day2"])
        year = int(g["year1"] or g["year2"])
        month = MONTHS.get(mon)
        if month:
            return valid(year, month, day)
    return None


# "I am Having Trouble" is not an introduction. Capitalised sentence words
# that follow "I am"/"I'm" would otherwise be captured as a person's name and
# then reported as contradicting the account.
_NOT_NAME_WORDS = frozenset("""
having trying getting writing contacting calling emailing still unable
not just having wondering hoping looking after about with from very really
sorry afraid sure happy unhappy disappointed frustrated the a an my our your
yes yeah yep no nope ok okay hi hello hey thanks thank regards please
serial number model device unit machine account order shipment tracking
customer patient support service team today tomorrow yesterday morning
afternoon evening monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november
december cannot can't dont don't wont won't it its it's this that
i me need help turn on off work working wont lamp lights display screen
""".split())

# A name stated on its own is accepted only when we just asked for one.
# Without that gate every capitalised phrase in an e-mail body - product
# names, place names, the first word of a sentence - becomes a customer name.
_NAME_WORD = r"[A-Za-z][a-zA-Z'’\-]+"
_NAME_RUN_RE = re.compile(_NAME_WORD + r"(?:\s+" + _NAME_WORD + r"){1,2}")

# Labels that name the following words explicitly. One word is enough.
_NAME_LABEL_STRONG = re.compile(
    r"(?i:my name is|my name's|the name on the account is|"
    r"name on the account is|account name is|account name|patient name is|"
    r"patient name|customer name is|customer name|full name is|full name|"
    r"name\s*[:\-]|name is)\s*"
    r"(" + _NAME_WORD + r"(?:\s+" + _NAME_WORD + r"){0,2})")

# Weaker introductions. Require at least two capitalised words, so
# "this is urgent" and "I am Having Trouble" are not read as names.
_NAME_LABEL_WEAK = re.compile(
    r"(?i:this is|it'?s|its|i am|i'm|you'?re speaking to|speaking to)\s+"
    r"(" + _NAME_WORD + r"(?:\s+" + _NAME_WORD + r"){1,2})")

_NAME_SIGNOFF = re.compile(
    r"(?:thanks|thank you|regards|kind regards|best|sincerely|cheers)"
    r"\s*,?\s*\n+\s*(" + _NAME_WORD + r"(?:\s+" + _NAME_WORD + r"){1,2})\s*$",
    re.IGNORECASE | re.MULTILINE)

_DOB_LABEL = re.compile(
    r"(?:d\.?\s?o\.?\s?b\.?|date of birth|birth date|birthdate|birthday|"
    r"born(?:\s+on)?)", re.IGNORECASE)

_SERIAL_LABEL = re.compile(
    r"(?:serial(?:\s*(?:number|no\.?|#))?|s/n|\bsn\b)"
    r"\s*(?:is|[:#=-])?\s*", re.IGNORECASE)

# An unlabelled date is read as a date of birth only when we asked for one,
# or when the year is too old to be an order or delivery date.
_BIRTH_YEAR_CEILING = 2013


def _looks_like_a_name(candidate: str) -> bool:
    words = (candidate or "").split()
    if not words:
        return False
    return not any(w.lower().strip(".,'") in _NOT_NAME_WORDS for w in words)


def _mask(pattern, text: str) -> str:
    """Blank out a span so a later pattern cannot match inside it."""
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


# --- explicit corrections --------------------------------------------------

_CORRECTION_PHRASES = (
    "sorry", "apologies", "apologise", "apologize", "my mistake", "mistake",
    "i was wrong", "that was wrong", "wrong", "incorrect", "mistyped",
    "typo", "correction", "correct that", "i meant", "should be",
    "should have been", "scratch that", "ignore that", "disregard",
    "let me correct", "actually it", "actually its", "actually it's",
    "gave you the wrong", "that's not right", "thats not right",
)

_FIELD_WORDS = (
    ("serial", re.compile(r"\bserial|s/n", re.IGNORECASE)),
    ("dob", re.compile(r"date of birth|\bd\.?o\.?b\b|birth ?day|\bborn\b",
                       re.IGNORECASE)),
    ("name", re.compile(r"\bname\b", re.IGNORECASE)),
)


def correction_signal(text: str) -> dict:
    """Does this message say an earlier detail was wrong, and which one?

    Used so a customer can replace a mistyped serial or a wrong name instead
    of being stuck with the first value they happened to give.
    """
    low = (text or "").lower()
    corrects = any(p in low for p in _CORRECTION_PHRASES)
    fields = [f for f, rx in _FIELD_WORDS if corrects and rx.search(low)]
    return {"corrects": corrects, "fields": fields}


@dataclass
class Claim:
    """What the customer has told us, however they phrased it."""
    name: Optional[str] = None
    dob: Optional[str] = None
    serial: Optional[str] = None
    serial_issue: Optional[str] = None
    serial_input: Optional[str] = None
    serial_status: Optional[str] = None
    serial_candidates: tuple = ()
    serial_offsets: tuple = ()
    dob_issue: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None

    @classmethod
    def from_text(cls, text: str, known_serials=(), expecting=(), known_names=()) -> "Claim":
        """Pull whatever identification the message happens to contain.

        Only what the *customer* supplied counts. Nothing is filled in from
        the account record here - verifying a record against itself would
        be circular and would make the name point meaningless.

        `expecting` is the set of verification points the last question asked
        for. It widens what counts as an answer: a bare name or an unlabelled
        date is accepted as a reply to "what is the name on the account?" but
        is not harvested out of an unrelated e-mail.
        """
        t = text or ""
        expecting = tuple(expecting or ())

        # --- name, in order of how explicit the phrasing is
        name = None
        # Exact account-name spans support ordinary lower/upper-case input.
        # This is extraction only; no account data is filled into the claim.
        for known in sorted(known_names, key=len, reverse=True):
            match = re.search(r"(?<!\w)" + r"\s+".join(
                re.escape(p) for p in known.split()) + r"(?!\w)", t, re.I)
            if match:
                name = match.group(0)
                break
        name_text = _mask(ANY_DOB_RE, t)
        for rx in (_NAME_LABEL_STRONG, _NAME_LABEL_WEAK):
            m = rx.search(name_text)
            if not name and m and _looks_like_a_name(m.group(1)):
                name = re.split(r'\s+(?:and|my|dob|date|born)\b', m.group(1), flags=re.I)[0].strip()
                break
        if not name:
            m = _NAME_SIGNOFF.search(t.strip())
            if m and _looks_like_a_name(m.group(1)):
                name = m.group(1).strip()
        if not name and POINT_NAME in expecting:
            name = cls._bare_name(t)

        # --- date of birth: labelled first, then unlabelled if plausible
        dob = None
        dob_issue = None
        lab = _DOB_LABEL.search(t)
        if lab:
            dm = ANY_DOB_RE.search(t[lab.end():lab.end() + 30])
            if dm:
                dob = normalise_dob(dm.group(0))
                if not dob:
                    dob_issue = "ambiguous_or_invalid"
        if dob is None:
            for dm in ANY_DOB_RE.finditer(t):
                cand = normalise_dob(dm.group(0))
                if not cand:
                    if POINT_DOB in expecting:
                        dob_issue = "ambiguous_or_invalid"
                    continue
                if (POINT_DOB in expecting
                        or int(cand[:4]) <= _BIRTH_YEAR_CEILING):
                    dob = cand
                    break

        # --- serial: normalise presentation only to one canonical ERP value
        serial = serial_issue = serial_input = serial_status = None
        serial_candidates = []
        serial_offsets = []
        label = _SERIAL_LABEL.search(t)
        if label:
            tail = t[label.end():label.end() + 40]
            # A labelled value is not exempt from the DOB/date exclusion.
            # In particular, ``serial number: 1946-11-18`` contains an
            # eight-digit presentation-shaped substring and used to become a
            # device identifier.
            masked_tail = _mask(ANY_DOB_RE, tail)
            sm = SERIAL_RE.search(masked_tail)
            if sm:
                start = label.end() + sm.start(1)
                serial_input = t[start:start + len(sm.group(1))]
                resolution = resolve_serial(serial_input, known_serials)
                serial = (resolution.canonical if resolution.status in
                          ("matched", "canonical") else None)
                serial_status = resolution.status
                serial_candidates.append(serial_input)
                serial_offsets.append((start, start + len(serial_input)))
                if resolution.status in ("malformed", "ambiguous", "unknown"):
                    serial_issue = resolution.status
            else:
                token = re.match(r"([A-Z0-9][A-Z0-9-]{2,20})", tail,
                                 re.IGNORECASE)
                if token:
                    serial_input = token.group(1)
                    serial_issue = "malformed"
                    serial_status = "malformed"
                    serial_candidates.append(serial_input)
                    serial_offsets.append((label.end() + token.start(1),
                                           label.end() + token.end(1)))
        if serial is None and serial_issue is None:
            masked = _mask(ANY_DOB_RE, t)
            masked = _mask(EMAIL_RE, masked)
            masked = _mask(PHONE_RE, masked)
            found = list(SERIAL_RE.finditer(masked))
            resolutions = []
            for match in found:
                candidate = t[match.start(1):match.end(1)]
                resolution = resolve_serial(candidate, known_serials)
                resolutions.append((candidate, match.start(1), match.end(1), resolution))
            usable = [row for row in resolutions
                      if row[3].status in ("matched", "canonical")]
            distinct = {row[3].canonical for row in usable}
            serial_candidates.extend(row[0] for row in resolutions)
            serial_offsets.extend((row[1], row[2]) for row in resolutions)
            if len(distinct) > 1:
                serial_issue = serial_status = "conflicting"
            elif usable:
                candidate, _, _, resolution = usable[0]
                serial_input = candidate
                serial = resolution.canonical
                serial_status = resolution.status
            # When the caller supplied the ERP's exact serial inventory,
            # unrelated numbers (order ids, dates, authorization codes) are
            # not silently promoted to a serial. Unknown serials are still
            # retained when the customer labels them explicitly above.
            if serial is None and serial_issue is None and found and not known_serials:
                serial_input = resolutions[0][0]
                resolved = resolve_serial(serial_input)
                serial = resolved.canonical
                serial_status = resolved.status

        # Detect multiple different valid identifiers even when the first one
        # followed a serial label.  Position never chooses authority.
        all_masked = _mask(ANY_DOB_RE, t)
        all_masked = _mask(EMAIL_RE, all_masked)
        all_masked = _mask(PHONE_RE, all_masked)
        all_rows = []
        for match in SERIAL_RE.finditer(all_masked):
            candidate = t[match.start(1):match.end(1)]
            resolution = resolve_serial(candidate, known_serials)
            if resolution.status in ("matched", "canonical"):
                all_rows.append((candidate, match.start(1), match.end(1), resolution))
        if len({row[3].canonical for row in all_rows}) > 1:
            serial = serial_input = None
            serial_issue = serial_status = "conflicting"
            serial_candidates = [row[0] for row in all_rows]
            serial_offsets = [(row[1], row[2]) for row in all_rows]

        phone = None
        pm = PHONE_RE.search(t)
        if pm:
            phone = pm.group(0)

        email = None
        em = EMAIL_RE.search(t)
        if em:
            email = em.group(0)

        return cls(name=name, dob=dob, dob_issue=dob_issue, serial=serial,
                   serial_issue=serial_issue, serial_input=serial_input,
                   serial_status=serial_status,
                   serial_candidates=tuple(serial_candidates),
                   serial_offsets=tuple(serial_offsets),
                   phone=phone, email=email)

    @staticmethod
    def _bare_name(text: str) -> Optional[str]:
        """A name typed on its own, once the other identifiers are removed."""
        cleaned = _mask(ANY_DOB_RE, text or "")
        cleaned = _mask(EMAIL_RE, cleaned)
        cleaned = _mask(PHONE_RE, cleaned)
        cleaned = _mask(SERIAL_RE, cleaned)
        cleaned = re.split(r"\band\s+my\b|\b(?:date of birth|dob|birthday)\b", cleaned, flags=re.I)[0]
        cleaned = re.sub(r"^\s*(?:hi|hello|yes,?\s*i can\.?|it'?s|my name is)\s*", "", cleaned, flags=re.I)
        candidate = cleaned.strip(' \n\r\t,.;:!?')
        m = _NAME_RUN_RE.fullmatch(candidate)
        if m and _looks_like_a_name(candidate):
            return candidate
        # A single word is a name only when it is essentially the whole reply.
        words = [w for w in re.split(r"[\s,.;:!?]+", cleaned.strip()) if w]
        if (len(words) == 1 and re.fullmatch(_NAME_WORD, words[0])
                and _looks_like_a_name(words[0])):
            return words[0]
        return None


@dataclass
class VerificationResult:
    verified: bool
    points_matched: list = field(default_factory=list)
    points_failed: list = field(default_factory=list)
    contradictions: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    locators_used: list = field(default_factory=list)
    account_id: Optional[str] = None
    customer_name: Optional[str] = None
    serial_number: Optional[str] = None
    candidates: list = field(default_factory=list)
    reason: str = ""
    # None while no single account has been located; False for the physician
    # and organization identities, which carry no date of birth at all and
    # must not be pushed down a name + DOB path that cannot succeed.
    dob_on_record: Optional[bool] = None
    customer_kind: Optional[str] = None

    @property
    def needs_more_information(self) -> bool:
        return not self.verified and not self.contradictions

    def as_dict(self):
        return {
            "verified": self.verified,
            "points_matched": self.points_matched,
            "points_failed": self.points_failed,
            "contradictions": self.contradictions,
            "missing": self.missing,
            "locators_used": self.locators_used,
            "account_id": self.account_id,
            "customer_name": self.customer_name,
            "serial_number": self.serial_number,
            "candidates": self.candidates,
            "reason": self.reason,
            "dob_on_record": self.dob_on_record,
            "customer_kind": self.customer_kind,
        }

    @property
    def usable_points(self) -> tuple:
        """Which of the three points can realistically be matched.

        Rule IV-1 is unchanged - any two of name, date of birth and exact
        serial. This only reports that for an account with no date of birth
        on file, the two that remain are name and serial.
        """
        if self.dob_on_record is False:
            return (POINT_NAME, POINT_SERIAL)
        return VERIFICATION_POINTS


def _dedupe(rows) -> list:
    seen, out = set(), []
    for r in rows:
        if r and r["account_id"] not in seen:
            seen.add(r["account_id"])
            out.append(r)
    return out


# `normalise_name` strips punctuation from the claim, so the stored name has
# to be stripped the same way or "Dr. Maria Wilson" never matches
# "dr maria wilson" and a real customer looks like a stranger.
_NAME_SQL = ("replace(replace(replace(replace(lower(customer_name),"
             "'.',''),',',''),'-',''),'''','')")


def _by_name(store, name) -> list:
    exact = store.query(
        f"SELECT * FROM customers WHERE {_NAME_SQL}=?",
        (normalise_name(name),))
    if exact:
        return exact
    return store.query(
        f"SELECT * FROM customers WHERE {_NAME_SQL} LIKE ?",
        (f"%{normalise_name(name)}%",))


def _by_contact(store, claim: Claim) -> list:
    rows = []
    if claim.email:
        rows += store.query("SELECT * FROM customers WHERE lower(email)=?",
                            (claim.email.lower(),))
    if claim.phone:
        digits = normalise_phone(claim.phone)
        if len(digits) >= 10:
            rows += store.query(
                "SELECT * FROM customers WHERE "
                "replace(replace(replace(replace(phone,'(',''),')',''),"
                "' ',''),'-','') LIKE ? OR "
                "replace(replace(replace(replace(mobile_phone,'(',''),')',''),"
                "' ',''),'-','') LIKE ?",
                (f"%{digits[-10:]}", f"%{digits[-10:]}"))
    return _dedupe(rows)


def _by_dob(store, dob: str) -> list:
    value = normalise_dob(dob)
    if not value:
        return []
    return store.query("SELECT * FROM customers WHERE date_of_birth=?",
                       (value,))


def _candidates_by_locator(store, claim: Claim) -> list:
    """Find whose account this probably is, before verifying anything.

    The name is tried first. A serial is only used to *locate* a record when
    nothing else does, because if the name points at one account and the
    serial at another, the useful finding is that the serial belongs to
    someone else - not that the name failed to match whoever owns the serial.
    """
    by_name = _by_name(store, claim.name) if claim.name else []
    by_contact = _by_contact(store, claim)
    by_dob = _by_dob(store, claim.dob) if claim.dob else []

    by_serial = []
    if claim.serial:
        dev = store.device(claim.serial)
        if dev and dev.get("account_id"):
            owner = store.customer(dev["account_id"])
            by_serial = [owner] if owner else []

    pools = [p for p in (by_name, by_contact, by_dob, by_serial) if p]
    if pools:
        # When several locators agree, take the intersection; when they
        # disagree, keep everything so the ambiguity is reported.
        common = set.intersection(
            *[{r["account_id"] for r in p} for p in pools])
        if common:
            return [r for r in _dedupe(sum(pools, []))
                    if r["account_id"] in common]
        # A unique name/contact locator plus a serial or DOB for somebody
        # else is a contradiction on that located account, not a generic
        # multi-candidate result. Preserve that account so ``verify`` can
        # record the exact cross-account conflict and trigger handoff.
        if len(by_name) == 1:
            return by_name
        if len(by_contact) == 1:
            return by_contact
        return _dedupe(sum(pools, []))

    return []


def verify(store, claim: Claim, account_id: Optional[str] = None
           ) -> VerificationResult:
    """Apply rule IV-1 against the simulated ERP."""
    res = VerificationResult(verified=False)

    candidates = ([store.customer(account_id)] if account_id
                  else _candidates_by_locator(store, claim))
    candidates = [c for c in candidates if c]

    if claim.email:
        res.locators_used.append("email")
    if claim.phone:
        res.locators_used.append("phone")

    if not candidates:
        res.missing = [p for p in VERIFICATION_POINTS
                       if not getattr(claim, {"customer_name": "name",
                                              "date_of_birth": "dob",
                                              "serial_number": "serial"}[p])]
        # A serial that is in no record is worth saying so, even with nothing
        # else to go on: it is a different finding from "you have told me
        # nothing", and the customer can act on it.
        if claim.serial_issue:
            res.points_failed.append(POINT_SERIAL)
            issue = ("matches more than one canonical ERP serial"
                     if claim.serial_issue == "ambiguous" else
                     "does not meet a fictional serial format")
            res.reason = (f"serial {claim.serial_input!r} {issue}; it was not "
                          "corrected or guessed")
        elif claim.serial and store.device(claim.serial) is None:
            res.points_failed.append(POINT_SERIAL)
            res.reason = (
                f"serial {claim.serial!r} is not in the ERP; serials are "
                f"matched exactly and are never corrected or guessed")
        elif claim.name and not _by_name(store, claim.name):
            res.reason = (f"no customer named {claim.name!r} is on file")
        else:
            res.reason = ("no candidate customer could be located from the "
                          "details supplied")
        return res

    if len(candidates) > 1:
        res.candidates = [c["account_id"] for c in candidates]
        res.reason = (f"{len(candidates)} customers match the details "
                      f"supplied; more information is needed to tell them "
                      f"apart")
        return res

    cust = candidates[0]
    res.account_id = cust["account_id"]
    res.customer_name = cust["customer_name"]
    res.dob_on_record = bool(cust.get("date_of_birth"))
    res.customer_kind = cust.get("customer_kind")

    # --- point 1: name
    if claim.name:
        if normalise_name(claim.name) == normalise_name(cust["customer_name"]):
            res.points_matched.append(POINT_NAME)
        elif normalise_name(claim.name) in normalise_name(
                cust["customer_name"]) or normalise_name(
                cust["customer_name"]) in normalise_name(claim.name):
            # A partial name match counts only when a locator already tied
            # us to this record.
            if res.locators_used:
                res.points_matched.append(POINT_NAME)
            else:
                res.points_failed.append(POINT_NAME)
        elif _by_name(store, claim.name):
            # The name belongs to a different real customer. That is a
            # genuine conflict and must not be resolved by the agent.
            res.contradictions.append(
                f"name given ({claim.name!r}) does not match the account "
                f"holder ({cust['customer_name']!r})")
            res.points_failed.append(POINT_NAME)
        else:
            # The name matches nobody on file. An unrecognised string is not
            # evidence of a different person - it is more likely a nickname,
            # a practice name, or something the extractor misread - so the
            # point simply does not count.
            res.points_failed.append(POINT_NAME)
            res.missing.append(POINT_NAME)
    else:
        res.missing.append(POINT_NAME)

    # --- point 2: date of birth
    if claim.dob:
        want = normalise_dob(cust.get("date_of_birth"))
        got = normalise_dob(claim.dob)
        if want and got and want == got:
            res.points_matched.append(POINT_DOB)
        elif want and got:
            res.contradictions.append(
                f"date of birth given ({got}) does not match the record "
                f"({want})")
            res.points_failed.append(POINT_DOB)
        elif not want:
            # A physician or organization account holds no date of birth, so
            # the customer supplying one proves nothing either way. It is not
            # a contradiction and it is not a point.
            res.points_failed.append(POINT_DOB)
        else:
            res.missing.append(POINT_DOB)
    elif res.dob_on_record is not False:
        res.missing.append(POINT_DOB)

    # --- point 3: exact serial number
    if claim.serial_issue:
        res.points_failed.append(POINT_SERIAL)
        res.missing.append(POINT_SERIAL)
    elif claim.serial:
        dev = store.device(claim.serial)
        if dev is None:
            res.contradictions.append(
                f"serial {claim.serial!r} is not in the ERP; serials are "
                f"matched exactly and are never corrected or guessed")
            res.points_failed.append(POINT_SERIAL)
        elif dev["account_id"] != cust["account_id"]:
            res.contradictions.append(
                f"serial {claim.serial!r} belongs to a different account "
                f"({dev['account_id']}), not {cust['account_id']}")
            res.points_failed.append(POINT_SERIAL)
        else:
            res.points_matched.append(POINT_SERIAL)
            res.serial_number = claim.serial
    else:
        res.missing.append(POINT_SERIAL)

    if res.contradictions:
        res.reason = "contradictory identification: " + "; ".join(
            res.contradictions)
        return res

    if len(res.points_matched) >= REQUIRED_POINTS:
        res.verified = True
        res.reason = (f"verified on {len(res.points_matched)} points: "
                      f"{', '.join(res.points_matched)}")
    else:
        need = REQUIRED_POINTS - len(res.points_matched)
        res.reason = (
            f"only {len(res.points_matched)} verification point(s) matched "
            f"({', '.join(res.points_matched) or 'none'}); {need} more "
            f"needed from {', '.join(res.missing) or 'the remaining points'}")
    return res


_FRIENDLY_POINT = {
    POINT_NAME: "the name on the account",
    POINT_DOB: "the date of birth on the account",
    POINT_SERIAL: "the serial number from the label on the unit",
}


def points_to_request(res: VerificationResult, attempt: int = 0,
                      limit: Optional[int] = None) -> list:
    """Which verification points still need asking for, in a useful order.

    Two things shape the order. Only points that *can* match are offered, so
    an account with no date of birth is never asked for one. And after the
    first unsuccessful exchange the serial is asked for first, because it
    identifies the actual unit as well as verifying the account - repeating
    the same name-and-date question is what strands a customer.
    """
    usable = res.usable_points
    still = [p for p in usable if p not in res.points_matched]
    if limit:
        # The question skips a point we already know is wrong - but the
        # accept-list (limit 0) keeps it, so a corrected value is still read.
        narrowed = [p for p in still if p not in res.points_failed]
        still = narrowed or still
    if attempt >= 1 and POINT_SERIAL in still:
        still = [POINT_SERIAL] + [p for p in still if p != POINT_SERIAL]
    if limit is None:
        limit = max(REQUIRED_POINTS - len(res.points_matched), 1)
    return still[:limit] if limit else still


def request_message(res: VerificationResult, attempt: int = 0) -> str:
    """What to ask the customer for, without re-asking for what we have."""
    if res.contradictions:
        return ("Some of the details do not match our records, so I am not "
                "able to confirm the account from here. A member of the "
                "support team will pick this up with you.")

    asks = [_FRIENDLY_POINT[p] for p in points_to_request(res, attempt)]
    if not asks:
        asks = [_FRIENDLY_POINT[POINT_SERIAL]]

    if attempt == 0:
        lead = ("Thank you for contacting us. I'd be happy to help. To make "
                "sure I am looking at the correct equipment information, "
                "could you please provide ")
    elif res.account_id is None:
        lead = ("I still have not been able to find the account from that. "
                "To be sure I am looking at the right unit, could you give "
                "me ")
    else:
        lead = ("Thank you. I have found the account and only need one more "
                "detail to confirm the correct equipment. Could you please "
                "provide ")
    return lead + " and ".join(asks) + "?"


def unmatched_message(res: VerificationResult) -> str:
    """Said when the details were read correctly but match no record.

    Deliberately different wording from `request_message`, so a customer
    whose details simply are not in our records is not asked the same
    question a fourth time as though we had not understood them.
    """
    return ("I have those details, but I cannot find an account that matches "
            "them, so I am not able to confirm this one from here. Rather "
            "than keep asking you the same thing, I will pass this to a "
            "member of the support team with everything you have told me and "
            "they will check it with you directly.")


# --- device revision provenance ------------------------------------------

@dataclass
class RevisionAssessment:
    revision: Optional[str]
    status: str
    basis: str
    usable_for_troubleshooting: bool
    reason: str
    human_confirmed_by: Optional[str] = None

    def as_dict(self):
        return dict(self.__dict__)


def case_revision_authorized(store, case):
    """Identity and a proposed/rejected attestation never grant revision trust."""
    device = store.device(case.get('serial_number')) if case.get('serial_number') else None
    if not device or case.get('revision_confirmation') == 'rejected':
        return False
    human = (case.get('revision_confirmed_by') if
             case.get('revision_confirmation') == 'confirmed' and case.get('revision_confirmed_at')
             and case.get('model') == device.get('model')
             and case.get('revision') == device.get('revision') else None)
    return assess_revision(device, human_confirmation=human).usable_for_troubleshooting


def _device_value(device, key, default=None):
    if isinstance(device, dict):
        return device.get(key, default)
    return getattr(device, key, default) if device is not None else default


def _revision_register():
    path = os.path.join(config.DATA_DIR, "revision_register.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle).get("entries", {})
    except (FileNotFoundError, OSError, ValueError):
        return {}


def assess_revision(device: dict, human_confirmation: Optional[str] = None
                    ) -> RevisionAssessment:
    """Decide whether this revision may drive revision-specific instructions.

    Rule RV-1: an install-date inference is not enough on its own. Either the
    revision register verified it, or a named human confirmed it on the case.
    """
    revision = _device_value(device, "revision")
    serial = _device_value(device, "serial_number")
    status = _device_value(device, "revision_status")
    basis = _device_value(device, "revision_basis", "") or ""

    # The legacy in-memory ERP device lacks revision_status.  Reconstruct the
    # same RV-1 authority from the controlled register so direct tool callers
    # cannot turn mere revision presence into confirmation.
    if not status:
        entry = _revision_register().get(serial, {}) if serial else {}
        verified = entry.get("verified_revision")
        if verified and revision and verified != revision:
            status = config.REVISION_CONTRADICTORY
            basis = f"revision register says {verified}; ERP record says {revision}"
        elif verified:
            status = config.REVISION_VERIFIED
            revision = verified
            basis = (f"revision register entry verified by {entry.get('verified_by')} "
                     f"on {entry.get('verified_on')}")
        elif revision:
            status = config.REVISION_INFERRED
            basis = basis or "revision present without a controlled register verification"
        else:
            status = config.REVISION_MISSING

    if human_confirmation:
        return RevisionAssessment(
            revision, status, basis, True,
            f"revision confirmed on this case by {human_confirmation}",
            human_confirmed_by=human_confirmation)

    if status == config.REVISION_VERIFIED:
        return RevisionAssessment(revision, status, basis, True,
                                  "revision verified in the revision register")
    if status == config.REVISION_CONTRADICTORY:
        return RevisionAssessment(
            revision, status, basis, False,
            "revision records contradict each other; a person has to settle "
            "which revision this unit actually is")
    if status == config.REVISION_MISSING:
        return RevisionAssessment(
            revision, status, basis, False,
            "no revision is recorded for this unit and none can be inferred")
    return RevisionAssessment(
        revision, status, basis, False,
        f"revision {revision} is inferred from the install date only and has "
        f"not been verified; revision-specific instructions need confirmation "
        f"first")


def revision_context(device, human_confirmation: Optional[str] = None) -> dict:
    """Structured context accepted by every revision-sensitive QMS boundary."""
    assessment = assess_revision(device, human_confirmation=human_confirmation)
    return {
        "serial_number": _device_value(device, "serial_number"),
        "model": _device_value(device, "model"),
        "revision": assessment.revision,
        "status": assessment.status,
        "basis": assessment.basis,
        "usable_for_troubleshooting": assessment.usable_for_troubleshooting,
        "reason": assessment.reason,
        "human_confirmed_by": assessment.human_confirmed_by,
    }
