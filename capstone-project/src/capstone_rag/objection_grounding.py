"""Grounding of reviewer objections against the authorized evidence (rc.25, C3).

A reviewer objection is actionable only when it identifies:

* the draft span it is about (``draft_span``),
* the rule or source passage it relies on (``rule`` and/or ``source_ids``),
* the intended correction (``correction``).

This module checks what can be checked deterministically. It never decides
that a draft is correct; it decides whether an OBJECTION is supported by the
evidence that was actually presented:

=========================  ===================================================
verdict                    meaning
=========================  ===================================================
``supported``              a deterministic check confirms the objection
                           (private data disclosed before verification, an
                           instruction absent from every authorized passage,
                           an authorized task instruction that is missing)
``contradicted``           the objection asks to remove or forbid content
                           that an authorized, applicable passage explicitly
                           supports (e.g. the moulded-insert compartment check
                           of QMS-DOC-0111 S2)
``invalid_citation``       a cited source id was not in the authorized set
                           presented to the reviewer, or does not exist
``span_not_found``         the quoted draft span is not in the draft
``not_applicable``         the cited passage belongs to a co-retrieved
                           document whose task is not used by this draft
``unverified_semantic``    nothing checkable contradicts or confirms it; it
                           may go to the bounded writer as ADVISORY guidance
                           and the independent validator stays authoritative
``unstructured``           legacy free-text instruction without span/rule
=========================  ===================================================

Source existence alone is never sufficient: an ``add``/``remove`` objection is
supported or contradicted by what the cited passage SAYS, not by its id.
"""
from __future__ import annotations

import hashlib
import json
import re

GROUNDING_VERSION = 'objection-grounding-2026-11-19.1'
OBJECTION_KINDS = ('remove', 'add', 'change', 'identity', 'safety', 'tone', 'no_defect')
SUPPORT_RECALL = 0.6          # share of a sentence's content words found in a passage
ABSENT_RECALL = 0.35          # below this a required passage is treated as missing
_SPELLING = {'moulded': 'molded', 'mould': 'mold', 'colour': 'color', 'centre': 'center',
             'odour': 'odor', 'licence': 'license', 'behaviour': 'behavior', 'labelled': 'labeled'}
_NEGATED_SOURCE_CLAIM = re.compile(r"\b(not (?:in|mentioned|supported|authori[sz]ed|allowed|permitted)|unsupported|no such|"
                                   r"forbidden|prohibited|must not|should not|isn.t (?:in|mentioned)|not part of)\b", re.I)

# rc.25 review round 2: bounded so a worst-case reviewer reply fits its
# declared budget (agentic_reviewer.REVIEWER_MAX_TOKENS). A cut draft_span is
# still a prefix of the quoted text, so span matching keeps working.
OBJECTION_SCHEMA = {'type': 'array', 'maxItems': 3, 'items': {'type': 'object', 'properties': {
    'kind': {'type': 'string', 'enum': list(OBJECTION_KINDS)},
    'draft_span': {'type': 'string', 'maxLength': 160},
    'rule': {'type': 'string', 'maxLength': 120},
    'source_ids': {'type': 'array', 'items': {'type': 'string', 'maxLength': 64}, 'maxItems': 2},
    'correction': {'type': 'string', 'maxLength': 200}},
    'required': ['kind', 'draft_span', 'rule', 'source_ids', 'correction'], 'additionalProperties': False}}


def _norm_words(text):
    from .grounding import content_words
    words = set()
    for w in content_words(text or ''):
        words.add(_SPELLING.get(w, w))
    return words


def recall(sentence, passage):
    cw = _norm_words(sentence)
    if not cw:
        return 0.0
    return len(cw & _norm_words(passage)) / len(cw)


def _sentences(text):
    from .grounding import split_sentences
    return split_sentences((text or '').split('[For representative:')[0])


def _strip(source_id):
    return str(source_id or '').split('source:', 1)[-1].strip()


def fingerprint(objection):
    core = {k: re.sub(r'\s+', ' ', str(objection.get(k) or '')).strip().lower() for k in ('kind', 'draft_span', 'correction')}
    core['source_ids'] = sorted(_strip(s) for s in objection.get('source_ids') or [])
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def normalise(objections, legacy_text=None):
    """Structured objections from a reviewer decision (or legacy free text)."""
    out = []
    for o in objections or []:
        if not isinstance(o, dict) or o.get('kind') not in OBJECTION_KINDS:
            continue
        out.append({'kind': o['kind'], 'draft_span': str(o.get('draft_span') or '')[:240], 'rule': str(o.get('rule') or '')[:200],
                    'source_ids': [_strip(s) for s in (o.get('source_ids') or []) if isinstance(s, str) and s.strip()][:4],
                    'correction': str(o.get('correction') or '')[:300]})
    if not out and (legacy_text or '').strip():
        out.append({'kind': 'change', 'draft_span': '', 'rule': '', 'source_ids': [], 'correction': legacy_text.strip()[:300],
                    'legacy_unstructured': True})
    for o in out:
        o['objection_id'] = 'OBJ-' + fingerprint(o)
    return out


def _span_sentences(span, draft):
    if not span.strip():
        return []
    sentences = _sentences(draft)
    low = span.lower().strip(' ."\'')
    hits = [s for s in sentences if low and low in s.lower()]
    if hits:
        return hits
    return [s for s in sentences if recall(span, s) >= SUPPORT_RECALL and recall(s, span) >= 0.4]


