"""Deterministic, resource-aware worker registry and scheduling policy.

The coordinator remains the authority.  Workers publish capabilities and
health; the scheduler only records an assignment decision.  It never changes
a task's model or capability requirements to make an unavailable worker fit.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .coordination import timestamp, uid
from . import telemetry


SCHEMA_VERSION = "1.0"
SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_registry (
 worker_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL, node_id TEXT NOT NULL,
 hostname TEXT NOT NULL, endpoint TEXT, worker_type TEXT NOT NULL,
 compute_class TEXT NOT NULL, cpu_cores INTEGER, ram_mb INTEGER,
 gpu_name TEXT, vram_mb INTEGER, installed_models TEXT NOT NULL,
 available_models TEXT NOT NULL, loaded_models TEXT NOT NULL,
 supported_task_types TEXT NOT NULL, capabilities TEXT NOT NULL,
 max_concurrency INTEGER NOT NULL, queue_capacity INTEGER NOT NULL,
 active_tasks INTEGER NOT NULL DEFAULT 0, queued_tasks INTEGER NOT NULL DEFAULT 0,
 health_state TEXT NOT NULL, last_heartbeat REAL NOT NULL,
 measurement_provenance TEXT NOT NULL, operational_state TEXT NOT NULL,
 registered_at TEXT NOT NULL, retired_at TEXT);
CREATE TABLE IF NOT EXISTS worker_health_snapshots (
 snapshot_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, health_state TEXT NOT NULL,
 active_tasks INTEGER NOT NULL, queued_tasks INTEGER NOT NULL,
 installed_models TEXT NOT NULL, available_models TEXT NOT NULL,
 loaded_models TEXT NOT NULL, provenance TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scheduler_decisions (
 decision_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
 selected_worker_id TEXT, eligible_workers TEXT NOT NULL,
 excluded_workers TEXT NOT NULL, policy_version TEXT NOT NULL,
 fallback_authorized INTEGER NOT NULL, rationale TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_worker_health ON worker_registry(health_state,operational_state);
CREATE INDEX IF NOT EXISTS ix_scheduler_task ON scheduler_decisions(task_id,at);
CREATE TABLE IF NOT EXISTS execution_resource_limits (
 scope_type TEXT NOT NULL, scope_key TEXT NOT NULL,
 max_concurrency INTEGER NOT NULL, active_count INTEGER NOT NULL DEFAULT 0,
 queue_capacity INTEGER NOT NULL, queued_count INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL, PRIMARY KEY(scope_type,scope_key));
"""


class NoEligibleWorker(RuntimeError):
    pass


class ResourceLimitExceeded(NoEligibleWorker):
    pass


def _j(value):
    return json.dumps(value, sort_keys=True, default=str)


def _loads(value):
    return json.loads(value or '[]')


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    node_id: str
    hostname: str
    worker_type: str
    compute_class: str
    capabilities: tuple[str, ...]
    supported_task_types: tuple[str, ...]
    max_concurrency: int = 1
    queue_capacity: int = 8
    endpoint: str | None = None
    cpu_cores: int | None = None
    ram_mb: int | None = None
    gpu_name: str | None = None
    vram_mb: int | None = None
    installed_models: tuple[str, ...] = ()
    available_models: tuple[str, ...] = ()
    loaded_models: tuple[str, ...] = ()
    health_state: str = 'healthy'
    measurement_provenance: str = 'configured'


