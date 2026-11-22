"""rc.25 A1: producer-side closure of a stopped worker's owned assignments.

Race fixture from EXP-c922 evidence: CALL-5f51088294624b689e49ace80ad7d366,
worker exit 16:11:27, Gateway completion 16:12:05, reporting lease end
16:13:05. The three are kept as separate facts.
"""
import json
import sqlite3
from pathlib import Path

from capstone_rag import lab_reconciliation as lr
from capstone_rag import telemetry
from capstone_rag.store import Store

TRIAL, OTHER = 'RUN-TRIAL-A1', 'RUN-NEIGHBOUR'
CALL = 'CALL-5f51088294624b689e49ace80ad7d366'
EXIT_AT = 1795000287.0   # stands for 16:11:27


def _row(event_type, status, *, workflow=TRIAL, agent='lab_reviewer', task='draft:CASE-1-DRAFT-00003', corr=CALL, detail=None, n=0):
    return {'event_id': f'ARENA-{workflow}-{event_type}-{corr}-{n}', 'schema_version': '1.0', 'timestamp': f'2026-11-18T16:1{n}:00+00:00',
            'event_type': event_type, 'case_id': 'CASE-1', 'workflow_id': workflow, 'task_id': task, 'agent_id': agent,
            'sender_agent_id': None, 'receiver_agent_id': None, 'correlation_id': corr, 'logical_home_node': 'coordinator',
            'execution_node': 'coordinator', 'tool_node': None, 'inference_node': 'head-192-0-2-3', 'model_id': 'qwen3:4b',
            'endpoint_id': 'head-192-0-2-3', 'task_status': status, 'summary': 'EXPERIMENT x', 'evidence_refs': '[]',
            'detail': json.dumps(detail or {'request_id': corr, 'route_role': 'lab_reviewer', 'logical_agent_status': 'known'})}


