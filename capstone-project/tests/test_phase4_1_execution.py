import asyncio
import hashlib
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from capstone_rag import store as store_mod, telemetry
from capstone_rag.execution import (ConcurrentExecutionEngine, IllegalTransition,
    TaskRepository, TaskSpec, stable_task_key)
from capstone_rag.mcp_services import MCPGateway
from capstone_rag.phase4_runtime import collect_initial_evidence
from capstone_rag.scheduler import (DeterministicScheduler, NoEligibleWorker,
    WorkerRegistry, WorkerSpec, ensure_local_workers)
from capstone_rag.webapp.app import create_app


def make_store(tmp_path):
    st = store_mod.Store(str(tmp_path / 'phase4.db'))
    st.insert('cases', {'case_id': 'CASE-410001', 'created_at': store_mod.now(),
        'updated_at': store_mod.now(), 'channel': 'email', 'status': 'New',
        'identity_verified': 0, 'rma_needed': 0, 'ai_state': 'queued',
        'subject': 'Synthetic Phase 4.1 case', 'case_version': 0})
    st.insert('devices', {'serial_number': '72377-DL1', 'model': 'P2400',
        'revision': 'Rev-A', 'revision_status': 'verified',
        'revision_basis': 'synthetic ERP record'})
    st.insert('messages', {'message_id': 'MSG-410001', 'case_id': 'CASE-410001',
        'created_at': store_mod.now(), 'direction': 'inbound', 'channel': 'email',
        'author': 'synthetic.customer', 'subject': 'Lamp',
        'body': 'Serial 72377-DL1 lamp is dark.', 'status': 'received'})
    return st


def spec(kind, *, workflow='WF-1', dependencies=(), capability='deterministic.test'):
    return TaskSpec(workflow, 'CASE-410001', 'worker_' + kind.replace('.', '_'),
        kind, capability, workflow, stable_task_key(workflow, kind),
        dependency_set=tuple(dependencies), timeout_seconds=10)


def test_schema_11_is_forward_migrated(tmp_path):
    st = make_store(tmp_path)
    assert st.one("SELECT value FROM meta WHERE key='schema_version'")['value'] == '11'
    names = {row['name'] for row in st.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'execution_tasks', 'worker_registry', 'mcp_tool_invocations'} <= names


def test_dynamic_agent_registry_has_no_population_cap_and_keeps_identity(tmp_path):
    st = make_store(tmp_path)
    for count in (2, 10, 25, 50, 100):
        for index in range(count):
            telemetry.register(st, f'reviewer-{index}', f'Reviewer {index}', 'reviewer',
                               capabilities=('artifact.review',))
        rows = {row['agent_id']: row for row in telemetry.registry(st)}
        assert all(f'reviewer-{index}' in rows for index in range(count))
    before = next(row for row in telemetry.registry(st) if row['agent_id'] == 'reviewer-1')
    telemetry.retire(st, 'reviewer-1')
    telemetry.register(st, 'reviewer-1', 'Reviewer One', 'reviewer',
                       capabilities=('artifact.review',))
    after = next(row for row in telemetry.registry(st) if row['agent_id'] == 'reviewer-1')
    assert before['color'] == after['color'] and not after['retired']


def test_task_transitions_dependencies_and_illegal_transition(tmp_path):
    st = make_store(tmp_path); repo = TaskRepository(st)
    first = repo.create(spec('first'))
    second = repo.create(spec('second', dependencies=(first['task_id'],)))
    assert first['state'] == 'eligible'
    assert second['state'] == 'waiting_for_dependency'
    with pytest.raises(IllegalTransition):
        repo.transition(first['task_id'], 'completed', 'test', 'cannot skip execution')
    repo.assign(first['task_id'], 'local-tools'); repo.begin_attempt(first['task_id'])
    repo.complete(first['task_id'], {'value': 1})
    assert repo.refresh_dependencies(second['task_id'])['state'] == 'eligible'