class WorkerRegistry:
    def __init__(self, store):
        self.store = store

    def register(self, spec: WorkerSpec):
        if not spec.worker_id or not spec.capabilities:
            raise ValueError('Worker identity and at least one capability are required')
        if spec.max_concurrency < 1 or spec.queue_capacity < 0:
            raise ValueError('Worker concurrency must be positive and queue capacity non-negative')
        now = timestamp()
        existing = self.store.one('SELECT * FROM worker_registry WHERE worker_id=?', (spec.worker_id,))
        row = {
            'worker_id': spec.worker_id, 'schema_version': SCHEMA_VERSION,
            'node_id': spec.node_id, 'hostname': spec.hostname,
            'endpoint': spec.endpoint, 'worker_type': spec.worker_type,
            'compute_class': spec.compute_class, 'cpu_cores': spec.cpu_cores,
            'ram_mb': spec.ram_mb, 'gpu_name': spec.gpu_name,
            'vram_mb': spec.vram_mb, 'installed_models': _j(spec.installed_models),
            'available_models': _j(spec.available_models),
            'loaded_models': _j(spec.loaded_models),
            'supported_task_types': _j(spec.supported_task_types),
            'capabilities': _j(spec.capabilities),
            'max_concurrency': spec.max_concurrency,
            'queue_capacity': spec.queue_capacity,
            'active_tasks': int(existing['active_tasks']) if existing else 0,
            'queued_tasks': int(existing['queued_tasks']) if existing else 0,
            'health_state': spec.health_state, 'last_heartbeat': time.time(),
            'measurement_provenance': spec.measurement_provenance,
            'operational_state': 'active',
            'registered_at': existing['registered_at'] if existing else now,
            'retired_at': None,
        }
        self.store.upsert('worker_registry', row, 'worker_id')
        telemetry.emit(self.store, 'worker.recovered' if existing else 'worker.registered',
            agent_id='coordinator', correlation_id=spec.worker_id,
            execution_node=spec.node_id, endpoint_id=spec.worker_id,
            task_status=spec.health_state,
            summary=f'Worker {spec.worker_id} registered as {spec.health_state}',
            detail={'worker_type': spec.worker_type, 'compute_class': spec.compute_class,
                    'provenance': spec.measurement_provenance})
        return self.get(spec.worker_id)

    def get(self, worker_id):
        row = self.store.one('SELECT * FROM worker_registry WHERE worker_id=?', (worker_id,))
        return self._decode(row) if row else None

    def all(self):
        return [self._decode(row) for row in self.store.query(
            'SELECT * FROM worker_registry ORDER BY registered_at,worker_id')]

    @staticmethod
    def _decode(row):
        out = dict(row)
        for key in ('installed_models', 'available_models', 'loaded_models',
                    'supported_task_types', 'capabilities'):
            out[key] = _loads(out[key])
        return out

    def heartbeat(self, worker_id, *, health_state, active_tasks=None,
                  queued_tasks=None, installed_models=None, available_models=None,
                  loaded_models=None, provenance='worker_heartbeat'):
        old = self.get(worker_id)
        if not old:
            raise ValueError('Unknown worker')
        values = {
            'health_state': health_state, 'last_heartbeat': time.time(),
            'active_tasks': old['active_tasks'] if active_tasks is None else int(active_tasks),
            'queued_tasks': old['queued_tasks'] if queued_tasks is None else int(queued_tasks),
            'installed_models': _j(old['installed_models'] if installed_models is None else installed_models),
            'available_models': _j(old['available_models'] if available_models is None else available_models),
            'loaded_models': _j(old['loaded_models'] if loaded_models is None else loaded_models),
            'measurement_provenance': provenance,
        }
        self.store.update('worker_registry', 'worker_id', worker_id, values)
        self.store.insert('worker_health_snapshots', {
            'snapshot_id': uid('WORKER-SNAPSHOT'), 'worker_id': worker_id,
            'health_state': health_state, 'active_tasks': values['active_tasks'],
            'queued_tasks': values['queued_tasks'],
            'installed_models': values['installed_models'],
            'available_models': values['available_models'],
            'loaded_models': values['loaded_models'], 'provenance': provenance,
            'observed_at': timestamp()})
        if old['health_state'] != health_state:
            telemetry.emit(self.store,
                'worker.recovered' if health_state == 'healthy' else 'worker.unavailable',
                agent_id='coordinator', correlation_id=worker_id,
                execution_node=old['node_id'], endpoint_id=worker_id,
                task_status=health_state,
                summary=f'Worker {worker_id} is {health_state}',
                detail={'provenance': provenance})
        return self.get(worker_id)

    def retire(self, worker_id):
        row = self.get(worker_id)
        if not row:
            return None
        self.store.update('worker_registry', 'worker_id', worker_id,
                          {'operational_state': 'retired', 'retired_at': timestamp()})
        return self.get(worker_id)

    def reserve(self, worker_id):
        with self.store.transaction():
            row = self.store.one('SELECT * FROM worker_registry WHERE worker_id=?', (worker_id,))
            if not row or row['health_state'] != 'healthy' or row['operational_state'] != 'active':
                raise NoEligibleWorker('Worker became unavailable before assignment')
            if row['active_tasks'] >= row['max_concurrency']:
                raise NoEligibleWorker('Worker concurrency limit reached')
            self.store.update('worker_registry', 'worker_id', worker_id,
                              {'active_tasks': row['active_tasks'] + 1})

    def release(self, worker_id):
        with self.store.transaction():
            row = self.store.one('SELECT active_tasks FROM worker_registry WHERE worker_id=?', (worker_id,))
            if row:
                self.store.update('worker_registry', 'worker_id', worker_id,
                                  {'active_tasks': max(0, row['active_tasks'] - 1)})


