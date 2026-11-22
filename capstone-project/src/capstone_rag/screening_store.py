"""Persistence for model-compatibility screening (registry database).

Attempts are immutable (triggers reject UPDATE and DELETE). Eligibility is
derived from attempts on read, so it can never disagree with the evidence, and
is marked stale when the role's contract hash, the task version or the model
digest changes.
"""
from __future__ import annotations

import csv
import io
import json
import time
import uuid
from contextlib import closing

from .ai_settings import canonical, fingerprint
from .lab_evidence import NON_CAPABILITY

SCHEMA = """
CREATE TABLE IF NOT EXISTS role_screen_attempts(
  attempt_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE,
  model TEXT NOT NULL, model_digest TEXT, role TEXT NOT NULL, task_id TEXT NOT NULL, task_version INTEGER NOT NULL,
  task_set_version TEXT, kind TEXT, contract_hash TEXT, settings_hash TEXT, num_ctx INTEGER, num_predict INTEGER,
  outcome TEXT NOT NULL CHECK(outcome IN ('passed','failed','blocked')), failure_class TEXT,
  routing_verified INTEGER, executed_models TEXT, diagnoses_attempt_id TEXT, duration_ms REAL, at REAL NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS role_screen_by_model_role ON role_screen_attempts(model, role);
CREATE TRIGGER IF NOT EXISTS role_screen_attempts_no_update BEFORE UPDATE ON role_screen_attempts
  BEGIN SELECT RAISE(ABORT, 'screening attempts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS role_screen_attempts_no_delete BEFORE DELETE ON role_screen_attempts
  BEGIN SELECT RAISE(ABORT, 'screening attempts are immutable'); END;
"""
STATUSES = ('passed', 'failed', 'inconclusive_blocked', 'not_tested')


def ensure(registry):
    with registry.connect() as db:
        db.executescript(SCHEMA)


def attempt_from_run(run):
    """Build one attempt row from a finished role_screen run (or None)."""
    scenario = run.get('scenario') or {}
    if run.get('level') != 'role_screen' or not scenario.get('screen_task'):
        return None
    role = scenario['screen_role'];metrics = run.get('metrics') or {}
    audit = metrics.get('routing_audit') or {}
    route = ((run.get('profile') or {}).get('routes') or {}).get(role) or {}
    options = route.get('options') or {}
    spec = run.get('specialist_result') or {}
    screening = spec.get('screening') or {}
    role_audit = (audit.get('by_role') or {}).get(role) or {}
    from .lab_evidence import classify_failure
    if run.get('status') == 'completed' and audit and not audit.get('routing_verified'):
        outcome, failure = 'blocked', 'incorrect_routing'
    elif run.get('status') == 'completed' and spec.get('task_accuracy') is True:
        outcome, failure = 'passed', None
    elif run.get('status') == 'completed':
        failure = 'invalid_structured_output' if not spec.get('structured_output_valid') else (
            'insufficient_output_budget' if audit.get('output_truncated_calls') else 'incorrect_answer')
        outcome = 'blocked' if failure in NON_CAPABILITY else 'failed'
    else:
        failure = run.get('failure_class') or classify_failure(run.get('error'), audit)
        outcome = 'blocked' if failure in NON_CAPABILITY else 'failed'
    return {'attempt_id': 'SCR-' + uuid.uuid4().hex, 'experiment_id': run.get('experiment_id'), 'run_id': run['id'],
            'model': route.get('model') or '?', 'model_digest': (role_audit.get('model_digests') or [None])[0],
            'role': role, 'task_id': scenario['screen_task'], 'task_version': int(scenario.get('screen_task_version') or 0),
            'task_set_version': scenario.get('task_set_version'), 'kind': scenario.get('screen_kind'),
            'contract_hash': screening.get('contract_hash'),
            'settings_hash': fingerprint({k: options.get(k) for k in ('num_ctx', 'num_predict', 'temperature', 'top_p', 'top_k')}),
            'num_ctx': options.get('num_ctx'), 'num_predict': options.get('num_predict'),
            'outcome': outcome, 'failure_class': failure,
            'routing_verified': None if not audit else int(bool(audit.get('routing_verified'))),
            'executed_models': canonical(role_audit.get('executed_models') or []),
            'diagnoses_attempt_id': scenario.get('diagnoses_attempt_id'),
            'duration_ms': run.get('duration_ms') or metrics.get('total_ms'), 'at': time.time(),
            'payload': canonical({'checks': spec.get('checks'), 'output': spec.get('output'), 'error': run.get('error'),
                                  'mechanism': screening.get('mechanism'), 'mode': screening.get('mode'),
                                  'budget': {k: role_audit.get(k) for k in ('max_input_tokens_estimate', 'max_prompt_eval_count', 'done_reasons', 'thinking_chars')},
                                  'truncated_calls': len(audit.get('output_truncated_calls') or []),
                                  'context_budget_failures': audit.get('context_budget_failures') or [],
                                  'route_fallbacks': audit.get('route_fallbacks') or [],
                                  'routing_mismatches': audit.get('routing_mismatches') or []})}


