"""Routing, budget and failure-classification evidence for Testing Lab runs.

Built only from recorded inference calls (inference_calls.payload) and the
run's requested profile, so a run cannot count as evidence for a model or
budget it did not actually use.
"""
from __future__ import annotations

FAILURE_CLASSES = (
    'incorrect_answer', 'missing_or_poor_evidence', 'context_overflow', 'insufficient_output_budget',
    'invalid_structured_output', 'tool_or_permission_failure', 'native_tools_unsupported', 'incorrect_routing',
    'handoff_or_orchestration_defect', 'timeout', 'model_unavailable', 'infrastructure_failure',
    'safety_violation', 'insufficient_evidence')
# Classes that say nothing about the model's ability to do the role.
NON_CAPABILITY = {'context_overflow', 'insufficient_output_budget', 'incorrect_routing', 'handoff_or_orchestration_defect',
                  'timeout', 'model_unavailable', 'infrastructure_failure', 'insufficient_evidence'}


VOLATILE_OPTIONS = {'seed'}  # varies per repetition by design; not part of identity


def configuration_identity(profile, architecture=None):
    """Stable identity of the experimental configuration under test.

    Built from the actual specification: every system-under-test route
    (model, inference options except the per-repetition seed, tool strategy,
    delegation, enabled flag) and the methodology/execution plan. The
    simulated reviewer is identified separately (reviewer_id). Labels are for
    display only."""
    from .ai_settings import LAB_ROLES, canonical, fingerprint
    routes = (profile or {}).get('routes') or {}
    sut = {}
    for role, route in sorted(routes.items()):
        if role in LAB_ROLES:
            continue
        sut[role] = {'model': route.get('model'), 'enabled': route.get('enabled', True), 'tool_strategy': route.get('tool_strategy'),
                     'options': {k: v for k, v in sorted((route.get('options') or {}).items()) if k not in VOLATILE_OPTIONS}}
    methodology = None
    if architecture:
        from .candidate_execution import execution_plan
        try:
            methodology = {'id': architecture.get('id'), 'plan': execution_plan(architecture)}
        except Exception:
            methodology = {'id': architecture.get('id'), 'plan': None}
        # rc.25: a changed method definition is a new registry version; its
        # cells must never share an identity with the old ones. v1 (and
        # unversioned) identities are left byte-identical for history.
        if architecture.get('registry_version') not in (None, 'agentic-matrix/1'):
            methodology.update(registry_version=architecture['registry_version'], configuration_hash=architecture.get('configuration_hash'))
    components = {'routes': sut, 'methodology': methodology or {'id': 'candidate-a', 'plan': 'baseline'}}
    reviewer_route = routes.get('lab_reviewer') or {}
    reviewer = {'model': reviewer_route.get('model'), 'policy_version': reviewer_route.get('policy_version'),
                'options': {k: v for k, v in sorted((reviewer_route.get('options') or {}).items()) if k not in VOLATILE_OPTIONS}} if reviewer_route else None
    models = sorted({r['model'] for r in sut.values() if r['model']})
    by_model = {}
    for role, r in sut.items():
        by_model.setdefault(r['model'], []).append(role)
    if len(models) == 1:
        label = models[0]
    else:
        minority = sorted(by_model, key=lambda m: len(by_model[m]))[:-1]
        label = 'team: ' + ', '.join(f"{'+'.join(sorted(by_model[m]))}={m}" for m in minority) + f"; rest={max(by_model, key=lambda m: len(by_model[m]))}"
    return {'configuration_id': 'CFG-' + fingerprint(components)[:16], 'reviewer_id': ('REV-' + fingerprint(reviewer)[:12]) if reviewer else None,
            'label': label + f" · {(methodology or {}).get('id') or 'baseline'}", 'components': components, 'reviewer': reviewer}


