"""rc.25 lab-start regression: /api/lab/start must not grow with lab history.

On the lab laptop after EXP-f253 (640 agentic trials mirrored into the live
operations.db), Test-CapstoneLabHeads.ps1 timed out on every head: launch()
backed up the WHOLE live database and then DELETE + VACUUM-ed the mirrored
trial events, a cost that grows with every campaign ever run (measured here:
5.2 s at 300k mirrored rows, 9.2 s at 600k, ~75 % VACUUM; the laptop is
slower). ``experiments.snapshot_database`` copies only operational rows.
"""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from capstone_rag import config, experiments
from capstone_rag.store import Store

MIRROR_ROWS = 100_000
HISTORY_RUNS = 3_000
LIMIT_SECONDS = 5          # the head check aborts a request after 30 s


def _mirror(db, n, start=0):
    cols = [c[1] for c in db.execute('PRAGMA table_info(arena_events)') if c[1] != 'sequence']
    detail = json.dumps({'request_id': 'CALL-x', 'route_role': 'lab_reviewer', 'note': 'x' * 600})
    base = {'schema_version': '1.0', 'timestamp': '2026-11-18T20:00:00+00:00', 'event_type': 'inference.completed', 'case_id': 'CASE-1',
            'workflow_id': 'RUN-old', 'task_id': 'draft:1', 'agent_id': 'lab_reviewer', 'correlation_id': 'CALL-x', 'logical_home_node': 'coordinator',
            'execution_node': 'coordinator', 'inference_node': 'head-192-0-2-3', 'model_id': 'qwen3:4b', 'endpoint_id': 'head-192-0-2-3',
            'task_status': 'completed', 'summary': 'EXPERIMENT RUN-old qwen3:4b', 'evidence_refs': '[]', 'detail': detail}
    rows = ([{**base, 'event_id': f'ARENA-OLD-{i}'}.get(c) for c in cols] for i in range(start, start + n))
    db.executemany(f'INSERT INTO arena_events ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})', rows)


def _live(tmp_path, mirrored):
    path = tmp_path / 'live.db'
    with sqlite3.connect(config.DB_PATH) as a, sqlite3.connect(path) as b:
        a.backup(b)
    with sqlite3.connect(path) as db:
        _mirror(db, mirrored)
    return path


def test_snapshot_is_identical_to_backup_and_prune(tmp_path):
    live = _live(tmp_path, 50)
    with sqlite3.connect(live) as db:
        cols = [c[1] for c in db.execute('PRAGMA table_info(arena_events)') if c[1] != 'sequence']
        db.execute(f'INSERT INTO arena_events ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                   [{'event_id': 'OP-1', 'event_type': 'case.created', 'summary': 'real operational event', 'schema_version': '1.0',
                     'timestamp': 't', 'evidence_refs': '[]', 'detail': '{}'}.get(c) for c in cols])
        db.execute(f'INSERT INTO arena_events ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                   [{'event_id': 'OP-2', 'event_type': 'case.created', 'summary': 'experiment note typed by a person', 'schema_version': '1.0',
                     'timestamp': 't', 'evidence_refs': '[]', 'detail': '{}'}.get(c) for c in cols])
    old, new = tmp_path / 'old.db', tmp_path / 'new.db'
    with closing(sqlite3.connect(live)) as a, closing(sqlite3.connect(old)) as b:
        a.backup(b)
    assert experiments.prune_snapshot(old) == {'experiment_mirror_events': 51}
    assert experiments.snapshot_database(live, new) == {'experiment_mirror_events': 51}

    def content(path):
        with closing(sqlite3.connect(path)) as db:
            value = {'schema': sorted(db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex%'")),
                     'user_version': db.execute('PRAGMA user_version').fetchone()[0],
                     'integrity': db.execute('PRAGMA integrity_check').fetchone()[0]}
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                value[name] = list(db.execute(f'SELECT * FROM "{name}" ORDER BY rowid'))
            return value
    a, b = content(old), content(new)
    assert a == b and b['integrity'] == 'ok'
    events = {r[0] for r in sqlite3.connect(new).execute('SELECT event_id FROM arena_events')}
    # Same LIKE rule as prune_snapshot (case-insensitive): both drop OP-2; operational OP-1 kept.
    assert 'OP-1' in events and not any(e.startswith('ARENA-OLD-') for e in events)
    with closing(sqlite3.connect(new)) as db:                                                   # triggers were recreated
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0] == \
            sqlite3.connect(live).execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]