class DeterministicScheduler:
    POLICY_VERSION = 'phase4.1-first-fit-v1'

    def __init__(self, store):
        self.store = store
        self.registry = WorkerRegistry(store)

    def select(self, task, *, fallback_authorized=False):
        required_capability = task['capability_requirement']
        required_model = task.get('required_model')
        eligible_nodes = set(task.get('model_eligibility') or [])
        eligible, excluded = [], []
        for worker in self.registry.all():
            reasons = []
            metric=self.store.one('SELECT payload,at FROM node_metrics WHERE node_id=? ORDER BY at DESC LIMIT 1',(worker['node_id'],))
            measured=json.loads(metric['payload']) if metric and time.time()-metric['at']<=120 else {}
            worker['_measured_cpu']=measured.get('cpu_percent')
            worker['_measured_ram_available']=measured.get('ram_available_bytes')
            if measured.get('ram_available_bytes') is not None and measured['ram_available_bytes']<64*1024*1024:
                reasons.append('measured_critical_memory_pressure')
            if worker['operational_state'] != 'active': reasons.append('not_active')
            if worker['health_state'] != 'healthy': reasons.append('unhealthy')
            if required_capability not in worker['capabilities']: reasons.append('capability_mismatch')
            if task.get('task_type') not in worker['supported_task_types'] and '*' not in worker['supported_task_types']:
                reasons.append('task_type_mismatch')
            if required_model and required_model not in worker['available_models']:
                reasons.append('model_unavailable')
            if eligible_nodes and worker['node_id'] not in eligible_nodes and worker['worker_id'] not in eligible_nodes:
                reasons.append('explicit_assignment_policy_mismatch')
            if worker['active_tasks'] >= worker['max_concurrency']:
                reasons.append('concurrency_exhausted')
            if worker['queued_tasks'] >= worker['queue_capacity']:
                reasons.append('queue_full')
            if reasons:
                excluded.append({'worker_id': worker['worker_id'], 'reasons': reasons})
            else:
                eligible.append(worker)
        eligible.sort(key=lambda w: (
            w['active_tasks'] / w['max_concurrency'], w['queued_tasks'],
            0 if required_model and required_model in w['loaded_models'] else 1,
            w.get('_measured_cpu') if w.get('_measured_cpu') is not None else 100,
            0 if w['compute_class'] == 'local' else 1, w['worker_id']))
        selected = eligible[0] if eligible else None
        rationale = (f"Selected {selected['worker_id']} by health, exact capability/model, "
                     'capacity, load, model residency, fresh measured CPU pressure when available, locality, stable ID') if selected else (
                     'No worker satisfies exact health, capability, model and capacity requirements')
        self.store.insert('scheduler_decisions', {
            'decision_id': uid('SCHEDULE'), 'task_id': task['task_id'],
            'workflow_id': task['workflow_id'],
            'selected_worker_id': selected['worker_id'] if selected else None,
            'eligible_workers': _j([w['worker_id'] for w in eligible]),
            'excluded_workers': _j(excluded), 'policy_version': self.POLICY_VERSION,
            'fallback_authorized': int(bool(fallback_authorized)),
            'rationale': rationale, 'at': timestamp()})
        if not selected:
            raise NoEligibleWorker(rationale)
        return selected

    def configure_limit(self, scope_type, scope_key, *, max_concurrency, queue_capacity=8):
        if scope_type not in {'model', 'service'} or max_concurrency < 1 or queue_capacity < 0:
            raise ValueError('Invalid resource limit')
        old = self.store.one(
            'SELECT * FROM execution_resource_limits WHERE scope_type=? AND scope_key=?',
            (scope_type, scope_key))
        self.store.execute(
            'INSERT INTO execution_resource_limits(scope_type,scope_key,max_concurrency,'
            'active_count,queue_capacity,queued_count,updated_at) VALUES(?,?,?,?,?,?,?) '
            'ON CONFLICT(scope_type,scope_key) DO UPDATE SET '
            'max_concurrency=excluded.max_concurrency,queue_capacity=excluded.queue_capacity,'
            'updated_at=excluded.updated_at',
            (scope_type, scope_key, int(max_concurrency),
             int(old['active_count']) if old else 0, int(queue_capacity),
             int(old['queued_count']) if old else 0, timestamp()))

    def reserve_resources(self, task):
        scopes = []
        if task.get('required_model'):
            scopes.append(('model', task['required_model']))
        scopes.extend(('service', tool.split('.', 1)[0]) for tool in task.get('required_tools', []))
        acquired = []
        with self.store.transaction():
            for scope_type, scope_key in scopes:
                row = self.store.one(
                    'SELECT * FROM execution_resource_limits WHERE scope_type=? AND scope_key=?',
                    (scope_type, scope_key))
                if not row:
                    continue
                if row['active_count'] >= row['max_concurrency']:
                    for acquired_type, acquired_key in acquired:
                        self.store.execute(
                            'UPDATE execution_resource_limits SET active_count=max(0,active_count-1),updated_at=? '
                            'WHERE scope_type=? AND scope_key=?',
                            (timestamp(), acquired_type, acquired_key))
                    raise ResourceLimitExceeded(
                        f'{scope_type} {scope_key} concurrency limit reached')
                self.store.execute(
                    'UPDATE execution_resource_limits SET active_count=active_count+1,updated_at=? '
                    'WHERE scope_type=? AND scope_key=?',
                    (timestamp(), scope_type, scope_key))
                acquired.append((scope_type, scope_key))
        return acquired

    def release_resources(self, scopes):
        with self.store.transaction():
            for scope_type, scope_key in scopes:
                self.store.execute(
                    'UPDATE execution_resource_limits SET active_count=max(0,active_count-1),updated_at=? '
                    'WHERE scope_type=? AND scope_key=?',
                    (timestamp(), scope_type, scope_key))


