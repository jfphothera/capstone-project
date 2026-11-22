"""Physical inference enrollment extends the existing scheduler registry.

Inventory is not qualification. Only successful, digest-bound completion
evidence is published as available capacity. Host metrics remain nullable.
"""
import ipaddress
import json
from datetime import datetime, timezone

from .coordination import timestamp, uid
from .scheduler import WorkerRegistry, WorkerSpec


STATES = frozenset({'discovered', 'enrolling', 'qualified', 'available', 'busy',
                    'degraded', 'unreachable', 'disabled', 'quarantined', 'retired'})
SCHEMA = """
CREATE TABLE IF NOT EXISTS resource_authorizations (
 address TEXT PRIMARY KEY,resource_type TEXT NOT NULL,owner TEXT NOT NULL,
 applications TEXT NOT NULL,permitted_uses TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS node_locations (
 node_id TEXT PRIMARY KEY,address TEXT NOT NULL UNIQUE,endpoint TEXT NOT NULL,
 location_version INTEGER NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS physical_nodes (
 node_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
 tier TEXT NOT NULL, hardware_basis TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS node_qualification_evidence (
 evidence_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, observed_at TEXT NOT NULL,
 qualification_version TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cluster_policy (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS node_metrics (
 sample_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,at REAL NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS arena_viewer_sessions (
 session_hash TEXT PRIMARY KEY,expires_at REAL NOT NULL,csrf TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qualified_models (
 node_id TEXT NOT NULL,model TEXT NOT NULL,digest TEXT NOT NULL,version TEXT NOT NULL,
 passed INTEGER NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(node_id,model));
CREATE TABLE IF NOT EXISTS node_economics (node_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS qualification_no_update
 BEFORE UPDATE ON node_qualification_evidence BEGIN SELECT RAISE(ABORT,'Immutable qualification'); END;
CREATE TRIGGER IF NOT EXISTS qualification_no_delete
 BEFORE DELETE ON node_qualification_evidence BEGIN SELECT RAISE(ABORT,'Immutable qualification'); END;
"""