class _Gateway:
    def models(self):
        resources = []
        for n in range(2, 11):
            details = {'qwen3.5:4b': {'qualified_operations': ['completion'], 'execution_policy': {'max_context': 8192}, 'qualified_capabilities': []},
                       'all-minilm:latest': {'qualified_operations': ['embedding'], 'execution_policy': {}, 'qualified_capabilities': []}}
            resources.append({'resource_id': f'head-192-0-2-{n}', 'status': 'healthy', 'models': list(details), 'model_details': details})
        return {'contract': 'shared.inference/1', 'resources': resources}
    def policy(self): return {'application_id': 'capstone-research', 'allowed_models': ['*']}
    def availability(self, model, operation='/api/generate', **k): return {'status': 'available', 'resources': {}}
    def request(self, path, **k): raise RuntimeError('404')
    def status(self, rid): return {}


LARGE_MIRROR_ROWS = 450_000     # the laptop's live DB after EXP-f253: 449,898 mirrored rows


def _lab_app(tmp_path, monkeypatch, mirrored, *, campaign=None):
    from fastapi.testclient import TestClient
    from capstone_rag.webapp.app import create_app
    from capstone_rag.webapp.routers import lab
    data = tmp_path / 'data'; data.mkdir()
    monkeypatch.setattr(config, 'DATA_DIR', str(data))
    monkeypatch.setenv('CAPSTONE_AI_SETTINGS', str(tmp_path / 'ai_configuration.db'))
    monkeypatch.setenv('CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG', str(tmp_path / 'research.json'))
    monkeypatch.setenv('CAPSTONE_LAB_HEADS_FILE', str(Path(__file__).parent / 'fixtures' / 'lab-heads-2026-11-08.json'))
    monkeypatch.setattr(lab.gateway_transport, 'client', lambda workload='operational': _Gateway())
    monkeypatch.setattr(lab.gateway_transport, 'enabled', lambda: True)
    dispatched = []
    monkeypatch.setattr(experiments.Manager, '_campaign', campaign or (lambda self, eid: dispatched.append((eid, len(self.rows(eid))))))
    lab._cache.update(at=0, value=None, building=False, inflight=False, error=None, generation=0)
    live = _live(tmp_path, mirrored)
    return TestClient(create_app(store=Store(str(live)), start_workers=False)), data, dispatched


