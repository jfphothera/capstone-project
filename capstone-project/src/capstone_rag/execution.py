"""Durable Phase 4.1 task graph and bounded concurrent execution engine."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
import time
from dataclasses import dataclass

from .coordination import timestamp, uid
from .scheduler import DeterministicScheduler, NoEligibleWorker, ensure_local_workers
from . import telemetry
from . import runtime_identity,task_context

HEARTBEAT_SECONDS=30


SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_tasks (
 task_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, case_id TEXT NOT NULL,
 parent_task_id TEXT, correlation_id TEXT NOT NULL, logical_agent_id TEXT NOT NULL,
 task_type TEXT NOT NULL, capability_requirement TEXT NOT NULL,
 required_tools TEXT NOT NULL, required_model TEXT, model_eligibility TEXT NOT NULL,
 input_evidence_refs TEXT NOT NULL, consumed_case_version INTEGER NOT NULL,
 dependency_set TEXT NOT NULL, state TEXT NOT NULL, priority INTEGER NOT NULL,
 deadline_at REAL NOT NULL, timeout_seconds REAL NOT NULL,
 assigned_worker_id TEXT, attempt_count INTEGER NOT NULL,
 max_attempts INTEGER NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
 result_reference TEXT, error_category TEXT, cancellation_state TEXT NOT NULL,
 created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_execution_ready ON execution_tasks(state,priority,deadline_at);
CREATE INDEX IF NOT EXISTS ix_execution_workflow ON execution_tasks(workflow_id,case_id,state);
CREATE TABLE IF NOT EXISTS execution_task_dependencies (
 task_id TEXT NOT NULL, predecessor_task_id TEXT NOT NULL,
 required_state TEXT NOT NULL DEFAULT 'completed',
 PRIMARY KEY(task_id,predecessor_task_id));
CREATE TABLE IF NOT EXISTS execution_task_transitions (
 transition_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, from_state TEXT,
 to_state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
 correlation_id TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS execution_attempts (
 attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt_number INTEGER NOT NULL,
 worker_id TEXT, started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT,
 error_category TEXT, timing_ms REAL, UNIQUE(task_id,attempt_number));
CREATE TABLE IF NOT EXISTS execution_results (
 result_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt_number INTEGER NOT NULL,
 status TEXT NOT NULL, payload TEXT NOT NULL, evidence_refs TEXT NOT NULL,
 consumed_case_version INTEGER NOT NULL, observed_case_version INTEGER NOT NULL,
 worker_id TEXT, created_at TEXT NOT NULL, UNIQUE(task_id,attempt_number));
CREATE TABLE IF NOT EXISTS execution_recovery_records (
 recovery_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, previous_state TEXT NOT NULL,
 recovered_state TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL);
"""


STATES = {
    'created', 'eligible', 'queued', 'assigned', 'running',
    'waiting_for_dependency', 'waiting_for_tool', 'waiting_for_inference',
    'completed', 'failed', 'canceled', 'blocked', 'escalated',
}
TRANSITIONS = {
    None: {'created'},
    'created': {'eligible', 'waiting_for_dependency', 'canceled', 'blocked'},
    'waiting_for_dependency': {'eligible', 'blocked', 'canceled'},
    'eligible': {'queued', 'assigned', 'blocked', 'canceled', 'escalated'},
    'queued': {'assigned', 'blocked', 'canceled', 'escalated'},
    'assigned': {'running', 'queued', 'blocked', 'canceled', 'escalated'},
    'running': {'waiting_for_tool', 'waiting_for_inference', 'completed', 'failed',
                'blocked', 'canceled', 'escalated'},
    'waiting_for_tool': {'running', 'failed', 'blocked', 'canceled', 'escalated'},
    'waiting_for_inference': {'running', 'failed', 'blocked', 'canceled', 'escalated'},
    'failed': {'queued', 'escalated'},
    'blocked': {'eligible', 'escalated', 'canceled'},
    'completed': set(), 'canceled': set(), 'escalated': set(),
}

