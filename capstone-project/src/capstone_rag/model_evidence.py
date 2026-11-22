"""Scoped model evidence and recommendation registry (rc.25, U1/T1).

Replaces unsupported universal labels ("most accurate", "fastest",
"recommended") with dated, role-scoped evidence records. Presentation rules:

* tested for a role -> green ("tested", NOT safe / approved / best);
* a scoped discourage recommendation -> gray, which takes precedence;
* untested -> the original normal appearance;
* advisory appearance NEVER disables, hides or deselects an option, and is
  never a compatibility block. Installation, permission and compatibility
  requirements are separate and keep their own explicit reasons.

Exact build identity is tracked: the captured campaigns recorded tags but not
digests, so seeded records say so (``tested_digest: None``). When the Gateway
reports a digest for a model whose tested digest is known and different, the
state is ``build_changed_retest_needed``.

The native tool-calling capability evidence also drives the tool strategy
resolver used by the experiment builder AND the lab UI (one resolved profile).
"""
from __future__ import annotations

import copy
import re

REGISTRY_VERSION = 'model-evidence-2026-11-19.1'
EVIDENCE_DATE = '2026-11-19'
EXPERIMENTS = {'baseline': 'EXP-297fb3ed87844b10b9eef4a654026f22', 'c922': 'EXP-c922a79470f945579bb2951cc758896a',
               'f253': 'EXP-f253c55fa55c4d9893c70bd14e41398d'}

SEED = [
    {'model': 'qwen3.5:4b', 'role': 'main', 'state': 'tested', 'recommendation': 'none', 'experiments': ['baseline', 'c922', 'f253'],
     'strategy': 'native', 'budget': {'num_ctx': 8192, 'num_predict': 512},
     'reason': 'Tested main-model reference; green for the tested role. No production endorsement.'},
    {'model': 'qwen3.5:9b', 'role': 'main', 'state': 'tested', 'recommendation': 'none', 'experiments': ['c922'],
     'strategy': 'native', 'budget': {'num_ctx': 8192, 'num_predict': 512},
     'reason': 'Tested main model in the partial c922 campaign; comparative quality evidence incomplete.'},
    {'model': 'llama3.2:3b', 'role': 'main', 'state': 'tested', 'recommendation': 'none', 'experiments': ['f253'],
     'strategy': 'native', 'budget': {'num_ctx': 8192, 'num_predict': 512},
     'reason': 'Tested main model; extra native tool arguments on 20/94 calls remain a Phase 4 contract issue.'},
    {'model': 'qwen3:1.7b', 'role': 'main', 'state': 'tested', 'recommendation': 'none', 'experiments': ['f253'],
     'strategy': 'native', 'budget': {'num_ctx': 8192, 'num_predict': 512},
     'reason': 'Tested main model; extra native arguments on all 101 inspected calls need contract validation. '
               'Different family generation and head 6 use confound a pure size comparison.'},
    {'model': 'gemma3:4b', 'role': 'main', 'state': 'tested', 'recommendation': 'discourage', 'experiments': ['c922'],
     'strategy': 'native', 'budget': {'num_ctx': 8192, 'num_predict': 512}, 'native_tools': False,
     'reason': 'Native tools unsupported in the observed configuration (21 native attempts, 0 completed: 18 HTTP 400, 3 unavailable) '
               'and unresolved required-role compatibility (12 HTTP 500 structured customer-communication requests, cause unresolved). '
               'Native incompatibility does not prove mediated support useless; an explicit mediated profile must be qualified separately.'},
    {'model': 'granite4:3b', 'role': 'main', 'state': 'tested', 'recommendation': 'discourage', 'experiments': ['f253'],
     'strategy': 'native', 'budget': {'num_ctx': 8192, 'num_predict': 512}, 'scope': 'uniform all-role profile',
     'reason': 'Gray for the uniform all-role profile: 122/389 QMS and 21/70 peer calls reached the output limit with invalid JSON. '
               'Role-specific eligibility is kept; not unusable for all bounded tasks.'},
    {'model': 'qwen3:4b', 'role': 'lab_reviewer', 'state': 'tested', 'recommendation': 'none', 'experiments': ['baseline', 'c922', 'f253'],
     'strategy': None, 'budget': {'num_ctx': 8192, 'num_predict': 1024},
     'reason': 'Tested as the fixed simulated reviewer (reviewer-role scope only; not main-model evidence). Correctness calibration '
               'warning: 14 checked first-draft objections demanded removing an approved compartment check.'},
]


