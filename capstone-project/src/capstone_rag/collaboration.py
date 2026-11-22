"""Coordinator-owned specialist delegation, conflict, and transition records.

Agents may propose work and findings.  Only this repository assigns work,
enforces bounded delegation, accepts current results, and records the
operational transition selected from a plan.
"""
from __future__ import annotations

import hashlib
import json
import time

from .coordination import timestamp, uid
from . import telemetry


SCHEMA = """
CREATE TABLE IF NOT EXISTS delegated_tasks (
 task_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
 parent_task_id TEXT, issuer TEXT NOT NULL, recipient TEXT NOT NULL,
 purpose TEXT NOT NULL, based_on_version INTEGER NOT NULL,
 required_inputs TEXT NOT NULL, evidence_ids TEXT NOT NULL,
 permitted_tools TEXT NOT NULL, operation_scope TEXT NOT NULL,
 authorization_context TEXT NOT NULL, deadline_at REAL NOT NULL,
 dependency_ids TEXT NOT NULL, depth INTEGER NOT NULL, attempt INTEGER NOT NULL,
 max_attempts INTEGER NOT NULL, status TEXT NOT NULL, result TEXT,
 missing_evidence TEXT NOT NULL, conflicts TEXT NOT NULL,
 recommended_next_action TEXT, idempotency_key TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_delegated_case ON delegated_tasks(case_id,workflow_id,status);
CREATE TABLE IF NOT EXISTS delegation_events (
 event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, case_id TEXT NOT NULL,
 kind TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS specialist_conflicts (
 conflict_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
 based_on_version INTEGER NOT NULL, finding_ids TEXT NOT NULL,
 conflict_type TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL,
 resolution TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_transitions (
 transition_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
 plan_id TEXT, selected_action TEXT NOT NULL, evidence_ids TEXT NOT NULL,
 rejected_alternatives TEXT NOT NULL, prerequisites TEXT NOT NULL,
 permitted_command TEXT NOT NULL, resulting_state TEXT NOT NULL,
 human_approvals TEXT NOT NULL, execution_status TEXT NOT NULL,
 detail TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, at TEXT NOT NULL);
"""

MAX_DEPTH = 3
MAX_REQUESTS = 12
MAX_ATTEMPTS = 2


def _json(value):
    return json.dumps(value, sort_keys=True, default=str)


def stable_key(*parts):
    return "DELEGATE-" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:24]