def test_two_and_three_tasks_overlap_under_resource_limit(tmp_path):
    st = make_store(tmp_path); engine = ConcurrentExecutionEngine(st, max_concurrency=3)
    tasks = [engine.repo.create(spec(f'cpu.{index}')) for index in range(3)]
    barrier = threading.Barrier(3); times = {}; lock = threading.Lock()

    def work(task):
        with lock: times[task['task_id']] = [time.perf_counter(), None]
        barrier.wait(timeout=5)
        digest = b'x'
        for _ in range(20_000):
            digest = hashlib.sha256(digest).digest()
        with lock: times[task['task_id']][1] = time.perf_counter()
        return {'digest': digest.hex()}

    states = engine.run([task['task_id'] for task in tasks],
                        {f'cpu.{index}': work for index in range(3)})
    assert all(row['state'] == 'completed' for row in states.values())
    assert max(start for start, _ in times.values()) < min(end for _, end in times.values())
    attempts = st.query('SELECT started_at,ended_at,timing_ms FROM execution_attempts')
    assert len(attempts) == 3 and all(row['timing_ms'] > 0 for row in attempts)


def test_failed_predecessor_blocks_join_and_cancellation_publishes_no_result(tmp_path):
    st = make_store(tmp_path); repo = TaskRepository(st)
    first = repo.create(spec('bad'))
    repo.assign(first['task_id'], 'local-tools'); repo.begin_attempt(first['task_id'])
    repo.fail(first['task_id'], 'invalid_task_input', 'deterministic failure')
    join = repo.create(spec('join', dependencies=(first['task_id'],)))
    assert repo.refresh_dependencies(join['task_id'])['state'] == 'blocked'
    cancel = repo.create(spec('cancel'))
    repo.cancel(cancel['task_id'], 'test', 'user canceled')
    assert repo.result(cancel['task_id']) is None


def test_scheduler_filters_health_model_and_capacity_and_records_reason(tmp_path):
    st = make_store(tmp_path); registry = ensure_local_workers(st)
    registry.register(WorkerSpec('gpu-ok', 'gpu-node', 'gpu.local', 'ollama', 'gpu',
        ('gpu.inference',), ('model.review',), endpoint='http://gpu.local:11434',
        installed_models=('review:1',), available_models=('review:1',),
        loaded_models=(), max_concurrency=1, measurement_provenance='configured_test'))
    registry.register(WorkerSpec('gpu-bad', 'gpu-bad', 'bad.local', 'ollama', 'gpu',
        ('gpu.inference',), ('model.review',), available_models=('review:1',),
        health_state='unavailable', measurement_provenance='simulated_failure'))
    repo = TaskRepository(st)
    task = repo.create(TaskSpec('WF-GPU', 'CASE-410001', 'validator', 'model.review',
        'gpu.inference', 'WF-GPU', 'gpu-task', required_model='review:1'))
    selected = DeterministicScheduler(st).select(task)
    assert selected['worker_id'] == 'gpu-ok'
    decision = st.one('SELECT * FROM scheduler_decisions WHERE task_id=?', (task['task_id'],))
    assert any(row['worker_id'] == 'gpu-bad' and 'unhealthy' in row['reasons']
               for row in json.loads(decision['excluded_workers']))
    registry.heartbeat('gpu-ok', health_state='unavailable', provenance='fault_injection')
    with pytest.raises(NoEligibleWorker):
        DeterministicScheduler(st).select(task)


def test_queue_and_per_service_resource_limits_are_enforced(tmp_path):
    st = make_store(tmp_path)
    engine = ConcurrentExecutionEngine(st, max_concurrency=2, queue_capacity=1)
    one = engine.repo.create(TaskSpec('WF-LIMIT', 'CASE-410001', 'erp_research',
        'limited.erp', 'erp.read', 'WF-LIMIT', 'limit-one',
        required_tools=('erp.lookup_device',)))
    two = engine.repo.create(TaskSpec('WF-LIMIT', 'CASE-410001', 'erp_research_2',
        'limited.erp', 'erp.read', 'WF-LIMIT', 'limit-two',
        required_tools=('erp.lookup_device',)))
    with pytest.raises(OverflowError):
        engine.run([one['task_id'], two['task_id']], {'limited.erp': lambda task: {}})
    engine.scheduler.configure_limit('service', 'erp', max_concurrency=1, queue_capacity=1)
    st.execute("UPDATE execution_resource_limits SET active_count=1 "
               "WHERE scope_type='service' AND scope_key='erp'")
    called = []
    state = engine.run([one['task_id']], {'limited.erp': lambda task: called.append(task) or {}})
    assert not called and state[one['task_id']]['state'] == 'queued'


