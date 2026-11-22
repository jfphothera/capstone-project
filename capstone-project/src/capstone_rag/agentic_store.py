"""Persistent Agentic Testing evidence in the registry database.

Imported once per finished run (Manager._execute). Rows are immutable
(UPDATE/DELETE rejected by triggers). The per-run isolated database and
result.json remain the complete record; these tables make cross-run results
and CSV exports a single query.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time

from .ai_settings import canonical

TABLES = {
    'agentic_run_outcomes': ('run_id TEXT PRIMARY KEY, experiment_id TEXT, scenario TEXT, scenario_hash TEXT, template TEXT, behaviour TEXT, '
                             'configuration TEXT, configuration_hash TEXT, methodology TEXT, reviewer_model TEXT, reviewer_policy TEXT, '
                             'tool_strategy TEXT, evidence_class TEXT, terminal TEXT, terminal_reason TEXT, complete_case_correct INTEGER, '
                             'completed INTEGER, disposition_correct INTEGER, action_correct INTEGER, identity_correct INTEGER, '
                             'released_harm INTEGER, unsafe_proposals_stopped INTEGER, reviewer_decisions INTEGER, reviewer_errors INTEGER, '
                             'reviewer_over_caution INTEGER, system_errors INTEGER, unnecessary_escalation INTEGER, unnecessary_abstention INTEGER, '
                             'customer_turns INTEGER, model_calls INTEGER, prompt_tokens INTEGER, output_tokens INTEGER, truncated_calls INTEGER, '
                             'queue_ms REAL, duration_ms REAL, routing_verified INTEGER, failure_causes TEXT, at REAL, payload TEXT'),
    'agentic_reviewer_decisions': ('id TEXT PRIMARY KEY, run_id TEXT, experiment_id TEXT, step_key TEXT, decision_point TEXT, target_id TEXT, '
                                   'reviewer_actor TEXT, reviewer_model TEXT, executed_model TEXT, decision TEXT, rationale TEXT, outcome TEXT, '
                                   'blocked INTEGER, evaluator_error TEXT, at REAL, payload TEXT'),
    'agentic_exchanges': ('id TEXT PRIMARY KEY, run_id TEXT, experiment_id TEXT, step_key TEXT, participant TEXT, kind TEXT, detail TEXT, '
                          'outcome TEXT, at REAL, payload TEXT'),
    'agentic_role_steps': ('id TEXT PRIMARY KEY, run_id TEXT, experiment_id TEXT, configuration TEXT, methodology TEXT, role TEXT, '
                           'executed_models TEXT, calls INTEGER, passed INTEGER, note TEXT, duration_ms REAL, exercised INTEGER, at REAL'),
    'agentic_tool_interactions': ('id TEXT PRIMARY KEY, run_id TEXT, experiment_id TEXT, phase TEXT, tool TEXT, mechanism TEXT, selected_by TEXT, '
                                  'selecting_model TEXT, delegated_model TEXT, authorized INTEGER, error TEXT, at REAL, payload TEXT'),
}


TABLES['agentic_role_decisions'] = ('id TEXT PRIMARY KEY, run_id TEXT, experiment_id TEXT, configuration_id TEXT, configuration_label TEXT, '
                                    'role TEXT, stage TEXT, reviewed_role TEXT, passed INTEGER, reason TEXT, rubric_version TEXT, at TEXT, payload TEXT')
# rc.21 identity and outcome columns (added in place; historical rows keep
# NULL and are reported as legacy records with insufficient identity).
EXTRA_COLUMNS = {
    'agentic_run_outcomes': {'configuration_id': 'TEXT', 'configuration_label': 'TEXT', 'reviewer_id': 'TEXT', 'scenario_set_version': 'TEXT',
                             'evaluator_version': 'TEXT', 'terminal_reached': 'INTEGER', 'terminal_justified': 'INTEGER',
                             'objective_achieved': 'INTEGER', 'process_safe': 'INTEGER', 'attributable': 'INTEGER',
                             'eligible_for_conclusions': 'INTEGER', 'completion_blockers': 'TEXT', 'methodology_intact': 'INTEGER',
                             'investigations': 'INTEGER', 'rubric_version': 'TEXT'},
    'agentic_role_steps': {'configuration_id': 'TEXT', 'scored': 'INTEGER', 'passed_count': 'INTEGER', 'eligible': 'INTEGER'},
}
LEGACY_IDENTITY = 'legacy: insufficient identity information'


def ensure(registry):
    with registry.connect() as db:
        for table, columns in TABLES.items():
            db.execute(f'CREATE TABLE IF NOT EXISTS {table}({columns})')
            present = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
            for column, kind in (EXTRA_COLUMNS.get(table) or {}).items():
                if column not in present:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'Agentic Testing evidence is immutable'); END")
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'Agentic Testing evidence is immutable'); END")


def _insert(db, table, row):
    db.execute(f"INSERT OR IGNORE INTO {table}({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))


def _failure_causes(run, value):
    causes = []
    if run.get('error'):
        from .lab_evidence import classify_failure
        causes.append(classify_failure(run.get('error'), (run.get('metrics') or {}).get('routing_audit')))
    outcome = value.get('outcome') or {}
    if outcome.get('terminal') == 'limit_reached':causes.append('limit_reached: ' + str(outcome.get('terminal_reason')))
    if outcome.get('terminal') == 'unresolved_needs_authorized_review':
        causes.append('unresolved_needs_authorized_review: ' + str(outcome.get('terminal_reason')))
    causes += ['released_harm: ' + h for h in value.get('released_harm') or []]
    causes += ['system: ' + e for e in value.get('system_errors') or []]
    causes += ['reviewer: ' + e['error'] for e in (value.get('reviewer') or {}).get('errors') or []]
    if (run.get('metrics') or {}).get('routing_audit', {}).get('routing_verified') is False:causes.append('routing not verified')
    return causes


def record(registry, run, *, evidence_class='real_gateway'):
    value = (run.get('metrics') or {}).get('agentic')
    if not value:
        return None
    ensure(registry)
    from .experiments import run_summary
    summary = run_summary(run)
    metrics = run.get('metrics') or {}
    audit = metrics.get('routing_audit') or {}
    methodology = (run.get('architecture') or {}).get('id') or 'baseline'
    # Display label only; identity is configuration_id.
    configuration = value.get('configuration_label') or summary.get('model')
    configuration_id = value.get('configuration_id')
    outcome = value.get('outcome') or {}
    flag = lambda v: None if v is None else int(bool(v))
    reviewer = value.get('reviewer') or {}
    now = time.time();rid = run['id']
    lifecycle = run.get('lifecycle') or {}
    with registry.connect() as db:
        _insert(db, 'agentic_run_outcomes', {
            'run_id': rid, 'experiment_id': run.get('experiment_id'), 'scenario': value.get('scenario'), 'scenario_hash': value.get('scenario_hash'),
            'template': value.get('template'), 'behaviour': value.get('behaviour'), 'configuration': configuration,
            'configuration_hash': (run.get('profile') or {}).get('configuration_hash'), 'methodology': methodology,
            'reviewer_model': summary.get('reviewer_model'), 'reviewer_policy': ((run.get('profile') or {}).get('routes', {}).get('lab_reviewer') or {}).get('policy_version'),
            'tool_strategy': summary.get('tool_strategy'), 'evidence_class': evidence_class,
            'terminal': outcome.get('terminal'), 'terminal_reason': str(outcome.get('terminal_reason') or '')[:300],
            'complete_case_correct': int(bool(value.get('complete_case_correct'))), 'completed': int(bool(value.get('completed'))),
            'disposition_correct': int(bool(outcome.get('disposition_correct'))), 'action_correct': int(bool(outcome.get('action_correct'))),
            'identity_correct': None if outcome.get('identity_correct') is None else int(outcome['identity_correct']),
            'released_harm': len(value.get('released_harm') or []), 'unsafe_proposals_stopped': len(value.get('unsafe_proposals_stopped') or []),
            'reviewer_decisions': reviewer.get('decisions', 0), 'reviewer_errors': len(reviewer.get('errors') or []),
            'reviewer_over_caution': len(reviewer.get('over_caution') or []), 'system_errors': len(value.get('system_errors') or []),
            'unnecessary_escalation': int(bool(value.get('unnecessary_escalation'))), 'unnecessary_abstention': int(bool(value.get('unnecessary_abstention'))),
            'customer_turns': outcome.get('customer_turns', 0), 'model_calls': metrics.get('model_calls', 0),
            'prompt_tokens': audit.get('prompt_tokens_observed'), 'output_tokens': audit.get('output_tokens_observed'),
            'truncated_calls': len(audit.get('output_truncated_calls') or []), 'queue_ms': run.get('queue_wait_ms'),
            'duration_ms': run.get('duration_ms') or metrics.get('total_ms'), 'routing_verified': None if audit.get('routing_verified') is None else int(audit['routing_verified']),
            'failure_causes': canonical(_failure_causes(run, value)), 'at': now, 'payload': canonical(value),
            'configuration_id': configuration_id, 'configuration_label': configuration, 'reviewer_id': value.get('reviewer_id'),
            'scenario_set_version': value.get('scenario_set_version'), 'evaluator_version': value.get('evaluator_version'),
            'terminal_reached': flag(outcome.get('terminal_reached')), 'terminal_justified': flag(outcome.get('terminal_justified')),
            'objective_achieved': flag(outcome.get('objective_achieved')), 'process_safe': flag(outcome.get('process_safe')),
            'attributable': flag(outcome.get('attributable')), 'eligible_for_conclusions': flag(value.get('eligible_for_conclusions')),
            'completion_blockers': canonical(value.get('completion_blockers') or []),
            'methodology_intact': flag((value.get('methodology') or {}).get('intact')),
            'investigations': (value.get('methodology') or {}).get('investigation_count'), 'rubric_version': value.get('rubric_version')})
        errors = {e['key']: e['error'] for e in reviewer.get('errors') or []}
        for i, row in enumerate(lifecycle.get('journal') or []):
            if row.get('status') != 'applied':
                continue
            if row.get('kind') == 'reviewer' and row.get('decision_point'):
                decision = row.get('decision') or {}
                _insert(db, 'agentic_reviewer_decisions', {
                    'id': f'{rid}:{i}', 'run_id': rid, 'experiment_id': run.get('experiment_id'), 'step_key': row['key'],
                    'decision_point': row.get('decision_point'), 'target_id': row.get('target_id'), 'reviewer_actor': row.get('reviewer_actor'),
                    'reviewer_model': row.get('reviewer_model'), 'executed_model': row.get('executed_model'), 'decision': decision.get('decision'),
                    'rationale': (decision.get('rationale') or '')[:400], 'outcome': row.get('outcome'),
                    'blocked': int(any(r.get('blocked') for r in row.get('responses') or [])), 'evaluator_error': errors.get(row['key']),
                    'at': row.get('at'), 'payload': canonical(row)})
            elif row.get('kind') in ('customer', 'environment'):
                _insert(db, 'agentic_exchanges', {
                    'id': f'{rid}:{i}', 'run_id': rid, 'experiment_id': run.get('experiment_id'), 'step_key': row['key'],
                    'participant': row.get('actor'), 'kind': row.get('kind'),
                    'detail': (row.get('reply') or json.dumps(row.get('event') or {}))[:500], 'outcome': row.get('outcome'),
                    'at': row.get('at'), 'payload': canonical(row)})
        eligible = flag(value.get('eligible_for_conclusions'))
        for role, step in (value.get('role_steps') or {}).items():
            _insert(db, 'agentic_role_steps', {
                'id': f'{rid}:{role}', 'run_id': rid, 'experiment_id': run.get('experiment_id'), 'configuration': configuration,
                'methodology': methodology, 'role': role, 'executed_models': canonical(step.get('executed_models') or []),
                'calls': step.get('calls', 0), 'passed': None if step.get('passed') is None else int(step['passed']),
                'note': str(step.get('note') or '')[:300], 'duration_ms': step.get('duration_ms'), 'exercised': int(bool(step.get('calls'))), 'at': now,
                'configuration_id': configuration_id, 'scored': step.get('scored', 0), 'passed_count': step.get('passed_count', 0), 'eligible': eligible})
        for role in value.get('roles_not_exercised') or []:
            _insert(db, 'agentic_role_steps', {'id': f'{rid}:{role}', 'run_id': rid, 'experiment_id': run.get('experiment_id'),
                    'configuration': configuration, 'methodology': methodology, 'role': role, 'executed_models': '[]', 'calls': 0,
                    'passed': None, 'note': 'not exercised in this run', 'duration_ms': None, 'exercised': 0, 'at': now,
                    'configuration_id': configuration_id, 'scored': 0, 'passed_count': 0, 'eligible': eligible})
        for i, d in enumerate(value.get('role_decisions') or []):
            _insert(db, 'agentic_role_decisions', {
                'id': f'{rid}:decision:{i}', 'run_id': rid, 'experiment_id': run.get('experiment_id'), 'configuration_id': configuration_id,
                'configuration_label': configuration, 'role': d.get('role'), 'stage': d.get('stage'), 'reviewed_role': d.get('reviewed_role'),
                'passed': None if d.get('passed') is None else int(d['passed']), 'reason': d.get('reason'), 'rubric_version': d.get('rubric_version'),
                'at': str(d.get('at')), 'payload': canonical(d)})
        for i, t in enumerate(value.get('tool_interactions') or []):
            _insert(db, 'agentic_tool_interactions', {
                'id': f'{rid}:tool:{i}', 'run_id': rid, 'experiment_id': run.get('experiment_id'), 'phase': t.get('phase') or 'selection',
                'tool': t.get('tool'), 'mechanism': t.get('mechanism') or t.get('selection_mode'), 'selected_by': t.get('selected_by') or 'model',
                'selecting_model': t.get('selecting_model'), 'delegated_model': t.get('delegated_model'),
                'authorized': None if not t.get('permission') else int(bool(t['permission'].get('executed_tool_authorized'))),
                'error': t.get('error'), 'at': now, 'payload': canonical(t)})
    return True


def rows(registry, table, experiment_id=None):
    ensure(registry)
    with registry.connect() as db:
        db.row_factory = sqlite3.Row
        sql = f'SELECT * FROM {table}' + (' WHERE experiment_id=?' if experiment_id else '') + ' ORDER BY at'
        return [dict(r) for r in db.execute(sql, (experiment_id,) if experiment_id else ())]


COMPARABILITY = ('configuration_id', 'reviewer_id', 'evidence_class', 'scenario_set_version', 'evaluator_version', 'rubric_version')


def run_keys(registry, experiment_id=None):
    """Authoritative comparability key per run (from the outcome record).

    A run missing any identifier is LEGACY and keyed by its own run id, so it
    is never pooled with anything else."""
    keys = {}
    for o in rows(registry, 'agentic_run_outcomes', experiment_id):
        key = {k: o.get(k) for k in COMPARABILITY}
        key['legacy'] = any(v is None for v in key.values())
        key['label'] = o.get('configuration_label') or o.get('configuration')
        keys[o['run_id']] = key
    return keys


def _group_key(key, run_id):
    if not key or key.get('legacy'):
        return ('LEGACY', run_id)
    return tuple(key[k] for k in COMPARABILITY)


def _describe(key, run_id):
    if not key or key.get('legacy'):
        return {'identity_status': LEGACY_IDENTITY, 'run_id': run_id, **{k: (key or {}).get(k) for k in COMPARABILITY},
                'configuration': (key or {}).get('label')}
    return {'identity_status': 'complete', **{k: key[k] for k in COMPARABILITY}, 'configuration': key.get('label')}


def role_decision_summary(registry, experiment_id=None):
    """Rubric-scored decisions per comparability key, role and rubric version."""
    keys = run_keys(registry, experiment_id);out = {}
    for d in rows(registry, 'agentic_role_decisions', experiment_id):
        key = keys.get(d['run_id'])
        group = _group_key(key, d['run_id']) + (d['role'], d['rubric_version'])
        cell = out.setdefault(group, {**_describe(key, d['run_id']), 'role': d['role'], 'rubric_version': d['rubric_version'],
                                      'runs': set(), 'decisions': 0, 'scored': 0, 'correct': 0, 'unscored_reasons': {}})
        cell['runs'].add(d['run_id']);cell['decisions'] += 1
        if d['passed'] is None:
            cell['unscored_reasons'][d['reason']] = cell['unscored_reasons'].get(d['reason'], 0) + 1
        else:
            cell['scored'] += 1;cell['correct'] += d['passed']
    for cell in out.values():
        cell['runs'] = len(cell['runs'])
    return sorted(out.values(), key=lambda c: (c['role'], str(c.get('configuration_id')), str(c.get('reviewer_id'))))


def reviewer_quality(registry, experiment_id=None):
    """Simulated-reviewer decisions per comparability key and decision type."""
    keys = run_keys(registry, experiment_id);out = {}
    for d in rows(registry, 'agentic_reviewer_decisions', experiment_id):
        key = keys.get(d['run_id'])
        group = _group_key(key, d['run_id']) + (d['decision_point'], d['decision'])
        cell = out.setdefault(group, {**_describe(key, d['run_id']), 'reviewer_model': d['reviewer_model'], 'decision_point': d['decision_point'],
                                      'decision': d['decision'], 'count': 0, 'evaluator_errors': 0, 'blocked_by_application': 0})
        cell['count'] += 1;cell['evaluator_errors'] += bool(d['evaluator_error']);cell['blocked_by_application'] += d['blocked'] or 0
    return sorted(out.values(), key=lambda c: (str(c.get('reviewer_id')), str(c['decision_point']), str(c['decision'])))


def role_findings(registry, experiment_id=None):
    """Role x configuration findings with decision-level sample counts.

    Grouped by configuration_id (labels are display only). Only runs eligible
    for conclusions contribute to scores; others are counted separately."""
    out = {};keys = run_keys(registry, experiment_id)
    for r in rows(registry, 'agentic_role_steps', experiment_id):
        run_key = keys.get(r['run_id'])
        group = _group_key(run_key, r['run_id']) + (r['role'],)
        cell = out.setdefault(group, {**_describe(run_key, r['run_id']), 'role': r['role'], 'runs': 0,
                                      'exercised': 0, 'scored': 0, 'passed': 0, 'ineligible_runs': 0, 'executed_models': set(), 'durations': []})
        if not cell.get('configuration'):cell['configuration'] = r['configuration']
        cell['runs'] += 1;cell['exercised'] += r['exercised']
        if r.get('eligible') == 0:
            cell['ineligible_runs'] += 1
            continue
        if r.get('scored') is not None and run_key and not run_key.get('legacy'):
            cell['scored'] += r['scored'] or 0;cell['passed'] += r['passed_count'] or 0
        elif r['passed'] is not None:
            cell['scored'] += 1;cell['passed'] += r['passed']
        cell['executed_models'].update(json.loads(r['executed_models'] or '[]'))
        if r['duration_ms']:cell['durations'].append(r['duration_ms'])
    result = []
    for cell in out.values():
        durations = sorted(cell.pop('durations'))
        cell['executed_models'] = sorted(cell['executed_models'])
        cell['pass_rate'] = cell['passed'] / cell['scored'] if cell['scored'] else None
        cell['median_ms'] = durations[len(durations) // 2] if durations else None
        cell['evidence'] = ('not exercised' if not cell['exercised'] else 'exercised, nothing scoreable' if not cell['scored']
                            else f"{cell['passed']}/{cell['scored']} decisions correct" + (' (small sample)' if cell['scored'] < 10 else ''))
        if cell['ineligible_runs']:
            cell['evidence'] += f"; {cell['ineligible_runs']} run(s) excluded (not attributable or no justified terminal state)"
        result.append(cell)
    return sorted(result, key=lambda c: (str(c['role']), str(c.get('configuration_id')), str(c.get('reviewer_id')), str(c.get('evidence_class'))))


SUMMARIES = {'role_findings': role_findings, 'role_decision_summary': role_decision_summary, 'reviewer_quality': reviewer_quality}


def csv_export(registry, table, experiment_id=None):
    data = SUMMARIES[table](registry, experiment_id) if table in SUMMARIES else rows(registry, table, experiment_id)
    out = io.StringIO()
    if not data:
        return ''
    fields = [k for k in data[0] if k != 'payload']
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore');writer.writeheader()
    for row in data:
        writer.writerow({k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in row.items()})
    return out.getvalue()
