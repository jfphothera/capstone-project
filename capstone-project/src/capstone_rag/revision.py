"""Explicit draft revision (rc.25, items C1/C2).

Before rc.25 a representative's or simulated reviewer's correction rejected the
draft and called the generic ``agent.handle`` with the reviewer's text appended
to the customer's first message. That handler searched only exact-revision
documents, so a missing-accessory reply lost its model-independent logistics
procedure (QMS-DOC-0111) and was replaced by an unrelated no-procedure /
engineer-handoff template, without parent linkage or validation provenance.

``revise`` is a task-preserving revision of THE EXISTING ARTIFACT:

* inputs: the original draft and its SHA-256, the current conversation, the
  task (intent, draft kind, procedure), case version and identity state, the
  frozen authorized evidence contract of that draft, the run's method/model
  profile and structured reviewer objections;
* reviewer text is never parsed as customer facts or as a new request;
* objections are grounded first (``objection_grounding``); contradicted or
  invented-citation objections are rejected with their reason and their
  target content is kept;
* the bounded writer revises the draft against the same authorized passages
  (one model attempt plus one feedback redraft; a deterministic removal is the
  only non-model edit); no new evidence is searched;
* the replacement keeps parent linkage and provenance (sources, method, route,
  verification) and gets its own frozen-package validation record, so the
  send gate applies to it exactly as to the original;
* progress is a changed, validated draft. The same draft/objection/evidence
  state seen again, an unchanged candidate, or the configured bound stops the
  revision as ``unresolved`` (needs authorized review) with the reason, the
  objections, attempted changes and sources preserved. Nothing is approved,
  resolved or re-timed;
* each request is idempotent on its command id across retries and restarts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from . import objection_grounding as og
from . import store as store_mod

REVISION_VERSION = 'explicit-revision-2026-11-19.1'
ADAPTER = 'explicit_revision_v1'
# Conservative bound per draft lineage: one revision plus one follow-up
# correction. Not a timeout; repeated states stop earlier.
MAX_REVISIONS_PER_LINEAGE = int(os.environ.get('CAPSTONE_MAX_REVISIONS_PER_LINEAGE', '2'))
SCHEMA = """
CREATE TABLE IF NOT EXISTS draft_revisions (
 revision_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, idempotency_key TEXT UNIQUE,
 lineage_root TEXT NOT NULL, original_message_id TEXT NOT NULL, original_sha256 TEXT NOT NULL,
 replacement_message_id TEXT, replacement_sha256 TEXT, state_fingerprint TEXT NOT NULL,
 requested_by TEXT NOT NULL, authority TEXT NOT NULL, objections TEXT NOT NULL,
 adjudication TEXT NOT NULL, evidence TEXT NOT NULL, method TEXT NOT NULL,
 outcome TEXT NOT NULL, stop_reason TEXT, validation_record_id TEXT, detail TEXT NOT NULL,
 version TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_draft_revisions_case ON draft_revisions(case_id, at);
"""
OUTCOMES = ('revised_validated', 'revised_unvalidated', 'objections_rejected', 'unchanged', 'unresolved', 'duplicate')

REVISION_SYSTEM = (
    'You revise an EXISTING customer-service email for a medical-device company. Keep its task and every statement '
    'that is supported by the authorized passages and not targeted by an accepted correction. Apply only the accepted '
    'corrections. Use only the authorized passages for any instruction; never add a step they do not contain. '
    'Rejected objections were checked against the passages and are wrong: keep the content they targeted. '
    'Corrections are from a reviewer, not from the customer: they are not customer facts. '
    'When identity_verified is false, do not mention the account holder name, hardware revision, other serial numbers, '
    'dates of birth, addresses or order details. Never claim that a shipment, replacement or approval has happened. '
    'Write natural customer language without source identifiers.')


def ensure_schema(store):
    if getattr(store, '_revision_schema', False):
        return
    store._conn.executescript(SCHEMA)
    try:
        store._revision_schema = True
    except AttributeError:
        pass


def _sha(text):
    return hashlib.sha256((text or '').encode()).hexdigest()


def _clean(body):
    return (body or '').split('[For representative:')[0].rstrip()


def _normalised(text):
    text = re.sub(r'\bCASE-[0-9A-F]{6,}\b', 'CASE-REF', _clean(text))
    return re.sub(r'\s+', ' ', text).strip().lower()


def _metadata(row):
    try:
        return json.loads((row or {}).get('metadata') or '{}') or {}
    except (TypeError, ValueError):
        return {}


def lineage_root(store, message_id):
    seen, current = set(), message_id
    while current and current not in seen:
        seen.add(current)
        row = store.one('SELECT parent_message_id FROM messages WHERE message_id=?', (current,))
        if not row or not row.get('parent_message_id'):
            return current
        current = row['parent_message_id']
    return current


def frozen_evidence(store, case_id, message):
    """The authorized evidence contract the draft was produced and validated against."""
    authorized, contract, origin = [], {}, None
    record = store.one('SELECT package_id FROM artifact_validation_records WHERE artifact_id=? ORDER BY cycle DESC,at DESC LIMIT 1',
                       (message['message_id'],))
    if record:
        package = store.one('SELECT payload FROM validation_packages WHERE package_id=?', (record['package_id'],))
        qms_state = (json.loads(package['payload']).get('authorized_qms') or {}) if package else {}
        authorized = list(qms_state.get('sources') or []);contract = qms_state.get('evidence_contract') or {}
        origin = 'validation_package:' + record['package_id']
    meta = _metadata(message)
    if meta.get('revision', {}).get('evidence_chunk_ids'):
        authorized = authorized or list(meta['revision']['evidence_chunk_ids'])
        contract = contract or meta['revision'].get('contract') or {}
        origin = origin or 'parent_revision_contract'
    if not authorized:
        finding = store.one("SELECT payload FROM specialist_findings WHERE case_id=? AND role='qms_research' ORDER BY at DESC LIMIT 1", (case_id,))
        if finding:
            payload = json.loads(finding['payload'] or '{}')
            authorized = list(payload.get('sources') or []);contract = payload.get('evidence_contract') or {}
            origin = 'qms_research_finding'
    used = list(meta.get('source_chunk_ids') or [])
    if not authorized and used:
        authorized, origin = used, 'draft_source_metadata'
    return {'authorized_chunk_ids': list(dict.fromkeys(authorized + [u for u in used if u in authorized])),
            'used_chunk_ids': used, 'contract': contract, 'origin': origin or 'none'}


def _context(store, case_id):
    from . import identity, qms
    case = store.case(case_id) or {}
    device = store.device(case.get('serial_number')) if case.get('serial_number') else None
    confirmed = identity.case_revision_authorized(store, case) if case else False
    return case, device, {'model': case.get('model'), 'revision': case.get('revision'),
                          'revision_context': identity.revision_context(device, case.get('revision_confirmed_by') if confirmed else None) if device else {},
                          'compatibility': qms.load_compatibility(),
                          'safety_hold': bool(case.get('safety_hold')) or str(case.get('classification') or '').startswith(('adverse_event', 'device_safety_hazard'))}


def authorized_passages(store, case_id, evidence):
    """Re-authorize the frozen chunks under the CURRENT case context."""
    from . import qms, qms_policy
    case, device, context = _context(store, case_id)
    lookup = {c.chunk_id: c for c in qms.load_chunks()}
    passages, refused = [], []
    for cid in evidence['authorized_chunk_ids']:
        chunk = lookup.get(cid)
        if not chunk:
            refused.append({'chunk_id': cid, 'reason': 'not in the controlled corpus'});continue
        verdict = qms_policy.authorize(chunk, operation='customer_answer', recipient='customer', context=context)
        if verdict.allowed:
            passages.append({'chunk_id': cid, 'document_id': chunk.document_id, 'procedure_id': chunk.procedure_id,
                             'scope': chunk.scope, 'section': chunk.section_path, 'text': chunk.text, '_chunk': chunk})
        else:
            refused.append({'chunk_id': cid, 'reason': '; '.join(verdict.denial_reasons)})
    return passages, refused


def private_values(store, case_id):
    """Account facts that may not appear in a draft before identity is verified."""
    case = store.case(case_id) or {}
    inbound = ' '.join((m['body'] or '') for m in store.messages(case_id) if m['direction'] == 'inbound').lower()
    accounts = {case.get('account_id')} if case.get('account_id') else set()
    if case.get('serial_number'):
        row = store.device(case['serial_number'])
        if row and row.get('account_id'):
            accounts.add(row['account_id'])
    values = []
    for account in accounts:
        c = store.one('SELECT * FROM customers WHERE account_id=?', (account,)) or {}
        values += [c.get(k) for k in ('customer_name', 'date_of_birth', 'phone', 'mobile_phone', 'email', 'street')]
        values += [d['serial_number'] for d in store.devices_for_account(account)]
    return [v for v in dict.fromkeys(values) if v and str(v).lower() not in inbound]


def _used_documents(evidence, passages):
    by_id = {p['chunk_id']: p for p in passages}
    docs = {by_id[c]['document_id'] for c in evidence['used_chunk_ids'] if c in by_id}
    if not docs:
        docs = {c.split('#', 1)[0] for c in evidence['used_chunk_ids']}
    return docs


def _conversation(store, case_id, limit=6):
    rows = [m for m in store.messages(case_id) if m['direction'] == 'inbound' or m['status'] == 'sent']
    return [{'from': 'customer' if m['direction'] == 'inbound' else 'support', 'text': _clean(m['body'])[:1200]} for m in rows[-limit:]]


def _write(store, case_id, original, passages, accepted, rejected_items, case, feedback='', prefer_llm=True):
    from . import generation, inference
    context = {'original_draft': _clean(original['body']), 'task': _metadata(original).get('kind') or 'agent_response',
               'procedure_id': _metadata(original).get('procedure_id'),
               'identity_verified': bool(case.get('identity_verified')),
               'conversation': _conversation(store, case_id),
               'authorized_passages': [{'id': p['chunk_id'], 'section': p['section'], 'text': p['text']} for p in passages],
               'accepted_corrections': [{'kind': a['kind'], 'draft_span': a['draft_span'], 'correction': a['correction'],
                                         'checked': a['verdict']} for a in accepted],
               'rejected_objections': [{'draft_span': r['draft_span'], 'objection': r['correction'], 'why_rejected': r['reason']} for r in rejected_items]}
    if feedback:
        context['previous_attempt_rejected_because'] = feedback
    if not (prefer_llm and inference.current() and generation.llm_available()):
        return None, None
    text = generation.compose_customer(REVISION_SYSTEM, json.dumps(context, default=str))
    return generation.customer_text(text), inference.model()


def _deterministic_removal(original, accepted):
    """The only non-model edit: drop sentences an accepted remove/identity objection targets."""
    from .grounding import split_sentences
    body = _clean(original['body'])
    targets = set()
    for a in accepted:
        if a['kind'] in ('remove', 'identity') and a['verdict'] == 'supported':
            targets.update(og._span_sentences(a['draft_span'], body))
    if not targets:
        return None
    kept = body
    for sentence in targets:
        kept = kept.replace(sentence, '')
    kept = re.sub(r'[ \t]+\n', '\n', re.sub(r'\n{3,}', '\n\n', kept)).strip()
    return kept if kept and kept != body else None


def _deterministic_rewrite(store, case_id, original, passages, case):
    from types import SimpleNamespace
    from . import generation
    inbound = store.one("SELECT body FROM messages WHERE case_id=? AND direction='inbound' ORDER BY created_at DESC LIMIT 1", (case_id,)) or {}
    verified = bool(case.get('identity_verified'))
    device = SimpleNamespace(model=case.get('model') or 'device', revision=case.get('revision') if verified else None,
                             serial_number=case.get('serial_number') if verified else None,
                             customer_name=case.get('customer_name') if verified else None, account_id=None)
    chunks = [p['_chunk'] for p in passages]
    try:
        gen = generation.generate_grounded(inbound.get('body') or '', device.customer_name or 'there', device, chunks,
                                           _metadata(original).get('procedure_id') or chunks[0].procedure_id,
                                           prefer_llm=False, public_context=not verified)
    except Exception:
        return None
    return generation.customer_text(gen.text) if gen and gen.text else None


def _check(store, case_id, original, candidate, passages, evidence):
    from . import response_policy
    failures = []
    verdict = response_policy.validate_case(store, case_id, candidate)
    failures += verdict['failures']
    probe = {**original, 'metadata': json.dumps({**_metadata(original), 'source_chunk_ids': [p['chunk_id'] for p in passages]})}
    procedural = response_policy.validate_procedural_edit(store, case_id, probe, candidate)
    failures += procedural['failures']
    if _normalised(candidate) == _normalised(original['body']):
        failures.append({'fact': 'candidate repeats the rejected draft', 'conflicting_text': 'unchanged'})
    return failures


def _record(store, row):
    ensure_schema(store)
    store.insert('draft_revisions', {**{k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in row.items()},
                                     'version': REVISION_VERSION, 'at': store_mod.now()})


def _prior(store, case_id, key):
    ensure_schema(store)
    return store.one('SELECT * FROM draft_revisions WHERE idempotency_key=?', (key,)) if key else None


def revise(store, case_id, message_id, *, objections=None, feedback='', authorizer, authority='simulated_reviewer',
           idempotency_key=None, method=None, presented_source_ids=None, prefer_llm=True, escalate_case=None):
    """Revise one draft. Returns a result dict; ``ok`` is True only for a
    validated replacement. ``unresolved`` marks a stopped revision."""
    from . import activity
    ensure_schema(store)
    prior = _prior(store, case_id, idempotency_key)
    if prior:
        detail = json.loads(prior['detail'] or '{}')
        return {**detail.get('result', {}), 'duplicate': True, 'revision_id': prior['revision_id']}
    original = store.one('SELECT * FROM messages WHERE message_id=? AND case_id=?', (message_id, case_id))
    if not original:
        return {'ok': False, 'reason': 'draft not found in this case', 'outcome': 'unresolved'}
    if original['status'] != 'draft':
        return {'ok': False, 'reason': f"{message_id} is {original['status']}, not a draft", 'outcome': 'unresolved'}
    case, _, _ = _context(store, case_id)
    revision_id = 'REVN-' + hashlib.sha256(f'{case_id}|{message_id}|{idempotency_key or store_mod.now()}'.encode()).hexdigest()[:20]
    evidence = frozen_evidence(store, case_id, original)
    passages, refused = authorized_passages(store, case_id, evidence)
    items = og.normalise(objections, feedback)
    human = authority == 'human_representative'
    adjudication = og.adjudicate(items, original['body'], passages, presented_ids=presented_source_ids,
                                 used_document_ids=_used_documents(evidence, passages),
                                 private_values=private_values(store, case_id),
                                 identity_verified=bool(case.get('identity_verified')), safety_hold=bool(case.get('safety_hold')))
    if human:
        # A named representative's correction is an instruction, not a claim to
        # be adjudicated; the writer and validators still bound the result.
        for a in adjudication:
            if a['verdict'] in ('unstructured', 'unverified_semantic'):
                a['verdict'] = 'representative_instruction'
    accepted = [a for a in adjudication if a['verdict'] in ('supported', 'unverified_semantic', 'unstructured', 'representative_instruction')
                and a['kind'] != 'no_defect']
    refused_objections = og.rejected(adjudication)
    root = lineage_root(store, message_id)
    fingerprint = hashlib.sha256(json.dumps({'draft': _normalised(original['body']), 'objections': sorted(a['objection_id'] for a in adjudication),
                                             'evidence': sorted(evidence['authorized_chunk_ids'])}, sort_keys=True).encode()).hexdigest()[:24]
    method = dict(method or {});method.setdefault('adapter', ADAPTER)
    base = {'revision_id': revision_id, 'case_id': case_id, 'idempotency_key': idempotency_key, 'lineage_root': root,
            'original_message_id': message_id, 'original_sha256': _sha(original['body']), 'state_fingerprint': fingerprint,
            'requested_by': authorizer, 'authority': authority, 'objections': items, 'adjudication': adjudication,
            'evidence': {**{k: v for k, v in evidence.items()}, 'reauthorized_chunk_ids': [p['chunk_id'] for p in passages], 'refused': refused},
            'method': method}
    activity.emit('methodology', 'Draft revision under the explicit revision operation', 'running',
                  {'trigger': 'draft_revision', 'adapter': ADAPTER, 'revision_id': revision_id, 'workflow_id': None, 'resume': False,
                   'governed_by_methodology': False, 'run_methodology': method.get('methodology_id'),
                   'note': 'Task-preserving revision of the existing artifact on the run\'s customer_communication route; '
                           'methodology stages (supervisor, peer, reflection, debate) are not re-run for a reviewer correction.'})

    def stop(outcome, reason, **extra):
        result = {'ok': False, 'unresolved': outcome == 'unresolved', 'outcome': outcome, 'reason': reason, 'stop_reason': reason,
                  'revision_id': revision_id, 'message_id': message_id,
                  'objections_rejected': [{'objection_id': r['objection_id'], 'verdict': r['verdict'], 'reason': r['reason'],
                                           'evidence': r['evidence']} for r in refused_objections],
                  'objections_accepted': [a['objection_id'] for a in accepted], **extra}
        _record(store, {**base, 'outcome': outcome, 'stop_reason': reason, 'replacement_message_id': None, 'replacement_sha256': None,
                        'validation_record_id': None, 'detail': {'result': result, **extra}})
        store.audit('draft_revision_' + outcome, actor=authorizer, case_id=case_id, summary=f'{message_id}: {reason}'[:240],
                    detail={'revision_id': revision_id, 'adjudication': [{k: a[k] for k in ('objection_id', 'kind', 'verdict', 'reason')} for a in adjudication]})
        if outcome == 'unresolved' and (escalate_case if escalate_case is not None else authority == 'simulated_reviewer'):
            # Never inside a running workflow (a case update there makes the
            # in-flight finding stale); a named human is already in the loop.
            from .caseflow import HUMAN_REVIEW
            if (store.case(case_id) or {}).get('status') not in ('Resolved', 'Closed'):
                store.update('cases', 'case_id', case_id, {'status': HUMAN_REVIEW, 'updated_at': store_mod.now()})
        activity.emit('revision', 'Draft revision ' + outcome, 'awaiting_human_review' if outcome == 'unresolved' else 'completed',
                      {'revision_id': revision_id, 'outcome': outcome, 'reason': reason})
        return result

    # Progress and bound checks before any model call.
    repeat = store.one('SELECT revision_id,outcome FROM draft_revisions WHERE case_id=? AND state_fingerprint=? AND revision_id!=?',
                       (case_id, fingerprint, revision_id))
    if repeat:
        return stop('unresolved', 'the same draft, objections and evidence were already revised without progress '
                                  f"({repeat['revision_id']}: {repeat['outcome']}); needs authorized review", repeated_revision_id=repeat['revision_id'])
    done = store.one("SELECT COUNT(*) AS n FROM draft_revisions WHERE lineage_root=? AND outcome IN ('revised_validated','revised_unvalidated','unchanged','objections_rejected')",
                     (root,))['n']
    if done >= MAX_REVISIONS_PER_LINEAGE:
        return stop('unresolved', f'revision bound reached ({done} of {MAX_REVISIONS_PER_LINEAGE} for this draft lineage); needs authorized review')
    if not accepted:
        if refused_objections:
            return stop('objections_rejected', 'no objection is supported by the authorized evidence; the draft is unchanged: '
                        + '; '.join(f"{r['objection_id']} {r['verdict']}" for r in refused_objections))
        if any(a['kind'] == 'no_defect' for a in adjudication):
            return stop('objections_rejected', 'reviewer recorded no material defect; nothing to revise')
        return stop('unresolved', 'no actionable objection was given')
    # Without re-authorized passages the writer may still produce a safe
    # clarification; any device instruction it adds fails the procedural check.
    candidate, model, mode, attempts = None, None, None, []
    feedback_text = ''
    for attempt in range(2):
        try:
            text, used_model = _write(store, case_id, original, passages, accepted, refused_objections, case, feedback_text, prefer_llm=prefer_llm)
        except Exception as exc:  # recorded; the deterministic edit is still allowed
            attempts.append({'attempt': attempt + 1, 'mode': 'llm', 'error': str(exc)[:300]});text, used_model = None, None
        if text is None:
            break
        failures = _check(store, case_id, original, text, passages, evidence)
        attempts.append({'attempt': attempt + 1, 'mode': 'llm', 'model': used_model, 'failures': failures[:6]})
        if not failures:
            candidate, model, mode = text, used_model, 'llm_revision';break
        feedback_text = '; '.join(str(f.get('fact') or f)[:160] for f in failures[:4])
    if candidate is None:
        removal = _deterministic_removal(original, accepted)
        if removal:
            failures = _check(store, case_id, original, removal, passages, evidence)
            attempts.append({'attempt': len(attempts) + 1, 'mode': 'deterministic_removal', 'failures': failures[:6]})
            if not failures:
                candidate, mode = removal, 'deterministic_removal'
    if candidate is None and human and passages:
        # A named representative without a model session: the labelled
        # deterministic writer re-states only authorized passage sentences.
        # Recorded as a fallback that did not apply the written instruction.
        fallback = _deterministic_rewrite(store, case_id, original, passages, case)
        if fallback:
            failures = _check(store, case_id, original, fallback, passages, evidence)
            attempts.append({'attempt': len(attempts) + 1, 'mode': 'deterministic_source_rewrite', 'failures': failures[:6],
                             'instruction_applied': False})
            if not failures:
                candidate, mode = fallback, 'deterministic_source_rewrite'
    if candidate is None:
        unchanged = any(any(f.get('conflicting_text') == 'unchanged' for f in a.get('failures') or []) for a in attempts)
        return stop('unchanged' if unchanged else 'unresolved',
                    'no revised draft passed the deterministic checks' + (' (the writer repeated the rejected draft)' if unchanged else ''),
                    attempts=attempts)
    note = '[For representative:' + original['body'].split('[For representative:', 1)[1] if '[For representative:' in (original['body'] or '') else ''
    body = candidate + ('\n\n' + note if note and store.one("SELECT 1 FROM rmas WHERE case_id=? AND status='proposed'", (case_id,)) else '')
    meta = _metadata(original)
    retained = [p['chunk_id'] for p in passages if p['chunk_id'] in set(evidence['used_chunk_ids']) or not evidence['used_chunk_ids']]
    from . import review
    with store.transaction():
        return _commit_replacement(store, case_id, case, original, message_id, revision_id, body, meta, retained, root, passages,
                                   evidence, method, model, mode, accepted, refused_objections, feedback, authorizer, base, attempts, review)


def _commit_replacement(store, case_id, case, original, message_id, revision_id, body, meta, retained, root, passages,
                        evidence, method, model, mode, accepted, refused_objections, feedback, authorizer, base, attempts, review):
    from . import activity
    new_id = f"{case_id}-DRAFT-{store.next_id('draft', '{:05d}')}"
    review.reject_draft(store, case_id, message_id, authorizer, note=f'revision {revision_id} requested')
    store.insert('messages', {
        'message_id': new_id, 'case_id': case_id, 'run_id': original.get('run_id'), 'created_at': store_mod.now(),
        'direction': 'outbound', 'channel': original.get('channel') or case.get('channel'), 'author': 'ai (revision)',
        'subject': original.get('subject') or f"Re: {case.get('subject')}", 'body': body, 'status': 'draft',
        'approved_by': None, 'approved_at': None, 'parent_message_id': message_id,
        'metadata': json.dumps({'kind': meta.get('kind') or 'agent_response', 'generation_mode': mode,
                                'source_chunk_ids': retained, 'statement_authority': meta.get('statement_authority') or ('approved_qms' if retained else 'case_facts'),
                                'procedure_id': meta.get('procedure_id'), 'grounded': None,
                                'revision': {'revision_id': revision_id, 'version': REVISION_VERSION, 'revision_of': message_id,
                                             'parent_sha256': _sha(original['body']), 'lineage_root': root,
                                             'evidence_chunk_ids': [p['chunk_id'] for p in passages], 'contract': evidence['contract'],
                                             'evidence_origin': evidence['origin'], 'method': method, 'model': model,
                                             'route': 'customer_communication' if model else None,
                                             'objections_accepted': [a['objection_id'] for a in accepted],
                                             'objections_rejected': [r['objection_id'] for r in refused_objections]}})})
    store.insert('recommendations', {
        'rec_id': store.next_id('rec', 'REC-{:06d}'), 'case_id': case_id, 'run_id': None, 'created_at': store_mod.now(),
        'kind': 'response', 'status': 'revised', 'payload': body, 'original_payload': original['body'],
        'feedback': feedback or '; '.join(a['correction'] for a in accepted)[:500], 'revision_count': 1,
        'decided_by': authorizer, 'decided_at': store_mod.now()})
    record = _validate_replacement(store, case_id, new_id, passages, evidence)
    outcome = 'revised_validated' if record and record['disposition'] == 'pass' else 'revised_unvalidated'
    result = {'ok': outcome == 'revised_validated', 'outcome': outcome, 'message_id': new_id, 'replaces': message_id,
              'revision_id': revision_id, 'verified': outcome == 'revised_validated', 'validation': (record or {}).get('disposition'),
              'summary': f'Revised draft {new_id} ({outcome})',
              'objections_accepted': [a['objection_id'] for a in accepted],
              'objections_rejected': [{'objection_id': r['objection_id'], 'verdict': r['verdict'], 'reason': r['reason']} for r in refused_objections],
              'unresolved': False}
    if outcome != 'revised_validated':
        result['reason'] = 'the replacement did not pass independent validation; sending stays blocked'
    if (record or {}).get('disposition') == 'target_mismatch':
        result['reason'] = (f"validation froze a different draft ({record.get('validated_message_id')}), so the replacement is unvalidated; "
                            'sending stays blocked')
    store.update('messages', 'message_id', new_id, {'metadata': json.dumps({**json.loads(store.one('SELECT metadata FROM messages WHERE message_id=?', (new_id,))['metadata']),
                                                                             'grounded': outcome == 'revised_validated',
                                                                             'verification': {'disposition': (record or {}).get('disposition'), 'record_id': (record or {}).get('record_id')}})})
    _record(store, {**base, 'outcome': outcome, 'stop_reason': None, 'replacement_message_id': new_id, 'replacement_sha256': _sha(body),
                    'validation_record_id': (record or {}).get('record_id'), 'detail': {'result': result, 'attempts': attempts, 'mode': mode}})
    store.audit('response_revised', actor='ai', case_id=case_id, summary=f'{new_id} revises {message_id} ({outcome})',
                detail={'revision_id': revision_id, 'requested_by': authorizer, 'mode': mode, 'model': model})
    activity.emit('revision', 'Draft revised against its authorized evidence', 'completed' if result['ok'] else 'rejected',
                  {'revision_id': revision_id, 'message_id': new_id, 'outcome': outcome, 'mode': mode})
    return result


def _validate_replacement(store, case_id, message_id, passages, evidence):
    """Frozen-package validation of the replacement (deterministic checks)."""
    from . import independent_review
    workflow_id = 'REVISION-' + message_id
    state = {'qms': {'sources': [p['chunk_id'] for p in passages], 'evidence_contract': evidence['contract']}}
    row, package = independent_review.freeze(store, case_id, workflow_id, state, 0)
    picked = (package.get('draft') or {}).get('message_id')
    if picked != message_id:
        # Fail closed: the package froze another draft, so nothing validated
        # this replacement. Recorded explicitly; sending stays blocked.
        return {'disposition': 'target_mismatch', 'record_id': None, 'validated_message_id': picked}
    independent_review.evaluate(store, row, package, None)
    return store.one('SELECT * FROM artifact_validation_records WHERE artifact_id=? AND workflow_id=? ORDER BY at DESC LIMIT 1',
                     (message_id, workflow_id))


def history(store, case_id):
    ensure_schema(store)
    rows = store.query('SELECT * FROM draft_revisions WHERE case_id=? ORDER BY at', (case_id,))
    for row in rows:
        for key in ('objections', 'adjudication', 'evidence', 'method', 'detail'):
            row[key] = json.loads(row[key] or 'null')
    return rows
