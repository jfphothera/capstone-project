"""Operational ticketing profile (rc.27, 2026-11-21 final ticketing decision).

One explicit, versioned role-routed profile for the human-operated ticket system:

* method C1 = topology C (fixed linear team) + treatment 1 (source-grounded ReAct writer, dense retrieval, the
  existing bounded selective planning entry rule). Built from the SAME versioned definitions the Testing Lab ran
  (``agentic_methodologies`` / ``candidate_execution.execution_plan``); the experiment matrix is not changed.
* role routes: Qwen 3.5 2B for intake, advisory ERP, QMS research and native tool selection; Qwen 3 14B for planning,
  customer writing, communication review and independent validation. Expected builds are pinned
  (``expected_digest``): a changed build is refused by the Gateway and the case is handed to the human with that
  reason; no other model is substituted.
* output allowances: double the per-purpose caps of the selected roles, 360 for ERP identity advisory. Applied at
  request construction (``cap``) and only inside a session pinned to THIS profile; Testing Lab defaults are unchanged.
* no ``lab_reviewer`` route: the named human representative replaces the Lab's simulated reviewer.

The profile snapshot carries the policy, so a case keeps it for every later pass (queued retry, human resume,
revision, customer follow-up): ``inference.session`` reuses a case's first snapshot. Cases created before this
profile keep their own snapshot and the default orchestrator.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os

PROFILE_ID = 'ticketing-c1-2026-11-21'
PROFILE_VERSION = 'ticketing-c1/1'
MODEL_2B = 'qwen3.5:2b'
MODEL_14B = 'qwen3:14b'
# rc.28: the bare digest exactly as the Gateway reports it (Ollama /api/tags). rc.27 wrote 'sha256:<digest>', which
# matched no head, so every call waited out the 300 s admission deadline (field evidence 2026-11-21 17:24).
EXPECTED_DIGESTS = {
    MODEL_2B: '0689d44085e06d165161a8a9a1731344278cfb5aade63a3c3dbdb48ab54b130a',
    MODEL_14B: 'bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8',
}
ROLE_MODELS = {
    'ticket_interpretation': MODEL_2B,
    'erp_research': MODEL_2B,        # advisory only; exact ERP verification stays deterministic
    'qms_research': MODEL_2B,
    'investigation': MODEL_2B,       # native tool selection
    'resolution_planning': MODEL_14B,
    'customer_communication': MODEL_14B,
    'communication_review': MODEL_14B,
    'independent_validation': MODEL_14B,
}
# Per-purpose maximum output tokens (maximum allowances, not requested lengths). Doubled from the rc.26 caps;
# ERP identity advisory 360 = three times the observed 120.
OUTPUT_CAPS = {
    'interpret_request': 320,         # was 160
    'erp_identity_advisory': 360,     # was 120
    'qms_search_query': 192,          # was 96
    'qms_coverage_summary': 640,      # was 320
    'native_tool_selection': 256,     # was 128
    'mediated_tool_selection': 192,   # was 96
    'compare_plans': 640,             # was 320 (only when the selective planner branches)
    'grounded_writer': 1200,          # was 600 (ReAct source-grounded writer)
    'customer_composition': 640,      # was 320
    'review_clarity': 400,            # was 200 (C1: only after a branched plan)
    'validate_draft': 640,            # was 320
}
ROUTE_CEILING = max(OUTPUT_CAPS.values())   # the route option may only lower a cap (inference.infer takes the min)
NUM_CTX = 8192                              # the qualified context; not enlarged


def enabled():
    """CAPSTONE_OPERATIONAL_PROFILE: 'on' forces it, 'off' keeps the built-in default profile. Default ('auto'): on in
    the installed application (Start-Capstone sets CAPSTONE_INFERENCE_MODE=gateway), off under the unit-test suite."""
    value = os.environ.get('CAPSTONE_OPERATIONAL_PROFILE', 'auto').strip().lower()
    if value in ('0', 'off', 'false', 'none'):
        return False
    if value in ('1', 'on', 'true'):
        return True
    return os.environ.get('CAPSTONE_INFERENCE_MODE') == 'gateway' and 'PYTEST_CURRENT_TEST' not in os.environ


def method():
    """The C1 definition and its execution policy, from the tested versioned registry."""
    from . import agentic_methodologies as am
    from .candidate_execution import execution_plan
    cell = next(c for c in am.candidates(['C'], ['1']))
    plan = execution_plan({'methodology': cell['methodology'], 'id': cell['id'], 'name': cell['name']})
    definition_hash = hashlib.sha256(json.dumps(cell['methodology'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return cell, plan, definition_hash


def profile(registry):
    gateway = next((r.get('endpoint') for r in (registry.get('profiles', 'gateway-default') or {}).get('routes', {}).values()), 'gateway')
    routes = {role: {'endpoint': gateway, 'model': model, 'enabled': True, 'expected_digest': EXPECTED_DIGESTS[model],
                     'options': {'temperature': 0.1, 'num_ctx': NUM_CTX, 'num_predict': ROUTE_CEILING}}
              for role, model in ROLE_MODELS.items()}
    return {'id': PROFILE_ID, 'name': 'Ticketing · C1 · Qwen3.5 2B front / Qwen3 14B back', 'default_endpoint': gateway,
            'routes': routes, 'retrieval_profile': 'minilm', 'strict': True, 'case_budget_seconds': 600}


def snapshot(registry):
    """The case snapshot new operational cases are pinned to (profile + C1 policy + output caps), hashed."""
    from . import ai_settings, experiments
    value = profile(registry)
    try:
        registry.put('profiles', value)    # visible and versioned in the existing Registry; activation is not used
    except Exception as exc:               # a registry without the Gateway endpoint (tests): the snapshot still pins it
        value['registry_note'] = 'not stored in the registry: ' + str(exc)[:160]
    snap = registry.snapshot(copy.deepcopy(value))
    cell, plan, definition_hash = method()
    snap['profile_version'] = PROFILE_VERSION
    snap['output_caps'] = dict(OUTPUT_CAPS)
    snap['expected_builds'] = dict(EXPECTED_DIGESTS)
    snap['operational_policy'] = {
        'schema': 'capstone.operational-policy/1', 'method_id': cell['id'], 'method_name': cell['name'],
        'method_definition_hash': definition_hash, 'registry_version': am_registry(),
        'topology_id': 'C', 'treatment_id': '1', 'retrieval': 'dense (qualified operational index)',
        'execution_policy': plan, 'self_correction': 'bounded grounding redraft (react.write_verified_reply)',
        'human_review': 'named representative (no lab_reviewer route)',
        'operational_activation': 'ticketing decision 2026-11-21 (outputs/final-ticketing-evaluation)',
        'source_fingerprint': experiments.source_revision()}
    snap.pop('configuration_hash', None)
    snap['configuration_hash'] = ai_settings.fingerprint(snap)
    return snap


def am_registry():
    from . import agentic_methodologies as am
    return am.CURRENT_REGISTRY


def install(store, registry):
    """Pin new operational cases to this profile (Runtime start). Returns the snapshot or None."""
    if not enabled():
        return None
    snap = snapshot(registry)
    store.inference_profile_snapshot = snap
    return snap


def _snapshot_now():
    from . import inference
    state = inference.current()
    return (state or {}).get('snapshot') or {}


def cap(purpose, default):
    """Output cap for one purpose inside the current session: the profile's allowance, else the code default."""
    caps = _snapshot_now().get('output_caps') or {}
    try:
        return int(caps.get(purpose, default))
    except (TypeError, ValueError):
        return default


def policy_for_case(store, case_id):
    """The operational policy pinned in this case's snapshot (None for cases created before the profile)."""
    row = store.one("SELECT payload FROM inference_snapshots WHERE scope_key=? ORDER BY at DESC LIMIT 1", ('case:' + case_id,))
    if row:
        return (json.loads(row['payload']).get('operational_policy') or None)
    snap = getattr(store, 'inference_profile_snapshot', None) or {}
    return snap.get('operational_policy')


def orchestrator_policy(store, case_id, trigger):
    policy = policy_for_case(store, case_id)
    if not policy:
        return None
    return {**policy['execution_policy'], '_trigger': trigger, 'operational_policy_hash': policy['method_definition_hash']}