def _run_dir(tmp_path):
    rd = tmp_path / 'EXP-A1' / TRIAL
    rd.mkdir(parents=True)
    rows = [
        _row('inference.queued', 'queued', n=0),
        _row('inference.started', 'running', n=1, detail={'request_id': CALL, 'gateway_request_id': 'REQ-gw-1', 'route_role': 'lab_reviewer'}),
        _row('task.created', 'queued', agent='lab-customer-simulator', task='customer:CASE-1-SHIP-1', corr='CASE-1-SHIP-1', n=2),
        _row('task.started', 'running', agent='lab-customer-simulator', task='customer:CASE-1-SHIP-1', corr='CASE-1-SHIP-1', n=3),
        _row('inference.queued', 'queued', corr='CALL-done', n=4), _row('inference.completed', 'completed', corr='CALL-done', n=5),
        # A neighbouring trial's open call must never be closed by this trial.
        _row('inference.started', 'running', workflow=OTHER, corr='CALL-neighbour', n=6),
    ]
    (rd / 'arena-mirror.imported-1.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    live = tmp_path / 'live.db'
    Store(str(live)).close()
    run = {'id': TRIAL, 'experiment_id': 'EXP-A1', 'directory': str(rd), 'source_database': str(live)}
    (rd / 'manifest.json').write_text(json.dumps(run), encoding='utf-8')
    return run, rd, live


def _events(live, *types):
    with sqlite3.connect(live) as db:
        return [dict(zip(('event_type', 'workflow_id', 'agent_id', 'task_id', 'correlation_id', 'task_status', 'detail'), r)) for r in db.execute(
            'SELECT event_type,workflow_id,agent_id,task_id,correlation_id,task_status,detail FROM arena_events WHERE event_type IN (%s) ORDER BY sequence'
            % ','.join('?' * len(types)), types)]


EXIT = {'kind': 'timeout_kill', 'reason': 'worker exceeded the explicit wall-clock limit and was stopped', 'confirmed_at': EXIT_AT}


def test_hard_kill_closes_only_this_trials_open_assignments(tmp_path):
    run, rd, live = _run_dir(tmp_path)
    summary = lr.reconcile(run, 'ATTEMPT-1', EXIT, str(live))
    assert summary['outstanding'] == 2 and summary['closed'] == 2 and summary['late_result_watch'] == 1
    rows = _events(live, 'inference.interrupted', 'task.interrupted')
    assert {(r['event_type'], r['agent_id'], r['correlation_id']) for r in rows} == {
        ('inference.interrupted', 'lab_reviewer', CALL), ('task.interrupted', 'lab-customer-simulator', 'CASE-1-SHIP-1')}
    assert all(r['workflow_id'] == TRIAL and r['task_status'] == 'interrupted' for r in rows)
    rec = json.loads(rows[0]['detail'])['reconciliation']
    assert rec['gateway_request_id'] == 'REQ-gw-1' and rec['request_id'] == CALL and rec['exit_kind'] == 'timeout_kill'
    assert rec['logical_agent_id'] == 'lab_reviewer' and 'confirmed worker exit' in rec['provenance']
    assert not any(r['correlation_id'] == 'CALL-neighbour' for r in rows)


def test_duplicate_closure_is_idempotent(tmp_path):
    run, rd, live = _run_dir(tmp_path)
    lr.reconcile(run, 'ATTEMPT-1', EXIT, str(live));lr.reconcile(run, 'ATTEMPT-1', EXIT, str(live))
    assert len(_events(live, 'inference.interrupted', 'task.interrupted')) == 2


def test_unavailable_live_database_keeps_the_outbox_pending(tmp_path):
    run, rd, live = _run_dir(tmp_path)
    blocked = tmp_path / 'no-such-dir' / 'live.db'
    try:
        lr.reconcile(run, 'ATTEMPT-1', EXIT, str(blocked))
    except sqlite3.OperationalError:
        pass
    assert (rd / lr.OUTBOX).exists() and not (rd / lr.DONE).exists()
    # Coordinator restart: pending closures are imported once the live database is reachable.
    assert lr.flush_pending(tmp_path, lambda run_dir: str(live)) == 2
    assert lr.flush_pending(tmp_path, lambda run_dir: str(live)) == 0
    assert len(_events(live, 'inference.interrupted', 'task.interrupted')) == 2


def test_late_gateway_completion_is_a_separate_linked_fact(tmp_path):
    run, rd, live = _run_dir(tmp_path)
    lr.reconcile(run, 'ATTEMPT-1', EXIT, str(live))
    reads = []
    def status(request_id):
        reads.append(request_id)
        return {'request': {'request_id': request_id, 'status': 'completed', 'finished_at': '2026-11-18T16:12:05+00:00', 'resource_id': 'head-192-0-2-3'}}
    assert lr.check_late_results(run, str(live), status, now=EXIT_AT + 10) == 0        # not due yet: nothing read
    assert not reads
    lr.check_late_results(run, str(live), status, now=EXIT_AT + 40)
    late = _events(live, 'inference.late_result')
    assert len(late) == 1 and late[0]['task_status'] == 'completed' and late[0]['correlation_id'] == CALL
    detail = json.loads(late[0]['detail'])['late_result']
    assert detail['caller_state'] == 'interrupted' and detail['trial_outcome_unaffected'] and detail['linked_interruption_event_id'].startswith('ARENA-RECON-')
    # The interruption stays recorded; nothing is rewritten.
    assert len(_events(live, 'inference.interrupted')) == 1
    lr.check_late_results(run, str(live), status, now=EXIT_AT + 4000)
    assert len(_events(live, 'inference.late_result')) == 1


def test_normal_finish_with_nothing_open_writes_nothing(tmp_path):
    run, rd, live = _run_dir(tmp_path)
    rows = [json.loads(l) for l in (rd / 'arena-mirror.imported-1.jsonl').read_text().splitlines()]
    rows += [_row('inference.completed', 'completed', n=7), _row('task.completed', 'completed', agent='lab-customer-simulator',
                                                                  task='customer:CASE-1-SHIP-1', corr='CASE-1-SHIP-1', n=8)]
    (rd / 'arena-mirror.imported-1.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    summary = lr.reconcile(run, 'ATTEMPT-1', {**EXIT, 'kind': 'normal_finish'}, str(live))
    assert summary['closed'] == 0 and not _events(live, 'inference.interrupted', 'task.interrupted')


def test_restart_closes_only_when_the_worker_lease_expired(tmp_path):
    run, rd, live = _run_dir(tmp_path)
    with sqlite3.connect(rd / 'worker-lease.db') as db:
        db.execute('CREATE TABLE lease (id INTEGER PRIMARY KEY,owner TEXT,expires REAL)');db.execute('INSERT INTO lease VALUES(1,?,?)', ('w', 9e12))
    assert lr.lease_expired(rd) is False                     # unconfirmed: Arena's fallback keeps it
    with sqlite3.connect(rd / 'worker-lease.db') as db:
        db.execute('UPDATE lease SET expires=0')
    assert lr.lease_expired(rd) is True


def test_waiting_review_requests_and_queued_tasks_are_closed_but_not_twice(tmp_path):
    run, rd, live = _run_dir(tmp_path)
    extra = [_row('human_review.requested', 'waiting', agent='coordinator', task='review:GRAPH-1:abc', corr='review:GRAPH-1:abc', n=7),
             _row('task.created', 'queued', agent='resolution_planning', task='TASK-planning', corr='TASK-planning', n=8)]
    with open(rd / 'arena-mirror.imported-1.jsonl', 'a', encoding='utf-8') as stream:
        stream.write(''.join(json.dumps(r) + '\n' for r in extra))
    first = lr.reconcile(run, 'ATTEMPT-1', EXIT, str(live))
    assert first['closed'] == 4
    rows = _events(live, 'task.interrupted')
    assert {r['task_id'] for r in rows} >= {'review:GRAPH-1:abc', 'TASK-planning'}
    second = lr.reconcile(run, 'ATTEMPT-2', {**EXIT, 'kind': 'coordinator_restart_lease_expired'}, str(live))
    assert second['closed'] == 0                         # already closed in attempt 1: never re-closed