class Coordinator:
    def __init__(self, store, workflow_id):
        self.store = store
        self.workflow_id = workflow_id

    def _event(self, task, kind, actor, payload):
        self.store.insert('delegation_events', {
            'event_id': uid('DELEG-EV'), 'task_id': task['task_id'],
            'case_id': task['case_id'], 'kind': kind, 'actor': actor,
            'payload': _json(payload), 'at': timestamp()})

    def delegate(self, *, case_id, issuer, recipient, purpose,
                 based_on_version, required_inputs=(), evidence_ids=(),
                 permitted_tools=(), operation_scope='advisory',
                 authorization_context=None, dependency_ids=(),
                 parent_task_id=None, depth=1, deadline_seconds=120,
                 idempotency_key=None):
        if depth > MAX_DEPTH:
            raise ValueError('Maximum delegation depth exceeded')
        if recipient == issuer:
            raise ValueError('A specialist cannot delegate to itself')
        key = idempotency_key or stable_key(
            self.workflow_id, issuer, recipient, purpose,
            ','.join(sorted(map(str, evidence_ids))))
        old = self.store.one('SELECT * FROM delegated_tasks WHERE idempotency_key=?', (key,))
        if old:
            return old
        count = self.store.one(
            'SELECT count(*) AS n FROM delegated_tasks WHERE case_id=? AND workflow_id=?',
            (case_id, self.workflow_id))['n']
        if count >= MAX_REQUESTS:
            raise ValueError('Maximum specialist requests per case exceeded')
        if parent_task_id and not self.store.one(
                'SELECT 1 FROM delegated_tasks WHERE task_id=? AND case_id=?',
                (parent_task_id, case_id)):
            raise ValueError('Delegation parent is not part of this case')
        task = {
            'task_id': uid('TASK'), 'case_id': case_id,
            'workflow_id': self.workflow_id, 'parent_task_id': parent_task_id,
            'issuer': issuer, 'recipient': recipient, 'purpose': purpose,
            'based_on_version': based_on_version,
            'required_inputs': _json(list(required_inputs)),
            'evidence_ids': _json(list(dict.fromkeys(evidence_ids))),
            'permitted_tools': _json(list(permitted_tools)),
            'operation_scope': operation_scope,
            'authorization_context': _json(authorization_context or {}),
            'deadline_at': time.time() + min(600, max(5, deadline_seconds)),
            'dependency_ids': _json(list(dependency_ids)), 'depth': depth,
            'attempt': 0, 'max_attempts': MAX_ATTEMPTS, 'status': 'assigned',
            'result': None, 'missing_evidence': '[]', 'conflicts': '[]',
            'recommended_next_action': None, 'idempotency_key': key,
            'created_at': timestamp(), 'updated_at': timestamp()}
        self.store.insert('delegated_tasks', task)
        self._event(task, 'assigned', 'coordinator', {
            'issuer': issuer, 'recipient': recipient, 'purpose': purpose,
            'based_on_version': based_on_version,
            'dependencies': list(dependency_ids)})
        telemetry.emit(self.store, 'task.created', case_id=case_id,
            workflow_id=self.workflow_id, task_id=task['task_id'], agent_id=recipient,
            sender_agent_id=issuer, receiver_agent_id=recipient,
            correlation_id=task['task_id'], task_status='assigned',
            summary=f'{recipient.replace("_", " ")} task created', evidence_refs=evidence_ids)
        if issuer != 'coordinator':
            telemetry.emit(self.store, 'handoff.sent', case_id=case_id,
                workflow_id=self.workflow_id, task_id=task['task_id'], agent_id=issuer,
                sender_agent_id=issuer, receiver_agent_id=recipient,
                correlation_id=task['task_id'], task_status='sent',
                summary=f'{issuer.replace("_", " ")} requested {recipient.replace("_", " ")}')
        return task

    def start(self, task_id, recipient):
        task = self.store.one('SELECT * FROM delegated_tasks WHERE task_id=?', (task_id,))
        if not task or task['recipient'] != recipient:
            raise ValueError('Task is not assigned to this specialist')
        if task['status'] == 'completed':
            return task
        if task['attempt'] >= task['max_attempts']:
            raise ValueError('Specialist retry budget exhausted')
        attempt = task['attempt'] + 1
        self.store.update('delegated_tasks', 'task_id', task_id, {
            'status': 'running', 'attempt': attempt, 'updated_at': timestamp()})
        task = {**task, 'status': 'running', 'attempt': attempt}
        self._event(task, 'started', recipient, {'attempt': attempt})
        if task['issuer'] != 'coordinator':
            telemetry.emit(self.store, 'handoff.received', case_id=task['case_id'],
                workflow_id=self.workflow_id, task_id=task_id, agent_id=recipient,
                sender_agent_id=task['issuer'], receiver_agent_id=recipient,
                correlation_id=task_id, task_status='acknowledged',
                summary=f'{recipient.replace("_", " ")} acknowledged handoff')
        telemetry.emit(self.store, 'task.started', case_id=task['case_id'],
            workflow_id=self.workflow_id, task_id=task_id, agent_id=recipient,
            correlation_id=task_id, task_status='running',
            summary=f'{recipient.replace("_", " ")} started')
        return task

    def complete(self, task_id, recipient, result, *, missing_evidence=(),
                 conflicts=(), recommended_next_action=None):
        task = self.store.one('SELECT * FROM delegated_tasks WHERE task_id=?', (task_id,))
        if not task or task['recipient'] != recipient:
            raise ValueError('Task is not assigned to this specialist')
        current = self.store.case(task['case_id'])['case_version']
        # A task may finish after its immediate dependency committed one
        # finding, but cannot overwrite facts from a later workflow/message.
        stale = self.store.one(
            'SELECT 1 FROM workflow_runs WHERE case_id=? AND workflow_id!=? '
            "AND status IN ('running','paused','completed') AND at>? LIMIT 1",
            (task['case_id'], task['workflow_id'], task['created_at']))
        status = 'stale' if stale else 'completed'
        values = {'status': status, 'result': _json(result),
                  'missing_evidence': _json(list(missing_evidence)),
                  'conflicts': _json(list(conflicts)),
                  'recommended_next_action': recommended_next_action,
                  'updated_at': timestamp()}
        self.store.update('delegated_tasks', 'task_id', task_id, values)
        self._event({**task, **values}, status, recipient, {
            'result': result, 'missing_evidence': list(missing_evidence),
            'conflicts': list(conflicts), 'case_version_at_completion': current})
        telemetry.emit(self.store, 'task.completed' if status == 'completed' else 'task.waiting',
            case_id=task['case_id'], workflow_id=self.workflow_id, task_id=task_id,
            agent_id=recipient, correlation_id=task_id, task_status=status,
            summary=f'{recipient.replace("_", " ")} {status}',
            evidence_refs=json.loads(task['evidence_ids']))
        return {**task, **values}

    def fail(self, task_id, recipient, error):
        task = self.store.one('SELECT * FROM delegated_tasks WHERE task_id=?', (task_id,))
        if not task:
            raise ValueError('Unknown delegated task')
        status = 'failed' if task['attempt'] >= task['max_attempts'] else 'retryable'
        self.store.update('delegated_tasks', 'task_id', task_id, {
            'status': status, 'result': _json({'error': str(error)}),
            'recommended_next_action': 'human_review' if status == 'failed' else 'retry',
            'updated_at': timestamp()})
        self._event(task, status, recipient, {'error': str(error)})
        telemetry.emit(self.store, 'task.failed', case_id=task['case_id'],
            workflow_id=self.workflow_id, task_id=task_id, agent_id=recipient,
            correlation_id=task_id, task_status=status,
            summary=f'{recipient.replace("_", " ")} failed', detail={'retryable': status == 'retryable'})
        return status

    def defer(self, task_id, actor, reason, next_action='customer_clarification'):
        task = self.store.one('SELECT * FROM delegated_tasks WHERE task_id=?', (task_id,))
        if not task:
            raise ValueError('Unknown delegated task')
        values={'status':'awaiting_evidence','missing_evidence':_json([reason]),
                'recommended_next_action':next_action,'updated_at':timestamp()}
        self.store.update('delegated_tasks','task_id',task_id,values)
        self._event({**task,**values},'awaiting_evidence',actor,
                    {'reason':reason,'recommended_next_action':next_action})
        telemetry.emit(self.store, 'task.waiting', case_id=task['case_id'],
            workflow_id=self.workflow_id, task_id=task_id, agent_id=task['recipient'],
            correlation_id=task_id, task_status='awaiting_evidence',
            summary=f'{task["recipient"].replace("_", " ")} is waiting for evidence')
        return {**task,**values}

    def conflict(self, case_id, conflict_type, description, finding_ids,
                 based_on_version):
        existing = self.store.one(
            'SELECT * FROM specialist_conflicts WHERE case_id=? AND workflow_id=? '
            'AND conflict_type=? AND description=?',
            (case_id, self.workflow_id, conflict_type, description))
        if existing:
            return existing
        row = {'conflict_id': uid('CONFLICT'), 'case_id': case_id,
               'workflow_id': self.workflow_id, 'based_on_version': based_on_version,
               'finding_ids': _json(list(finding_ids)),
               'conflict_type': conflict_type, 'description': description,
               'status': 'unresolved', 'resolution': None, 'at': timestamp()}
        self.store.insert('specialist_conflicts', row)
        return row

    def transition(self, *, case_id, plan, permitted_command,
                   resulting_state, execution_status, prerequisites=(),
                   human_approvals=(), detail=None):
        selected = plan.get('selected_action', 'human_review')
        key = stable_key(self.workflow_id, 'transition', selected,
                         plan.get('selected_plan_id'))
        old = self.store.one('SELECT * FROM plan_transitions WHERE idempotency_key=?', (key,))
        if old:
            return old
        rejected = [p for p in plan.get('plans', []) if p.get('action') != selected]
        row = {'transition_id': uid('TRANSITION'), 'case_id': case_id,
               'workflow_id': self.workflow_id, 'plan_id': plan.get('selected_plan_id'),
               'selected_action': selected,
               'evidence_ids': _json(plan.get('evidence_ids', [])),
               'rejected_alternatives': _json(rejected),
               'prerequisites': _json(list(prerequisites)),
               'permitted_command': permitted_command,
               'resulting_state': resulting_state,
               'human_approvals': _json(list(human_approvals)),
               'execution_status': execution_status,
               'detail': _json(detail or {}), 'idempotency_key': key,
               'at': timestamp()}
        self.store.insert('plan_transitions', row)
        return row

    def trace(self, case_id):
        return {
            'tasks': self.store.query(
                'SELECT * FROM delegated_tasks WHERE case_id=? ORDER BY created_at', (case_id,)),
            'events': self.store.query(
                'SELECT * FROM delegation_events WHERE case_id=? ORDER BY at', (case_id,)),
            'conflicts': self.store.query(
                'SELECT * FROM specialist_conflicts WHERE case_id=? ORDER BY at', (case_id,)),
            'transitions': self.store.query(
                'SELECT * FROM plan_transitions WHERE case_id=? ORDER BY at', (case_id,)),
        }