def ensure_local_workers(store):
    """Register truthful Phase 4.1 workers; remote CPU heads remain simulated."""
    registry = WorkerRegistry(store)
    from .runtime_identity import coordinator_id
    specs = (
        WorkerSpec('local-tools', coordinator_id(), 'localhost',
                   'in_process', 'local',
                   ('erp.read', 'qms.read', 'ticket.read', 'operations.propose',
                    'safety.classify', 'deterministic.test'), ('*',),
                   max_concurrency=4, queue_capacity=32,
                   measurement_provenance='configured_local_process'),
        WorkerSpec('simulated-offline-node', 'simulated-node', 'simulated.invalid',
                   'simulated_remote', 'cpu', ('deterministic.test',), ('*',),
                   max_concurrency=2, queue_capacity=4, health_state='unavailable',
                   measurement_provenance='deterministic_fault_injection'),
    )
    for spec in specs:
        registry.register(spec)
    return registry


def probe_configured_ollama_workers(store, snapshot, *, timeout=3.0):
    """Publish real configured Ollama state without conflating its dimensions."""
    import urllib.request
    from .ai_settings import private_url
    registry = WorkerRegistry(store)
    observed = []
    route_models = {}
    for route in snapshot.get('routes', {}).values():
        route_models.setdefault(route.get('endpoint'), set()).add(route.get('model'))
    for endpoint_id, endpoint in snapshot.get('endpoints', {}).items():
        url = str(endpoint.get('url') or '').rstrip('/')
        private_url(url) # Explicit configured origins, not a fixed subnet scan.
        installed, loaded, health, provenance = [], [], 'unavailable', 'configured_not_reachable'
        try:
            with urllib.request.urlopen(url + '/api/tags', timeout=timeout) as response:
                tags = json.loads(response.read().decode())
            installed = sorted({row.get('name') or row.get('model') for row in tags.get('models', [])
                                if row.get('name') or row.get('model')})
            with urllib.request.urlopen(url + '/api/ps', timeout=timeout) as response:
                ps = json.loads(response.read().decode())
            loaded = sorted({row.get('name') or row.get('model') for row in ps.get('models', [])
                             if row.get('name') or row.get('model')})
            health, provenance = 'healthy', 'ollama_api_tags_and_ps'
        except Exception:
            pass
        configured_models = sorted(value for value in route_models.get(endpoint_id, set()) if value)
        location=store.one('SELECT * FROM node_locations WHERE endpoint=?',(url,))
        node_id=location['node_id'] if location else endpoint.get('resource_id',endpoint_id)
        policy=store.one('SELECT * FROM resource_authorizations WHERE address=?',(location['address'],)) if location else None
        authorized=bool(policy and policy['resource_type']=='shared_inference' and 'capstone' in json.loads(policy['applications']) and 'inference' in json.loads(policy['permitted_uses']))
        qualified=store.query('SELECT * FROM qualified_models WHERE node_id=? AND passed=1',(node_id,)) if authorized else []
        digests={m.get('name') or m.get('model'):m.get('digest') for m in tags.get('models',[])} if health=='healthy' else {}
        available=tuple(r['model'] for r in qualified if r['model'] in installed and digests.get(r['model'])==r['digest'])
        previous=registry.get('ollama-'+node_id)
        spec = WorkerSpec(
            worker_id='ollama-' + node_id, node_id=node_id,
            hostname=url.split('//', 1)[-1].split(':', 1)[0], endpoint=url,
            worker_type='ollama', compute_class=previous['compute_class'] if previous else 'unknown',
            capabilities=('model.inference',), supported_task_types=('model.inference',),
            max_concurrency=int(endpoint.get('max_concurrency', 1)), queue_capacity=8,
            installed_models=tuple(installed),
            available_models=available,
            loaded_models=tuple(loaded), health_state=health if authorized and available else 'unavailable',
            measurement_provenance=provenance)
        observed.append(registry.register(spec))
    return observed
