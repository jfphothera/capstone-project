"""Versioned decision rubrics for the stage and review agents.

Each rubric scores ONE decision against evidence available from the
complete-ticket run (the finding the agent was given, deterministic checks,
and labelled scenario expectations). Results are ``passed`` True/False with a
reason, or ``None`` (unscored) when the evidence cannot decide. Being
exercised, returning valid JSON or not being truncated is never a pass.

All decisions across the lifecycle are scored; later decisions never
overwrite earlier ones.
"""
from __future__ import annotations

import json
import re

RUBRIC_VERSION = 'role-rubrics-2026-11-16.1'
ALLOWED_DELEGATES = {'', 'none', 'erp_research', 'qms_research', 'resolution_planning', 'communication_review', 'independent_validation'}
_STOP = {'the', 'a', 'an', 'for', 'of', 'to', 'and', 'or', 'is', 'are', 'in', 'on', 'with', 'no', 'not', 'any', 'this', 'that', 'be', 'it'}


def _words(value):
    text = json.dumps(value, default=str).lower() if not isinstance(value, str) else value.lower()
    return {w for w in re.findall(r'[a-z][a-z\-]{3,}', text) if w not in _STOP}


def _material_issues(finding):
    """Material problems recorded in the finding the agent reviewed."""
    issues = []
    for key in ('missing_evidence', 'conflicts', 'coverage_gap'):
        value = finding.get(key)
        if value:issues += value if isinstance(value, list) else [value]
    if finding.get('blocked'):issues.append(finding['blocked'])
    for plan in finding.get('plans') or []:
        if plan.get('action') == finding.get('selected_action'):
            issues += plan.get('hard_gate_verdicts') or []
    return [str(i) for i in issues if i]


def _mentions(text, issues):
    if not issues:
        return False
    words = _words(text)
    return any(len(_words(i) & words) >= 1 for i in issues)


def supervisor(decision, finding):
    """Identifies material missing evidence; justified continue/delegate."""
    if not isinstance(decision, dict) or 'continue' not in decision:
        return None, 'no supervisor decision recorded'
    if finding.get('controlled_failure'):
        return None, 'specialist failed; nothing substantive to supervise'
    issues = _material_issues(finding)
    if decision.get('requested_specialist', '') not in ALLOWED_DELEGATES:
        return False, 'requested a specialist outside the permitted set'
    if issues:
        flagged = (decision.get('continue') is False) or _mentions(decision.get('concern', ''), issues)
        return (True, 'identified the material gap') if flagged else (False, f'missed material gap: {issues[0][:80]}')
    if decision.get('continue') is False:
        return False, 'stopped a complete finding without a recorded gap'
    return True, 'continued a complete finding'


def peer_reviewer(exchange, finding):
    """Useful evidence question / real issue; reply resolves or preserves uncertainty."""
    turns = (exchange or {}).get('turns') or []
    if len(turns) < 2:
        return None, 'exchange incomplete'
    challenge, reply = turns[0], turns[1]
    if not str(challenge.get('question') or '').strip():
        return False, 'asked no question'
    issues = _material_issues(finding)
    disagreement = str(challenge.get('disagreement') or '').strip().lower()
    substantive = bool(disagreement) and not re.fullmatch(r'(none|no|n/?a|no disagreement)\.?', disagreement)
    if substantive and not issues:
        return False, 'disagreement not supported by any issue recorded in the finding'
    if issues and not (_mentions(challenge.get('question', '') + ' ' + disagreement, issues)):
        return False, f'did not engage the recorded issue: {issues[0][:80]}'
    if issues and reply.get('resolved') is True and not str(reply.get('missing_evidence') or '').strip():
        return False, 'reply claimed resolution while the finding records a gap'
    return True, 'raised the real issue or a useful question; uncertainty preserved'


def reflection(decision, plan, coverage_gap=None):
    """Supported weakness and appropriate check."""
    if not isinstance(decision, dict) or 'proceed' not in decision:
        return None, 'no reflection recorded'
    issues = _material_issues({**(plan or {}), 'coverage_gap': coverage_gap})
    if issues:
        ok = decision.get('proceed') is False or _mentions(f"{decision.get('weakness', '')} {decision.get('recommended_check', '')}", issues)
        return (True, 'named a supported weakness') if ok else (False, f'proceeded past {issues[0][:80]}')
    if decision.get('proceed') is False:
        return False, 'blocked a plan with no recorded gap or failed gate'
    return True, 'proceeded with a sound plan'


def communication_review(critique, draft_ok, *, kind='clarity'):
    """Detect substantive defects; no invented objections to acceptable drafts.

    draft_ok: deterministic verdict on the same draft (True acceptable,
    False defective, None unknown -> unscored)."""
    if draft_ok is None:
        return None, 'no deterministic verdict on the reviewed draft'
    if kind == 'clarity':
        flagged = critique.get('clear') is False
    else:
        flagged = bool([i for i in critique.get('issues') or [] if str(i).strip()])
    if draft_ok and flagged:
        return False, 'objected to an acceptable draft'
    if not draft_ok and not flagged:
        return False, 'missed a defective draft'
    return True, 'flagged a defective draft' if flagged else 'accepted an acceptable draft'