def test_official_mcp_services_are_invoked_with_scope_provenance_and_idempotency(tmp_path):
    st = make_store(tmp_path); gateway = MCPGateway(st)
    erp = gateway.call(agent_id='erp_research', tool_name='erp.lookup_device',
        arguments={'serial': '72377-DL1'}, request_id='REQ-ERP',
        workflow_id='WF-MCP', case_id='CASE-410001')
    assert erp['status'] == 'matched' and erp['devices'][0]['revision'] == 'Rev-A'
    assert gateway.call(agent_id='erp_research', tool_name='erp.lookup_device',
        arguments={'serial': '72377-DL1'}, request_id='REQ-ERP',
        workflow_id='WF-MCP', case_id='CASE-410001') == erp
    history = gateway.call(agent_id='ticket_history', tool_name='ticket.lookup_history',
        arguments={'case_id': 'CASE-410001'}, request_id='REQ-TICKET',
        workflow_id='WF-MCP', case_id='CASE-410001')
    assert history['chronology'][0]['evidence_ref'] == 'MSG-410001'
    proposal = gateway.call(agent_id='operations', tool_name='operations.draft_proposal',
        arguments={'case_id': 'CASE-410001', 'proposal_type': 'rma', 'reason': 'test'},
        request_id='REQ-OPS', workflow_id='WF-MCP', case_id='CASE-410001')
    assert proposal['status'] == 'draft_only' and not proposal['executed']
    with pytest.raises(PermissionError):
        gateway.call(agent_id='ticket_history', tool_name='operations.draft_proposal',
            arguments={'case_id': 'CASE-410001', 'proposal_type': 'rma', 'reason': 'x'},
            request_id='REQ-DENIED')
    row = st.one("SELECT * FROM mcp_tool_invocations WHERE request_id='REQ-ERP'")
    assert row['transport'] == 'official-mcp-sdk-in-memory' and row['status'] == 'completed'


def test_mcp_timeout_fails_safely_and_is_audited(tmp_path, monkeypatch):
    st = make_store(tmp_path); gateway = MCPGateway(st)
    async def slow(*args, **kwargs):
        raise asyncio.TimeoutError
    monkeypatch.setattr(gateway, '_call', slow)
    with pytest.raises(TimeoutError):
        gateway.call(agent_id='erp_research', tool_name='erp.lookup_device',
            arguments={'serial': '72377-DL1'}, request_id='REQ-TIMEOUT', timeout=.001)
    row = st.one("SELECT * FROM mcp_tool_invocations WHERE request_id='REQ-TIMEOUT'")
    assert row['status'] == 'failed' and row['error_category'] == 'mcp_tool_timeout'


def test_existing_coordinator_bridge_runs_real_mcp_tasks_concurrently_and_joins(tmp_path):
    st = make_store(tmp_path)
    evidence = collect_initial_evidence(st, {'workflow_id': 'WF-LIVE',
        'cid': 'CASE-410001', 'message_id': 'MSG-410001',
        'body': 'Serial 72377-DL1 lamp is dark.'})
    assert evidence['status'] == 'completed'
    assert evidence['erp']['status'] == 'matched'
    assert evidence['ticket']['chronology'][0]['evidence_ref'] == 'MSG-410001'
    assert evidence['safety']['authority'] == 'deterministic_safety_classifier'
    tasks = st.query("SELECT * FROM execution_tasks WHERE workflow_id='WF-LIVE'")
    assert len(tasks) == 4 and all(row['state'] == 'completed' for row in tasks)
    attempts = st.query(
        "SELECT a.started_at,a.ended_at FROM execution_attempts a "
        "JOIN execution_tasks t ON t.task_id=a.task_id "
        "WHERE t.workflow_id='WF-LIVE' AND t.task_type LIKE 'initial.%' "
        "AND t.task_type!='initial.join'")
    starts = [__import__('datetime').datetime.fromisoformat(row['started_at']) for row in attempts]
    ends = [__import__('datetime').datetime.fromisoformat(row['ended_at']) for row in attempts]
    assert any(starts[i] < ends[j] and starts[j] < ends[i]
               for i in range(len(starts)) for j in range(i + 1, len(starts)))
    assert {row['tool_name'] for row in st.query(
        "SELECT tool_name FROM mcp_tool_invocations WHERE workflow_id='WF-LIVE'")} == {
            'erp.lookup_device', 'ticket.lookup_history'}
    assert st.one("SELECT * FROM arena_events WHERE workflow_id='WF-LIVE' "
                  "AND event_type='join.completed'")


