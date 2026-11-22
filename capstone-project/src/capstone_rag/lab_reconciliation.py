"""Producer-side closure of a stopped Testing Lab worker's assignments (rc.25, A1).

After the coordinator has CONFIRMED that a trial's worker process exited
(normal finish, failure exit, hard timeout kill, or a coordinator restart whose
worker lease has expired), and after the worker's final mirrored events were
imported, this module:

1. reads that trial's own mirrored Agent Arena rows (its run directory only;
   ``workflow_id`` is the trial id), so no neighbouring trial is touched;
2. finds the assignments that trial still owned: model calls
   (``inference.*`` by call id) and tasks (``task.*`` by agent and task id),
   including the simulated reviewer's (``agent_id`` ``lab_reviewer``) and the
   lab participants';
3. writes one ``inference.interrupted`` / ``task.interrupted`` row per
   outstanding assignment with reason, correlated request ids and provenance
   to a durable outbox in the run directory FIRST, then imports it into the
   live operations database idempotently (deterministic event ids,
   ``INSERT OR IGNORE``). A failed import or a coordinator restart leaves the
   outbox pending; ``flush_pending`` retries it;
4. records a later Gateway outcome of an interrupted call as a SEPARATE fact
   (``inference.late_result``), linked to the interruption, without resending,
   cancelling or freeing anything, and without changing the trial result.

Worker interruption is a caller fact; the downstream inference outcome is a
Gateway fact. Neither rewrites the other. Arena's existing lease-expiry and
abandonment fallback remains in charge of anything not confirmed here.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

RECONCILIATION_VERSION = 'capstone.lab-reconciliation/1'
OUTBOX = 'reconciliation-outbox.jsonl'
DONE = 'reconciliation-outbox.done.json'
WATCH = 'late-result-watch.json'
# Final states, including the execution engine's blocked / escalated finals
# (execution.py) - those must never receive a false "interrupted" row.
TERMINAL = {'inference.completed', 'inference.failed', 'inference.interrupted',
            'task.completed', 'task.failed', 'task.interrupted', 'task.cancelled', 'task.canceled', 'task.late_result_rejected',
            'task.blocked', 'task.escalated', 'human_review.resolved'}
IMPORT_ATTEMPTS = 3            # bounded: a busy live database leaves the outbox pending
IMPORT_BUSY_SECONDS = 2
LATE_CHECK_OFFSETS = (30, 120, 600, 1800)        # seconds after the confirmed exit
# rc.25 lab-start fix: no tree-wide scans of the experiment history. Run folders
# with a pending outbox or an active watch are indexed by marker files under
# <experiments>/.reconciliation-index/{outbox,watch}/; folders written before
# the index existed are found among RECENT experiments only.
INDEX_DIR = '.reconciliation-index'
RECENT_SECONDS = 7 * 86400
EXIT_KINDS = ('normal_finish', 'worker_failure_exit', 'timeout_kill', 'coordinator_restart_lease_expired')
_locks = {}
_locks_guard = threading.Lock()


def _lock_for(run_dir):
    """One lock per run directory, held only around its outbox files."""
    key = str(Path(run_dir).resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def mirror_rows(run_dir):
    rows, seen = [], set()
    for path in sorted(Path(run_dir).glob('arena-mirror*.jsonl')):
        for line in path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get('event_id') in seen:
                continue
            seen.add(row.get('event_id'));rows.append(row)
    rows.sort(key=lambda r: (r.get('timestamp') or '', r.get('event_id') or ''))
    return rows


def _key(row):
    kind = row.get('event_type') or ''
    # A model call is keyed by its call id. The execution engine also writes
    # ``inference.queued`` for a TASK waiting on inference under the task's
    # correlation id: that row belongs to the task, not to a call.
    if kind.startswith('inference.') and row.get('correlation_id') and (str(row['correlation_id']).startswith('CALL-') or not row.get('task_id')):
        return ('inference', row['correlation_id'])
    if kind.startswith('inference.') and row.get('task_id'):
        return ('task', row.get('agent_id') or '', row['task_id'])
    if (kind.startswith('task.') or kind.startswith('human_review.')) and row.get('task_id'):
        # queued, waiting and running tasks and open review requests alike
        return ('task', row.get('agent_id') or '', row['task_id'])
    return None


def outstanding(rows, trial_id):
    """Assignments of THIS trial whose last event is not terminal."""
    last, first = {}, {}
    for row in rows:
        if row.get('workflow_id') not in (trial_id, None):
            continue                       # never another trial's events
        key = _key(row)
        if not key:
            continue
        first.setdefault(key, row);last[key] = row
    result = []
    for key, row in last.items():
        if row['event_type'] in TERMINAL:
            continue
        detail = row.get('detail')
        detail = json.loads(detail) if isinstance(detail, str) else (detail or {})
        result.append({'key': key, 'kind': key[0], 'last': row, 'first': first[key], 'detail': detail,
                       'gateway_request_id': detail.get('gateway_request_id'), 'request_id': detail.get('request_id') or row.get('correlation_id')})
    return result


def _event_id(trial_id, attempt_id, key, suffix='interrupted'):
    return 'ARENA-RECON-' + hashlib.sha256('|'.join([trial_id, attempt_id or '', suffix] + list(key)).encode()).hexdigest()[:24]


def closure_rows(run, attempt_id, exit_info, items):
    from .coordination import timestamp
    rows = []
    for item in items:
        last = item['last']
        event_type = 'inference.interrupted' if item['kind'] == 'inference' else 'task.interrupted'
        reconciliation = {
            'version': RECONCILIATION_VERSION, 'reason': exit_info['reason'], 'exit_kind': exit_info['kind'],
            'worker_exit_confirmed_at': exit_info['confirmed_at'], 'worker_exit_basis': exit_info.get('basis'),
            'experiment_id': run.get('experiment_id'), 'trial_id': run['id'], 'attempt_id': attempt_id,
            'original_event_id': last.get('event_id'), 'original_event_type': last.get('event_type'),
            'original_task_status': last.get('task_status'), 'assignment_started_at': item['first'].get('timestamp'),
            'request_id': item['request_id'], 'gateway_request_id': item['gateway_request_id'],
            'logical_agent_id': last.get('agent_id'), 'participant': item['detail'].get('participant'),
            'provenance': 'capstone experiment coordinator: confirmed worker exit, final mirror imported, trial-owned assignment closed',
            'inference_outcome': 'not_observed_by_caller' if item['kind'] == 'inference' else None,
            'gateway_execution_lease': 'unchanged (Gateway uncertain-execution contract applies; nothing resent or cancelled)'}
        detail = {'event_contract': 'capstone.activity/1.1', 'reconciliation': reconciliation, 'caller_interrupted': True,
                  'system_under_test': item['detail'].get('system_under_test'), 'route_role': item['detail'].get('route_role')}
        rows.append({'event_id': _event_id(run['id'], attempt_id, item['key']), 'schema_version': '1.0', 'timestamp': timestamp(),
                     'event_type': event_type, 'case_id': last.get('case_id'), 'workflow_id': run['id'], 'task_id': last.get('task_id'),
                     'agent_id': last.get('agent_id'), 'sender_agent_id': None, 'receiver_agent_id': None,
                     'correlation_id': last.get('correlation_id'), 'logical_home_node': last.get('logical_home_node'),
                     'execution_node': last.get('execution_node'), 'tool_node': last.get('tool_node'),
                     'inference_node': last.get('inference_node'), 'model_id': last.get('model_id'), 'endpoint_id': last.get('endpoint_id'),
                     'task_status': 'interrupted',
                     'summary': ('EXPERIMENT Assignment interrupted: worker ' + exit_info['kind'].replace('_', ' '))[:240],
                     'evidence_refs': json.dumps([x for x in (last.get('event_id'), item['gateway_request_id']) if x]),
                     'detail': json.dumps(detail, sort_keys=True, default=str)})
    return rows


def _atomic_write(path, text):
    tmp = Path(str(path) + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as stream:
        stream.write(text);stream.flush();os.fsync(stream.fileno())
    os.replace(tmp, path)


def reconcile(run, attempt_id, exit_info, live_database, *, write=None):
    """Close the trial's outstanding assignments. Returns a summary.

    ``write`` (callable taking a zero-argument task) routes the live import
    through the coordinator's single writer; default imports inline."""
    run_dir = Path(run['directory'])
    # Closures already in the outbox (earlier attempts) are terminal facts too.
    rows = mirror_rows(run_dir) + [r for r in _read_outbox(run_dir) if r.get('event_type', '').endswith('.interrupted')]
    rows.sort(key=lambda r: (r.get('timestamp') or '', r.get('event_id') or ''))
    items = outstanding(rows, run['id'])
    closures = closure_rows(run, attempt_id, exit_info, items)
    with _lock_for(run_dir):
        existing = _read_outbox(run_dir)
        merged = {r['event_id']: r for r in existing + closures}
        _atomic_write(run_dir / OUTBOX, ''.join(json.dumps(r, sort_keys=True) + '\n' for r in merged.values()))
        if closures:
            mark(run_dir, 'outbox')
    watch = [{'event_id': r['event_id'], 'gateway_request_id': json.loads(r['detail'])['reconciliation']['gateway_request_id'],
              'call_id': r['correlation_id'], 'case_id': r['case_id'], 'agent_id': r['agent_id'], 'task_id': r['task_id'],
              'model_id': r['model_id'], 'exit_at': exit_info['confirmed_at']}
             for r in closures if r['event_type'] == 'inference.interrupted' and json.loads(r['detail'])['reconciliation']['gateway_request_id']]
    if watch:
        _merge_watch(run_dir, watch)
    task = lambda: flush(run_dir, live_database)
    (write or (lambda f: f()))(task)
    return {'version': RECONCILIATION_VERSION, 'exit': exit_info, 'outstanding': len(items), 'closed': len(closures),
            'late_result_watch': len(watch), 'event_ids': [r['event_id'] for r in closures]}