_NATIVE_BY_MODEL = None


def _recorded_native(model):
    """(native_tools, evidence_id) from the seeded records, without copying them on every call (lab state hot path)."""
    global _NATIVE_BY_MODEL
    if _NATIVE_BY_MODEL is None:
        _NATIVE_BY_MODEL = {}
        for r in records():
            if r.get('native_tools') is not None:
                _NATIVE_BY_MODEL.setdefault(r['model'], (r['native_tools'], r['evidence_id']))
    return _NATIVE_BY_MODEL.get(model)


def records():
    out = []
    for row in SEED:
        value = copy.deepcopy(row)
        value.update(registry_version=REGISTRY_VERSION, recommendation_date=EVIDENCE_DATE, tested_digest=None,
                     digest_status='tag only: the captured campaigns did not record a model digest',
                     runtime='Ollama 0.40.15 (heads, observed at recommendation time)', method_registry='agentic-matrix/1',
                     experiment_ids=[EXPERIMENTS[e] for e in row['experiments']], qualified=False,
                     evidence_id='EVID-' + row['model'].replace(':', '-') + '-' + row['role'], supersedes=None)
        out.append(value)
    return out


def _matches(record, model, role):
    if record['model'] != model:
        return False
    return role is None or record['role'] == role or (record['role'] == 'main' and role not in ('lab_reviewer',))


def presentation(model, role='main', *, gateway_digest=None):
    """Advisory appearance of one model option for one role.

    Returns appearance 'gray' | 'green' | 'normal' plus text badges. Never a
    selectability flag: ``selectable`` is always True here; real
    installation/permission/compatibility checks are reported separately."""
    rows = [r for r in records() if _matches(r, model, role)]
    badges, reasons = [], []
    appearance = 'normal'
    if any(r['state'] == 'tested' for r in rows):
        appearance = 'green';badges.append('tested (' + ', '.join(sorted({r['role'] for r in rows})) + ')')
    if any(r['recommendation'] == 'discourage' for r in rows):
        appearance = 'gray';badges.append('discouraged')
    for r in rows:
        reasons.append(r['reason'])
        if gateway_digest and r.get('tested_digest') and gateway_digest != r['tested_digest']:
            badges.append('build changed; retest needed')
    if rows and gateway_digest and not any(r.get('tested_digest') for r in rows):
        badges.append('tested build not recorded (tag only)')
    other_roles = sorted({r['role'] for r in records() if r['model'] == model} - {r['role'] for r in rows})
    if other_roles:
        badges.append('tested only as ' + ', '.join(other_roles))
    return {'model': model, 'role': role, 'appearance': appearance, 'selectable': True, 'badges': badges, 'reasons': reasons,
            'evidence_ids': [r['evidence_id'] for r in rows], 'note': 'Green means tested for this role, not safe, approved or best.'}


# --- tool strategy resolution (T1) ---------------------------------------------

def tool_facts(details):
    """Tool-calling facts for one model from the Gateway's per-head model details
    (models() 'model_details' entries): the Gateway's own native-tools test state
    and the runtime-declared capabilities (Ollama /api/show 'capabilities')."""
    states, declared, qualified = [], set(), set()
    for d in details or ():
        if not isinstance(d, dict):
            continue
        states.append(((d.get('qualifications') or {}).get('tools') or {}).get('state'))
        values = (d.get('declared_capabilities') or {}).get('values')
        if not isinstance(values, list):
            values = d.get('capabilities') if isinstance(d.get('capabilities'), list) else []
        declared.update(v for v in values if isinstance(v, str))
        qualified.update(d.get('qualified_capabilities') or [])
    tools_state = 'passed' if 'passed' in states else 'failed' if 'failed' in states else None
    return {'tools_state': tools_state, 'declared': sorted(declared), 'qualified': sorted(qualified)}