def test_qms_mcp_rejects_superseded_and_preserves_revision_filter(tmp_path):
    st = make_store(tmp_path); gateway = MCPGateway(st)
    result = gateway.call(agent_id='qms_research', tool_name='qms.search_authorized',
        arguments={'query': 'lamp troubleshooting', 'model': 'P2400', 'revision': 'Rev-A'},
        request_id='REQ-QMS', workflow_id='WF-MCP', case_id='CASE-410001')
    assert result['authorization_decision'] == 'not_granted_by_tool'
    assert all(row['approval_status'] != 'Superseded' for row in result['results'])
    assert all(not row['revision_scope'] or 'Rev-A' in row['revision_scope']
               for row in result['results'])


def test_restart_recovery_and_duplicate_task_delivery_are_safe(tmp_path):
    st = make_store(tmp_path); repo = TaskRepository(st)
    original = repo.create(spec('recover'))
    duplicate = repo.create(spec('recover'))
    assert duplicate['task_id'] == original['task_id']
    repo.assign(original['task_id'], 'local-tools'); repo.begin_attempt(original['task_id'])
    recovered = repo.recover()
    assert recovered == [original['task_id']]
    assert repo.get(original['task_id'])['state'] == 'queued'
    assert st.one('SELECT * FROM execution_recovery_records WHERE task_id=?',
                  (original['task_id'],))


def test_retry_classification_exhausts_transient_budget_without_retrying_authority(tmp_path):
    st = make_store(tmp_path); repo = TaskRepository(st)
    transient = repo.create(TaskSpec('WF-RETRY', 'CASE-410001', 'validator',
        'retry', 'deterministic.test', 'WF-RETRY', 'retry-task', max_attempts=2))
    repo.assign(transient['task_id'], 'local-tools'); repo.begin_attempt(transient['task_id'])
    assert repo.fail(transient['task_id'], 'worker_unavailable', 'offline')['state'] == 'queued'
    repo.assign(transient['task_id'], 'local-tools'); repo.begin_attempt(transient['task_id'])
    assert repo.fail(transient['task_id'], 'worker_unavailable', 'still offline')['state'] == 'escalated'
    authority = repo.create(TaskSpec('WF-AUTH', 'CASE-410001', 'validator',
        'authority', 'deterministic.test', 'WF-AUTH', 'authority-task'))
    repo.assign(authority['task_id'], 'local-tools'); repo.begin_attempt(authority['task_id'])
    assert repo.fail(authority['task_id'], 'missing_authority', 'approval required')['state'] == 'escalated'
    assert repo.get(authority['task_id'])['attempt_count'] == 1


def test_stale_evidence_result_is_rejected(tmp_path):
    st = make_store(tmp_path); repo = TaskRepository(st)
    task = repo.create(spec('stale'))
    repo.assign(task['task_id'], 'local-tools'); repo.begin_attempt(task['task_id'])
    st.update('cases', 'case_id', 'CASE-410001', {'case_version': 1})
    finished = repo.complete(task['task_id'], {'optimistic': True})
    assert finished['state'] == 'blocked' and finished['error_category'] == 'stale_evidence'
    assert repo.result(task['task_id'])['status'] == 'stale'


def test_url_tokens_are_rejected_and_viewer_session_is_read_only_and_expiring(tmp_path):
    st = make_store(tmp_path)
    app = create_app(prefer_llm=False, store=st, start_workers=False,
                     developer_token='phase4-secret')
    with TestClient(app) as client:
        assert client.get('/pressure-testing?developer_token=phase4-secret').status_code == 403
        assert client.get('/api/arena/events?developer_token=phase4-secret').status_code == 403
        login = client.post('/api/arena/session', json={'token': 'phase4-secret'})
        assert login.status_code == 200
        assert 'phase4-secret' not in login.headers['set-cookie']
        assert client.get('/api/arena/events').status_code == 200
        assert client.get('/pressure-testing').status_code == 200
        assert client.post('/api/arena/logout').status_code == 403
        assert client.post('/api/arena/logout', headers={
            'x-csrf-token': login.json()['csrf_token']}).status_code == 200
        expired = client.post('/api/arena/session', json={'token': 'phase4-secret'})
        session_id = client.cookies.get('capstone_developer_session')
        app.state.developer_sessions[session_id]['expires_at'] = 0
        assert client.get('/api/arena/events').status_code == 403