class ClusterEnrollment:
    def __init__(self, store, authorized_network, *, freshness_seconds=300):
        self.store = store
        self.network = ipaddress.ip_network(authorized_network)
        if not self.network.is_private:
            raise ValueError('An authorized private network is required')
        self.freshness_seconds = freshness_seconds
        self.workers = WorkerRegistry(store)

    def authorize_resource(self,address,*,resource_type,owner,applications,permitted_uses,node_id=None,endpoint=None):
        from urllib.parse import urlsplit
        address=str(ipaddress.ip_address(address))
        if ipaddress.ip_address(address) not in self.network:raise ValueError('Resource outside authorized network')
        if resource_type not in {'shared_inference','application_exclusive','exclusive_development_device','exclusive_test_device','shared_test_device','infrastructure_service','unknown'} or not owner:
            raise ValueError('Explicit resource type and owner required')
        if resource_type not in {'shared_inference','shared_test_device'} and applications:raise ValueError('Exclusive devices/infrastructure cannot be inference pools')
        endpoint=endpoint or f'http://{address}:11434'
        origin=urlsplit(endpoint)
        if origin.hostname!=address or origin.scheme not in {'http','https'} or origin.username or origin.password or origin.path not in ('','/') or origin.query or origin.fragment:
            raise ValueError('Approved node origin must match its authorized address')
        existing=self.store.one('SELECT * FROM node_locations WHERE address=?',(address,))
        node_id=node_id or (existing['node_id'] if existing else 'head-'+address.replace('.','-'))
        old=self.store.one('SELECT * FROM node_locations WHERE node_id=?',(node_id,))
        if existing and existing['node_id']!=node_id:raise ValueError('Address already belongs to another logical node')
        moved=bool(old and old['endpoint']!=endpoint)
        with self.store.transaction():
            if moved:
                if self.store.one('SELECT 1 FROM worker_registry WHERE node_id=? AND active_tasks>0',(node_id,)):
                    raise ValueError('Cannot relocate a node with active application tasks')
                self.store.execute('DELETE FROM resource_authorizations WHERE address=?',(old['address'],))
                self.store.execute('UPDATE qualified_models SET passed=0 WHERE node_id=?',(node_id,))
                self.store.execute("UPDATE physical_nodes SET endpoint=?,state='enrolling' WHERE node_id=?",(endpoint,node_id))
                self.store.execute("UPDATE worker_registry SET endpoint=?,hostname=?,health_state='unavailable',available_models='[]',loaded_models='[]' WHERE node_id=?",(endpoint,address,node_id))
            self.store.upsert('node_locations',{'node_id':node_id,'address':address,'endpoint':endpoint,
                'location_version':int(old['location_version'])+int(moved) if old else 1,'updated_at':timestamp()},'node_id')
            self.store.upsert('resource_authorizations',{'address':address,'resource_type':resource_type,'owner':owner,
                'applications':json.dumps(applications),'permitted_uses':json.dumps(permitted_uses)},'address')
            self.store.audit('resource_authorized',actor='infrastructure administrator',summary='Explicit resource policy/location update',
                detail={'node_id':node_id,'resource_type':resource_type,'owner':owner,'location_changed':moved})

    def permitted(self,address,application_id='capstone'):
        policy=self.store.one('SELECT * FROM resource_authorizations WHERE address=?',(address,))
        return bool(policy and policy['resource_type']=='shared_inference' and application_id in json.loads(policy['applications']) and 'inference' in json.loads(policy['permitted_uses']))

    def ingest(self, evidence, *, tier='unknown', hardware_basis='unavailable'):
        address = ipaddress.ip_address(evidence['address'])
        if address not in self.network or address in (self.network.network_address, self.network.broadcast_address):
            raise ValueError('Address outside authorized host range')
        if not self.permitted(str(address)):raise PermissionError('Explicit shared-inference authorization is required; LAN connectivity grants no access')
        location=self.store.one('SELECT * FROM node_locations WHERE address=?',(str(address),))
        endpoint = location['endpoint'] if location else f'http://{address}:11434'
        if evidence['endpoint'] != endpoint or evidence.get('actual_execution') is not True:
            raise ValueError('Endpoint provenance mismatch or simulated evidence')
        observed = datetime.fromisoformat(evidence['observed_at'])
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        if age < -30 or age > self.freshness_seconds:
            raise ValueError('Stale or future qualification evidence')
        node_id = location['node_id'] if location else 'head-' + str(address).replace('.', '-')
        worker_id = 'ollama-' + node_id
        installed = evidence.get('inventory', [])
        selected = evidence.get('model')
        entry = next((m for m in installed if m['name'] == selected), None)
        metadata = evidence.get('metadata', {}).get(selected, {})
        response = evidence.get('response', {})
        qualified = bool(entry and entry.get('digest') == evidence.get('digest')
                         and 'completion' in metadata.get('capabilities', [])
                         and evidence.get('generation_completed') is True
                         and evidence.get('correct') is True
                         and response.get('done') is True
                         and response.get('model') == selected
                         and str(response.get('response', '')).strip())
        previous = self.store.one('SELECT * FROM physical_nodes WHERE node_id=?', (node_id,))
        protected = previous and previous['state'] in {'disabled', 'quarantined', 'retired'}
        state = previous['state'] if protected else ('available' if qualified else 'degraded')
        if not installed and not protected:
            state = 'unreachable'
        with self.store.transaction():
            if entry:
                self.store.execute('INSERT INTO qualified_models VALUES(?,?,?,?,?,?) ON CONFLICT(node_id,model) DO UPDATE SET digest=excluded.digest,version=excluded.version,passed=excluded.passed,observed_at=excluded.observed_at',
                    (node_id,selected,evidence.get('digest') or '',json.dumps(evidence.get('version'),sort_keys=True),int(qualified),evidence['observed_at']))
            digests={m['name']:m.get('digest') for m in installed}
            available=[r['model'] for r in self.store.query('SELECT * FROM qualified_models WHERE node_id=? AND passed=1',(node_id,))
                       if digests.get(r['model'])==r['digest'] and r['version']==json.dumps(evidence.get('version'),sort_keys=True)]
            if not protected and available:state='available'
            self.store.upsert('physical_nodes', {
                'node_id': node_id, 'endpoint': endpoint, 'state': state,
                'tier': tier, 'hardware_basis': hardware_basis,
                'last_seen': evidence['observed_at']}, 'node_id')
            self.store.insert('node_qualification_evidence', {
                'evidence_id': uid('QUAL'), 'node_id': node_id,
                'observed_at': evidence['observed_at'],
                'qualification_version': '4.2-v1', 'evidence': json.dumps(evidence)})
            self.workers.register(WorkerSpec(
                worker_id, node_id, str(address), 'ollama', tier,
                ('model.inference',), ('*',), endpoint=endpoint,
                installed_models=tuple(m['name'] for m in installed),
                available_models=tuple(available) if not protected else (),
                loaded_models=tuple(m['name'] for m in evidence.get('residency_after', {}).get('models', [])),
                health_state='healthy' if state == 'available' else 'unavailable',
                measurement_provenance='real digest-bound inference; host metrics unavailable'))
            if protected:
                self.store.update('worker_registry', 'worker_id', worker_id, {'operational_state': state})
        return self.workers.get(worker_id)

    def set_state(self, node_id, state):
        if state not in {'disabled', 'quarantined', 'retired'}:
            raise ValueError('Re-enrollment requires fresh qualification evidence')
        with self.store.transaction():
            self.store.update('physical_nodes', 'node_id', node_id, {'state': state})
            self.store.execute('UPDATE worker_registry SET operational_state=?,health_state=? WHERE node_id=?',
                               (state, 'unavailable', node_id))

    def expire(self):
        for node in self.store.query('SELECT * FROM physical_nodes'):
            age = (datetime.now(timezone.utc)-datetime.fromisoformat(node['last_seen'])).total_seconds()
            if age > self.freshness_seconds and node['state'] in {'available', 'busy', 'qualified'}:
                self.store.update('physical_nodes', 'node_id', node['node_id'], {'state': 'unreachable'})
                self.store.execute('UPDATE worker_registry SET health_state=? WHERE node_id=?',
                                   ('unavailable', node['node_id']))

    def reenable(self, node_id):
        self.store.update('physical_nodes','node_id',node_id,{'state':'enrolling'})
        self.store.execute('UPDATE worker_registry SET operational_state=?,health_state=? WHERE node_id=?',
                           ('active','unavailable',node_id))
