"""Persistent review sessions for the simulated reviewer (rc.21).

Each reviewer call is stateless, so understanding must be carried
explicitly. A review session, bound to the exact artifact version (SHA-256),
holds concise structured review notes (findings, contradictions, open
questions, missing material) with references to artifact sections and source
IDs. Every later call - another section, an evidence request, the final
decision - receives the notes. No private reasoning is requested or stored.

Per artifact part the session tracks ``presented``, ``reviewed`` (findings
recorded for it) and ``synthesised`` (carried into the final decision).
Approval of an artifact reviewed in parts requires: every part reviewed, the
notes carried into a final synthesis that fits the context budget, and every
contradiction / open question / missing-material note explicitly resolved.
The session file survives interruption; an artifact change archives the old
session and requires re-review.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

SESSION_VERSION = 'review-session-2026-11-19.1'  # rc.25: note source ids grounded in the presented set
NOTE_KINDS = ('finding', 'contradiction', 'open_question', 'missing_material')
BLOCKING_KINDS = ('contradiction', 'open_question', 'missing_material')
MAX_NOTE_CHARS = 300
MAX_EVIDENCE_ROUNDS = 3
NOTES_SCHEMA = {'type': 'array', 'maxItems': 8, 'items': {'type': 'object', 'properties': {
    'kind': {'type': 'string', 'enum': list(NOTE_KINDS)}, 'text': {'type': 'string', 'maxLength': MAX_NOTE_CHARS},
    'section': {'type': 'string'}, 'source_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 6}},
    'required': ['kind', 'text', 'section', 'source_ids'], 'additionalProperties': False}}
INSPECT_SYSTEM = ('You are reviewing ONE section of a longer customer-service artifact for a medical-device company, as the accountable '
                  'representative. Record concise review notes only (not reasoning): problems in this section, contradictions with the '
                  'prior review notes or with the sources shown, open questions, and material you still need. Reference the section id '
                  'and any source ids. Prior notes are your own earlier findings on other sections; keep them in mind.')


class ReviewSession:
    def __init__(self, run_dir, key, artifact_sha=None):
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', key)
        self.path = Path(run_dir) / 'review-sessions' / f'{safe}.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else None
        if data and data.get('artifact_sha') != artifact_sha:
            # The artifact changed: earlier understanding no longer applies.
            archived = {k: v for k, v in data.items() if k != 'invalidated'}
            data = {'invalidated': (data.get('invalidated') or []) + [{**archived, 'invalidated_at': time.time(),
                                                                        'reason': 'artifact changed; re-review required'}]}
        self.data = data if data and data.get('artifact_sha') == artifact_sha else {
            'version': SESSION_VERSION, 'key': key, 'artifact_sha': artifact_sha, 'parts': {}, 'notes': [], 'rounds': [],
            'final': None, 'overflow': False, 'invalidated': (data or {}).get('invalidated') or []}

    # --- state -----------------------------------------------------------------
    def save(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, default=str), encoding='utf-8');tmp.replace(self.path)

    def register_parts(self, ids):
        for i in ids:
            self.data['parts'].setdefault(i, {'presented': False, 'reviewed': False, 'synthesised': False})

    def mark(self, ids, field):
        for i in ids:
            if i in self.data['parts']:
                self.data['parts'][i][field] = True

    def unreviewed(self):
        return sorted(i for i, p in self.data['parts'].items() if not p['reviewed'])

    def complete(self):
        return all(p['reviewed'] for p in self.data['parts'].values())

    def add_notes(self, notes, *, round_no, section=None, allowed_sources=()):
        added = []
        for n in notes or []:
            if not isinstance(n, dict) or n.get('kind') not in NOTE_KINDS or not str(n.get('text') or '').strip():
                continue
            cited = [s for s in n.get('source_ids') or [] if isinstance(s, str)][:6]
            allowed = {str(a).split('source:', 1)[-1] for a in allowed_sources or ()}
            # rc.25 (C3): a note may only cite sources actually presented; an
            # unknown id is kept as evidence of the error, never as support.
            valid = [s for s in cited if not allowed_sources or s.split('source:', 1)[-1] in allowed or not s.startswith(('source:', 'QMS-'))]
            note = {'id': f"N{len(self.data['notes']) + 1}", 'kind': n['kind'], 'text': str(n['text'])[:MAX_NOTE_CHARS],
                    'section': n.get('section') or section, 'source_ids': valid,
                    'invalid_source_ids': [s for s in cited if s not in valid],
                    'round': round_no, 'resolved': False, 'resolved_in_round': None}
            self.data['notes'].append(note);added.append(note['id'])
        return added

    def open_items(self):
        return [n for n in self.data['notes'] if n['kind'] in BLOCKING_KINDS and not n['resolved']]

    def resolve(self, ids, round_no):
        for n in self.data['notes']:
            if n['id'] in (ids or []) and n['kind'] in BLOCKING_KINDS:
                n['resolved'] = True;n['resolved_in_round'] = round_no

    def notes_view(self, budget_chars):
        """All blocking items always; findings while they fit. Anything left
        out is listed by id (visible), and the overflow blocks approval."""
        blocking = [n for n in self.data['notes'] if n['kind'] in BLOCKING_KINDS]
        findings = [n for n in self.data['notes'] if n['kind'] not in BLOCKING_KINDS]
        shown, omitted = list(blocking), []
        size = len(json.dumps(shown))
        for n in findings:
            s = len(json.dumps(n))
            if size + s <= budget_chars:
                shown.append(n);size += s
            else:
                omitted.append(n['id'])
        self.data['overflow'] = bool(omitted) or size > budget_chars
        return {'notes': shown, 'omitted_note_ids': omitted, 'open_item_ids': [n['id'] for n in self.open_items()],
                'parts': self.data['parts'], 'note': 'Your own earlier review notes on this artifact. Resolve open items explicitly '
                                                     '(resolved_note_ids) or do not approve.'}, size

    def summary(self):
        return {'artifact_sha': self.data['artifact_sha'], 'parts': self.data['parts'], 'notes': len(self.data['notes']),
                'open_items': [n['id'] for n in self.open_items()], 'rounds': len(self.data['rounds']), 'overflow': self.data['overflow'],
                'invalidated_sessions': len(self.data['invalidated']), 'final': self.data['final']}


def _model_call(view, mode):
    """The reviewer model on its own route. mode: 'inspect' | 'final'."""
    from . import agentic_reviewer as r, generation, inference
    shown = {k: v for k, v in view.items() if not k.startswith('_')}
    withheld = [w['id'] for w in view.get('withheld') or []]
    if mode == 'inspect':
        schema = {'type': 'object', 'properties': {'decision': {'type': 'string', 'enum': ['record_findings']}, 'notes': NOTES_SCHEMA},
                  'required': ['decision', 'notes'], 'additionalProperties': False}
        system = INSPECT_SYSTEM
    else:
        schema = r.schema(view['decision_point'], withheld)
        open_ids = (view.get('prior_review_notes') or {}).get('open_item_ids') or []
        schema['properties'].update({'notes': NOTES_SCHEMA, 'synthesis': {'type': 'string', 'maxLength': 800},
                                     'resolved_note_ids': {'type': 'array', 'items': {'type': 'string', 'enum': open_ids or ['none']}, 'maxItems': 12}})
        schema['required'] = list(schema['properties'])
        system = r.REVIEWER_SYSTEM + (' You are making the final decision. Use your prior review notes: resolve each open item explicitly '
                                      'or do not approve. Give a one-paragraph synthesis of the whole artifact.')
    # Reviewer telemetry identity only (Agent Arena); the request is unchanged.
    with r.telemetry_identity(view), inference.specialist('lab_reviewer'):
        model = inference.model()
        value = generation.compose_structured(system, shown, schema, purpose='lab_reviewer', max_tokens=r.REVIEWER_MAX_TOKENS)
    return value, model


def review_decision(store, cid, point, run_dir, *, decide=None, budget_chars=None):
    """Run a complete review for one decision point; returns (decision, model, info)."""
    from . import agentic_reviewer as r
    from .lab_faults import crash_point
    decide = decide or _model_call
    budget = budget_chars or r.reviewer_budget_chars()[0]
    first = r.observe(store, cid, point, budget)
    presentation = first.pop('_presentation', {})
    sha = (first.get('draft') or {}).get('sha256')
    session = ReviewSession(run_dir, f"{point['kind']}:{point['target_id']}", sha)
    parts = [i for i in presentation.get('presented_ids', []) + presentation.get('withheld_ids', []) if i.startswith('draft:part')]
    session.register_parts(parts)
    multipart = any(i.startswith('draft:part') for i in presentation.get('withheld_ids', []))
    model = None
    # Phase 1: inspect each unreviewed part of a multi-part artifact.
    if multipart:
        for part in session.unreviewed():
            notes_view, size = session.notes_view(budget // 3)
            view = r.observe(store, cid, {**point, 'requested_evidence': [part]}, max(2000, budget - size))
            pres = view.pop('_presentation', {})
            view.update(review_mode='inspect_section', section_under_review=part, prior_review_notes=notes_view)
            out, model = decide(view, 'inspect')
            session.mark([i for i in pres.get('presented_ids', []) if i.startswith('draft:part')], 'presented')
            if part in pres.get('presented_ids', []):
                session.add_notes(out.get('notes'), round_no=len(session.data['rounds']) + 1, section=part,
                                  allowed_sources=[i for i in pres.get('presented_ids', []) if i.startswith('source:')])
                session.mark([part], 'reviewed')
            session.data['rounds'].append({'mode': 'inspect', 'section': part, 'presented': pres.get('presented_ids'), 'at': time.time()})
            session.save()
            crash_point('review_section_recorded')
    # Phase 2: final decision with the notes (and bounded evidence requests).
    requested, out, pres = [], {}, {}
    for attempt in range(MAX_EVIDENCE_ROUNDS + 1):
        notes_view, size = session.notes_view(budget // 3)
        view = r.observe(store, cid, {**point, 'requested_evidence': requested, 'exclude_draft_parts': multipart}, max(2000, budget - size))
        pres = view.pop('_presentation', {})
        view.update(prior_review_notes=notes_view, review_session={'parts_total': len(session.data['parts']),
                                                                     'parts_reviewed': sum(p['reviewed'] for p in session.data['parts'].values())})
        out, model = decide(view, 'final')
        round_no = len(session.data['rounds']) + 1
        presented_parts = [i for i in pres.get('presented_ids', []) if i.startswith('draft:part')]
        session.mark(presented_parts, 'presented')
        session.add_notes(out.get('notes'), round_no=round_no, allowed_sources=[i for i in pres.get('presented_ids', []) if i.startswith('source:')])
        if not multipart:
            session.mark(presented_parts, 'reviewed')
        session.data['rounds'].append({'mode': 'final', 'decision': out.get('decision'), 'requested': out.get('requested_evidence'),
                                       'presented': pres.get('presented_ids'), 'at': time.time()})
        session.save()
        if out.get('decision') == 'request_evidence' and attempt < MAX_EVIDENCE_ROUNDS:
            requested = [i for i in out.get('requested_evidence') or [] if i in pres.get('withheld_ids', [])]
            continue
        break
    session.resolve(out.get('resolved_note_ids') or [], len(session.data['rounds']))
    blocked = None
    if out.get('decision') == 'approve_send':
        if not session.complete():
            blocked = 'not every part of the artifact was reviewed: ' + ', '.join(session.unreviewed())
        elif session.data['overflow']:
            blocked = 'review notes exceeded the context budget; some findings could not be carried into the decision'
        elif session.open_items():
            blocked = 'unresolved review items: ' + ', '.join(n['id'] for n in session.open_items())
        elif multipart and not str(out.get('synthesis') or '').strip():
            blocked = 'no final synthesis for an artifact reviewed in parts'
    if not blocked:
        session.mark([i for i, p in session.data['parts'].items() if p['reviewed']], 'synthesised')
    session.data['final'] = {'decision': out.get('decision'), 'blocked': blocked, 'synthesis': out.get('synthesis'), 'at': time.time()}
    session.save()
    return out, model, {'session': session.summary(), 'blocked': blocked, 'presentation': pres, 'multipart': multipart}