def _read_outbox(run_dir):
    path = Path(run_dir) / OUTBOX
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def import_bounded(rows, database):
    """INSERT OR IGNORE with a short bounded retry; raises when still busy
    (the caller leaves the outbox pending for the next flush)."""
    import sqlite3
    groups = {}
    for row in rows:
        groups.setdefault(tuple(row), []).append(row)
    last = None
    for attempt in range(IMPORT_ATTEMPTS):
        try:
            with sqlite3.connect(database, timeout=IMPORT_BUSY_SECONDS) as db:
                db.execute(f'PRAGMA busy_timeout={IMPORT_BUSY_SECONDS * 1000}')
                for columns, items in groups.items():
                    sql = 'INSERT OR IGNORE INTO arena_events (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
                    db.executemany(sql, [[item[key] for key in columns] for item in items])
            return len(rows)
        except sqlite3.OperationalError as exc:
            last = exc
            if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                raise
            time.sleep(.2 * (attempt + 1))
    raise last


def flush(run_dir, live_database):
    """Import pending outbox rows (idempotent). Pending on failure. The run's
    lock covers only its outbox files, never the database import, so a busy
    live database never blocks another trial or the coordinator."""
    run_dir = Path(run_dir)
    with _lock_for(run_dir):
        rows = _read_outbox(run_dir)
        done = json.loads((run_dir / DONE).read_text(encoding='utf-8')) if (run_dir / DONE).exists() else {'imported': []}
        pending = [r for r in rows if r['event_id'] not in set(done['imported'])]
    if not pending:
        return 0
    import_bounded(pending, live_database)
    with _lock_for(run_dir):
        done = json.loads((run_dir / DONE).read_text(encoding='utf-8')) if (run_dir / DONE).exists() else {'imported': []}
        done['imported'] = sorted(set(done['imported']) | {r['event_id'] for r in pending});done['at'] = time.time()
        _atomic_write(run_dir / DONE, json.dumps(done))
        if {r['event_id'] for r in _read_outbox(run_dir)} <= set(done['imported']):
            unmark(run_dir, 'outbox')
    return len(pending)