def _supporting(sentence, passages):
    best = None
    for p in passages:
        score = recall(sentence, p['text'])
        if score >= SUPPORT_RECALL and (best is None or score > best[1]):
            best = (p['chunk_id'], score)
    return best


def adjudicate(objections, draft_body, passages, *, presented_ids=None, used_document_ids=None,
               private_values=(), identity_verified=False, safety_hold=False):
    """Verdict per objection.

    ``passages``: authorized applicable passages [{chunk_id, document_id, text}];
    ``presented_ids``: the authorized source ids the reviewer was shown;
    ``used_document_ids``: documents whose task content the draft relies on;
    ``private_values``: account facts that may not appear before verification.
    """
    by_id = {p['chunk_id']: p for p in passages}
    presented = set(_strip(i) for i in (presented_ids if presented_ids is not None else by_id))
    used_docs = set(used_document_ids or ())
    results = []
    for o in objections:
        verdict, reason, evidence = 'unverified_semantic', 'no deterministic check confirms or contradicts this objection', {}
        cited = [i for i in o.get('source_ids') or []]
        bad = [i for i in cited if i not in by_id or i not in presented]
        spans = _span_sentences(o.get('draft_span') or '', draft_body)
        if o.get('legacy_unstructured'):
            verdict, reason = 'unstructured', 'free-text instruction without a draft span, rule or source'
        elif o['kind'] == 'no_defect':
            verdict, reason = 'supported', 'explicit no-defect finding'
        elif o['kind'] == 'identity':
            leaked = [v for v in private_values if v and str(v).lower() in (draft_body or '').lower()]
            if not identity_verified and leaked:
                verdict, reason, evidence = 'supported', 'draft shows account facts while identity is unverified', {'disclosed': len(leaked)}
            elif identity_verified and not leaked:
                verdict, reason = 'unverified_semantic', 'identity is verified; no private account fact disclosed before verification'
        elif o['kind'] == 'safety' and safety_hold:
            verdict, reason = 'supported', 'case is under a safety hold; safety objections are retained'
        elif bad:
            verdict, reason, evidence = 'invalid_citation', 'cited source id is not among the authorized sources presented', {'invalid_source_ids': bad}
        elif o['kind'] in ('remove', 'change') and o.get('draft_span') and not spans:
            verdict, reason = 'span_not_found', 'the quoted span does not occur in the draft'
        elif o['kind'] in ('remove', 'change') and spans:
            claims_unsupported = bool(_NEGATED_SOURCE_CLAIM.search((o.get('rule') or '') + ' ' + (o.get('correction') or '')))
            support = [(_supporting(s, passages), s) for s in spans]
            supported = [(sp, s) for sp, s in support if sp]
            if supported and (o['kind'] == 'remove' or claims_unsupported) and o['kind'] != 'safety':
                verdict = 'contradicted'
                reason = 'an authorized applicable passage explicitly supports the content this objection asks to remove'
                evidence = {'supporting_chunk_ids': sorted({sp[0] for sp, _ in supported}), 'span_sentences': [s for _, s in supported][:3]}
            elif not supported and o['kind'] == 'remove':
                verdict = 'supported'
                reason = 'the targeted draft sentence is not supported by any authorized passage'
                evidence = {'span_sentences': spans[:3]}
        elif o['kind'] == 'add':
            targets = [by_id[i] for i in cited if i in by_id]
            if targets:
                applicable = [t for t in targets if not used_docs or t.get('document_id') in used_docs]
                if not applicable:
                    verdict, reason = 'not_applicable', 'cited passage belongs to a co-retrieved document whose task this draft does not use'
                    evidence = {'cited_documents': sorted({t.get('document_id') for t in targets}), 'used_documents': sorted(used_docs)}
                else:
                    wanted = o.get('correction') or o.get('rule') or ''
                    backing = max(recall(wanted, t['text']) for t in applicable) if wanted.strip() else 0.0
                    present = recall(wanted, draft_body) if wanted.strip() else 1.0
                    if backing < ABSENT_RECALL:
                        verdict, reason = 'unsupported_by_cited_source', 'the cited passage does not say what the objection asks to add'
                        evidence = {'cited_chunk_ids': [t['chunk_id'] for t in applicable], 'support': round(backing, 3)}
                    elif present >= SUPPORT_RECALL:
                        verdict, reason = 'contradicted', 'the draft already contains the cited authorized content'
                        evidence = {'covered_chunk_ids': [t['chunk_id'] for t in applicable], 'coverage': round(present, 3)}
                    else:
                        verdict, reason = 'supported', 'an applicable authorized instruction is missing from the draft'
                        evidence = {'missing_from_chunk_ids': [t['chunk_id'] for t in applicable], 'coverage': round(present, 3)}
        elif o['kind'] == 'safety' and safety_hold:
            verdict, reason = 'supported', 'case is under a safety hold; safety objections are retained'
        results.append({**o, 'verdict': verdict, 'reason': reason, 'evidence': evidence, 'grounding_version': GROUNDING_VERSION})
    return results


def actionable(results):
    """Objections the writer may act on (supported first, advisory second)."""
    return [r for r in results if r['verdict'] in ('supported', 'unverified_semantic', 'unstructured') and r['kind'] != 'no_defect']


def rejected(results):
    return [r for r in results if r['verdict'] in ('contradicted', 'invalid_citation', 'span_not_found', 'not_applicable',
                                                   'unsupported_by_cited_source')]
