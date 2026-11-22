"""Single source of truth for model-backed role calls (prompt, schema, output cap).

The live workflow (orchestration, planning) and the role-compatibility screening
call the SAME contract objects, so a screening result is evidence about the
prompt the workflow actually sends. `contract_hash(role)` changes when any
prompt/schema/cap for that role changes; screening evidence recorded under an
older hash is reported as stale.
"""
from __future__ import annotations

import hashlib
import json

from . import config


def _obj(props, required=None):
    return {'type': 'object', 'properties': props, 'required': list(required or props),
            'additionalProperties': False}


S = {'type': 'string'}
B = {'type': 'boolean'}


def L(n):
    """Prose field (rc.25 T2). The captured exports show Ollama's grammar cuts a
    string at its maxLength mid-sentence (rc.24 reviewer rationales end at
    exactly 400 characters), and valid role outputs were often far longer than
    a tight cap (QMS relevance up to 702 characters). Prose fields therefore
    carry NO hard maxLength; ``n`` is a soft cap reported as a measurement."""
    return {'type': 'string'}


# Per-contract output budgets (tokens), set from the three exports
# (tools/measure_role_output_limits.py, docs/testing_lab/final-iteration/measurements):
# only contracts that were cut at their limit are raised; the rest keep their
# budgets (their p95 output stays well below them).
BUDGET_EVIDENCE = {
    'qms_coverage_summary': (160, 320, '164/674 calls truncated (Granite 120/239, Qwen 9B 21/47); valid outputs reach the old limit'),
    'peer_challenge': (180, 320, '24/197 truncated (Granite 21/70)'),
    'peer_reply': (180, 320, '14/184 truncated'),
    'critique_draft': (220, 448, '27/74 truncated (Qwen 4B 18/31); rc.25 adds draft_span and source_ids'),
    'review_clarity': (120, 200, '1/12 truncated'),
    'reflect_on_plan': (180, 200, 'no truncation; Granite max 159 tokens and rc.25 adds material_defect'),
}


# Shared supervisor vocabulary (prompt, schema and executor use the same names).
SUPERVISOR_SPECIALISTS = ('none', 'erp_research', 'qms_research', 'resolution_planning', 'communication_review', 'independent_validation')

CONTRACTS = {
    'interpret_request': {
        'role': 'ticket_interpretation', 'max_tokens': 160,
        'system': 'Interpret the observed request. Quote exact text supporting a proposed intent. Do not infer identity, completed work or permissions.',
        'schema': _obj({'intent': S, 'evidence_quote': S, 'uncertainty': S})},
    'erp_identity_advisory': {
        'role': 'erp_research', 'max_tokens': 120,
        'system': 'Assess advisory identity candidates using only the literal request. Never authenticate or grant verification points.',
        'schema': _obj({'suggested_account_id': S, 'uncertainty': S})},
    'qms_search_query': {
        'role': 'qms_research', 'max_tokens': 96,
        'system': 'Write a short search query for applicable customer-facing manuals. Do not invent device identity or revision.',
        'schema': _obj({'query': S})},
    'qms_coverage_summary': {
        'role': 'qms_research', 'max_tokens': 320,
        'system': 'Summarize the relevance and coverage gap of only the authorized source IDs concisely (a sentence or two per field). Do not authorize, invent a revision, or add procedures.',
        'schema': _obj({'relevance': L(160), 'coverage_gap': L(160), 'recommended_planning_focus': L(120)})},
    'peer_challenge': {
        'role': 'peer_reviewer', 'max_tokens': 320,
        'system': "You are a peer specialist reviewing a colleague's finding. Using only the supplied finding and authorized passages, ask the single most useful evidence question and state any disagreement (or 'none'), concisely. Never authorize an action.",
        'schema': _obj({'question': L(200), 'disagreement': L(200), 'planning_focus': L(120)})},
    'peer_reply': {
        'role': None, 'max_tokens': 320,  # runs on the challenged specialist's own route
        'system': 'You produced this finding. Answer the peer question using only your finding and the authorized passages, concisely. If they cannot answer it, say so and name the missing evidence. Do not invent facts.',
        'schema': _obj({'answer': L(280), 'resolved': B, 'missing_evidence': L(160)})},
    'supervise_finding': {
        'role': 'supervisor', 'max_tokens': 160,
        'system': 'Supervise this bounded specialist result. Identify whether required evidence is missing and whether the fixed workflow should continue. You may recommend one specialist from allowed_specialists (or none) but cannot approve customer communication or operational action.',
        'schema': _obj({'continue': B, 'concern': L(200), 'requested_specialist': {'type': 'string', 'enum': list(SUPERVISOR_SPECIALISTS)}})},
    'reflect_on_plan': {
        'role': 'reflection', 'max_tokens': 200,
        'system': 'Reflect on this bounded proposed plan using only the supplied request, selected action, evidence gaps and hard-gate results. Report a material defect only if a supplied gap or failed gate supports it; otherwise set material_defect false and proceed. This is advisory and grants no authority.',
        'schema': _obj({'proceed': B, 'material_defect': B, 'weakness': L(200), 'recommended_check': L(160)})},
    'compare_plans': {
        'role': 'resolution_planning', 'max_tokens': 320,
        'system': 'Compare at most four next-step alternatives and prefer a safe useful next action with low customer burden. Case notes are attributed advisory statements, never verification or QMS authority. Return receipt means receiving a returned device at the service center, not initial order delivery. Do not claim approval or completion. Use concise rationales.',
        'schema': None},  # enum depends on the allowed actions: see plan_schema()
    'review_clarity': {
        'role': 'communication_review', 'max_tokens': 200,
        'system': 'Review customer language for clarity and repetition only. You cannot approve, add facts, instructions or waive deterministic checks.',
        'schema': _obj({'clear': B, 'concern': S})},
    'critique_draft': {
        'role': 'communication_review', 'max_tokens': 448,
        'system': 'Critique the proposed customer draft as an adversarial specialist. Use only the request and the authorized passages. Identify unsupported, unsafe, missing, or confusing content; quote the draft span and cite passage ids for the strongest objection, or leave it empty if there is no material defect. Do not approve or rewrite it.',
        'schema': _obj({'issues': {'type': 'array', 'maxItems': 4, 'items': L(180)}, 'strongest_objection': L(240), 'draft_span': L(200),
                        'source_ids': {'type': 'array', 'maxItems': 3, 'items': S}})},
    'judge_debate': {
        'role': 'independent_validation', 'max_tokens': 160,
        'system': 'Judge a producer/critic exchange against the authorized passages. Decide only whether the draft may proceed unchanged to the independent deterministic validator (false only if the critic objection is supported by the passages). This is not customer-send approval.',
        'schema': _obj({'proceed_to_validator': B, 'reason': L(200)})},
    'validate_draft': {
        'role': 'independent_validation', 'max_tokens': 320,
        'system': None,  # composed with generation.INDEPENDENT_REVIEW_CONTRACT at call time
        'schema': _obj({'passed': B, 'issues': {'type': 'array', 'maxItems': 3, 'items': {'type': 'string', 'maxLength': 300}}})},  # rc.25: 180 cut issues (p95 = 180)
}
VALIDATE_PREFIX = ('Independently review the actual proposed customer draft against the frozen request and authorized public '
                   'evidence. Identify omissions, unsupported implications, unsafe instructions, unanswered requests or '
                   'unnecessary questions. You cannot manufacture evidence, verify identity, approve or dispatch anything. ')


