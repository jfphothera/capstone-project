"""rc.16 Testing Lab engine: fast results, low contention, local services."""
import json
import sqlite3
import time
from types import SimpleNamespace

from capstone_rag import experiments, remote_services, telemetry
from capstone_rag.ai_settings import Registry, canonical
from capstone_rag.store import Store
from capstone_rag.webapp.routers import lab


def _run(eid, number, status, *, heavy=True):
    run = {
        'id': f'RUN-{number:04d}', 'experiment_id': eid, 'status': status,
        'directory': f'Z:/does-not-exist/RUN-{number:04d}',  # results must never open run files
        'scenario': {'id': 'fuzzy-identity' if number % 2 else 'qms-technical'},
        'profile': {'routes': {'investigation': {'model': 'qwen3.5:4b'}}},
        'architecture': {'id': f'method-{"ABCDEFGH"[number % 8]}{number % 4 + 1}', 'methodology': {}},
        'approval_mode': 'automated', 'queued_at': 1000 + number,
    }
    if status in ('completed', 'failed', 'paused'):
        run.update(attempt_started_at=2000 + number, duration_ms=12000 + number,
            metrics={'model_calls': 7, 'all_scored_checks_passed': number % 3 == 0, 'critical_violations': [],
                'case_disposition': 'AI Reviewed', 'communication_metrics': {'agent_handoffs': 2},
                'inference_correlations': [{'resource_id': 'head-192-0-2-3', 'gateway_request_id': f'REQ-{number}'}]},
            # Realistic bulk that the Results screen must not have to parse.
            snapshots=[{'blob': 'x' * 20000}], review_actions=[{'blob': 'y' * 20000}])
        if status == 'failed':
            run['error'] = 'Gateway execution failed: timed out'
        if status == 'paused':
            run['pending'] = {'emails': [{'message_id': 'M1', 'body': 'Draft', 'metadata': '{}'}]}
    return run


def _manager(tmp_path):
    registry = Registry(tmp_path / 'settings.db')
    manager = experiments.Manager(registry)
    return registry, manager


def test_results_reads_only_summaries_and_stays_fast(tmp_path, monkeypatch):
    registry, manager = _manager(tmp_path)
    eid = 'EXP-FAST'
    manager.save({'id': eid, 'name': 'Agentic Evaluation', 'status': 'running', 'created_at': time.time(),
                  'concurrency': 20, 'planned_runs': 300, 'failure_limit': 300, 'approval_mode': 'automated'})
    statuses = ['completed'] * 200 + ['failed'] * 20 + ['paused'] * 5 + ['running'] * 20 + ['queued'] * 55
    for number, status in enumerate(statuses):
        manager._store_run(_run(eid, number, status))
    monkeypatch.setattr(lab, 'get_runtime', lambda: SimpleNamespace(experiments=manager))

    started = time.perf_counter()
    data = lab.results(eid, limit=100)
    elapsed = time.perf_counter() - started

    exp = data['experiments'][0]
    assert elapsed < 1.0, f'results took {elapsed:.2f}s'
    assert (exp['completed_runs'], exp['failed_runs'], exp['paused_runs'], exp['active_runs'], exp['queued_runs']) == (200, 20, 5, 20, 55)
    assert exp['runs_total'] == 300 and len(exp['runs']) == 100
    assert [run['status'] for run in exp['runs'][:25]] == ['running'] * 20 + ['paused'] * 5
    assert exp['runs'][20]['pending']['emails'][0]['message_id'] == 'M1'
    assert exp['error_groups'] == [{'classification': 'gateway_timeout', 'count': 20, 'example': 'Gateway execution failed: timed out'}]
    assert sum(row['runs'] for row in exp['comparisons']) == 300
    assert all('snapshots' not in run and 'review_actions' not in run for run in exp['runs'])
    assert len(json.dumps(data)) < 200_000


def test_stopped_campaign_reports_queued_runs_as_not_run(tmp_path, monkeypatch):
    registry, manager = _manager(tmp_path)
    manager.save({'id': 'EXP-STOP', 'status': 'incomplete', 'created_at': 1, 'failure_limit': 3})
    for number, status in enumerate(['failed'] * 3 + ['queued'] * 4):
        manager._store_run(_run('EXP-STOP', number, status))
    monkeypatch.setattr(lab, 'get_runtime', lambda: SimpleNamespace(experiments=manager))
    exp = lab.results('EXP-STOP')['experiments'][0]
    assert exp['termination_reason'] == 'failure_limit_reached'
    assert exp['not_run_runs'] == 4 and exp['queued_runs'] == 0
    assert sum(run['status'] == 'not_run' for run in exp['runs']) == 4