def _history(reg, data, runs=HISTORY_RUNS):
    with reg.connect() as db:
        for e in range(runs // 200):
            eid = f'EXP-hist{e:03d}'; ed = data / 'experiments' / eid
            db.execute('INSERT INTO experiments VALUES (?,?)', (eid, json.dumps({'id': eid, 'status': 'completed', 'created_at': e, 'directory': str(ed)})))
            for r in range(200):
                rid = f'RUN-h{e:03d}{r:04d}'; rd = ed / rid; rd.mkdir(parents=True)
                (rd / 'manifest.json').write_text('{}', encoding='utf-8')
                db.execute('INSERT INTO experiment_runs VALUES (?,?,?)', (rid, eid, json.dumps({'id': rid, 'experiment_id': eid, 'status': 'completed',
                                                                                                'directory': str(rd), 'duration_ms': 1000.0})))


def _ready_head(client):
    state = client.get('/api/lab/state?refresh=true').json()
    for _ in range(200):
        if state.get('inventory_status') == 'ready':
            break
        time.sleep(.05); state = client.get('/api/lab/state').json()
    return state['heads'][0]['resource_id']


def SMOKE(head):
    return {'experiment': 'smoke', 'models': ['qwen3.5:4b'], 'heads': [head], 'repetitions': 1}


def test_start_returns_at_once_with_a_450k_row_live_database(tmp_path, monkeypatch):
    def no_full_prune(path):
        raise AssertionError('launch must not back up the whole live database and VACUUM it (cost grows with lab history)')
    monkeypatch.setattr(experiments, 'prune_snapshot', no_full_prune)
    client, data, dispatched = _lab_app(tmp_path, monkeypatch, LARGE_MIRROR_ROWS)
    with client:
        from capstone_rag.webapp.runtime import get_runtime
        manager = get_runtime().experiments
        _history(manager.registry, data)
        head = _ready_head(client)
        timings = {}

        def timed(name, call):
            t = time.perf_counter(); response = call(); timings[name] = time.perf_counter() - t
            assert response.status_code == 200, response.text[:300]
            return response.json()
        started = timed('start', lambda: client.post('/api/lab/start', json=SMOKE(head)))
        assert started['status'] == 'preparing' and started['snapshot_sha256'] is None
        timed('results_new', lambda: client.get(f"/api/lab/results?experiment_id={started['id']}"))
        timed('results_history', lambda: client.get('/api/lab/results?experiment_id=EXP-hist000'))
        timed('state', lambda: client.get('/api/lab/state'))
        assert timings['start'] < 2 and all(seconds < LIMIT_SECONDS for seconds in timings.values()), timings
        manager.threads[started['id']].join(120)
        experiment = manager.registry.get('experiments', started['id'])
        assert experiment['status'] == 'running' and experiment['preparation']['state'] == 'ready'
        manifest = json.loads((data / 'experiments' / started['id'] / 'manifest.json').read_text(encoding='utf-8'))
        assert manifest['snapshot_pruned'] == {'experiment_mirror_events': LARGE_MIRROR_ROWS}
        assert experiment['snapshot_sha256'] == manifest['source_snapshot_sha256']
        with closing(sqlite3.connect(manifest['source_snapshot'])) as db:
            assert db.execute("SELECT COUNT(*) FROM arena_events WHERE summary LIKE 'EXPERIMENT %'").fetchone()[0] == 0
        runs = manager.rows(started['id'])
        assert len(runs) == 1 and runs[0]['source_snapshot_sha256'] == manifest['source_snapshot_sha256']
        assert dispatched == [(started['id'], 1)]          # dispatch only after the snapshot and trial rows exist


def test_no_trial_exists_or_dispatches_before_its_snapshot(tmp_path, monkeypatch):
    import threading
    gate, entered = threading.Event(), threading.Event()
    real = experiments.snapshot_database

    def slow_snapshot(source, target):
        entered.set(); gate.wait(30)
        return real(source, target)
    monkeypatch.setattr(experiments, 'snapshot_database', slow_snapshot)
    client, data, dispatched = _lab_app(tmp_path, monkeypatch, 1_000)
    with client:
        from capstone_rag.webapp.runtime import get_runtime
        manager = get_runtime().experiments
        head = _ready_head(client)
        t = time.perf_counter(); started = client.post('/api/lab/start', json=SMOKE(head)).json(); elapsed = time.perf_counter() - t
        try:
            assert entered.wait(10) and elapsed < 2
            results = client.get(f"/api/lab/results?experiment_id={started['id']}").json()['experiments'][0]
            assert results['status'] == 'preparing' and results['runs'] == [] and results['runs_total'] == 0
            assert manager.rows(started['id']) == [] and not dispatched
        finally:
            gate.set()
        manager.threads[started['id']].join(60)
        assert dispatched == [(started['id'], 1)]
        assert (data / 'experiments' / started['id'] / 'source-snapshot.db').exists()


def test_failed_or_interrupted_preparation_never_leaves_a_running_campaign(tmp_path, monkeypatch):
    def broken(source, target):
        raise OSError('disk full')
    monkeypatch.setattr(experiments, 'snapshot_database', broken)
    client, data, dispatched = _lab_app(tmp_path, monkeypatch, 10)
    with client:
        from capstone_rag.webapp.runtime import get_runtime
        manager = get_runtime().experiments
        started = client.post('/api/lab/start', json=SMOKE(_ready_head(client))).json()
        manager.threads[started['id']].join(30)
        failed = manager.registry.get('experiments', started['id'])
        assert failed['status'] == 'failed' and failed['termination_reason'] == 'preparation_failed' and 'disk full' in failed['error']
        assert manager.rows(started['id']) == [] and not dispatched
        # A coordinator restart while a campaign is still preparing closes it as incomplete.
        manager.save({'id': 'EXP-prep', 'status': 'preparing', 'created_at': time.time(), 'directory': str(data / 'experiments' / 'EXP-prep')})
        manager.recover()
        assert manager.registry.get('experiments', 'EXP-prep')['termination_reason'] == 'interrupted_while_preparing'


def test_results_for_a_640_run_campaign_stay_fast(tmp_path, monkeypatch):
    client, data, _ = _lab_app(tmp_path, monkeypatch, 10)
    with client:
        from capstone_rag.webapp.runtime import get_runtime
        reg = get_runtime().experiments.registry
        _history(reg, data)
        methods = [f'method-{letter}{number}' for letter in 'ABCDEFGH' for number in '1234']
        models = ['qwen3.5:4b', 'qwen3.5:9b', 'llama3.2:3b', 'gemma3:4b']
        with reg.connect() as db:
            db.execute('INSERT INTO experiments VALUES (?,?)', ('EXP-big', json.dumps({'id': 'EXP-big', 'status': 'running', 'created_at': 9e9,
                       'directory': str(data / 'experiments' / 'EXP-big'), 'planned_runs': 640, 'concurrency': 16})))
            for r in range(640):
                rid = f'RUN-big{r:04d}'
                db.execute('INSERT INTO experiment_runs VALUES (?,?,?)', (rid, 'EXP-big', json.dumps({
                    'id': rid, 'experiment_id': 'EXP-big', 'status': 'completed' if r < 600 else 'running', 'duration_ms': 1000.0 + r,
                    'model': models[r % 4], 'case': f'case-{r % 10}', 'correct': r % 3 == 0, 'architecture': {'id': methods[r % 32]},
                    'directory': str(data / 'experiments' / 'EXP-big' / rid)})))
        client.get('/api/lab/results?experiment_id=EXP-big')                     # one-time summary backfill
        worst = 0
        for _ in range(5):                                                          # the page polls every 3 s
            t = time.perf_counter(); body = client.get('/api/lab/results?experiment_id=EXP-big').json(); worst = max(worst, time.perf_counter() - t)
        assert body['experiments'][0]['runs_total'] == 640 and worst < 2, worst


def test_reconciliation_scans_skip_old_history(tmp_path):
    """flush_pending / the late-result watch visit indexed and recent run
    folders only, never every trial folder of the lab history."""
    import os
    from capstone_rag import lab_reconciliation as lr
    root = tmp_path / 'experiments'
    old = time.time() - 30 * 86400
    for e in range(15):
        ed = root / f'EXP-old{e:02d}'
        for r in range(200):
            rd = ed / f'RUN-o{e:02d}{r:03d}'; rd.mkdir(parents=True)
            (rd / lr.OUTBOX).write_text('', encoding='utf-8'); (rd / lr.WATCH).write_text('[]', encoding='utf-8')
        os.utime(ed, (old, old))
    asked = []
    started = time.perf_counter()
    assert lr.flush_pending(root, lambda run_dir: asked.append(run_dir)) == 0
    assert asked == [] and time.perf_counter() - started < 1                       # 3,000 old folders never walked
    assert lr.candidate_run_dirs(root, 'watch', lr.WATCH) == []
    # An indexed (pending) run in an old experiment is still found.
    pending = root / 'EXP-old00' / 'RUN-o00000'
    lr.mark(pending, 'outbox'); lr.mark(pending, 'watch')
    assert [Path(p).name for p in lr.candidate_run_dirs(root, 'outbox', lr.OUTBOX)] == ['RUN-o00000']
    assert [Path(p).name for p in lr.candidate_run_dirs(root, 'watch', lr.WATCH, include_recent=False)] == ['RUN-o00000']
    # A recent experiment written before the index existed is still found.
    fresh = root / 'EXP-new' / 'RUN-n1'; fresh.mkdir(parents=True); (fresh / lr.OUTBOX).write_text('', encoding='utf-8')
    assert 'RUN-n1' in [Path(p).name for p in lr.candidate_run_dirs(root, 'outbox', lr.OUTBOX)]


def test_snapshot_cost_does_not_scale_with_mirrored_history(tmp_path):
    """Same live database, old pipeline vs new: the new one skips the copy +
    DELETE + VACUUM of every mirrored row (measured 5.2 s -> 0.2 s at 300k)."""
    live = _live(tmp_path, MIRROR_ROWS)
    old, new = tmp_path / 'old.db', tmp_path / 'new.db'
    started = time.perf_counter()
    with closing(sqlite3.connect(live)) as a, closing(sqlite3.connect(old)) as b:
        a.backup(b)
    experiments.prune_snapshot(old)
    old_seconds = time.perf_counter() - started
    started = time.perf_counter()
    assert experiments.snapshot_database(live, new) == {'experiment_mirror_events': MIRROR_ROWS}
    new_seconds = time.perf_counter() - started
    assert new_seconds < .5 * old_seconds, (new_seconds, old_seconds)