def record(registry, run):
    row = attempt_from_run(run)
    if not row:
        return None
    ensure(registry)
    with registry.connect() as db:
        if db.execute('SELECT 1 FROM role_screen_attempts WHERE run_id=?', (row['run_id'],)).fetchone():
            return None  # a run is recorded once; resumes never overwrite evidence
        db.execute(f"INSERT INTO role_screen_attempts({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
    return row


def attempts(registry, *, model=None, role=None, experiment_id=None):
    ensure(registry)
    sql, args = 'SELECT * FROM role_screen_attempts WHERE 1=1', []
    for column, value in (('model', model), ('role', role), ('experiment_id', experiment_id)):
        if value:
            sql += f' AND {column}=?';args.append(value)
    with registry.connect() as db:
        db.row_factory = __import__('sqlite3').Row
        return [dict(r) for r in db.execute(sql + ' ORDER BY at', args)]


def _current_digests(registry):
    """Latest observed digest per model (from the most recent attempt)."""
    latest = {}
    for row in attempts(registry):
        if row['model_digest']:
            latest[row['model']] = row['model_digest']
    return latest


def eligibility(registry, models=None, roles=None, current_digests=None):
    """Derived eligibility per (model, role) from immutable attempts.

    Current evidence must match: the role's contract hash, the task version,
    the model digest (live Gateway digest when supplied, else the newest
    observed) and the settings of the most recent screening for that
    model/role. Budget-diagnostic attempts are kept regardless of settings:
    they exist to show the budget a role needs.
    """
    from . import role_contracts, role_screening
    rows = attempts(registry);roles = list(roles or role_screening.MODEL_ROLES)
    models = list(models or sorted({r['model'] for r in rows}))
    digests = {**_current_digests(registry), **{k: v for k, v in (current_digests or {}).items() if v}}
    latest_settings = {}
    for r in rows:  # ordered by time
        if not r['diagnoses_attempt_id']:
            latest_settings[(r['model'], r['role'])] = r['settings_hash']
    tasks = {r: [t for t in role_screening.TASKS if t['role'] == r] for r in roles}
    hashes = {r: role_contracts.contract_hash(r) for r in roles}
    matrix = []
    for model in models:
        for role in roles:
            mine = [r for r in rows if r['model'] == model and r['role'] == role]
            current = [r for r in mine if r['contract_hash'] == hashes[role]
                       and any(t['id'] == r['task_id'] and t['version'] == r['task_version'] for t in tasks[role])
                       and (not r['model_digest'] or not digests.get(model) or r['model_digest'] == digests[model])
                       and (r['diagnoses_attempt_id'] or r['settings_hash'] == latest_settings.get((model, role)))]
            stale = bool(mine) and len(current) < len(mine)
            per_task = {}
            for t in tasks[role]:
                ts = [r for r in current if r['task_id'] == t['id']]
                passed = [r for r in ts if r['outcome'] == 'passed']
                failed = [r for r in ts if r['outcome'] == 'failed']
                blocked = [r for r in ts if r['outcome'] == 'blocked']
                per_task[t['id']] = {'kind': t['kind'], 'attempts': len(ts), 'passed': len(passed), 'failed': len(failed),
                                     'blocked': len(blocked),
                                     # A capability failure outweighs a pass; a pass outweighs an
                                     # infrastructure/budget block; blocks alone are inconclusive.
                                     'status': 'not_tested' if not ts else 'failed' if failed else 'passed' if passed else 'inconclusive_blocked',
                                     'failure_classes': sorted({r['failure_class'] for r in ts if r['failure_class']}),
                                     'budget_used': max([r['num_ctx'] or 0 for r in passed] or [0]) or None}
            statuses = [v['status'] for v in per_task.values()]
            if not current:
                status = 'not_tested'
            elif 'failed' in statuses:
                status = 'failed'
            elif statuses and all(s == 'passed' for s in statuses):
                status = 'passed'
            else:
                status = 'inconclusive_blocked'
            durations = [r['duration_ms'] for r in current if r['duration_ms'] and r['outcome'] == 'passed']
            matrix.append({'model': model, 'role': role, 'status': status, 'stale_attempts': len(mine) - len(current),
                           'stale': stale and not current, 'attempts': len(current), 'tasks': per_task,
                           'tasks_required': len(tasks[role]),
                           'routing_unverified': sum(1 for r in current if r['routing_verified'] == 0),
                           'median_passed_ms': sorted(durations)[len(durations) // 2] if durations else None,
                           'required_num_ctx': max([v['budget_used'] or 0 for v in per_task.values()] or [0]) or None,
                           'contract_hash': hashes[role]})
    return matrix


def csv_export(registry, kind='summary', **filters):
    out = io.StringIO()
    if kind == 'attempts':
        rows = attempts(registry, **filters)
        fields = ['attempt_id', 'experiment_id', 'run_id', 'model', 'model_digest', 'role', 'task_id', 'task_version', 'kind',
                  'outcome', 'failure_class', 'routing_verified', 'executed_models', 'num_ctx', 'num_predict', 'contract_hash',
                  'settings_hash', 'diagnoses_attempt_id', 'duration_ms', 'at']
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore');writer.writeheader()
        for r in rows:
            writer.writerow(r)
    else:
        rows = eligibility(registry, models=filters.get('models'), roles=filters.get('roles'))
        fields = ['model', 'role', 'status', 'attempts', 'tasks_required', 'stale_attempts', 'routing_unverified',
                  'median_passed_ms', 'required_num_ctx', 'failed_tasks', 'blocked_tasks', 'failure_classes']
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore');writer.writeheader()
        for r in rows:
            writer.writerow({**r, 'failed_tasks': ' '.join(k for k, v in r['tasks'].items() if v['status'] == 'failed'),
                             'blocked_tasks': ' '.join(k for k, v in r['tasks'].items() if v['status'] == 'inconclusive_blocked'),
                             'failure_classes': ' '.join(sorted({c for v in r['tasks'].values() for c in v['failure_classes']}))})
    return out.getvalue()


def diagnostic_spec(registry, attempt_id, *, factor=2):
    """Plan a budget diagnostic for one failed/blocked attempt, linked to it."""
    row = next((r for r in attempts(registry) if r['attempt_id'] == attempt_id), None)
    if not row:
        raise ValueError('Unknown screening attempt')
    if row['failure_class'] not in ('context_overflow', 'insufficient_output_budget', 'invalid_structured_output'):
        raise ValueError('Budget diagnostics apply to context/output-budget or truncated-JSON failures only (this attempt: %s)' % row['failure_class'])
    budget = {}
    if row['failure_class'] == 'context_overflow':
        budget['num_ctx'] = min(65536, int(row['num_ctx'] or 8192) * factor)
    else:
        budget['num_predict'] = min(8192, int(row['num_predict'] or 512) * factor)
    return {'level': 'role_screen', 'gateway_models': [row['model']], 'screening_task_ids': [row['task_id']],
            'role_budgets': {row['role']: budget}, 'diagnoses_attempt_id': attempt_id, 'repetitions': 1,
            'note': 'Diagnostic for attempt %s: same model and task, %s raised; the original attempt stays recorded unchanged.' % (attempt_id, ', '.join(budget))}
