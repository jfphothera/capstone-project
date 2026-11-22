"""Gateway discovery adapter (rc.25, client side of G1/G5).

Follows the Gateway 0.4.6 / client 0.4.2 contract
(``Inference Gateway\\docs\\handoff\\GATEWAY-0.4.6-CLIENT-CONTRACT.md``) and stays
compatible with Gateway 0.4.5. rc.25 keeps the vendored client 0.4.1 (hash-pinned
release); the additive 0.4.6 routes are reached through its generic
``request()`` when present, and the 0.4.2 methods are used when the vendored
client offers them:

* ``features(gateway)``: detected from the policy reply (``availability_included``)
  or a 0.4.2 client's ``gateway_info()``; feature names, never a version
  comparison; cached per client object;
* ``policy(gateway)``: operator policy alone (``/v1/policy?availability=0``)
  when ``policy_without_availability`` is offered, else the full 0.4.5 policy;
* ``fleet(gateway, ...)``: one application-scoped ``/v1/fleet`` snapshot for
  the trial's models / operations / context / capabilities, when
  ``fleet_snapshot`` is offered; ``None`` otherwise;
* ``model_states``: installed / qualified / available as SEPARATE states. On
  0.4.5 a model missing from the qualified listing is ``installed_unknown``,
  never "not installed";
* ``campaign_snapshot``: a bounded file snapshot shared by every worker process
  of a campaign on Gateway 0.4.5 (0.4.6 already coalesces launches in its own
  5 s fleet cache). Failed discovery is never cached.

Every snapshot is advisory: admission and dispatch re-check authorization,
digest, qualification, health and suspension on each real request.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
from pathlib import Path

ADAPTER_VERSION = 'gateway-capabilities-adapter-2026-11-19.2'
SNAPSHOT_TTL_SECONDS = int(os.environ.get('CAPSTONE_GATEWAY_SNAPSHOT_TTL', '300'))
SUSPENDED = ('model_suspended', 'model_suspended_on_resource')
_FEATURES = {}


def features(gateway):
    """Feature detection without an extra endpoint: a Gateway 0.4.6 answers
    ``/v1/policy?availability=0`` with ``availability_included: false``; older
    Gateways ignore the query (or answer 404) and their full policy is reused,
    so 0.4.5 pays nothing extra. A 0.4.2 client's ``gateway_info()`` is used
    when present."""
    key = id(gateway)
    if key not in _FEATURES:
        value = {'release': None, 'features': [], 'policy': None}
        try:
            if callable(getattr(gateway, 'gateway_info', None)):
                info = gateway.gateway_info()
                value.update(release=info.get('release'), features=list(info.get('features') or []))
            else:
                probe = gateway.request('/v1/policy?availability=0')
                value['policy'] = probe
                if isinstance(probe, dict) and probe.get('availability_included') is False:
                    value['features'] = ['policy_without_availability', 'fleet_snapshot']
        except Exception:
            pass
        _FEATURES[key] = value
    return _FEATURES[key]


def policy(gateway):
    detected = features(gateway)
    if 'policy_without_availability' in detected['features']:
        if detected.get('policy') is not None:
            return {'source': 'policy(availability=False)', 'policy': detected['policy']}
        try:
            return {'source': 'policy(availability=False)', 'policy': gateway.request('/v1/policy?availability=0')}
        except Exception:
            pass
    if detected.get('policy') is not None:
        return {'source': 'policy (Gateway 0.4.5 compatibility path; full policy)', 'policy': detected['policy']}
    return {'source': 'policy (Gateway 0.4.5 compatibility path)', 'policy': gateway.policy()}


def fleet(gateway, models=None, *, operations=None, num_ctx=None, capabilities=()):
    if 'fleet_snapshot' not in features(gateway)['features']:
        return None
    if callable(getattr(gateway, 'fleet_cached', None)):
        return gateway.fleet_cached(models, operations=operations, num_ctx=num_ctx, capabilities=capabilities)
    query = {}
    if models:query['models'] = ','.join(models)
    if operations:query['operations'] = ','.join(operations)
    if num_ctx is not None:query['num_ctx'] = str(num_ctx)
    if capabilities:query['capabilities'] = ','.join(capabilities)
    return gateway.request('/v1/fleet' + ('?' + urllib.parse.urlencode(query) if query else ''), timeout=30)


def _from_fleet(entry, operation):
    if entry.get('authorized') is False:
        return {'installed': None, 'qualified': None, 'availability': 'not_permitted', 'reason': 'not permitted for this identity', 'source': 'gateway fleet'}
    installed = (entry.get('installed') or {}).get('state')
    qualified = (entry.get('qualified') or {}).get('state')
    status = ((entry.get('available') or {}).get('status') or {}).get(operation)
    resources = entry.get('resources') or {}
    reason = {'installed': None, 'not_installed': 'not installed on any head this identity may use',
              'unknown': 'inventory not fresh; installation not confirmed'}.get(installed)
    if installed == 'installed' and status in SUSPENDED:
        reason = 'installed, temporarily unavailable: suspended after an upstream failure (automatic recovery probe pending)'
    elif installed == 'installed' and qualified == 'not_qualified':
        reason = 'installed but not qualified for this operation on its current build'
    elif status == 'busy':
        reason = 'available, all eligible slots busy now (it will queue)'
    elif status and status not in ('available', 'busy'):
        reason = reason or status.replace('_', ' ')
    return {'installed': {'installed': True, 'not_installed': False}.get(installed), 'installed_state': installed,
            'qualified': {'qualified': True, 'not_qualified': False}.get(qualified), 'qualified_state': qualified,
            'availability': status, 'reason': reason, 'heads': sorted(resources),
            'digest': next((r.get('inventory', {}).get('digest') for r in resources.values() if (r.get('inventory') or {}).get('digest')), None),
            'qualified_capabilities': (entry.get('qualified') or {}).get('capabilities') or [],
            'qualified_operations': sorted({o for r in resources.values() for o in ((r.get('qualification') or {}).get('operations') or [])}),
            'max_context': (entry.get('qualified') or {}).get('max_context'), 'source': 'gateway fleet'}


def model_states(gateway, models=None, *, operation='/api/generate', num_ctx=None, capabilities=()):
    """{model: {installed, qualified, availability, reason, ...}} with explicit sources."""
    try:
        snapshot = fleet(gateway, list(models) if models else None, operations=[operation], num_ctx=num_ctx, capabilities=list(capabilities))
    except Exception:
        snapshot = None
    if snapshot is not None:
        out = {e['model']: {**_from_fleet(e, operation), 'snapshot_id': (snapshot.get('snapshot') or {}).get('snapshot_id')}
               for e in snapshot.get('models') or []}
        for name in models or ():
            out.setdefault(name, {'installed': None, 'qualified': None, 'availability': 'unknown', 'source': 'gateway fleet',
                                  'reason': 'not reported by the fleet snapshot'})
        return out
    inventory = gateway.models()
    listed = {}
    for resource in inventory.get('resources', []):
        for name in resource.get('models', []):
            detail = (resource.get('model_details') or {}).get(name, {})
            listed.setdefault(name, []).append({'resource_id': resource.get('resource_id'), **detail})
    out = {}
    for name in (list(models) if models else sorted(listed)):
        heads = listed.get(name) or []
        if not heads:
            out[name] = {'installed': None, 'qualified': None, 'availability': 'installed_unknown', 'source': 'gateway 0.4.5 qualified listing',
                         'reason': 'not in the current qualified listing (it may be suspended or unqualified); installation cannot be confirmed by this Gateway version'}
            continue
        try:
            check = (gateway.availability(name, operation, num_ctx=num_ctx, capabilities=list(capabilities)) if capabilities
                     else gateway.availability(name, operation, num_ctx=num_ctx))
        except Exception as exc:
            check = {'status': 'unknown', 'reason': str(exc)[:200]}
        out[name] = {'installed': True, 'qualified': any(('embedding' if operation == '/api/embed' else 'completion') in (h.get('qualified_operations') or []) for h in heads),
                     'availability': check.get('status'), 'reason': check.get('reason'), 'heads': [h['resource_id'] for h in heads],
                     'digest': next((h.get('digest') for h in heads if h.get('digest')), None),
                     'qualified_capabilities': sorted({c for h in heads for c in (h.get('qualified_capabilities') or [])}),
                     'qualified_operations': sorted({o for h in heads for o in (h.get('qualified_operations') or [])}),
                     'max_context': max([(h.get('execution_policy') or {}).get('max_context') or 0 for h in heads] or [0]),
                     'source': 'gateway 0.4.5 listing + scoped availability'}
    return out


def installed_models_from_fleet(gateway, models, *, operations=('/api/generate', '/api/embed'), num_ctx=None):
    """Registry-health entries from one fleet call (None on Gateway 0.4.5)."""
    snapshot = fleet(gateway, list(models), operations=list(operations), num_ctx=num_ctx)
    if snapshot is None:
        return None
    rows = []
    for entry in snapshot.get('models') or []:
        if entry.get('authorized') is False:
            continue
        state = _from_fleet(entry, '/api/generate')
        embed = ((entry.get('available') or {}).get('status') or {}).get('/api/embed')
        operation = '/api/embed' if 'embedding' in state['qualified_operations'] and 'completion' not in state['qualified_operations'] else '/api/generate'
        status = ((entry.get('available') or {}).get('status') or {}).get(operation) if operation == '/api/generate' else embed
        if entry.get('authorized') is False or state.get('installed_state') != 'installed':
            continue
        rows.append({'name': entry['model'], 'qualified_operations': state['qualified_operations'],
                     'qualified_capabilities': state['qualified_capabilities'], 'digest': state['digest'],
                     'execution_policy': {'max_context': state['max_context']},
                     'availability': {'status': status, 'reason': state['reason']}, 'currently_available': status == 'available',
                     'fleet': {'installed': state['installed_state'], 'qualified': state['qualified_state'],
                               'snapshot_id': (snapshot.get('snapshot') or {}).get('snapshot_id')}})
    return rows


def _root():
    from . import config
    path = Path(config.DATA_DIR) / 'gateway-snapshots'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key(workload, endpoint, models, operation, num_ctx, capabilities):
    # The Gateway and identity are part of the key: a snapshot never crosses
    # client configurations (application scope).
    identity = os.environ.get('CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG' if workload == 'research' else 'CAPSTONE_GATEWAY_CLIENT_CONFIG') or ''
    try:
        identity += '|' + json.loads(Path(identity).read_text(encoding='utf-8-sig')).get('url', '') if identity else ''
    except (OSError, ValueError):
        pass
    payload = json.dumps({'w': workload, 'e': endpoint, 'm': sorted(models), 'o': operation, 'c': num_ctx, 'k': sorted(capabilities), 'g': identity}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def campaign_snapshot(fetch, *, workload, endpoint, models, operation='/api/generate', num_ctx=None, capabilities=(), ttl=None, now=None):
    """Shared preflight snapshot; ``fetch()`` builds a fresh value when stale."""
    ttl = SNAPSHOT_TTL_SECONDS if ttl is None else ttl
    now = now or time.time()
    path = _root() / (_key(workload, endpoint, models, operation, num_ctx, capabilities) + '.json')
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
            if now - cached['fetched_at'] < ttl:
                return {**cached['value'], 'snapshot': {'cached': True, 'fetched_at': cached['fetched_at'], 'age_seconds': round(now - cached['fetched_at'], 3),
                                                         'ttl_seconds': ttl, 'adapter': ADAPTER_VERSION}}
        except (ValueError, KeyError, OSError):
            pass
    value = fetch()
    if isinstance(value, dict) and value.get('inventory_scope') == 'fleet_snapshot_selected_models':
        # Gateway 0.4.6 already coalesces launches in its own 5 s fleet cache;
        # its snapshot is never kept beyond that validity here.
        return {**value, 'snapshot': {'cached': False, 'fetched_at': now, 'stored': False, 'source': 'gateway fleet', 'adapter': ADAPTER_VERSION}}
    if isinstance(value, dict) and value.get('reachable') is False:
        # A failed discovery is never cached: the next worker asks again.
        return {**value, 'snapshot': {'cached': False, 'fetched_at': now, 'stored': False, 'adapter': ADAPTER_VERSION}}
    tmp = path.with_suffix('.%d.tmp' % os.getpid())
    tmp.write_text(json.dumps({'fetched_at': now, 'value': value}, default=str), encoding='utf-8')
    os.replace(tmp, path)
    return {**value, 'snapshot': {'cached': False, 'fetched_at': now, 'age_seconds': 0, 'ttl_seconds': ttl, 'adapter': ADAPTER_VERSION}}


def invalidate():
    """Drop every snapshot (permission revocation, refusal, changed digest)."""
    for path in _root().glob('*.json'):
        try:
            path.unlink()
        except OSError:
            pass
