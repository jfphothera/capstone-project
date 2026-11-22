"""rc.17: parallel flows stay full even when the live database is slow."""
import json
import threading
import time

from capstone_rag import experiments
from capstone_rag.ai_settings import Registry


def test_finished_trials_release_slots_while_live_writes_are_slow(tmp_path, monkeypatch):
    registry = Registry(tmp_path / 'settings.db')
    manager = experiments.Manager(registry)
    eid = 'EXP-DISPATCH'
    directory = tmp_path / eid
    directory.mkdir()
    manager.save({'id': eid, 'status': 'running', 'created_at': time.time(), 'concurrency': 4, 'failure_limit': 100,
                  'planned_runs': 12, 'directory': str(directory), 'source_database': str(tmp_path / 'live.db')})
    for number in range(12):
        manager._store_run({'id': f'RUN-{number:02d}', 'experiment_id': eid, 'status': 'queued', 'directory': str(directory),
                            'scenario': {'id': 'x'}, 'profile': {'routes': {}}, 'queued_at': time.time()})

    live_writes = []

    def slow_live_store(path):  # a contended live database: every write takes 0.5 s
        class Slow:
            pass
        return Slow()

    def slow_emit(store, event_type, **fields):
        time.sleep(0.5)
        live_writes.append(event_type)

    monkeypatch.setattr(manager, '_live_store', slow_live_store)
    monkeypatch.setattr('capstone_rag.telemetry.emit', slow_emit)

    def fake_execute(run, resume=False):
        manager._arena(eid, 'experiment.trial.started', 'running', 'started', run)
        time.sleep(0.2)  # the worker process
        finished = {**run, 'status': 'completed'}
        manager._store_run(finished)
        manager._arena(eid, 'experiment.trial.completed', 'completed', 'done', finished)
        return finished

    monkeypatch.setattr(manager, '_execute', fake_execute)
    started = time.perf_counter()
    manager._campaign(eid)
    elapsed = time.perf_counter() - started

    events = [json.loads(line) for line in (directory / 'coordinator.jsonl').read_text(encoding='utf-8').splitlines()]
    active = peak = 0
    gaps, last_finish = [], None
    for event in events:
        if event['event'] == 'trial.dispatched':
            active += 1; peak = max(peak, active)
            if last_finish is not None:
                gaps.append(event['at'] - last_finish); last_finish = None
        elif event['event'] == 'trial.finished':
            active -= 1; last_finish = event['at']
    # 24 live writes x 0.5 s = 12 s if finished trials held their slots; with the
    # background writer the 12 trials take about 3 waves x 0.2 s of dispatch time.
    assert peak == 4
    assert max(gaps) < 0.3, gaps
    assert sum(1 for e in events if e['event'] == 'trial.dispatched') == 12
    assert len(live_writes) == 24  # every live write still happened (flushed at campaign end)
    assert registry.get('experiments', eid)['status'] == 'completed'
    assert elapsed < 20