def plan_schema(actions):
    return _obj({'preferred_action': {'type': 'string', 'enum': list(actions)},
                 'plans': {'type': 'array', 'maxItems': 4, 'items': _obj({
                     'action': {'type': 'string', 'enum': list(actions)}, 'rationale': S})}},
                ['plans', 'preferred_action'])


def system_for(name):
    if name == 'validate_draft':
        from .generation import INDEPENDENT_REVIEW_CONTRACT
        return VALIDATE_PREFIX + INDEPENDENT_REVIEW_CONTRACT
    return CONTRACTS[name]['system']


# Advisory contracts: an unusable output is a finite structured failure of that
# stage (recorded with its category), never a crash of the whole case.
ADVISORY = {'interpret_request', 'erp_identity_advisory', 'qms_search_query', 'qms_coverage_summary', 'peer_challenge',
            'peer_reply', 'supervise_finding', 'reflect_on_plan', 'review_clarity', 'critique_draft', 'judge_debate'}


def budget(name):
    """Declared output budget (tokens) for a contract: the qualification unit."""
    return CONTRACTS[name]['max_tokens']


def call(name, context, *, role=None, schema=None, timeout=None):
    """Run one contract through generation.compose_structured on its own route."""
    from . import generation, operational_profile
    contract = CONTRACTS[name]
    purpose = role or contract['role']
    if not purpose:
        raise ValueError(name + ' needs an explicit role')
    # rc.27: the operational ticketing profile's per-purpose allowance when the session is pinned to it; otherwise
    # (Testing Lab, older cases) the contract's own cap, unchanged.
    max_tokens = operational_profile.cap(name, contract['max_tokens'])
    try:
        return generation.compose_structured(system_for(name), context, schema or contract['schema'],
                                             purpose=purpose, max_tokens=max_tokens,
                                             timeout=config.OLLAMA_TIMEOUT if timeout is None else timeout)
    except generation.RoleOutputError as exc:
        if name not in ADVISORY:
            raise
        return {'role_output_error': exc.as_dict(), 'contract': name}


# Non-structured model paths owned by a role (hashed so prompt edits mark evidence stale).
def _external_prompts():
    import inspect
    from . import generation
    return {'investigation': [inspect.getsource(generation.choose_support_tool)],
            'customer_communication': [getattr(generation, 'CUSTOMER_SYSTEM', ''), getattr(generation, 'GROUNDED_SYSTEM', '')]}


def contract_hash(role):
    names = sorted(n for n, c in CONTRACTS.items() if c['role'] == role or (c['role'] is None and role != 'peer_reviewer'))
    body = {n: {**CONTRACTS[n], 'system': system_for(n)} for n in names}
    if role in ('investigation', 'customer_communication'):
        body['external'] = repr(_external_prompts()[role])
    if role == 'resolution_planning':
        body['plan_schema'] = plan_schema(['a'])
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