def _marker(run_dir, kind):
    run_dir = Path(run_dir)
    return run_dir.parent.parent / INDEX_DIR / kind / f'{run_dir.parent.name}__{run_dir.name}'


def mark(run_dir, kind):
    path = _marker(run_dir, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(Path(run_dir).resolve()), encoding='utf-8')


def unmark(run_dir, kind):
    try:
        _marker(run_dir, kind).unlink()
    except FileNotFoundError:
        pass


def recent_run_dirs(experiment_root, *, now=None):
    """Run folders of experiments touched in the last RECENT_SECONDS only (one
    listing of the experiment folders; old campaigns' trials are never walked)."""
    now = now or time.time();root = Path(experiment_root)
    if not root.exists():
        return
    for experiment in root.iterdir():
        if not experiment.name.startswith('EXP-'):
            continue
        try:
            if now - experiment.stat().st_mtime > RECENT_SECONDS:
                continue
        except OSError:
            continue
        for run_dir in experiment.iterdir():
            if run_dir.name.startswith('RUN-'):
                yield run_dir


def candidate_run_dirs(experiment_root, kind, filename, *, include_recent=True):
    """Indexed run folders for ``kind`` plus recent folders that hold ``filename``."""
    found = {}
    folder = Path(experiment_root) / INDEX_DIR / kind
    if folder.exists():
        for marker in folder.iterdir():
            try:
                path = Path(marker.read_text(encoding='utf-8').strip())
            except OSError:
                continue
            found[str(path)] = path
    if include_recent:
        for run_dir in recent_run_dirs(experiment_root):
            if (run_dir / filename).exists():
                found.setdefault(str(run_dir.resolve()), run_dir)
    return list(found.values())