def test_summaries_backfill_runs_stored_before_rc16(tmp_path):
    registry, manager = _manager(tmp_path)
    legacy = _run('EXP-OLD', 1, 'completed')
    with registry.connect() as db:
        db.execute('INSERT INTO experiment_runs VALUES (?,?,?)', (legacy['id'], 'EXP-OLD', canonical(legacy)))
    rows = manager.summaries('EXP-OLD')
    assert [row['id'] for row in rows] == ['RUN-0001'] and rows[0]['model_calls'] == 7
    with registry.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM experiment_run_summaries').fetchone()[0] == 1


def test_registry_open_is_write_free_after_first_migration_and_uses_wal(tmp_path):
    path = tmp_path / 'settings.db'
    Registry(path)
    registry = Registry(path)
    with registry.connect() as db:
        registry._migrate(db)
        assert db.total_changes == 0
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert registry.get('profiles', 'gateway-default')['id'] == 'gateway-default'
    assert registry.get('experiments', 'missing') is None


def test_worker_mirror_file_is_imported_once_into_the_live_database(tmp_path):
    live = Store(str(tmp_path / 'live.db'))
    isolated = Store(str(tmp_path / 'run.db'))
    mirror = tmp_path / 'arena-mirror.jsonl'
    isolated.arena_mirror_file = str(mirror)
    isolated.arena_experiment_run_id = 'RUN-MIRROR'
    for number in range(3):
        telemetry.emit(isolated, 'tool.completed', workflow_id='WF', summary=f'step {number}')
    assert live.one("SELECT COUNT(*) AS n FROM arena_events WHERE workflow_id='RUN-MIRROR'")['n'] == 0
    assert telemetry.import_mirror(mirror, live.path) == 3
    rows = live.query("SELECT summary FROM arena_events WHERE workflow_id='RUN-MIRROR' ORDER BY sequence")
    assert [row['summary'] for row in rows] == ['EXPERIMENT step 0', 'EXPERIMENT step 1', 'EXPERIMENT step 2']
    assert not mirror.exists() and telemetry.import_mirror(mirror, live.path) == 0
    live.close(); isolated.close()


def test_snapshot_drops_only_mirrored_experiment_events(tmp_path):
    live = Store(str(tmp_path / 'live.db'))
    telemetry.emit(live, 'case.created', summary='real operational event')
    other = Store(str(tmp_path / 'other.db'))
    other.arena_mirror_file = str(tmp_path / 'm.jsonl'); other.arena_experiment_run_id = 'RUN-X'
    telemetry.emit(other, 'tool.completed', summary='trial event')
    telemetry.import_mirror(tmp_path / 'm.jsonl', live.path)
    live.close(); other.close()
    snapshot = tmp_path / 'snapshot.db'
    with sqlite3.connect(tmp_path / 'live.db') as source, sqlite3.connect(snapshot) as target:
        source.backup(target)
    assert experiments.prune_snapshot(snapshot) == {'experiment_mirror_events': 1}
    with sqlite3.connect(snapshot) as db:
        summaries = [row[0] for row in db.execute('SELECT summary FROM arena_events')]
    assert 'real operational event' in summaries and not any(s.startswith('EXPERIMENT ') for s in summaries)


def test_business_services_default_to_local_even_with_config_files(monkeypatch, tmp_path):
    monkeypatch.setenv('CAPSTONE_QMS_SERVICE_CONFIG', str(tmp_path / 'qms.json'))
    monkeypatch.setenv('CAPSTONE_ERP_SERVICE_CONFIG', str(tmp_path / 'erp.json'))
    monkeypatch.delenv('CAPSTONE_BUSINESS_SERVICES', raising=False)
    assert remote_services.mode() == 'local'
    assert not remote_services.configured('qms') and not remote_services.configured('erp')
    monkeypatch.setenv('CAPSTONE_BUSINESS_SERVICES', 'remote')
    assert remote_services.configured('qms') and remote_services.configured('erp')