RETRYABLE_ERRORS = {'transient_inference_timeout', 'worker_unavailable', 'mcp_tool_timeout'}
DETERMINISTIC_ERRORS = {'invalid_task_input', 'missing_authority', 'qms_revision_mismatch',
                        'human_approval_required', 'validation_rejection',
                        'stale_evidence', 'dependency_failure'}


def _j(value):
    return json.dumps(value, sort_keys=True, default=str)


def _loads(value):
    return json.loads(value or '[]')


def stable_task_key(*parts):
    return 'EXEC-' + hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:32]


class IllegalTransition(ValueError):
    pass


@dataclass(frozen=True)
class TaskSpec:
    workflow_id: str
    case_id: str
    logical_agent_id: str
    task_type: str
    capability_requirement: str
    correlation_id: str
    idempotency_key: str
    parent_task_id: str | None = None
    required_tools: tuple[str, ...] = ()
    required_model: str | None = None
    model_eligibility: tuple[str, ...] = ()
    input_evidence_refs: tuple[str, ...] = ()
    dependency_set: tuple[str, ...] = ()
    priority: int = 50
    timeout_seconds: float = 30.0
    max_attempts: int = 2


class TaskRepository:
    def __init__(self, store):
        self.store = store

    def get(self, task_id):
        row = self.store.one('SELECT * FROM execution_tasks WHERE task_id=?', (task_id,))
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row):
        result = dict(row)
        for key in ('required_tools', 'model_eligibility', 'input_evidence_refs', 'dependency_set'):
            result[key] = _loads(result[key])
        return result

    def create(self, spec: TaskSpec):
        old = self.store.one('SELECT * FROM execution_tasks WHERE idempotency_key=?',
                             (spec.idempotency_key,))
        if old:
            return self._decode(old)
        if spec.timeout_seconds <= 0 or spec.max_attempts < 1:
            raise ValueError('Task timeout and attempt budget must be positive')
        case = self.store.case(spec.case_id)
        if not case:
            raise ValueError('Task case does not exist')
        for dep in spec.dependency_set:
            parent = self.get(dep)
            if not parent or parent['workflow_id'] != spec.workflow_id or parent['case_id'] != spec.case_id:
                raise ValueError('Every dependency must belong to the same case and workflow')
        telemetry.register(self.store, spec.logical_agent_id,
                           spec.logical_agent_id.replace('_', ' ').title(),
                           spec.capability_requirement)
        now = timestamp(); task_id = uid('EXEC-TASK')
        row = {
            'task_id': task_id, 'workflow_id': spec.workflow_id,
            'case_id': spec.case_id, 'parent_task_id': spec.parent_task_id,
            'correlation_id': spec.correlation_id,
            'logical_agent_id': spec.logical_agent_id,
            'task_type': spec.task_type,
            'capability_requirement': spec.capability_requirement,
            'required_tools': _j(spec.required_tools),
            'required_model': spec.required_model,
            'model_eligibility': _j(spec.model_eligibility),
            'input_evidence_refs': _j(spec.input_evidence_refs),
            'consumed_case_version': int(case['case_version']),
            'dependency_set': _j(spec.dependency_set), 'state': 'created',
            'priority': int(spec.priority),
            'deadline_at': time.time() + float(spec.timeout_seconds),
            'timeout_seconds': float(spec.timeout_seconds),
            'assigned_worker_id': None, 'attempt_count': 0,
            'max_attempts': int(spec.max_attempts),
            'idempotency_key': spec.idempotency_key,
            'result_reference': None, 'error_category': None,
            'cancellation_state': 'not_requested',
            'created_at': now, 'started_at': None, 'completed_at': None,
            'updated_at': now,
        }
        with self.store.transaction():
            self.store.insert('execution_tasks', row)
            for dep in spec.dependency_set:
                self.store.insert('execution_task_dependencies', {
                    'task_id': task_id, 'predecessor_task_id': dep,
                    'required_state': 'completed'})
            self._record_transition(row, None, 'created', 'coordinator', 'durable task created')
        telemetry.emit(self.store, 'task.created', case_id=spec.case_id,
            workflow_id=spec.workflow_id, task_id=task_id,
            agent_id=spec.logical_agent_id, correlation_id=spec.correlation_id,
            task_status='created', summary=f'{spec.logical_agent_id} task created',
            evidence_refs=spec.input_evidence_refs,
            detail={'task_type': spec.task_type, 'capability': spec.capability_requirement})
        self.transition(task_id,
                        'waiting_for_dependency' if spec.dependency_set else 'eligible',
                        'coordinator', 'dependency evaluation' if spec.dependency_set else 'no dependencies')
        return self.get(task_id)

    def _record_transition(self, task, old, new, actor, reason):
        self.store.insert('execution_task_transitions', {
            'transition_id': uid('EXEC-TRANSITION'), 'task_id': task['task_id'],
            'from_state': old, 'to_state': new, 'actor': actor,
            'reason': reason, 'correlation_id': task['correlation_id'],
            'at': timestamp()})

    def transition(self, task_id, new_state, actor, reason, **changes):
        if new_state not in STATES:
            raise IllegalTransition(f'Unknown task state {new_state}')
        with self.store.transaction():
            raw = self.store.one('SELECT * FROM execution_tasks WHERE task_id=?', (task_id,))
            if not raw:
                raise ValueError('Unknown task')
            old = raw['state']
            if new_state == old:
                return self._decode(raw)
            if new_state not in TRANSITIONS[old]:
                raise IllegalTransition(f'Illegal task transition {old} -> {new_state}')
            values = {'state': new_state, 'updated_at': timestamp(), **changes}
            if new_state == 'running' and not raw['started_at']:
                values['started_at'] = timestamp()
            if new_state in {'completed', 'failed', 'canceled', 'blocked', 'escalated'}:
                values['completed_at'] = timestamp()
            self.store.update('execution_tasks', 'task_id', task_id, values)
            self._record_transition(raw, old, new_state, actor, reason)
        event = {
            'eligible': 'task.eligible', 'queued': 'task.queued',
            'assigned': 'task.assigned', 'running': 'task.started',
            'waiting_for_dependency': 'dependency.blocked',
            'waiting_for_tool': 'task.waiting_tool',
            'waiting_for_inference': 'inference.queued',
            'completed': 'task.completed', 'failed': 'task.failed',
            'canceled': 'task.canceled', 'blocked': 'task.blocked',
            'escalated': 'task.escalated',
        }.get(new_state, 'task.updated')
        task = self.get(task_id)
        worker=self.store.one('SELECT * FROM worker_registry WHERE worker_id=?',(task.get('assigned_worker_id'),))
        telemetry.emit(self.store, event, case_id=task['case_id'],
            workflow_id=task['workflow_id'], task_id=task_id,
            agent_id=task['logical_agent_id'], correlation_id=task['correlation_id'],
            execution_node=runtime_identity.coordinator_id(),
            inference_node=worker['node_id'] if worker and worker['worker_type']=='ollama' else None,
            task_status=new_state, summary=reason,
            evidence_refs=task['input_evidence_refs'],detail={'assigned_worker_id':task.get('assigned_worker_id'),
                'deadline_at':task['deadline_at'],'attempt':task['attempt_count'],
                'lease_seconds':HEARTBEAT_SECONDS*2 if new_state=='running' else None})
        return task

    def refresh_dependencies(self, task_id):
        task = self.get(task_id)
        if not task or task['state'] != 'waiting_for_dependency':
            return task
        dependencies = [self.get(dep) for dep in task['dependency_set']]
        failed = [d for d in dependencies if d['state'] in {'failed', 'canceled', 'blocked', 'escalated'}]
        if failed:
            return self.transition(task_id, 'blocked', 'coordinator',
                'required predecessor did not complete', error_category='dependency_failure')
        if dependencies and all(d['state'] == 'completed' for d in dependencies):
            task = self.transition(task_id, 'eligible', 'coordinator',
                                   'all dependencies completed')
            telemetry.emit(self.store, 'dependency.released', case_id=task['case_id'],
                workflow_id=task['workflow_id'], task_id=task_id,
                agent_id=task['logical_agent_id'], correlation_id=task['correlation_id'],
                task_status='eligible', summary='All task dependencies released')
        return task

    def assign(self, task_id, worker_id):
        task = self.get(task_id)
        if task['state'] == 'eligible':
            task = self.transition(task_id, 'queued', 'scheduler', 'admitted to bounded queue')
        return self.transition(task_id, 'assigned', 'scheduler', f'assigned to {worker_id}',
                               assigned_worker_id=worker_id)

    def begin_attempt(self, task_id):
        task = self.get(task_id)
        if task['attempt_count'] >= task['max_attempts']:
            return self.transition(task_id, 'escalated', 'executor', 'retry budget exhausted',
                                   error_category='retry_exhausted')
        number = task['attempt_count'] + 1
        self.store.update('execution_tasks', 'task_id', task_id, {'attempt_count': number})
        self.store.insert('execution_attempts', {
            'attempt_id': uid('EXEC-ATTEMPT'), 'task_id': task_id,
            'attempt_number': number, 'worker_id': task['assigned_worker_id'],
            'started_at': timestamp(), 'ended_at': None, 'outcome': None,
            'error_category': None, 'timing_ms': None})
        return self.transition(task_id, 'running', task['logical_agent_id'],
                               f'attempt {number} started')

    def complete(self, task_id, payload, *, evidence_refs=()):
        task = self.get(task_id)
        case_version = int(self.store.case(task['case_id'])['case_version'])
        stale = case_version != int(task['consumed_case_version'])
        result_id = uid('EXEC-RESULT')
        status = 'stale' if stale else 'accepted'
        self.store.insert('execution_results', {
            'result_id': result_id, 'task_id': task_id,
            'attempt_number': task['attempt_count'], 'status': status,
            'payload': _j(payload), 'evidence_refs': _j(evidence_refs),
            'consumed_case_version': task['consumed_case_version'],
            'observed_case_version': case_version,
            'worker_id': task['assigned_worker_id'], 'created_at': timestamp()})
        elapsed = self._finish_attempt(task, status, None)
        if stale:
            return self.transition(task_id, 'blocked', 'coordinator',
                'result rejected because authoritative case evidence changed',
                result_reference=result_id, error_category='stale_evidence')
        return self.transition(task_id, 'completed', task['logical_agent_id'],
            f'task completed in {elapsed:.3f} ms', result_reference=result_id)

    def fail(self, task_id, error_category, reason):
        task = self.get(task_id)
        if error_category not in RETRYABLE_ERRORS | DETERMINISTIC_ERRORS | {'internal_error', 'deadline_expired'}:
            error_category = 'internal_error'
        self._finish_attempt(task, 'failed', error_category)
        task = self.transition(task_id, 'failed', task['logical_agent_id'], reason,
                               error_category=error_category)
        if error_category in RETRYABLE_ERRORS and task['attempt_count'] < task['max_attempts']:
            return self.transition(task_id, 'queued', 'coordinator',
                                   f'retry authorized for {error_category}',
                                   completed_at=None, assigned_worker_id=None)
        return self.transition(task_id, 'escalated', 'coordinator',
                               f'non-retryable or exhausted failure: {error_category}')

    def _finish_attempt(self, task, outcome, error_category):
        row = self.store.one('SELECT * FROM execution_attempts WHERE task_id=? AND attempt_number=?',
                             (task['task_id'], task['attempt_count']))
        if not row:
            return 0.0
        start = __import__('datetime').datetime.fromisoformat(row['started_at'])
        end = __import__('datetime').datetime.fromisoformat(timestamp())
        elapsed = (end - start).total_seconds() * 1000
        self.store.update('execution_attempts', 'attempt_id', row['attempt_id'], {
            'ended_at': timestamp(), 'outcome': outcome,
            'error_category': error_category, 'timing_ms': elapsed})
        return elapsed

    def cancel(self, task_id, actor, reason):
        task = self.get(task_id)
        if task['state'] in {'completed', 'canceled', 'escalated'}:
            return task
        return self.transition(task_id, 'canceled', actor, reason,
                               cancellation_state='confirmed')

    def result(self, task_id):
        task = self.get(task_id)
        if not task or not task['result_reference']:
            return None
        row = self.store.one('SELECT * FROM execution_results WHERE result_id=?',
                             (task['result_reference'],))
        if row:
            row['payload'] = json.loads(row['payload'])
            row['evidence_refs'] = json.loads(row['evidence_refs'])
        return row

    def recover(self, *, stale_after_seconds=0):
        now = time.time(); recovered = []
        for task in self.store.query(
                "SELECT * FROM execution_tasks WHERE state IN ('assigned','running','waiting_for_tool','waiting_for_inference')"):
            if stale_after_seconds and now - task['deadline_at'] < stale_after_seconds:
                continue
            previous = task['state']
            # Recovery is an explicit exception to ordinary forward execution:
            # an orphaned assignment is returned to the durable queue.
            with self.store.transaction():
                self.store.update('execution_tasks', 'task_id', task['task_id'], {
                    'state': 'queued', 'assigned_worker_id': None,
                    'error_category': 'worker_unavailable', 'updated_at': timestamp()})
                self._record_transition(task, previous, 'queued', 'recovery',
                                        'stale in-flight assignment reconciled after restart')
                self.store.insert('execution_recovery_records', {
                    'recovery_id': uid('RECOVERY'), 'task_id': task['task_id'],
                    'previous_state': previous, 'recovered_state': 'queued',
                    'reason': 'at-least-once recovery; result must be idempotent',
                    'at': timestamp()})
            recovered.append(task['task_id'])
        return recovered