def flush_pending(experiment_root, live_database_for):
    """Coordinator start-up: import every outbox left pending by a restart or
    an unavailable live database. ``live_database_for(run_dir)`` -> path or None.
    Bounded: indexed and recent run folders only, never the whole history."""
    count = 0
    for run_dir in candidate_run_dirs(experiment_root, 'outbox', OUTBOX):
        outbox = Path(run_dir) / OUTBOX
        if not outbox.exists():
            unmark(run_dir, 'outbox');continue
        database = live_database_for(outbox.parent)
        if database:
            try:
                count += flush(outbox.parent, database)
            except Exception as exc:  # stays pending; never hidden
                print(json.dumps({'component': 'testing_lab.reconciliation', 'run_dir': str(outbox.parent), 'error': str(exc)}), flush=True)
    return count


def _merge_watch(run_dir, entries):
    path = Path(run_dir) / WATCH
    with _lock_for(run_dir):
        current = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        known = {e['event_id'] for e in current}
        current += [{**e, 'checks': [], 'state': 'watching'} for e in entries if e['event_id'] not in known]
        _atomic_write(path, json.dumps(current, indent=1))
        if any(e['state'] == 'watching' for e in current):
            mark(run_dir, 'watch')


def late_result_rows(run, entry, status):
    """A separate, linked fact for a Gateway outcome after the caller stopped."""
    from .coordination import timestamp
    request = (status or {}).get('request') or status or {}
    gateway_status = request.get('status') or (status or {}).get('status') or 'unknown'
    observed = gateway_status in ('completed', 'failed', 'cancelled', 'expired', 'unknown_execution')
    detail = {'event_contract': 'capstone.activity/1.1',
              'late_result': {'version': RECONCILIATION_VERSION, 'gateway_request_id': entry['gateway_request_id'], 'request_id': entry['call_id'],
                              'gateway_status': gateway_status, 'gateway_finished_at': request.get('finished_at') or request.get('completed_at'),
                              'resource_id': request.get('resource_id'), 'linked_interruption_event_id': entry['event_id'],
                              'caller_state': 'interrupted', 'trial_outcome_unaffected': True,
                              'provenance': 'Gateway request status read after the confirmed worker exit; nothing resent or cancelled'}}
    return {'event_id': _event_id(run['id'], entry['gateway_request_id'], ('late', entry['event_id']), 'late'), 'schema_version': '1.0',
            'timestamp': timestamp(), 'event_type': 'inference.late_result', 'case_id': entry.get('case_id'), 'workflow_id': run['id'],
            'task_id': entry.get('task_id'), 'agent_id': entry.get('agent_id'), 'sender_agent_id': None, 'receiver_agent_id': None,
            'correlation_id': entry['call_id'], 'logical_home_node': None, 'execution_node': None, 'tool_node': None,
            'inference_node': request.get('resource_id'), 'model_id': entry.get('model_id'), 'endpoint_id': request.get('resource_id'),
            'task_status': gateway_status if observed else 'unobserved',
            'summary': f'EXPERIMENT Gateway outcome after the caller stopped: {gateway_status}'[:240],
            'evidence_refs': json.dumps([entry['event_id'], entry['gateway_request_id']]),
            'detail': json.dumps(detail, sort_keys=True, default=str)}, observed