def call_audit(parsed_calls, requested_routes):
    """Per-run routing and budget audit from recorded calls."""
    by_role, mismatches, fallbacks, truncated, budget_failures = {}, [], [], [], []
    prompt_tokens = output_tokens = 0
    for call in parsed_calls:
        role = call.get('role')
        requested = (requested_routes.get(role) or {}).get('model') or call.get('requested_model')
        executed = call.get('model')
        entry = by_role.setdefault(role, {'requested_model': requested, 'executed_models': [], 'calls': 0,
                                          'requested_num_ctx': call.get('requested_num_ctx'),
                                          'requested_num_predict': call.get('requested_num_predict'),
                                          'max_input_tokens_estimate': 0, 'max_prompt_eval_count': None,
                                          'done_reasons': [], 'available_output_tokens': [], 'thinking_chars': 0, 'model_digests': [], 'duration_ms': 0.0})
        entry['calls'] += 1
        entry['duration_ms'] += float(call.get('duration_ms') or 0)
        if executed and executed not in entry['executed_models']:
            entry['executed_models'].append(executed)
        if call.get('model_digest') and call['model_digest'] not in entry['model_digests']:
            entry['model_digests'].append(call['model_digest'])
        if call.get('status') == 'completed' and requested and executed and executed != requested:
            mismatches.append({'call_id': call.get('call_id'), 'role': role, 'requested': requested, 'executed': executed})
        if call.get('route_fallback'):
            fallbacks.append({'call_id': call.get('call_id'), 'role': role, 'fallback_route': call['route_fallback']})
        check = call.get('budget_check') or {}
        entry['max_input_tokens_estimate'] = max(entry['max_input_tokens_estimate'], int(check.get('input_tokens_estimate') or 0))
        timings = call.get('timings') or {}
        if isinstance(timings.get('prompt_eval_count'), int):
            entry['max_prompt_eval_count'] = max(entry['max_prompt_eval_count'] or 0, timings['prompt_eval_count'])
            prompt_tokens += timings['prompt_eval_count']
        if isinstance(timings.get('eval_count'), int):
            output_tokens += timings['eval_count']
        if isinstance(call.get('available_output_tokens'), int):
            entry['available_output_tokens'].append(call['available_output_tokens'])
        if timings.get('done_reason'):
            entry['done_reasons'].append(timings['done_reason'])
            if timings['done_reason'] == 'length':
                truncated.append({'call_id': call.get('call_id'), 'role': role, 'finish_reason': 'length',
                                  'eval_count': timings.get('eval_count'), 'requested_num_predict': call.get('requested_num_predict')})
        entry['thinking_chars'] += int(call.get('thinking_chars') or 0)
        if call.get('budget_failure'):
            budget_failures.append({'call_id': call.get('call_id'), 'role': role, 'reason': call.get('error')})
    return {'by_role': by_role, 'routing_mismatches': mismatches, 'route_fallbacks': fallbacks,
            'routing_verified': bool(parsed_calls) and not mismatches and not fallbacks,
            'output_truncated_calls': truncated, 'context_budget_failures': budget_failures,
            'prompt_tokens_observed': prompt_tokens, 'output_tokens_observed': output_tokens,
            'effective_num_ctx': 'unverified (the serving backend does not report the applied context; requested value and Gateway qualification are recorded)'}


def classify_failure(error, audit=None, *, safety_violations=None):
    """Map a failed or incomplete attempt to an actionable class."""
    text = str(error or '').lower()
    audit = audit or {}
    if safety_violations:
        return 'safety_violation'
    if audit.get('routing_mismatches') or audit.get('route_fallbacks'):
        return 'incorrect_routing'
    if audit.get('context_budget_failures') or 'exceeds requested context' in text:
        return 'context_overflow'
    if 'unterminated string' in text or "expecting ','" in text or 'expecting property' in text:
        return 'insufficient_output_budget' if audit.get('output_truncated_calls') else 'invalid_structured_output'
    if 'line 1 column 1 (char 0)' in text or 'expecting value' in text:
        return 'invalid_structured_output'
    if 'does not support tools' in text or ('400' in text and 'tool' in text):
        return 'native_tools_unsupported'
    if 'permission' in text or 'refused:' in text:
        return 'tool_or_permission_failure'
    if 'timed out' in text or 'deadline' in text or 'wall-clock' in text:
        return 'timeout'
    if 'not installed' in text or 'unavailable' in text or 'refused' in text or 'withdrawn' in text:
        return 'model_unavailable'
    if 'gateway' in text or 'database is locked' in text or 'connection' in text or 'winerror' in text:
        return 'infrastructure_failure'
    if 'http error 400' in text:
        return 'context_overflow' if any((r.get('max_input_tokens_estimate') or 0) > (r.get('requested_num_ctx') or 10**9) for r in (audit.get('by_role') or {}).values()) else 'infrastructure_failure'
    return 'handoff_or_orchestration_defect' if error else 'insufficient_evidence'