class ConcurrentExecutionEngine:
    def __init__(self, store, *, max_concurrency=4, queue_capacity=32):
        if max_concurrency < 1 or queue_capacity < 1:
            raise ValueError('Concurrency and queue capacity must be positive')
        self.store = store; self.repo = TaskRepository(store)
        self.scheduler = DeterministicScheduler(store)
        self.max_concurrency = int(max_concurrency)
        self.queue_capacity = int(queue_capacity)
        self._lock = threading.Lock()
        ensure_local_workers(store)

    def run(self, task_ids, handlers):
        if len(task_ids) > self.queue_capacity:
            raise OverflowError('Execution queue capacity exceeded')
        submissions = []
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_concurrency,
                                                     thread_name_prefix='capstone-specialist')
        try:
            for task_id in task_ids:
                task = self.repo.refresh_dependencies(task_id) or self.repo.get(task_id)
                if task['state'] not in {'eligible', 'queued'}:
                    continue
                worker = None
                try:
                    worker = self.scheduler.select(task)
                    self.scheduler.registry.reserve(worker['worker_id'])
                    resource_scopes = self.scheduler.reserve_resources(task)
                except NoEligibleWorker as exc:
                    if worker:
                        self.scheduler.registry.release(worker['worker_id'])
                    if task['state'] == 'eligible':
                        self.repo.transition(task_id, 'queued', 'scheduler', str(exc))
                    continue
                task = self.repo.assign(task_id, worker['worker_id'])
                handler = handlers.get(task['task_type'])
                if not handler:
                    self.scheduler.release_resources(resource_scopes)
                    self.scheduler.registry.release(worker['worker_id'])
                    self.repo.transition(task_id, 'blocked', 'executor',
                                         'no handler registered', error_category='invalid_task_input')
                    continue
                future = pool.submit(self._invoke, task_id, worker['worker_id'], handler)
                submissions.append((task_id, worker['worker_id'], resource_scopes, future))
            for task_id, worker_id, resource_scopes, future in submissions:
                task = self.repo.get(task_id)
                remaining = max(0.001, task['deadline_at'] - time.time())
                try:
                    future.result(timeout=remaining)
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    current = self.repo.get(task_id)
                    if current['state'] == 'running':
                        self.repo.fail(task_id, 'deadline_expired', 'task deadline expired')
                finally:
                    self.scheduler.release_resources(resource_scopes)
                    self.scheduler.registry.release(worker_id)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return {task_id: self.repo.get(task_id) for task_id in task_ids}

    def _invoke(self, task_id, worker_id, handler):
        task = self.repo.begin_attempt(task_id)
        if task['state'] != 'running':
            return task
        stop=threading.Event()
        def heartbeat():
            while not stop.wait(HEARTBEAT_SECONDS):
                self.heartbeat(task_id)
        thread=threading.Thread(target=heartbeat,name='task-liveness',daemon=True);thread.start()
        try:
            with task_context.bind(task_id=task_id,workflow_id=task['workflow_id'],agent_id=task['logical_agent_id'],
                                   deadline_at=task['deadline_at'],attempt=task['attempt_count']):
                output = handler(task)
            current = self.repo.get(task_id)
            if current['state'] != 'running' or current['cancellation_state'] == 'confirmed':
                telemetry.emit(self.store, 'task.late_result_rejected',
                    case_id=current['case_id'], workflow_id=current['workflow_id'],
                    task_id=task_id, agent_id=current['logical_agent_id'],
                    correlation_id=current['correlation_id'], task_status=current['state'],
                    summary='Late or canceled task result was not published')
                return current
            evidence = output.pop('_evidence_refs', []) if isinstance(output, dict) else []
            return self.repo.complete(task_id, output, evidence_refs=evidence)
        except TimeoutError as exc:
            return self.repo.fail(task_id, 'mcp_tool_timeout', str(exc))
        except PermissionError as exc:
            return self.repo.fail(task_id, 'missing_authority', str(exc))
        except OSError as exc:
            if task.get('required_model'):
                self.scheduler.registry.heartbeat(worker_id,health_state='unavailable',available_models=[],provenance='inference transport failure')
                return self.repo.fail(task_id,'worker_unavailable',str(exc))
            return self.repo.fail(task_id,'internal_error',str(exc))
        except ValueError as exc:
            return self.repo.fail(task_id, 'invalid_task_input', str(exc))
        except Exception as exc:
            return self.repo.fail(task_id, 'internal_error', str(exc))
        finally:stop.set()

    def heartbeat(self,task_id):
        with self.store.transaction():
            task=self.repo.get(task_id)
            if not task or task['state'] not in {'running','waiting_for_tool','waiting_for_inference'}:return False
            telemetry.emit(self.store,'task.heartbeat',case_id=task['case_id'],workflow_id=task['workflow_id'],
                task_id=task_id,agent_id=task['logical_agent_id'],correlation_id=task['correlation_id'],
                task_status=task['state'],summary='Task worker remains active',
                detail={'deadline_at':task['deadline_at'],'lease_seconds':HEARTBEAT_SECONDS*2,'attempt':task['attempt_count']})
            return True


def join_results(repo: TaskRepository, join_task_id):
    """Resolve a deterministic join without model arbitration."""
    join = repo.refresh_dependencies(join_task_id)
    if join['state'] != 'eligible':
        return join, None
    results = []
    for dep in join['dependency_set']:
        result = repo.result(dep)
        if not result or result['status'] != 'accepted':
            return repo.transition(join_task_id, 'blocked', 'coordinator',
                                   'join predecessor has no accepted current result'), None
        results.append(result)
    return join, results