def independent_validation(passed, artifact_ok):
    """Correct accept/reject against the deterministic artifact verdict."""
    if passed is None or artifact_ok is None:
        return None, 'no comparable verdicts'
    if bool(passed) == bool(artifact_ok):
        return True, 'accepted an acceptable artifact' if passed else 'rejected a defective artifact'
    return False, 'accepted a defective artifact' if passed else 'rejected an acceptable artifact'


def score_run(store, cid, scenario):
    """All stage/review decisions in the run, scored in order."""
    from . import response_policy
    decisions = []
    forbidden = (scenario.get('evaluation') or {}).get('forbidden_text') or []

    def draft_ok(message_id):
        if not message_id:
            return None
        msg = store.one('SELECT body FROM messages WHERE message_id=?', (message_id,))
        if not msg:
            return None
        val = store.one('SELECT disposition FROM artifact_validation_records WHERE artifact_id=? ORDER BY cycle DESC,at DESC LIMIT 1', (message_id,))
        body = (msg['body'] or '').split('[For representative:')[0]
        try:
            checks = response_policy.validate_case(store, cid, body)['passed']
        except Exception:
            checks = None
        if any(t.lower() in body.lower() for t in forbidden):
            return False
        if val is None and checks is None:
            return None
        return bool((val is None or val['disposition'] == 'pass') and checks is not False)

    for row in store.query('SELECT finding_id,role,payload,at FROM specialist_findings WHERE case_id=? ORDER BY at', (cid,)):
        payload = json.loads(row['payload'] or '{}')
        if payload.get('supervisor_assessment'):
            passed, why = supervisor(payload['supervisor_assessment'], payload)
            decisions.append({'role': 'supervisor', 'reviewed_role': row['role'], 'finding_id': row['finding_id'], 'at': row['at'],
                              'decision': payload['supervisor_assessment'], 'passed': passed, 'reason': why})
        if payload.get('peer_exchange'):
            passed, why = peer_reviewer(payload['peer_exchange'], payload)
            decisions.append({'role': 'peer_reviewer', 'reviewed_role': row['role'], 'finding_id': row['finding_id'], 'at': row['at'],
                              'decision': payload['peer_exchange'].get('turns'), 'passed': passed, 'reason': why})
        if row['role'] == 'resolution_planning' and payload.get('reflection'):
            passed, why = reflection(payload['reflection'], payload, payload.get('coverage_gap'))
            decisions.append({'role': 'reflection', 'finding_id': row['finding_id'], 'at': row['at'], 'decision': payload['reflection'],
                              'passed': passed, 'reason': why})
        if row['role'] == 'communication_review':
            ok = draft_ok(payload.get('message_id'))
            if payload.get('critique'):
                passed, why = communication_review(payload['critique'], ok, kind='clarity')
                decisions.append({'role': 'communication_review', 'finding_id': row['finding_id'], 'at': row['at'], 'message_id': payload.get('message_id'),
                                  'decision': payload['critique'], 'passed': passed, 'reason': why})
            debate = payload.get('debate') or {}
            if debate.get('critic'):
                passed, why = communication_review(debate['critic'], ok, kind='critic')
                decisions.append({'role': 'communication_review', 'finding_id': row['finding_id'], 'at': row['at'], 'message_id': payload.get('message_id'),
                                  'decision': debate['critic'], 'passed': passed, 'reason': why, 'stage': 'debate_critic'})
            if debate.get('judge'):
                passed, why = independent_validation(debate['judge'].get('proceed_to_validator'), ok)
                decisions.append({'role': 'independent_validation', 'finding_id': row['finding_id'], 'at': row['at'], 'message_id': payload.get('message_id'),
                                  'decision': debate['judge'], 'passed': passed, 'reason': why, 'stage': 'debate_judge'})
    for review in store.query('SELECT r.message_id,r.passed,r.payload,r.at FROM response_reviews r JOIN messages m ON m.message_id=r.message_id '
                              'WHERE m.case_id=? ORDER BY r.at', (cid,)):
        passed, why = independent_validation(bool(review['passed']), draft_ok(review['message_id']))
        decisions.append({'role': 'independent_validation', 'message_id': review['message_id'], 'at': review['at'],
                          'decision': json.loads(review['payload'] or '{}').get('issues'), 'passed': passed, 'reason': why, 'stage': 'semantic_review'})
    for d in decisions:
        d['rubric_version'] = RUBRIC_VERSION
    return decisions


def summarise(decisions):
    out = {}
    for d in decisions:
        cell = out.setdefault(d['role'], {'decisions': 0, 'scored': 0, 'passed': 0, 'unscored_reasons': {}})
        cell['decisions'] += 1
        if d['passed'] is None:
            cell['unscored_reasons'][d['reason']] = cell['unscored_reasons'].get(d['reason'], 0) + 1
        else:
            cell['scored'] += 1;cell['passed'] += bool(d['passed'])
    return out