def check_late_results(run, live_database, status_reader, *, now=None, final=False):
    """One pass over a run's watch list. ``status_reader(gateway_request_id)``
    returns the Gateway request status document (read-only)."""
    run_dir = Path(run['directory']);path = run_dir / WATCH
    if not path.exists():
        return 0
    now = now or time.time();written = 0
    with _lock_for(run_dir):
        entries = json.loads(path.read_text(encoding='utf-8'))
    out = []
    for entry in entries:
        if entry['state'] != 'watching':
            continue
        due = [o for o in LATE_CHECK_OFFSETS if now >= entry['exit_at'] + o and o not in entry['checks']]
        if not due and not final:
            continue
        try:
            status = status_reader(entry['gateway_request_id'])
        except Exception as exc:
            status = {'status': 'status_unavailable', 'error': str(exc)[:200]}
        entry['checks'] = sorted(set(entry['checks']) | set(due))
        row, observed = late_result_rows(run, entry, status)
        if observed or final or len(entry['checks']) == len(LATE_CHECK_OFFSETS):
            out.append(row);entry['state'] = 'recorded' if observed else 'unobserved_final'
    with _lock_for(run_dir):
        _atomic_write(path, json.dumps(entries, indent=1))
        if not any(e['state'] == 'watching' for e in entries):
            unmark(run_dir, 'watch')
        if out:
            existing = _read_outbox(run_dir)
            merged = {r['event_id']: r for r in existing + out}
            _atomic_write(run_dir / OUTBOX, ''.join(json.dumps(r, sort_keys=True) + '\n' for r in merged.values()))
            mark(run_dir, 'outbox')
    if out:
        written = flush(run_dir, live_database)
    return written


def lease_expired(run_dir):
    """A worker is confirmed gone only when its renewable lease has expired."""
    import sqlite3
    path = Path(run_dir) / 'worker-lease.db'
    if not path.exists():
        return True
    with sqlite3.connect(path, timeout=5) as db:
        row = db.execute('SELECT expires FROM lease WHERE id=1').fetchone()
    return not row or row[0] <= time.time()


def import_mirror(run_dir, live_database):
    """Restart path: import the worker's remaining mirrored rows BEFORE any
    closure is computed (idempotent on event id; bounded)."""
    rows = mirror_rows(run_dir)
    if rows:
        import_bounded(rows, live_database)
    return len(rows)