def native_tools_evidence(model, gateway_capabilities=None):
    """(True | False | None, basis).

    Order: the Gateway's native-tools test (passed/failed); then the runtime's
    declared capabilities (Ollama's model template: 'tools' or not); then
    recorded lab evidence; otherwise unknown. A qualified-capability list
    WITHOUT 'tools' only means the tools test was never run: it is not evidence
    of no tool support (rc.25 field defect: every completion-only model was
    refused)."""
    caps = gateway_capabilities
    if isinstance(caps, dict):
        if caps.get('tools_state') == 'passed':
            return True, 'Gateway native-tools test passed'
        if caps.get('tools_state') == 'failed':
            return False, 'Gateway native-tools test failed'
        declared = caps.get('declared')
        if isinstance(declared, list) and declared:
            if 'tools' in declared:
                return True, 'runtime declares tool support (Ollama model capabilities)'
            return False, 'runtime does not declare tool support (Ollama model capabilities: ' + ', '.join(declared) + ')'
        caps = caps.get('qualified')
    if isinstance(caps, list) and 'tools' in caps:
        return True, 'gateway qualified capabilities'
    recorded = _recorded_native(model)
    if recorded:
        return recorded[0], 'lab evidence ' + recorded[1]
    if re.match(r'^gemma3', model or ''):
        return False, 'lab evidence: gemma3 family rejected native tool requests (HTTP 400)'
    return None, 'no capability evidence'


class ToolStrategyIncompatible(ValueError):
    pass


# Default delegate for a model without confirmed native tools: the first of these whose native tool calling is
# confirmed (Gateway tools test or Ollama-declared), then any other confirmed model.
DELEGATE_PREFERENCE = ('qwen3.5:4b', 'qwen3:8b', 'qwen3.5:9b', 'qwen3:4b', 'llama3.2:3b', 'qwen3.5:2b')


def default_delegate(model, candidates):
    """candidates: {model name: tool facts or qualified list} of usable models. Returns a tool-capable delegate or None."""
    candidates = candidates or {}
    for name in DELEGATE_PREFERENCE:
        if name != model and name in candidates and native_tools_evidence(name, candidates[name])[0] is True:
            return name
    ok = sorted(n for n, f in candidates.items() if n != model and native_tools_evidence(n, f)[0] is True)
    return ok[0] if ok else None


def resolve_tool_strategy(model, explicit=None, gateway_capabilities=None, *, required=True, delegates=None):
    """The strategy a profile will launch with, with its basis (T1).

    * an explicit user selection is preserved exactly;
    * explicit native on a model without native tools -> incompatible;
    * no selection: native when capability evidence supports it or is
      unknown (recorded as unknown, never silently switched after failure);
      a model WITHOUT native tools -> incompatible until the user explicitly
      selects mediated (same pinned model) or delegated (declared delegate).
    """
    native, basis = native_tools_evidence(model, gateway_capabilities)
    choice = dict(explicit or {})
    strategy = choice.get('strategy')
    if strategy:
        if strategy == 'native' and native is False:
            raise ToolStrategyIncompatible(f'{model}: native tool calling was explicitly selected but {basis} shows no native tools; '
                                           'select mediated (same model) or delegated (declared tool model)')
        return {'strategy': strategy, 'delegate_model': choice.get('delegate_model'), 'basis': 'explicit user selection',
                'native_tools': native, 'capability_basis': basis}
    if not required:
        return {'strategy': None, 'basis': 'no tool-carrying role in this profile', 'native_tools': native, 'capability_basis': basis}
    if native is not True and delegates:
        # Operator rule (rc.25 field fix): a model without confirmed native tools delegates its tool calls by default to
        # a model whose native tools ARE confirmed; shown under the model and changeable.
        delegate = default_delegate(model, delegates)
        if delegate:
            why = 'no native tool calling' if native is False else 'native tool calling not confirmed'
            return {'strategy': 'delegated', 'delegate_model': delegate, 'default': True,
                    'basis': f'default: {why} ({basis}); tool calls go to {delegate}',
                    'native_tools': native, 'capability_basis': basis}
    if native is False:
        raise ToolStrategyIncompatible(f'{model}: no native tool calling ({basis}). The automatic default would send native tools; '
                                       'choose an explicit mediated or delegated tool strategy for this model')
    return {'strategy': 'native', 'delegate_model': None,
            'basis': 'default native: capability ' + ('confirmed' if native else 'unknown (recorded; a native failure is never re-labelled)'),
            'native_tools': native, 'capability_basis': basis}
